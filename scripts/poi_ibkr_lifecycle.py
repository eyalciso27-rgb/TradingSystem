"""POI-IBKR-001 owner-run Paper order-lifecycle harness (WP-A3).

The FIRST code path in this repository that can send a real broker order.
It exists only to produce POI-IBKR-001 evidence — submit / acknowledge /
fill / protection / cancel / reconciliation — against the owner's IBKR
**Paper** gateway, one order at a time, under an owner confirmation.

It **reuses** the existing engines and adds no parallel path:

- ``trading_execution_agent.agent.ExecutionAgent`` — the only component
  that may submit; every gate (idempotency, pre-submit action, halt,
  PAPER-only, health, single-use authorization, no blind retry) runs here
  exactly as in production;
- ``trading_execution_agent.protection.ProtectionManager`` — decides the
  protective structure after a fill;
- ``trading_execution_agent.reconciliation.ReconciliationEngine`` —
  compares internal vs broker state; recommends, never acts;
- ``trading_broker_core.port.BrokerPort`` — the only broker boundary; the
  concrete IBKR adapter is loaded lazily at run time (tests inject a fake).

Hard safety envelope (constants, not options — there is no override flag):

- LOCALHOST ONLY: host must be 127.0.0.1/localhost.
- PAPER PORT ONLY: port must be exactly 4002. Port 4001 (live) is rejected
  by name with a dedicated refusal; every other port is rejected too.
- PAPER ENVIRONMENT ONLY: ``Environment.PAPER``; the account snapshot must
  report PAPER and the adapter separately enforces the ``DU`` account gate.
- FIXED INSTRUMENT ALLOWLIST: ``ALLOWED_SYMBOLS`` only; AAPL is allowed
  for the cancel path only (never for a deliberate fill).
- MINIMAL SIZE: quantity <= 1 share and estimated notional <= $50.
- LIMIT ORDERS ONLY for entries; the protective leg is the STOP produced
  by the ProtectionManager. No market orders, no bracket, no OCA.
- NO UNRESTRICTED SUBMIT SURFACE: the harness cannot be asked to send an
  arbitrary order. Symbol/side/quantity/price/type are validated against
  the envelope before anything is built, and a scenario decides the shape.
- DRY-RUN FIRST: ``--execute`` is required to send anything, and it is
  refused unless ``--confirm`` carries the token printed by the dry run of
  the *identical* parameters. A stale or copied token fails closed.
- FAIL-CLOSED + CLEANUP: any unexpected error aborts the run, attempts to
  cancel whatever was submitted, and reports FAIL.
- SANITIZED EVIDENCE: account ids are masked (``DU*****xx``); no
  credential is read, prompted for or printed (login happens only in the
  Gateway UI, by the owner).

Nothing here enables PAPER_AUTO or LIVE. Flatten is not implemented and
must stay disabled. A PASS is recorded only by the owner, from a real run.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading_application.pre_submit_check import (
    PreSubmitAction,
    PreSubmitCheck,
    PreSubmitCheckResult,
    SingleUseAuthorization,
    SubmitAuthorization,
)
from trading_broker_core.port import (
    BrokerCapabilityMode,
    BrokerOrderEvent,
    BrokerPort,
    BrokerPortError,
    CancelOrderRequest,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderState, OrderType
from trading_execution_agent.agent import ExecutionAgent, ExecutionRequest, ExecutionStatus
from trading_execution_agent.protection import (
    FillEvent,
    MissingCapabilityAction,
    ProtectionManager,
    ProtectionPolicy,
    ProtectionStructure,
)
from trading_execution_agent.reconciliation import (
    FindingKind,
    InternalOrderExpectation,
    InternalState,
    ReconciliationEngine,
    ReconciliationPolicy,
    ReconciliationTrigger,
)

HARNESS_VERSION = "poi-ibkr-lifecycle-1.0.0"

# -- safety envelope (constants; no CLI flag may widen any of these) --------
ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost"})
PAPER_API_PORT = 4002
LIVE_API_PORT = 4001
PAPER_ACCOUNT_PREFIX = "DU"

#: The only instruments this harness may ever touch (liquid, low-priced).
ALLOWED_SYMBOLS: frozenset[str] = frozenset({"F", "T", "AAPL"})
#: Allowed for the cancel path only — never for a deliberate fill.
CANCEL_PATH_ONLY_SYMBOLS: frozenset[str] = frozenset({"AAPL"})

MAX_QUANTITY = Decimal("1")
MAX_NOTIONAL = Decimal("50")
#: Entry limit must sit at least this far from the reference price on the
#: cancel path, so the resting order cannot fill by accident.
CANCEL_PATH_MIN_DISCOUNT = Decimal("0.20")
#: Protective stop distance below (BUY) the observed average fill price.
PROTECTION_STOP_FRACTION = Decimal("0.02")

CLIENT_ORDER_PREFIX = "poi-ibkr-001"
TOKEN_DOMAIN = "poi-ibkr-001-owner-run"
TOKEN_LENGTH = 12

SCENARIOS: tuple[str, ...] = (
    "preflight",
    "dry-run",
    "cancel-path",
    "fill-path",
    "reconnect",
    "cleanup",
)
#: Scenarios that may send anything at all.
SUBMITTING_SCENARIOS: frozenset[str] = frozenset({"cancel-path", "fill-path"})
#: Scenarios that legitimately run while a PoI order is still open, so the
#: "no leftover order" gate must not block them.
LEFTOVER_TOLERANT_SCENARIOS: frozenset[str] = frozenset({"reconnect", "cleanup"})

POLICY_REF = "poi-ibkr-001-lifecycle-policy-1"


class LifecycleRefused(Exception):
    """The safety envelope refused the run (always before any broker call)."""


def mask_account(value: str) -> str:
    """Mask an account id for evidence output (``DU*****32``)."""
    text = value.strip()
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}*****{text[-2:]}"


@dataclass(frozen=True, slots=True)
class LifecycleEnvelope:
    """The complete, validated description of one owner-run.

    Constructing this is the ONLY way to describe an order here; there is
    no path that accepts a free-form intent.
    """

    scenario: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    account_alias: str
    host: str
    port: int
    reference_price: Decimal | None = None

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise LifecycleRefused(f"unknown scenario {self.scenario!r}")
        if self.host not in ALLOWED_HOSTS:
            raise LifecycleRefused(
                f"host {self.host!r} refused: localhost only ({sorted(ALLOWED_HOSTS)})"
            )
        if self.port == LIVE_API_PORT:
            raise LifecycleRefused(
                f"port {LIVE_API_PORT} is the LIVE API port and is refused by"
                " construction; this harness is Paper-only"
            )
        if self.port != PAPER_API_PORT:
            raise LifecycleRefused(
                f"port {self.port} refused: only the paper API port {PAPER_API_PORT} is allowed"
            )
        if not self.account_alias.strip():
            raise LifecycleRefused("account_alias is mandatory")
        if self.scenario in ("preflight", "cleanup", "reconnect"):
            return
        if self.symbol not in ALLOWED_SYMBOLS:
            raise LifecycleRefused(
                f"symbol {self.symbol!r} is not in the allowlist {sorted(ALLOWED_SYMBOLS)}"
            )
        if self.scenario == "fill-path" and self.symbol in CANCEL_PATH_ONLY_SYMBOLS:
            raise LifecycleRefused(
                f"symbol {self.symbol!r} is cancel-path only and must never be"
                " used for a deliberate fill"
            )
        if self.side is not OrderSide.BUY:
            raise LifecycleRefused("only BUY entries are allowed (a SELL entry could open a short)")
        if self.quantity <= 0 or self.quantity > MAX_QUANTITY:
            raise LifecycleRefused(
                f"quantity {self.quantity} refused: must be > 0 and <= {MAX_QUANTITY}"
            )
        if self.limit_price <= 0:
            raise LifecycleRefused("limit_price must be positive (LIMIT orders only)")
        notional = self.quantity * self.limit_price
        if notional > MAX_NOTIONAL:
            raise LifecycleRefused(f"estimated notional {notional} exceeds the {MAX_NOTIONAL} cap")

    @property
    def client_order_id(self) -> str:
        return f"{CLIENT_ORDER_PREFIX}-{self.scenario}-{self.symbol}"


def confirmation_token(envelope: LifecycleEnvelope) -> str:
    """Token binding an execution to the exact dry-run parameters.

    The owner obtains it from a dry run and must pass it back verbatim; a
    token from different parameters (or from an older run with a different
    price) does not match, so ``--execute`` fails closed.
    """
    material = "|".join(
        (
            TOKEN_DOMAIN,
            HARNESS_VERSION,
            envelope.scenario,
            envelope.symbol,
            envelope.side.value,
            str(envelope.quantity),
            str(envelope.limit_price),
            envelope.account_alias,
            str(envelope.port),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:TOKEN_LENGTH].upper()


@dataclass
class LifecycleReport:
    """Ordered PASS/FAIL checklist rendered into the evidence artifact."""

    scenario: str
    executed: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def record(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append((name, ok, detail))

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def render(self) -> str:
        mode = "EXECUTED (real broker order path)" if self.executed else "DRY-RUN (nothing sent)"
        lines = [
            f"# POI-IBKR-001 lifecycle harness — {self.scenario}",
            "",
            f"harness: {HARNESS_VERSION}",
            f"mode: {mode}",
            "",
            "| check | result | detail |",
            "|---|---|---|",
        ]
        lines.extend(
            f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |"
            for name, ok, detail in self.checks
        )
        if self.notes:
            lines.extend(["", "## Notes", ""])
            lines.extend(f"- {note}" for note in self.notes)
        lines.extend(
            [
                "",
                f"RESULT: {'PASS' if self.passed else 'FAIL'}",
                "",
                "This artifact alone is NOT a PoI PASS: only the owner may mark",
                "POI-IBKR-001, from a real run, in docs/poi/STATUS.yaml.",
            ]
        )
        return "\n".join(lines)


def _authorized_request(
    envelope: LifecycleEnvelope,
    *,
    client_order_id: str,
    order_type: OrderType,
    side: OrderSide,
    quantity: Decimal,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
    correlation_id: str,
    now: datetime,
) -> ExecutionRequest:
    """Build the queue item the ExecutionAgent consumes.

    The authorization represents the **owner's explicit confirmation** for
    this single PoI order — it is minted only after the confirmation token
    matched, it is single-use, and it never bypasses any agent gate. It is
    deliberately NOT a claim that the automated decision → risk →
    allocation pipeline ran: this is a manual integration experiment, and
    the reason codes below say so.
    """
    candidate_id = f"{CLIENT_ORDER_PREFIX}:{client_order_id}"
    authorization = SubmitAuthorization(
        candidate_id=candidate_id,
        authorized_quantity=quantity,
        authorized_at=now,
    )
    pre_submit = PreSubmitCheckResult(
        candidate_id=candidate_id,
        action=PreSubmitAction.ALLOW,
        authorized_quantity=quantity,
        authorization=authorization,
        allocation_ref="owner-run:manual-minimal-size",
        risk_ref="owner-run:poi-envelope",
        decision_ref="owner-run:owner-confirmed",
        policy_ref=POLICY_REF,
        checks=(
            PreSubmitCheck(
                name="poi_safety_envelope",
                passed=True,
                limit=(
                    f"paper/localhost/{PAPER_API_PORT}; allowlist"
                    f" {sorted(ALLOWED_SYMBOLS)}; qty<={MAX_QUANTITY};"
                    f" notional<={MAX_NOTIONAL}"
                ),
                observed=f"{side.value} {quantity} {envelope.symbol} {order_type.value}",
                effect="PASS",
            ),
            PreSubmitCheck(
                name="owner_confirmation_token",
                passed=True,
                limit="token from a dry run of identical parameters",
                observed="matched",
                effect="PASS",
            ),
        ),
        reason_codes=("OWNER_CONFIRMED_POI_RUN", "NOT_AN_AUTOMATED_DECISION"),
        input_refs=(f"envelope:{envelope.scenario}",),
    )
    return ExecutionRequest(
        client_order_id=client_order_id,
        correlation_id=correlation_id,
        pre_submit=pre_submit,
        authorization=SingleUseAuthorization(authorization),
        environment=Environment.PAPER,
        account_alias=envelope.account_alias,
        symbol=envelope.symbol,
        side=side,
        order_type=order_type,
        limit_price=limit_price,
        stop_price=stop_price,
        strategy_id="poi-ibkr-001-manual",
        source_id="owner-manual",
    )


def _event_id(event: BrokerOrderEvent) -> str:
    """The engine's canonical id for one normalized broker event."""
    return f"{event.sequence}:{event.client_order_id}:{event.state.value}"


