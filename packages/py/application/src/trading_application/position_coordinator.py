"""Position coordinator (IMP-1004) — single writer for position actions.

Authority: Portfolio/Regime spec section 4 ("existing position with an
update: the Position Coordinator decides; no parallel order");
System_Functional section 16 (exit trigger table; protective orders synced
with the position manager; partial exits supported), 16.1 (exit decisions
use the same audit infrastructure), 31/31.1 (failure detection triggers;
stop/kill act on the deterministic fast path; RISK events reduce/close at
TOP priority; the actual exit reason is recorded as STOP/TARGET/
INVALIDATION/TIME/RISK/MANUAL/ERROR) and the position screen requirement
("winning trigger, rejected triggers and exit-order state").

The coordinator is the ONLY component allowed to decide stop moves,
targets, reductions and exits for a position (single writer). It is pure
and deterministic: frozen inputs (position snapshot + concurrent triggers
+ policy + as_of) produce ONE coherent ``PositionActionPlan`` — a single
winning trigger, every losing trigger listed with its rejection reason,
one action, and the working orders that must be cancelled so no
contradicting or duplicate order can exist. It never talks to a broker —
plans are executed later by the Execution Agent through its own gates.

Fail-closed rules:

- Trigger priorities come from policy only; a trigger type the policy does
  not rank fails closed (nothing invented in code).
- An UNKNOWN/stale position snapshot yields a NEEDS_REVIEW plan with no
  action (reconcile before anything else — never a blind action).
- While a plan is pending for a position, a lower-or-equal-priority plan is
  refused (idempotent duplicates return the pending plan); only a strictly
  higher-priority winner supersedes it, and the superseding plan carries
  the cancellation of the previous plan's order.
- A completed/failed/cancelled plan is terminal: it is never resurrected;
  new triggers start a new plan with a new sequence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_contracts.enums import Environment, ExitReason, ExitTriggerType

COORDINATOR_VERSION = "position-coordinator-1.0.0"

#: Authority-fixed mapping (31.1): which canonical exit reason each trigger
#: records. This is the spec's own wording, not an invented policy value.
TRIGGER_EXIT_REASON: Mapping[ExitTriggerType, ExitReason] = {
    ExitTriggerType.PROTECTIVE_STOP: ExitReason.STOP,
    ExitTriggerType.TAKE_PROFIT: ExitReason.TARGET,
    ExitTriggerType.TRAILING_POLICY: ExitReason.STOP,
    ExitTriggerType.THESIS_INVALIDATED: ExitReason.INVALIDATION,
    ExitTriggerType.TIME_EXIT: ExitReason.TIME,
    ExitTriggerType.RISK_REDUCTION: ExitReason.RISK,
    ExitTriggerType.NEW_MATERIAL_EVENT: ExitReason.INVALIDATION,
    ExitTriggerType.MANUAL_EXIT: ExitReason.MANUAL,
    ExitTriggerType.SYSTEM_SAFETY_EXIT: ExitReason.ERROR,
}


class PositionCoordinatorError(Exception):
    """Fail-closed coordinator failure (bad policy/inputs, lookahead)."""


class PlanAction(StrEnum):
    """The single coherent action a plan carries."""

    EXIT_FULL = "EXIT_FULL"
    REDUCE = "REDUCE"
    ADJUST_PROTECTION = "ADJUST_PROTECTION"
    NO_ACTION = "NO_ACTION"


class PlanStatus(StrEnum):
    """Lifecycle of an issued plan (single writer bookkeeping)."""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class CoordinatorPolicy:
    """All ranking rules — configuration input, nothing in code."""

    policy_version: str
    trigger_priorities: Mapping[ExitTriggerType, int]  # higher wins

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise PositionCoordinatorError("policy_version is mandatory")
        if not self.trigger_priorities:
            raise PositionCoordinatorError("trigger_priorities must not be empty")

    def priority_of(self, trigger_type: ExitTriggerType) -> int:
        priority = self.trigger_priorities.get(trigger_type)
        if priority is None:
            raise PositionCoordinatorError(
                f"policy {self.policy_version!r} does not rank trigger"
                f" {trigger_type.value}; failing closed"
            )
        return priority


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """Frozen position state (from reconciliation/fixtures)."""

    position_ref: str
    symbol: str
    environment: Environment
    open_quantity: Decimal
    protective_order_refs: tuple[str, ...]
    target_order_refs: tuple[str, ...]
    observed_at: datetime
    state_known: bool = True

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExitTrigger:
    """One (possibly concurrent) trigger asking to act on a position."""

    trigger_id: str
    trigger_type: ExitTriggerType
    position_ref: str
    observed_at: datetime
    reduce_quantity: Decimal | None = None  # None => full exit semantics
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.trigger_id.strip():
            raise PositionCoordinatorError("trigger_id is mandatory")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.reduce_quantity is not None and self.reduce_quantity <= 0:
            raise PositionCoordinatorError("reduce_quantity must be positive when given")


@dataclass(frozen=True, slots=True)
class RejectedTrigger:
    trigger_id: str
    trigger_type: ExitTriggerType
    reason: str


@dataclass(frozen=True, slots=True)
class PositionActionPlan:
    """One coherent, audit-ready plan for one position (single writer)."""

    plan_id: str
    sequence: int
    position_ref: str
    status: PlanStatus
    action: PlanAction
    quantity: Decimal
    exit_reason: ExitReason | None
    winning_trigger_id: str | None
    winning_trigger_type: ExitTriggerType | None
    rejected_triggers: tuple[RejectedTrigger, ...]
    cancel_order_refs: tuple[str, ...]
    supersedes_plan_id: str | None
    policy_ref: str
    coordinator_version: str
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]
    issued_at: datetime

    def __post_init__(self) -> None:
        if self.issued_at.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware")


class PositionCoordinator:
    """Deterministic single writer for stops, targets, reductions, exits."""

    def __init__(self, policy: CoordinatorPolicy) -> None:
        self._policy = policy
        self._active: dict[str, PositionActionPlan] = {}
        self._sequence = 0

    def coordinate(
        self,
        position: PositionSnapshot,
        triggers: Sequence[ExitTrigger],
        *,
        as_of: datetime,
    ) -> PositionActionPlan:
        """Resolve concurrent triggers into one coherent plan."""
        if as_of.tzinfo is None:
            raise PositionCoordinatorError("as_of must be timezone-aware")
        if position.observed_at > as_of:
            raise PositionCoordinatorError("lookahead: position snapshot is newer than as_of")
        for trigger in triggers:
            if trigger.observed_at > as_of:
                raise PositionCoordinatorError(
                    f"lookahead: trigger {trigger.trigger_id!r} is newer than as_of"
                )
            if trigger.position_ref != position.position_ref:
                raise PositionCoordinatorError(
                    f"trigger {trigger.trigger_id!r} targets a different position"
                )
        if not triggers:
            raise PositionCoordinatorError("at least one trigger is required")

        # Unknown/invalid position state: reconcile first, never act blindly.
        if not position.state_known or position.open_quantity <= 0:
            return self._issue(
                position,
                status=PlanStatus.NEEDS_REVIEW,
                action=PlanAction.NO_ACTION,
                quantity=Decimal(0),
                winner=None,
                rejected=tuple(
                    RejectedTrigger(t.trigger_id, t.trigger_type, "POSITION_STATE_UNKNOWN")
                    for t in self._dedup(triggers)
                ),
                cancel_refs=(),
                supersedes=None,
                codes=("POSITION_STATE_UNKNOWN", "RECONCILE_BEFORE_ANY_ACTION"),
                as_of=as_of,
            )

        unique = self._dedup(triggers)
        winner = self._pick_winner(unique)
        rejected = self._reject_losers(unique, winner)

        pending = self._active.get(position.position_ref)
        if pending is not None and pending.status is PlanStatus.PENDING:
            if pending.winning_trigger_type is not None and self._policy.priority_of(
                winner.trigger_type
            ) <= self._policy.priority_of(pending.winning_trigger_type):
                # Same or lower priority while a plan is pending: single
                # writer refuses a second, potentially conflicting plan.
                return self._issue(
                    position,
                    status=PlanStatus.REFUSED,
                    action=PlanAction.NO_ACTION,
                    quantity=Decimal(0),
                    winner=winner,
                    rejected=rejected,
                    cancel_refs=(),
                    supersedes=pending.plan_id,
                    codes=("PLAN_ALREADY_PENDING", f"PENDING_PLAN:{pending.plan_id}"),
                    as_of=as_of,
                    record=False,
                )
            # Strictly higher priority: supersede, cancelling the old plan.
            self._active[position.position_ref] = replace(
                pending, status=PlanStatus.SUPERSEDED
            )
            return self._build_action_plan(
                position, winner, rejected, as_of, supersedes=pending.plan_id
            )
        return self._build_action_plan(position, winner, rejected, as_of, supersedes=None)

    def mark(self, position_ref: str, status: PlanStatus) -> None:
        """Record a terminal result for the pending plan (single writer)."""
        pending = self._active.get(position_ref)
        if pending is None:
            raise PositionCoordinatorError(f"no plan for position {position_ref!r}")
        if pending.status is not PlanStatus.PENDING:
            raise PositionCoordinatorError(
                f"plan {pending.plan_id} is terminal ({pending.status.value});"
                " it is never resurrected"
            )
        if status is PlanStatus.PENDING:
            raise PositionCoordinatorError("mark() requires a terminal status")
        self._active[position_ref] = replace(pending, status=status)

    def active_plan(self, position_ref: str) -> PositionActionPlan | None:
        return self._active.get(position_ref)

    # -- internals -----------------------------------------------------------
    def _dedup(self, triggers: Sequence[ExitTrigger]) -> tuple[ExitTrigger, ...]:
        seen: dict[str, ExitTrigger] = {}
        for trigger in triggers:
            seen.setdefault(trigger.trigger_id, trigger)
        return tuple(seen.values())

    def _pick_winner(self, triggers: tuple[ExitTrigger, ...]) -> ExitTrigger:
        # Deterministic: highest policy priority, then earliest observed_at,
        # then trigger_id — never incidental input order.
        return sorted(
            triggers,
            key=lambda t: (
                -self._policy.priority_of(t.trigger_type),
                t.observed_at,
                t.trigger_id,
            ),
        )[0]

    def _reject_losers(
        self, triggers: tuple[ExitTrigger, ...], winner: ExitTrigger
    ) -> tuple[RejectedTrigger, ...]:
        rejected: list[RejectedTrigger] = []
        winner_is_full_exit = winner.reduce_quantity is None
        for trigger in triggers:
            if trigger.trigger_id == winner.trigger_id:
                continue
            if winner_is_full_exit and trigger.reduce_quantity is not None:
                reason = "ABSORBED_BY_FULL_EXIT"
            else:
                reason = "TRIGGER_LOST_PRIORITY"
            rejected.append(RejectedTrigger(trigger.trigger_id, trigger.trigger_type, reason))
        return tuple(rejected)

    def _build_action_plan(
        self,
        position: PositionSnapshot,
        winner: ExitTrigger,
        rejected: tuple[RejectedTrigger, ...],
        as_of: datetime,
        *,
        supersedes: str | None,
    ) -> PositionActionPlan:
        if winner.trigger_type is ExitTriggerType.TRAILING_POLICY:
            action = PlanAction.ADJUST_PROTECTION
            quantity = Decimal(0)
        elif winner.reduce_quantity is None:
            action = PlanAction.EXIT_FULL
            quantity = position.open_quantity  # per actual fills, never more
        elif winner.reduce_quantity >= position.open_quantity:
            action = PlanAction.EXIT_FULL
            quantity = position.open_quantity
        else:
            action = PlanAction.REDUCE
            quantity = winner.reduce_quantity
        # One coherent plan: an exit/reduce cancels the opposing working
        # orders so no contradicting or duplicate order can fill (16:
        # protective orders stay synced with the position manager).
        if action is PlanAction.EXIT_FULL:
            cancel_refs = position.protective_order_refs + position.target_order_refs
        elif action is PlanAction.REDUCE:
            cancel_refs = position.target_order_refs
        else:
            cancel_refs = ()
        codes = [f"WINNER_{winner.trigger_type.value}"]
        if supersedes is not None:
            codes.append(f"SUPERSEDES:{supersedes}")
        return self._issue(
            position,
            status=PlanStatus.PENDING,
            action=action,
            quantity=quantity,
            winner=winner,
            rejected=rejected,
            cancel_refs=cancel_refs,
            supersedes=supersedes,
            codes=tuple(codes),
            as_of=as_of,
        )

    def _issue(
        self,
        position: PositionSnapshot,
        *,
        status: PlanStatus,
        action: PlanAction,
        quantity: Decimal,
        winner: ExitTrigger | None,
        rejected: tuple[RejectedTrigger, ...],
        cancel_refs: tuple[str, ...],
        supersedes: str | None,
        codes: tuple[str, ...],
        as_of: datetime,
        record: bool = True,
    ) -> PositionActionPlan:
        self._sequence += 1
        plan = PositionActionPlan(
            plan_id=f"plan:{position.position_ref}:{self._sequence}",
            sequence=self._sequence,
            position_ref=position.position_ref,
            status=status,
            action=action,
            quantity=quantity,
            exit_reason=(
                TRIGGER_EXIT_REASON[winner.trigger_type]
                if winner is not None and status is PlanStatus.PENDING
                else None
            ),
            winning_trigger_id=winner.trigger_id if winner else None,
            winning_trigger_type=winner.trigger_type if winner else None,
            rejected_triggers=rejected,
            cancel_order_refs=cancel_refs,
            supersedes_plan_id=supersedes,
            policy_ref=f"coordinator-policy:{self._policy.policy_version}",
            coordinator_version=COORDINATOR_VERSION,
            reason_codes=codes,
            input_refs=(f"position:{position.position_ref}@{position.observed_at.isoformat()}",),
            issued_at=as_of,
        )
        if record and status in (PlanStatus.PENDING, PlanStatus.NEEDS_REVIEW):
            self._active[position.position_ref] = plan
        return plan


__all__ = [
    "COORDINATOR_VERSION",
    "TRIGGER_EXIT_REASON",
    "CoordinatorPolicy",
    "ExitTrigger",
    "PlanAction",
    "PlanStatus",
    "PositionActionPlan",
    "PositionCoordinator",
    "PositionCoordinatorError",
    "PositionSnapshot",
    "RejectedTrigger",
]
