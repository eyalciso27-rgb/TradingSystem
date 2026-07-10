"""Reconciliation engine (IMP-1006) — internal state vs broker snapshot.

Authority: Integration 9.5 (startup / periodic / event-driven
reconciliation of account, positions, orders and executions), 9.2 (after a
disconnect no new orders are allowed until a successful reconciliation),
9.4 (broker events are append-only and processed idempotently),
Decision_Contract 12.5 (UNKNOWN order state is resolved ONLY by
reconciliation). Task DoD: **a mismatch creates a controlled state and
blocks new entries if required**.

The engine COMPARES and RECOMMENDS — it never fixes anything by itself:

- it never submits, cancels or modifies an order (a static test enforces
  that no such call exists here);
- every finding carries a recommendation (NEEDS_REVIEW / RESYNC_STATE /
  BLOCK_NEW_ENTRIES) and full refs, but any future corrective action must
  go through the FULL pipeline: Risk -> Final Pre-submit -> one-time
  SubmitAuthorization -> Execution Agent;
- when a finding's kind is in the policy's ``blocking_kinds`` the report
  demands an entry block and includes a ready-made ``HaltRecord``
  recommendation for the emergency layer (the controlled state) — the
  engine itself does not enact it;
- unknown/unresolved broker order state is surfaced as a finding, never
  retried or guessed away.

Deterministic and sanitized: same internal state + same broker snapshot =>
the same report; findings reference aliases/ids, never a full broker
account id and never account monetary values beyond what the caller
already holds.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_application.emergency import HaltRecord
from trading_broker_core.port import BrokerPort
from trading_contracts.enums import Environment, HaltScope, OrderState

RECONCILIATION_VERSION = "reconciliation-engine-1.0.0"


class ReconciliationError(Exception):
    """Fail-closed reconciliation failure (bad policy/inputs)."""


class ReconciliationTrigger(StrEnum):
    """When this run was started (Integration 9.5)."""

    STARTUP = "STARTUP"
    PERIODIC = "PERIODIC"
    EVENT_DRIVEN = "EVENT_DRIVEN"


class FindingKind(StrEnum):
    """Canonical mismatch kinds between internal state and the broker."""

    MISSING_BROKER_ORDER = "MISSING_BROKER_ORDER"
    UNEXPECTED_BROKER_ORDER = "UNEXPECTED_BROKER_ORDER"
    ORDER_STATE_MISMATCH = "ORDER_STATE_MISMATCH"
    POSITION_MISMATCH = "POSITION_MISMATCH"
    PROTECTION_MISMATCH = "PROTECTION_MISMATCH"
    UNRESOLVED_UNKNOWN_ORDER = "UNRESOLVED_UNKNOWN_ORDER"
    DUPLICATE_FILL_EVENT = "DUPLICATE_FILL_EVENT"
    MISSING_FILL_EVENT = "MISSING_FILL_EVENT"


class Recommendation(StrEnum):
    """What the finding asks the OUTSIDE pipeline to do (never done here)."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    RESYNC_STATE = "RESYNC_STATE"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"


@dataclass(frozen=True, slots=True)
class ReconciliationPolicy:
    """Which mismatch kinds demand an entry block — config, not code."""

    policy_version: str
    blocking_kinds: frozenset[FindingKind]

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ReconciliationError("policy_version is mandatory")


@dataclass(frozen=True, slots=True)
class InternalOrderExpectation:
    client_order_id: str
    expected_state: OrderState
    is_protective: bool = False