def _drain_events(broker: BrokerPort, client_order_id: str) -> list[BrokerOrderEvent]:
    return [e for e in broker.order_events() if e.client_order_id == client_order_id]


def _await_state(
    broker: BrokerPort,
    client_order_id: str,
    wanted: frozenset[OrderState],
    *,
    timeout_seconds: float,
    sleep: Callable[[float], None],
    poll_seconds: float = 1.0,
) -> tuple[BrokerOrderEvent | None, list[BrokerOrderEvent]]:
    """Poll normalized order events until one reaches a wanted state.

    Returns the matching event (or ``None`` on timeout) plus everything
    observed. There is no retry of the ORDER here — only of the read.
    """
    seen: list[BrokerOrderEvent] = []
    deadline = timeout_seconds
    elapsed = 0.0
    while True:
        for event in _drain_events(broker, client_order_id):
            seen.append(event)
            if event.state in wanted:
                return event, seen
        if elapsed >= deadline:
            return None, seen
        sleep(poll_seconds)
        elapsed += poll_seconds


def _all_events(broker: BrokerPort, client_order_ids: Sequence[str]) -> list[BrokerOrderEvent]:
    wanted = set(client_order_ids)
    return [e for e in broker.order_events() if e.client_order_id in wanted]


def _reconcile(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    open_orders: tuple[InternalOrderExpectation, ...],
    positions: dict[str, Decimal],
    protected: dict[str, Decimal],
    processed_event_ids: frozenset[str],
    now: datetime,
    trigger: ReconciliationTrigger,
) -> None:
    engine = ReconciliationEngine(
        ReconciliationPolicy(
            policy_version=POLICY_REF,
            blocking_kinds=frozenset(
                {
                    FindingKind.PROTECTION_MISMATCH,
                    FindingKind.UNEXPECTED_BROKER_ORDER,
                    FindingKind.POSITION_MISMATCH,
                    FindingKind.UNRESOLVED_UNKNOWN_ORDER,
                }
            ),
        )
    )
    internal = InternalState(
        account_alias=envelope.account_alias,
        environment=Environment.PAPER,
        open_orders=open_orders,
        positions=positions,
        protected_quantities=protected,
        # Exactly the events this run actually consumed — never a blanket
        # "everything", so a lost event still surfaces as MISSING_FILL_EVENT.
        processed_fill_event_ids=processed_event_ids,
        as_of=now,
    )
    result = engine.run(broker, internal, trigger=trigger, as_of=now)
    detail = (
        f"reconciled={result.reconciled} findings={len(result.findings)}"
        f" entry_block_required={result.entry_block_required}"
    )
    report.record("reconciliation", result.reconciled, detail)
    for finding in result.findings:
        report.note(
            f"finding {finding.kind.value} on {finding.subject_ref}:"
            f" expected={finding.expected} observed={finding.observed}"
            f" -> {finding.recommendation.value}"
        )


