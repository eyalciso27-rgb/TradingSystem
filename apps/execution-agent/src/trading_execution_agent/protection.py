"""Broker-side protection manager (IMP-1005).

Authority: Integration 9.4 (protective orders are linked to the ACTUALLY
FILLED quantity, never the requested one; child orders are not transmitted
before the structure is valid; a partial fill updates position, protection
quantity and targets; partial target execution reduces the stop to the
remaining quantity), Integration 20.4 (OCA/bracket strictly by declared
capability; emulation requires explicit policy and is never a default),
Security 23.2/24.1 (an entry halt never cancels protective orders;
KEEP_PROTECTION is the default for every halt). Task DoD: **no required
position remains unprotected**.

The manager is deterministic and audit-ready. It consumes fill events and
produces ``ProtectionPlan``s — specifications of protective orders to
place/replace — which are NOT sent anywhere by this component: submission
happens later through the Execution Agent's own gates (authorization,
halt, health). Nothing here talks to a broker and nothing is real.

Fail-closed rules:

- Protection sizing follows fills only: after every processed fill the
  protected quantity must equal the filled quantity (the
  ``unprotected_positions()`` invariant is the DoD check).
- Structure by capability only: BRACKET/OCA specs are emitted only when
  the broker's declared capability is NATIVE. If the policy requires a
  structure the broker does not support, the plan carries the policy's
  ``missing_capability_action`` (MANUAL / NEEDS_REVIEW — never silent
  emulation) — and the plain protective stop is STILL placed so the
  position is never left unprotected.
- KEEP_PROTECTION: a protection quantity may only move to match the
  remaining position; reducing protection below the open quantity, or
  cancelling it without a replacement, is refused.
- Duplicate fill events (same ``fill_id``) are idempotent — no duplicate
  protective orders. An unknown/inconsistent position or broker state
  yields NEEDS_REVIEW with no action (reconcile first, never act blindly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_broker_core.port import (
    BrokerCapabilities,
    BrokerCapabilityMode,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderType

PROTECTION_VERSION = "protection-manager-1.0.0"


class ProtectionError(Exception):
    """Fail-closed protection failure (bad policy/inputs, unsafe request)."""


class ProtectionStructure(StrEnum):
    """Protective-order structure requested by policy."""

    PLAIN_STOP = "PLAIN_STOP"
    BRACKET = "BRACKET"
    OCA = "OCA"


class MissingCapabilityAction(StrEnum):
    """Policy response when the broker lacks the required structure."""

    MANUAL = "MANUAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class PlanStatus(StrEnum):
    OK = "OK"
    MANUAL = "MANUAL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ProtectionActionType(StrEnum):
    PLACE_STOP = "PLACE_STOP"
    REPLACE_STOP = "REPLACE_STOP"
    PLACE_TARGET = "PLACE_TARGET"
    REPLACE_TARGET = "REPLACE_TARGET"


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    """All protection rules — configuration input, nothing in code."""

    policy_version: str
    structure: ProtectionStructure
    missing_capability_action: MissingCapabilityAction

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ProtectionError("policy_version is mandatory")


@dataclass(frozen=True, slots=True)
class FillEvent:
    """One (possibly partial) fill that must become protected."""

    fill_id: str
    position_ref: str
    symbol: str
    environment: Environment
    entry_side: OrderSide
    filled_quantity: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    observed_at: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        if not self.fill_id.strip():
            raise ProtectionError("fill_id is mandatory")
        if self.filled_quantity <= 0:
            raise ProtectionError("filled_quantity must be positive")
        if self.stop_price <= 0:
            raise ProtectionError("stop_price must be positive (protection is mandatory)")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProtectionOrderSpec:
    """An UNSENT protective-order specification (never a broker order)."""

    spec_ref: str
    position_ref: str
    symbol: str
    side: OrderSide  # opposite of the entry
    order_type: OrderType
    quantity: Decimal
    stop_price: Decimal | None
    limit_price: Decimal | None
    oca_group: str | None


@dataclass(frozen=True, slots=True)
class ProtectionAction:
    action: ProtectionActionType
    spec: ProtectionOrderSpec
    replaces_spec_ref: str | None


@dataclass(frozen=True, slots=True)
class ProtectionPlan:
    """Audit-ready protection plan for one fill event (nothing is sent)."""

    plan_id: str
    position_ref: str
    status: PlanStatus
    actions: tuple[ProtectionAction, ...]
    protected_quantity: Decimal
    filled_quantity: Decimal
    policy_ref: str
    manager_version: str
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]
    correlation_id: str
    issued_at: datetime

    def __post_init__(self) -> None:
        if self.issued_at.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware")


@dataclass
class _PositionProtection:
    filled: Decimal = Decimal(0)
    protected: Decimal = Decimal(0)
    stop_spec_ref: str | None = None
    target_spec_ref: str | None = None
    seen_fill_ids: set[str] = field(default_factory=set)
    plans_by_fill: dict[str, ProtectionPlan] = field(default_factory=dict)


def _opposite(side: OrderSide) -> OrderSide:
    return OrderSide.SELL if side is OrderSide.BUY else OrderSide.BUY


class ProtectionManager:
    """Turns fills into protective-order plans; every fill gets protected."""

    def __init__(self, policy: ProtectionPolicy) -> None:
        self._policy = policy
        self._positions: dict[str, _PositionProtection] = {}
        self._sequence = 0

    def on_fill(
        self,
        fill: FillEvent,
        capabilities: BrokerCapabilities,
        *,
        as_of: datetime,
    ) -> ProtectionPlan:
        """Produce the plan that keeps the position fully protected."""
        if as_of.tzinfo is None:
            raise ProtectionError("as_of must be timezone-aware")
        if fill.observed_at > as_of:
            raise ProtectionError("lookahead: fill is newer than as_of")

        state = self._positions.setdefault(fill.position_ref, _PositionProtection())
        if fill.fill_id in state.seen_fill_ids:
            # Idempotent: the same fill never creates duplicate protection.
            return state.plans_by_fill[fill.fill_id]

        # The plain protective stop must be natively supported — protection
        # itself can never depend on an optional capability.
        stop_mode = capabilities.order_types.get(
            OrderType.STOP, BrokerCapabilityMode.UNSUPPORTED
        )
        if stop_mode is not BrokerCapabilityMode.NATIVE:
            # Record the fill WITHOUT protection so the DoD invariant
            # (unprotected_positions) makes the gap loudly visible.
            state.filled = state.filled + fill.filled_quantity
            plan = self._plan(
                fill,
                state,
                PlanStatus.NEEDS_REVIEW,
                actions=(),
                codes=("STOP_CAPABILITY_MISSING", "POSITION_WOULD_BE_UNPROTECTED"),
                as_of=as_of,
            )
            state.seen_fill_ids.add(fill.fill_id)
            state.plans_by_fill[fill.fill_id] = plan
            return plan

        structure, structure_codes, status = self._resolve_structure(capabilities)
        new_filled = state.filled + fill.filled_quantity
        oca_group = (
            f"oca:{fill.position_ref}" if structure in ("BRACKET", "OCA") else None
        )

        actions: list[ProtectionAction] = []
        # Stop leg: place on first fill, replace (resize) on partial fills so
        # protection always equals the actually-filled quantity (9.4).
        stop_spec = ProtectionOrderSpec(
            spec_ref=self._spec_ref(fill.position_ref, "stop"),
            position_ref=fill.position_ref,
            symbol=fill.symbol,
            side=_opposite(fill.entry_side),
            order_type=OrderType.STOP,
            quantity=new_filled,
            stop_price=fill.stop_price,
            limit_price=None,
            oca_group=oca_group,
        )
        actions.append(
            ProtectionAction(
                action=(
                    ProtectionActionType.REPLACE_STOP
                    if state.stop_spec_ref
                    else ProtectionActionType.PLACE_STOP
                ),
                spec=stop_spec,
                replaces_spec_ref=state.stop_spec_ref,
            )
        )
        # Target leg only when the structure supports it — never emulated.
        if fill.target_price is not None and structure in ("BRACKET", "OCA"):
            target_spec = ProtectionOrderSpec(
                spec_ref=self._spec_ref(fill.position_ref, "target"),
                position_ref=fill.position_ref,
                symbol=fill.symbol,
                side=_opposite(fill.entry_side),
                order_type=OrderType.LIMIT,
                quantity=new_filled,
                stop_price=None,
                limit_price=fill.target_price,
                oca_group=oca_group,
            )
            actions.append(
                ProtectionAction(
                    action=(
                        ProtectionActionType.REPLACE_TARGET
                        if state.target_spec_ref
                        else ProtectionActionType.PLACE_TARGET
                    ),
                    spec=target_spec,
                    replaces_spec_ref=state.target_spec_ref,
                )
            )
            state.target_spec_ref = target_spec.spec_ref

        state.filled = new_filled
        state.protected = new_filled  # protect every fill, exactly
        state.stop_spec_ref = stop_spec.spec_ref
        plan = self._plan(
            fill,
            state,
            status,
            actions=tuple(actions),
            codes=("PROTECT_EVERY_FILL", *structure_codes),
            as_of=as_of,
        )
        state.seen_fill_ids.add(fill.fill_id)
        state.plans_by_fill[fill.fill_id] = plan
        return plan

    def on_target_partial_execution(
        self, position_ref: str, executed_quantity: Decimal, *, as_of: datetime
    ) -> ProtectionOrderSpec:
        """Partial target execution reduces the stop to the remainder (9.4)."""
        if as_of.tzinfo is None:
            raise ProtectionError("as_of must be timezone-aware")
        state = self._positions.get(position_ref)
        if state is None or state.stop_spec_ref is None:
            raise ProtectionError(
                f"unknown protection state for {position_ref!r}; reconcile first"
            )
        if executed_quantity <= 0 or executed_quantity >= state.filled:
            raise ProtectionError("executed_quantity must be within the open position")
        remaining = state.filled - executed_quantity
        state.filled = remaining
        state.protected = remaining
        spec = ProtectionOrderSpec(
            spec_ref=self._spec_ref(position_ref, "stop"),
            position_ref=position_ref,
            symbol="",
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            quantity=remaining,
            stop_price=None,
            limit_price=None,
            oca_group=None,
        )
        state.stop_spec_ref = spec.spec_ref
        return spec

    def reduce_protection(self, position_ref: str, new_quantity: Decimal) -> None:
        """KEEP_PROTECTION: protection may only match the remaining position."""
        state = self._positions.get(position_ref)
        if state is None:
            raise ProtectionError(f"unknown position {position_ref!r}")
        if new_quantity < state.filled:
            raise ProtectionError(
                "refusing to reduce protection below the open position"
                " (KEEP_PROTECTION; exits go through the position coordinator)"
            )

    def unprotected_positions(self) -> tuple[str, ...]:
        """DoD invariant: positions whose fills are not fully protected."""
        return tuple(
            sorted(
                ref
                for ref, state in self._positions.items()
                if state.protected != state.filled or state.filled < 0
            )
        )

    # -- internals -----------------------------------------------------------
    def _resolve_structure(
        self, capabilities: BrokerCapabilities
    ) -> tuple[str, tuple[str, ...], PlanStatus]:
        policy = self._policy
        if policy.structure is ProtectionStructure.PLAIN_STOP:
            return "PLAIN_STOP", ("STRUCTURE_PLAIN_STOP",), PlanStatus.OK
        wanted = policy.structure.value
        mode = (
            capabilities.bracket
            if policy.structure is ProtectionStructure.BRACKET
            else capabilities.oco
        )
        if mode is BrokerCapabilityMode.NATIVE:
            return wanted, (f"STRUCTURE_{wanted}_NATIVE",), PlanStatus.OK
        # Capability missing: never silently emulate. The stop still gets
        # placed (position never unprotected); the target/linkage is handed
        # to the policy's action.
        status = (
            PlanStatus.MANUAL
            if policy.missing_capability_action is MissingCapabilityAction.MANUAL
            else PlanStatus.NEEDS_REVIEW
        )
        return (
            "PLAIN_STOP",
            (
                f"{wanted}_CAPABILITY_{mode.value}",
                "NO_SILENT_EMULATION",
                f"TARGET_LEG_{status.value}",
            ),
            status,
        )

    def _spec_ref(self, position_ref: str, kind: str) -> str:
        self._sequence += 1
        return f"protection:{position_ref}:{kind}:{self._sequence}"

    def _plan(
        self,
        fill: FillEvent,
        state: _PositionProtection,
        status: PlanStatus,
        *,
        actions: tuple[ProtectionAction, ...],
        codes: tuple[str, ...],
        as_of: datetime,
    ) -> ProtectionPlan:
        self._sequence += 1
        return ProtectionPlan(
            plan_id=f"protection-plan:{fill.position_ref}:{self._sequence}",
            position_ref=fill.position_ref,
            status=status,
            actions=actions,
            protected_quantity=state.protected,
            filled_quantity=state.filled,
            policy_ref=f"protection-policy:{self._policy.policy_version}",
            manager_version=PROTECTION_VERSION,
            reason_codes=codes,
            input_refs=(f"fill:{fill.fill_id}@{fill.observed_at.isoformat()}",),
            correlation_id=fill.correlation_id,
            issued_at=as_of,
        )


__all__ = [
    "PROTECTION_VERSION",
    "FillEvent",
    "MissingCapabilityAction",
    "PlanStatus",
    "ProtectionAction",
    "ProtectionActionType",
    "ProtectionError",
    "ProtectionManager",
    "ProtectionOrderSpec",
    "ProtectionPlan",
    "ProtectionPolicy",
    "ProtectionStructure",
]