@dataclass(frozen=True, slots=True)
class InternalState:
    """The system's own view, frozen at the start of the run."""

    account_alias: str
    environment: Environment
    open_orders: tuple[InternalOrderExpectation, ...]
    positions: Mapping[str, Decimal]  # symbol -> expected quantity
    protected_quantities: Mapping[str, Decimal]  # symbol -> protected qty
    processed_fill_event_ids: frozenset[str]
    as_of: datetime

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Finding:
    kind: FindingKind
    subject_ref: str  # client_order_id / symbol / event ref - never account ids
    expected: str
    observed: str
    recommendation: Recommendation


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Controlled, audit-ready outcome of one reconciliation run."""

    trigger: ReconciliationTrigger
    account_alias: str
    reconciled: bool
    findings: tuple[Finding, ...]
    entry_block_required: bool
    recommended_halt: HaltRecord | None
    policy_ref: str
    engine_version: str
    input_refs: tuple[str, ...]
    run_at: datetime

    def __post_init__(self) -> None:
        if self.run_at.tzinfo is None:
            raise ValueError("run_at must be timezone-aware")


_OPEN_STATES = frozenset(
    {
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_PENDING,
    }
)


class ReconciliationEngine:
    """Compares internal state with the broker; recommends, never acts."""

    def __init__(self, policy: ReconciliationPolicy) -> None:
        self._policy = policy

    def run(
        self,
        broker: BrokerPort,
        internal: InternalState,
        *,
        trigger: ReconciliationTrigger,
        as_of: datetime,
    ) -> ReconciliationReport:
        if as_of.tzinfo is None:
            raise ReconciliationError("as_of must be timezone-aware")
        if internal.as_of > as_of:
            raise ReconciliationError("lookahead: internal state is newer than as_of")

        broker_state = broker.reconcile(internal.account_alias)
        events = tuple(broker.order_events())

        findings: list[Finding] = []

        # -- orders: internal vs broker ------------------------------------
        broker_orders = {r.client_order_id: r for r in broker_state.open_orders}
        internal_ids = {o.client_order_id for o in internal.open_orders}
        for expectation in sorted(internal.open_orders, key=lambda o: o.client_order_id):
            receipt = broker_orders.get(expectation.client_order_id)
            if receipt is None:
                if expectation.expected_state in _OPEN_STATES:
                    findings.append(
                        Finding(
                            kind=FindingKind.MISSING_BROKER_ORDER,
                            subject_ref=f"order:{expectation.client_order_id}",
                            expected=expectation.expected_state.value,
                            observed="absent at broker",
                            recommendation=Recommendation.NEEDS_REVIEW,
                        )
                    )
                continue
            if receipt.state is OrderState.UNKNOWN:
                findings.append(
                    Finding(
                        kind=FindingKind.UNRESOLVED_UNKNOWN_ORDER,
                        subject_ref=f"order:{expectation.client_order_id}",
                        expected=expectation.expected_state.value,
                        observed=OrderState.UNKNOWN.value,
                        recommendation=Recommendation.NEEDS_REVIEW,
                    )
                )
            elif receipt.state is not expectation.expected_state:
                findings.append(
                    Finding(
                        kind=FindingKind.ORDER_STATE_MISMATCH,
                        subject_ref=f"order:{expectation.client_order_id}",
                        expected=expectation.expected_state.value,
                        observed=receipt.state.value,
                        recommendation=Recommendation.RESYNC_STATE,
                    )
                )
        for client_order_id in sorted(set(broker_orders) - internal_ids):
            findings.append(
                Finding(
                    kind=FindingKind.UNEXPECTED_BROKER_ORDER,
                    subject_ref=f"order:{client_order_id}",
                    expected="not tracked internally",
                    observed=broker_orders[client_order_id].state.value,
                    recommendation=Recommendation.NEEDS_REVIEW,
                )
            )

        # -- unresolved UNKNOWN ids reported by the broker layer ------------
        for client_order_id in broker_state.unresolved_client_order_ids:
            findings.append(
                Finding(
                    kind=FindingKind.UNRESOLVED_UNKNOWN_ORDER,
                    subject_ref=f"order:{client_order_id}",
                    expected="resolved state",
                    observed="UNKNOWN pending reconciliation",
                    recommendation=Recommendation.NEEDS_REVIEW,
                )
            )

        # -- positions -------------------------------------------------------
        broker_positions = {p.symbol: p.quantity for p in broker_state.positions}
        for symbol in sorted(set(internal.positions) | set(broker_positions)):
            expected = internal.positions.get(symbol, Decimal(0))
            observed = broker_positions.get(symbol, Decimal(0))
            if expected != observed:
                findings.append(
                    Finding(
                        kind=FindingKind.POSITION_MISMATCH,
                        subject_ref=f"position:{symbol}",
                        expected=str(expected),
                        observed=str(observed),
                        recommendation=Recommendation.BLOCK_NEW_ENTRIES,
                    )
                )

        # -- protection coverage ----------------------------------------------
        for symbol in sorted(broker_positions):
            observed_quantity = broker_positions[symbol]
            protected = internal.protected_quantities.get(symbol, Decimal(0))
            if observed_quantity > 0 and protected != observed_quantity:
                findings.append(
                    Finding(
                        kind=FindingKind.PROTECTION_MISMATCH,
                        subject_ref=f"position:{symbol}",
                        expected=f"protected {observed_quantity}",
                        observed=f"protected {protected}",
                        recommendation=Recommendation.NEEDS_REVIEW,
                    )
                )

        # -- fill events: idempotent, none lost, none duplicated ---------------
        seen: set[str] = set()
        for event in events:
            event_ref = f"event:{event.sequence}:{event.client_order_id}"
            event_id = f"{event.sequence}:{event.client_order_id}:{event.state.value}"
            if event_id in seen:
                findings.append(
                    Finding(
                        kind=FindingKind.DUPLICATE_FILL_EVENT,
                        subject_ref=event_ref,
                        expected="each broker event exactly once",
                        observed="duplicate sequence",
                        recommendation=Recommendation.NEEDS_REVIEW,
                    )
                )
                continue
            seen.add(event_id)
            if event_id not in internal.processed_fill_event_ids:
                findings.append(
                    Finding(
                        kind=FindingKind.MISSING_FILL_EVENT,
                        subject_ref=event_ref,
                        expected="processed internally",
                        observed="not processed",
                        recommendation=Recommendation.RESYNC_STATE,
                    )
                )

        ordered = tuple(findings)
        blocking = [f for f in ordered if f.kind in self._policy.blocking_kinds]
        entry_block_required = bool(blocking)
        recommended_halt = (
            HaltRecord(
                scope=HaltScope.ACCOUNT_HALT,
                environment=internal.environment,
                target_ref=internal.account_alias,  # alias only, never account id
                reason_code=f"RECONCILIATION_{blocking[0].kind.value}",
                keep_protection=True,
            )
            if entry_block_required
            else None
        )
        return ReconciliationReport(
            trigger=trigger,
            account_alias=internal.account_alias,
            reconciled=not ordered,
            findings=ordered,
            entry_block_required=entry_block_required,
            recommended_halt=recommended_halt,
            policy_ref=f"reconciliation-policy:{self._policy.policy_version}",
            engine_version=RECONCILIATION_VERSION,
            input_refs=(
                f"internal:{internal.account_alias}@{internal.as_of.isoformat()}",
                f"broker-reconcile:{internal.account_alias}"
                f"@{broker_state.reconciled_at.isoformat()}",
            ),
            run_at=as_of,
        )


__all__ = [
    "RECONCILIATION_VERSION",
    "Finding",
    "FindingKind",
    "InternalOrderExpectation",
    "InternalState",
    "Recommendation",
    "ReconciliationEngine",
    "ReconciliationError",
    "ReconciliationPolicy",
    "ReconciliationReport",
    "ReconciliationTrigger",
]