def _preflight(broker: BrokerPort, envelope: LifecycleEnvelope, report: LifecycleReport) -> None:
    """Read-only checks that must pass before anything may be submitted."""
    health = broker.health_check()
    report.record("broker_health", health.healthy, health.detail)

    snapshot = broker.get_account_snapshot(envelope.account_alias)
    report.record(
        "paper_environment",
        snapshot.environment is Environment.PAPER,
        f"environment={snapshot.environment.value}",
    )
    # Always masked: if an operator ever passes a raw DU id as the alias it
    # must still never reach stdout or the evidence artifact in full.
    report.record(
        "paper_account_gate",
        snapshot.account_alias.strip() != "",
        f"alias={mask_account(snapshot.account_alias)} (the adapter enforces the"
        f" {PAPER_ACCOUNT_PREFIX} prefix gate on the real account id)",
    )

    capabilities = broker.capabilities()
    stop_mode = capabilities.order_types.get(OrderType.STOP, BrokerCapabilityMode.UNSUPPORTED)
    report.record(
        "protective_stop_supported",
        stop_mode is BrokerCapabilityMode.NATIVE,
        f"STOP={stop_mode.value}; bracket={capabilities.bracket.value}"
        f"; oca={capabilities.oco.value}",
    )

    if envelope.scenario not in ("preflight", "cleanup", "reconnect"):
        instrument = broker.resolve_instrument(InstrumentQuery(symbol=envelope.symbol))
        report.record(
            "instrument_resolved",
            instrument.symbol == envelope.symbol,
            f"{instrument.symbol} @ {instrument.exchange} {instrument.currency}",
        )

    open_orders = broker.get_open_orders(envelope.account_alias)
    stale = tuple(o for o in open_orders if o.client_order_id.startswith(CLIENT_ORDER_PREFIX))
    if envelope.scenario not in LEFTOVER_TOLERANT_SCENARIOS:
        # Cleanup exists precisely to clear these and reconnect exists to
        # observe them, so neither may be blocked by them; every other
        # scenario refuses to start on top of a leftover.
        report.record(
            "no_stale_poi_orders",
            not stale,
            "none"
            if not stale
            else f"{len(stale)} stale PoI order(s) still open: run --scenario cleanup first",
        )
    else:
        report.record("open_poi_orders", True, f"{len(stale)} still open at the broker")
    positions = broker.get_positions(envelope.account_alias)
    report.record(
        "position_snapshot",
        True,
        f"{len(positions)} position(s) reported before the run",
    )


def _run_cancel_path(
    broker: BrokerPort,
    agent: ExecutionAgent,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
    sleep: Callable[[float], None],
    timeout_seconds: float,
) -> None:
    """Submit a far-from-market resting LIMIT, acknowledge, then cancel."""
    if envelope.reference_price is not None:
        floor = envelope.reference_price * (Decimal("1") - CANCEL_PATH_MIN_DISCOUNT)
        report.record(
            "cancel_path_price_safe",
            envelope.limit_price <= floor,
            f"limit {envelope.limit_price} must be <= {floor}"
            f" ({CANCEL_PATH_MIN_DISCOUNT:%} below reference) so it cannot fill",
        )

    correlation_id = f"{CLIENT_ORDER_PREFIX}-cancel-{int(now.timestamp())}"
    request = _authorized_request(
        envelope,
        client_order_id=envelope.client_order_id,
        order_type=OrderType.LIMIT,
        side=envelope.side,
        quantity=envelope.quantity,
        limit_price=envelope.limit_price,
        stop_price=None,
        correlation_id=correlation_id,
        now=now,
    )
    outcome = agent.process(request)
    report.record(
        "submit",
        outcome.status is ExecutionStatus.SUBMITTED,
        f"status={outcome.status.value} codes={','.join(outcome.reason_codes)}",
    )
    if outcome.status is not ExecutionStatus.SUBMITTED:
        return

    # Idempotency proof: the identical queue item must NOT create a second
    # order (the DoD's "zero duplicate orders").
    repeat = agent.process(request)
    report.record(
        "idempotent_resubmit",
        repeat is outcome or repeat.receipt == outcome.receipt,
        "the same client_order_id returned the original outcome; nothing re-sent",
    )

    ack, seen = _await_state(
        broker,
        envelope.client_order_id,
        frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}),
        timeout_seconds=timeout_seconds,
        sleep=sleep,
    )
    report.record(
        "acknowledged",
        ack is not None and ack.state is OrderState.ACKNOWLEDGED,
        f"observed states: {[e.state.value for e in seen] or 'none'}",
    )

    cancel_outcome = agent.cancel(envelope.client_order_id, correlation_id=correlation_id)
    report.record(
        "cancel_sent",
        cancel_outcome.status is ExecutionStatus.CANCEL_SENT,
        f"status={cancel_outcome.status.value}",
    )
    cancelled, seen_after = _await_state(
        broker,
        envelope.client_order_id,
        frozenset({OrderState.CANCELLED}),
        timeout_seconds=timeout_seconds,
        sleep=sleep,
    )
    report.record(
        "cancel_confirmed",
        cancelled is not None,
        f"observed states: {[e.state.value for e in seen_after] or 'none'}",
    )
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=(),
        positions={},
        protected={},
        processed_event_ids=frozenset(_event_id(e) for e in (*seen, *seen_after)),
        now=now,
        trigger=ReconciliationTrigger.PERIODIC,
    )


def _run_fill_path(
    broker: BrokerPort,
    agent: ExecutionAgent,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
    sleep: Callable[[float], None],
    timeout_seconds: float,
) -> None:
    """Submit a marketable LIMIT, then protect the resulting position."""
    correlation_id = f"{CLIENT_ORDER_PREFIX}-fill-{int(now.timestamp())}"
    request = _authorized_request(
        envelope,
        client_order_id=envelope.client_order_id,
        order_type=OrderType.LIMIT,
        side=envelope.side,
        quantity=envelope.quantity,
        limit_price=envelope.limit_price,
        stop_price=None,
        correlation_id=correlation_id,
        now=now,
    )
    outcome = agent.process(request)
    report.record(
        "submit",
        outcome.status is ExecutionStatus.SUBMITTED,
        f"status={outcome.status.value} codes={','.join(outcome.reason_codes)}",
    )
    if outcome.status is not ExecutionStatus.SUBMITTED:
        return

    fill, seen = _await_state(
        broker,
        envelope.client_order_id,
        frozenset({OrderState.FILLED, OrderState.PARTIALLY_FILLED}),
        timeout_seconds=timeout_seconds,
        sleep=sleep,
    )
    report.record(
        "fill_observed",
        fill is not None,
        f"observed states: {[e.state.value for e in seen] or 'none'}",
    )
    if fill is None:
        # Fail closed: an unfilled entry must not be left resting.
        cancel_outcome = agent.cancel(envelope.client_order_id, correlation_id=correlation_id)
        report.record(
            "unfilled_entry_cancelled",
            cancel_outcome.status is ExecutionStatus.CANCEL_SENT,
            f"no fill within {timeout_seconds}s; cancelled instead of waiting",
        )
        return

    positions = {p.symbol: p for p in broker.get_positions(envelope.account_alias)}
    position = positions.get(envelope.symbol)
    report.record(
        "position_reported",
        position is not None and position.quantity > 0,
        f"quantity={position.quantity if position else 0}",
    )
    if position is None:
        return

    stop_price = (position.average_price * (Decimal("1") - PROTECTION_STOP_FRACTION)).quantize(
        Decimal("0.01")
    )
    manager = ProtectionManager(
        ProtectionPolicy(
            policy_version=POLICY_REF,
            structure=ProtectionStructure.PLAIN_STOP,
            missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
        )
    )
    plan = manager.on_fill(
        FillEvent(
            fill_id=f"{envelope.client_order_id}-fill",
            position_ref=envelope.client_order_id,
            symbol=envelope.symbol,
            environment=Environment.PAPER,
            entry_side=envelope.side,
            filled_quantity=fill.filled_quantity,
            stop_price=stop_price,
            target_price=None,
            observed_at=fill.observed_at,
            correlation_id=correlation_id,
        ),
        broker.capabilities(),
        as_of=now,
    )
    report.record(
        "protection_planned",
        plan.protected_quantity >= fill.filled_quantity,
        f"status={plan.status.value} protected={plan.protected_quantity}"
        f"/{plan.filled_quantity} actions={len(plan.actions)}",
    )

    protective_ids: list[str] = []
    for action in plan.actions:
        spec = action.spec
        protective_id = f"{envelope.client_order_id}-{spec.spec_ref}"
        protective = _authorized_request(
            envelope,
            client_order_id=protective_id,
            order_type=spec.order_type,
            side=spec.side,
            quantity=spec.quantity,
            limit_price=spec.limit_price,
            stop_price=spec.stop_price,
            correlation_id=correlation_id,
            now=now,
        )
        protective_outcome = agent.process(protective)
        report.record(
            f"protection_submitted[{spec.spec_ref}]",
            protective_outcome.status is ExecutionStatus.SUBMITTED,
            f"{spec.side.value} {spec.quantity} {spec.order_type.value}"
            f" stop={spec.stop_price} status={protective_outcome.status.value}",
        )
        if protective_outcome.status is ExecutionStatus.SUBMITTED:
            protective_ids.append(protective_id)

    report.record(
        "no_unprotected_position",
        not manager.unprotected_positions() and len(protective_ids) == len(plan.actions),
        f"unprotected={list(manager.unprotected_positions())};"
        f" protective orders live={len(protective_ids)}",
    )
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=tuple(
            InternalOrderExpectation(
                client_order_id=order_id,
                expected_state=OrderState.ACKNOWLEDGED,
                is_protective=True,
            )
            for order_id in protective_ids
        ),
        positions={envelope.symbol: position.quantity},
        protected={envelope.symbol: plan.protected_quantity},
        processed_event_ids=frozenset(
            _event_id(e) for e in (*seen, *_all_events(broker, protective_ids))
        ),
        now=now,
        trigger=ReconciliationTrigger.PERIODIC,
    )
    report.note(
        "The position and its protective stop are still open on the paper"
        " account: close them manually in the Gateway/TWS UI (this harness"
        " never flattens — flatten stays hard-disabled)."
    )


def _run_reconnect(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
) -> None:
    """Prove state survives a Gateway restart: position + protection intact.

    Read-only: it re-reads what the broker reports after the reconnection
    and reconciles. It submits nothing and cancels nothing — if protection
    were missing, the correct response is an owner decision, not an
    automatic order.
    """
    positions = {p.symbol: p for p in broker.get_positions(envelope.account_alias)}
    live = {symbol: position for symbol, position in positions.items() if position.quantity > 0}
    open_orders = broker.get_open_orders(envelope.account_alias)
    protective = tuple(
        o
        for o in open_orders
        if o.client_order_id.startswith(CLIENT_ORDER_PREFIX) and "stop" in o.client_order_id
    )
    report.record(
        "positions_survived_reconnect",
        True,
        f"{len(live)} open position(s) reported after reconnect",
    )
    # The invariant that matters after a restart: no naked position.
    report.record(
        "protection_survived_reconnect",
        not live or bool(protective),
        f"{len(protective)} protective order(s) still resting for {len(live)} position(s)",
    )
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=tuple(
            InternalOrderExpectation(
                client_order_id=o.client_order_id,
                expected_state=o.state,
                is_protective=True,
            )
            for o in open_orders
        ),
        positions={symbol: position.quantity for symbol, position in live.items()},
        protected={symbol: position.quantity for symbol, position in live.items() if protective},
        processed_event_ids=frozenset(_event_id(e) for e in broker.order_events()),
        now=now,
        trigger=ReconciliationTrigger.STARTUP,
    )


def _run_cleanup(
    broker: BrokerPort,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    now: datetime,
) -> None:
    """Cancel every still-open order this harness created. Never flattens.

    Cleanup goes straight to the port: cancelling is the one broker call
    that reduces exposure, so it must not depend on minting a new
    authorization (which would be a submit permission).
    """
    open_orders = broker.get_open_orders(envelope.account_alias)
    mine = tuple(o for o in open_orders if o.client_order_id.startswith(CLIENT_ORDER_PREFIX))
    report.record("cleanup_scope", True, f"{len(mine)} PoI order(s) to cancel")
    for receipt in mine:
        cancel = broker.cancel_order(
            CancelOrderRequest(
                client_order_id=receipt.client_order_id,
                correlation_id=f"{CLIENT_ORDER_PREFIX}-cleanup",
            )
        )
        report.record(
            f"cleanup_cancel[{receipt.client_order_id}]",
            cancel.state in (OrderState.CANCELLED, OrderState.CANCEL_PENDING),
            f"state={cancel.state.value}",
        )
    _reconcile(
        broker,
        envelope,
        report,
        open_orders=(),
        positions={},
        protected={},
        # Cleanup reads the whole event stream, so it has genuinely seen
        # every event it reports as processed.
        processed_event_ids=frozenset(_event_id(e) for e in broker.order_events()),
        now=now,
        trigger=ReconciliationTrigger.EVENT_DRIVEN,
    )


def run_lifecycle(
    broker_factory: Callable[[], BrokerPort],
    envelope: LifecycleEnvelope,
    *,
    execute: bool,
    confirm_token: str | None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    timeout_seconds: float = 30.0,
) -> LifecycleReport:
    """Run one scenario end to end, fail-closed, with cleanup on error."""
    now_fn = clock or (lambda: datetime.now(tz=UTC))
    sleep_fn = sleep or time.sleep
    expected_token = confirmation_token(envelope)

    if execute and envelope.scenario in SUBMITTING_SCENARIOS:
        if confirm_token is None or confirm_token.strip().upper() != expected_token:
            raise LifecycleRefused(
                "--execute requires --confirm with the token printed by a dry run"
                " of these exact parameters; the supplied token does not match"
            )
    if execute and envelope.scenario not in SUBMITTING_SCENARIOS:
        raise LifecycleRefused(
            f"scenario {envelope.scenario!r} never submits; --execute is refused"
        )

    report = LifecycleReport(scenario=envelope.scenario, executed=execute)
    broker = broker_factory()
    connected = False
    agent = ExecutionAgent(broker, clock=now_fn)
    try:
        broker.connect()
        connected = True
        report.record("connect", True, f"{envelope.host}:{envelope.port} (paper, localhost)")
        _preflight(broker, envelope, report)
        if not report.passed:
            report.note("preflight failed; nothing was submitted (fail closed)")
            return report

        now = now_fn()
        if envelope.scenario == "cleanup":
            _run_cleanup(broker, envelope, report, now=now)
        elif envelope.scenario == "reconnect":
            _run_reconnect(broker, envelope, report, now=now)
        elif envelope.scenario == "preflight":
            # Read-only by definition: no what-if, no intent, nothing built.
            report.note("preflight is read-only; no order intent was constructed")
        elif not execute:
            # What-if only. preview_order never places anything, and the
            # intent below is discarded — no authorization is ever minted.
            preview = broker.preview_order(
                OrderIntent(
                    client_order_id=envelope.client_order_id,
                    authorization_ref="dry-run:what-if-only",
                    correlation_id=f"{CLIENT_ORDER_PREFIX}-dry-run",
                    environment=Environment.PAPER,
                    account_alias=envelope.account_alias,
                    symbol=envelope.symbol,
                    side=envelope.side,
                    order_type=OrderType.LIMIT,
                    quantity=envelope.quantity,
                    limit_price=envelope.limit_price,
                )
            )
            report.record(
                "what_if_preview",
                preview.estimated_notional <= MAX_NOTIONAL,
                f"estimated_notional={preview.estimated_notional}"
                f" warnings={list(preview.warnings)}",
            )
            report.note(f"NOTHING WAS SENT. Confirmation token: {expected_token}")
            report.note(
                "To execute, re-run the identical command with"
                f" --execute --confirm {expected_token}"
            )
        elif envelope.scenario == "cancel-path":
            _run_cancel_path(
                broker,
                agent,
                envelope,
                report,
                now=now,
                sleep=sleep_fn,
                timeout_seconds=timeout_seconds,
            )
        elif envelope.scenario == "fill-path":
            _run_fill_path(
                broker,
                agent,
                envelope,
                report,
                now=now,
                sleep=sleep_fn,
                timeout_seconds=timeout_seconds,
            )
    except BrokerPortError as error:
        report.record("fail_closed", False, f"broker error {error.code.value}; no retry")
        _emergency_cleanup(agent, envelope, report, execute=execute)
    except LifecycleRefused as refusal:
        report.record("fail_closed", False, str(refusal))
    except Exception as error:  # noqa: BLE001 - unknown => fail closed
        report.record(
            "fail_closed", False, f"unknown error, failing closed: {type(error).__name__}"
        )
        _emergency_cleanup(agent, envelope, report, execute=execute)
    finally:
        if connected:
            try:
                broker.disconnect()
                report.record("disconnect", True, "clean disconnect")
            except Exception:  # noqa: BLE001
                report.record("disconnect", False, "disconnect failed")
    return report


def _emergency_cleanup(
    agent: ExecutionAgent,
    envelope: LifecycleEnvelope,
    report: LifecycleReport,
    *,
    execute: bool,
) -> None:
    """Best-effort cancel of anything this run submitted. Never flattens."""
    if not execute:
        return
    try:
        outcome = agent.cancel(
            envelope.client_order_id, correlation_id=f"{CLIENT_ORDER_PREFIX}-abort"
        )
        report.note(f"abort cleanup: cancel status={outcome.status.value}")
    except Exception:  # noqa: BLE001
        report.note("abort cleanup FAILED — inspect open orders in the Gateway UI manually")


def _load_broker_factory(
    module_name: str, *, account_alias: str, host: str, port: int, client_id: int
) -> Callable[[], BrokerPort]:
    """Late, isolated import of the run-time adapter module (never at top).

    The module must expose ``create_broker`` or ``build_paper_adapter``
    with the canonical keyword signature. No broker SDK is imported by
    this harness itself; tests inject a fake factory instead.
    """
    import importlib

    module = importlib.import_module(module_name)
    builder = getattr(module, "create_broker", None) or getattr(module, "build_paper_adapter", None)
    if builder is None or not callable(builder):
        raise LifecycleRefused(
            f"broker module {module_name!r} must expose create_broker() or build_paper_adapter()"
        )

    def factory() -> BrokerPort:
        broker: BrokerPort = builder(
            account_aliases={account_alias: account_alias},
            host=host,
            port=port,
            client_id=client_id,
        )
        return broker

    return factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "POI-IBKR-001 Paper order-lifecycle harness. Owner-run only,"
            " localhost + paper port 4002 only, one minimal order at a time."
            " Dry-run by default: --execute additionally requires the"
            " confirmation token printed by a dry run of identical parameters."
        )
    )
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--account-alias", required=True, help="alias, never a broker account id")
    parser.add_argument("--symbol", default="F", help=f"one of {sorted(ALLOWED_SYMBOLS)}")
    parser.add_argument("--quantity", default="1", help=f"<= {MAX_QUANTITY}")
    parser.add_argument("--limit-price", default=None, help="required for submitting scenarios")
    parser.add_argument(
        "--reference-price",
        default=None,
        help="cancel path: current price, used to prove the limit cannot fill",
    )
    parser.add_argument("--host", default="127.0.0.1", help="127.0.0.1/localhost only")
    parser.add_argument("--port", type=int, default=PAPER_API_PORT, help="paper API port only")
    parser.add_argument("--client-id", type=int, default=7, help="IBKR API client id")
    parser.add_argument("--broker-module", required=True, help="module exposing create_broker()")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="ACTUALLY SEND to the paper broker (requires --confirm)",
    )
    parser.add_argument("--confirm", default=None, help="token printed by the dry run")
    parser.add_argument("--evidence-out", default=None, help="write the sanitized artifact here")
    return parser


def _decimal(value: str | None, *, default: Decimal) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(value)
    except (ValueError, ArithmeticError) as error:
        raise LifecycleRefused(f"not a valid decimal: {value!r}") from error


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        limit_price = _decimal(args.limit_price, default=Decimal("0"))
        if args.scenario in SUBMITTING_SCENARIOS and limit_price <= 0:
            raise LifecycleRefused("--limit-price is required for a submitting scenario")
        reference = (
            _decimal(args.reference_price, default=Decimal("0"))
            if args.reference_price is not None
            else None
        )
        envelope = LifecycleEnvelope(
            scenario=args.scenario,
            symbol=args.symbol,
            side=OrderSide.BUY,
            quantity=_decimal(args.quantity, default=Decimal("1")),
            limit_price=limit_price if limit_price > 0 else Decimal("1"),
            account_alias=args.account_alias,
            host=args.host,
            port=args.port,
            reference_price=reference,
        )
        factory = _load_broker_factory(
            args.broker_module,
            account_alias=args.account_alias,
            host=args.host,
            port=args.port,
            client_id=args.client_id,
        )
    except (LifecycleRefused, ImportError) as error:
        print(f"REFUSED: {error}")
        return 2

    report = run_lifecycle(
        factory,
        envelope,
        execute=args.execute,
        confirm_token=args.confirm,
        timeout_seconds=args.timeout_seconds,
    )
    rendered = report.render()
    print(rendered)
    if args.evidence_out is not None:
        Path(args.evidence_out).write_text(rendered + "\n", encoding="utf-8")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
