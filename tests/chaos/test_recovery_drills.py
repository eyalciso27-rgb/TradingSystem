"""IMP-1303 — chaos and recovery drills (deterministic, fakes only).

These drills prove the system's **fail-closed** behaviour across the fault
domains named in IMP-1303, using only the real in-process engines over the
canonical ``FakeBroker`` and in-memory fixtures. They never import an IBKR
SDK, open a socket, or touch any broker execution surface — no submit,
preview, cancel, replace or modify against a real broker is possible from
here. Recovery from every simulated fault goes through canonical
reconciliation, never a blind retry.

Fault domains covered as automated drills (see
``docs/runbooks/CHAOS_RECOVERY_DRILLS.md`` for the full gap analysis and
the host-only owner-run drills that remain BLOCKED by POI-IBKR-001):

- IBKR/API disconnect and reconnect — a submit while disconnected fails
  terminally; recovery is a reconciliation, not a resend.
- market-data loss and stale data — the final pre-submit gate refuses
  (missing / stale market or account snapshot) and mints no authorization.
- application/service restart — a restarted engine with empty state never
  resends; the open broker order is rebuilt only via reconciliation.
- duplicate delivery / replay — the same queue item and the same fill are
  idempotent; nothing is duplicated.
- UI outage — the kill-switch / halt gate stays deterministic with no UI
  and no LLM, and protective exits are never blocked by a halt.
- partial processing and reconciliation — an unprotected position is a
  loud, blocking finding; entries are blocked until it is resolved.

This file does not mark any PoI PASS and does not change POI-IBKR-001,
which remains BLOCKED / NOT RUN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.auth import InMemoryAuditSink, Principal, Role
from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.emergency import (
    EmergencyControlService,
    EntryCheckpoint,
    EntryContext,
    HaltRecord,
    InMemoryEmergencyCommandStore,
)
from trading_application.portfolio_allocator import CandidateAllocation
from trading_application.pre_submit_check import (
    ApprovalState,
    ChangeAction,
    ExecutionContext,
    FinalPreSubmitCheck,
    PreSubmitAction,
    PreSubmitCheckResult,
    PreSubmitPolicy,
    PreSubmitRequest,
    RefreshedAccount,
    RefreshedMarket,
    SingleUseAuthorization,
    StaleAction,
    SubmitAuthorization,
)
from trading_application.risk_engine import RiskResult
from trading_broker_core.fake import DEFAULT_CAPABILITIES, FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    OrderSide,
)
from trading_contracts.decision import DecisionResult
from trading_contracts.enums import (
    ActionTier,
    DecisionOutcome,
    Environment,
    HaltScope,
    OrderState,
    OrderType,
    PortfolioAction,
    RiskDecisionOutcome,
)
from trading_execution_agent.agent import (
    ExecutionAgent,
    ExecutionRequest,
    ExecutionStatus,
)
from trading_execution_agent.protection import (
    FillEvent,
    MissingCapabilityAction,
    PlanStatus,
    ProtectionManager,
    ProtectionPolicy,
    ProtectionStructure,
)
from trading_execution_agent.reconciliation import (
    FindingKind,
    InternalOrderExpectation,
    InternalState,
    Recommendation,
    ReconciliationEngine,
    ReconciliationPolicy,
    ReconciliationTrigger,
)

AS_OF = datetime(2026, 7, 22, 14, 0, tzinfo=UTC)
RECENT = AS_OF - timedelta(seconds=10)

ACCOUNT = AccountSnapshot(
    account_alias="paper-main",
    environment=Environment.PAPER,
    net_liquidation_value=Decimal("100000"),
    cash=Decimal("60000"),
    buying_power=Decimal("120000"),
    observed_at=AS_OF - timedelta(minutes=1),
)

RECON_POLICY = ReconciliationPolicy(
    policy_version="chaos-recon-1",
    blocking_kinds=frozenset(
        {
            FindingKind.POSITION_MISMATCH,
            FindingKind.PROTECTION_MISMATCH,
            FindingKind.UNRESOLVED_UNKNOWN_ORDER,
            FindingKind.UNEXPECTED_BROKER_ORDER,
        }
    ),
)


# ---------------------------------------------------------------------------
# Execution-side drill: the real ExecutionAgent + ReconciliationEngine over
# the canonical FakeBroker. No IBKR SDK, no socket, no real broker surface.
# ---------------------------------------------------------------------------
@dataclass
class ExecutionDrill:
    broker: FakeBroker = field(default_factory=lambda: FakeBroker(accounts={"paper-main": ACCOUNT}))

    def __post_init__(self) -> None:
        self.broker.connect()
        self.agent = ExecutionAgent(self.broker, clock=lambda: AS_OF)
        self.reconciliation = ReconciliationEngine(RECON_POLICY)

    def request(self, client_order_id: str, *, owner_approved: bool = True) -> ExecutionRequest:
        authorization = (
            SubmitAuthorization(
                candidate_id=f"cand-{client_order_id}",
                authorized_quantity=Decimal("1"),
                authorized_at=AS_OF,
            )
            if owner_approved
            else None
        )
        pre_submit = PreSubmitCheckResult(
            candidate_id=f"cand-{client_order_id}",
            action=PreSubmitAction.ALLOW,
            authorized_quantity=Decimal("1") if authorization else Decimal(0),
            authorization=authorization,
            allocation_ref=f"allocation:cand-{client_order_id}@ALLOW",
            risk_ref="risk:APPROVE",
            decision_ref="sha256:" + "c" * 64,
            policy_ref="pre-submit-policy:p1",
            checks=(),
            reason_codes=("PRE_SUBMIT_CLEARED",),
            input_refs=("observation:a-1@src-1",),
        )
        return ExecutionRequest(
            client_order_id=client_order_id,
            correlation_id=f"corr-{client_order_id}",
            pre_submit=pre_submit,
            authorization=SingleUseAuthorization(authorization) if authorization else None,
            environment=Environment.PAPER,
            account_alias="paper-main",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("10"),
            stop_price=None,
            strategy_id="strat-1",
            source_id="src-1",
        )

    def processed_event_ids(self) -> frozenset[str]:
        """Canonical ids of every broker event a healthy inbox would have seen."""
        return frozenset(
            f"{event.sequence}:{event.client_order_id}:{event.state.value}"
            for event in self.broker.order_events()
        )

    def reconcile(
        self,
        *,
        open_orders: tuple[InternalOrderExpectation, ...] = (),
        positions: dict[str, Decimal] | None = None,
        protected: dict[str, Decimal] | None = None,
        processed_event_ids: frozenset[str] | None = None,
        trigger: ReconciliationTrigger = ReconciliationTrigger.PERIODIC,
    ) -> object:
        internal = InternalState(
            account_alias="paper-main",
            environment=Environment.PAPER,
            open_orders=open_orders,
            positions=positions or {},
            protected_quantities=protected or {},
            processed_fill_event_ids=(
                processed_event_ids
                if processed_event_ids is not None
                else self.processed_event_ids()
            ),
            as_of=AS_OF,
        )
        return self.reconciliation.run(self.broker, internal, trigger=trigger, as_of=AS_OF)


# -- IBKR/API disconnect and reconnect --------------------------------------


def test_disconnect_makes_submit_fail_closed_without_retry() -> None:
    drill = ExecutionDrill()
    drill.broker.disconnect()  # API down mid-session
    outcome = drill.agent.process(drill.request("ord-disc"))
    assert outcome.status in (ExecutionStatus.FAILED, ExecutionStatus.NEEDS_REVIEW)
    assert outcome.receipt is None
    # Nothing was queued at the broker while it was down.
    drill.broker.connect()
    assert drill.broker.get_open_orders("paper-main") == ()


def test_reconnect_recovery_is_a_reconciliation_not_a_resend() -> None:
    drill = ExecutionDrill()
    drill.agent.process(drill.request("ord-rc"))  # one live order
    drill.broker.disconnect()
    drill.broker.connect()  # reconnect
    report = drill.reconcile(
        open_orders=(
            InternalOrderExpectation(
                client_order_id="ord-rc", expected_state=OrderState.ACKNOWLEDGED
            ),
        ),
        trigger=ReconciliationTrigger.STARTUP,
    )
    assert report.reconciled is True  # type: ignore[attr-defined]
    assert report.findings == ()  # type: ignore[attr-defined]
    assert len(drill.broker.get_open_orders("paper-main")) == 1  # never re-sent


# -- application / service restart ------------------------------------------


def test_service_restart_rebuilds_state_only_via_reconciliation() -> None:
    drill = ExecutionDrill()
    drill.agent.process(drill.request("ord-restart"))
    # Simulated process restart: a fresh agent with empty internal memory.
    restarted = ExecutionAgent(drill.broker, clock=lambda: AS_OF)
    assert restarted.cancel("ord-restart", correlation_id="c").status is ExecutionStatus.REFUSED
    # Recovery: startup reconciliation detects the broker order the restarted
    # agent knows nothing about — it recommends review, never resubmits.
    report = drill.reconcile(open_orders=(), trigger=ReconciliationTrigger.STARTUP)
    kinds = {f.kind for f in report.findings}  # type: ignore[attr-defined]
    assert FindingKind.UNEXPECTED_BROKER_ORDER in kinds
    for finding in report.findings:  # type: ignore[attr-defined]
        assert finding.recommendation in (
            Recommendation.NEEDS_REVIEW,
            Recommendation.RESYNC_STATE,
            Recommendation.BLOCK_NEW_ENTRIES,
        )
    assert len(drill.broker.get_open_orders("paper-main")) == 1  # untouched


# -- duplicate delivery / replay --------------------------------------------


def test_duplicate_queue_delivery_never_duplicates_an_order() -> None:
    drill = ExecutionDrill()
    request = drill.request("ord-dup")
    first = drill.agent.process(request)
    second = drill.agent.process(request)  # at-least-once redelivery
    assert second is first  # same terminal outcome, nothing re-sent
    assert len(drill.broker.get_open_orders("paper-main")) == 1


def test_duplicate_fill_never_duplicates_protection() -> None:
    manager = ProtectionManager(
        ProtectionPolicy(
            policy_version="chaos-prot-1",
            structure=ProtectionStructure.PLAIN_STOP,
            missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
        )
    )
    fill = FillEvent(
        fill_id="fill-dup",
        position_ref="pos-AAPL",
        symbol="AAPL",
        environment=Environment.PAPER,
        entry_side=OrderSide.BUY,
        filled_quantity=Decimal("1"),
        stop_price=Decimal("9"),
        target_price=None,
        observed_at=RECENT,
        correlation_id="corr-dup",
    )
    plan_a = manager.on_fill(fill, DEFAULT_CAPABILITIES, as_of=AS_OF)
    plan_b = manager.on_fill(fill, DEFAULT_CAPABILITIES, as_of=AS_OF)
    assert plan_b is plan_a
    assert plan_a.protected_quantity == Decimal("1")


# -- partial processing and reconciliation ----------------------------------


def test_unprotected_position_is_a_loud_blocking_finding() -> None:
    manager = ProtectionManager(
        ProtectionPolicy(
            policy_version="chaos-prot-2",
            structure=ProtectionStructure.PLAIN_STOP,
            missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
        )
    )
    no_stop = BrokerCapabilities(
        order_types={
            OrderType.LIMIT: BrokerCapabilityMode.NATIVE,
            OrderType.STOP: BrokerCapabilityMode.UNSUPPORTED,
            OrderType.MARKET: BrokerCapabilityMode.NATIVE,
        },
        bracket=BrokerCapabilityMode.UNSUPPORTED,
        oco=BrokerCapabilityMode.UNSUPPORTED,
        trailing=BrokerCapabilityMode.UNSUPPORTED,
        fractional=BrokerCapabilityMode.DISABLED,
        outside_rth=BrokerCapabilityMode.DISABLED,
        what_if=BrokerCapabilityMode.NATIVE,
        streaming_events=BrokerCapabilityMode.NATIVE,
    )
    fill = FillEvent(
        fill_id="fill-partial",
        position_ref="pos-AAPL",
        symbol="AAPL",
        environment=Environment.PAPER,
        entry_side=OrderSide.BUY,
        filled_quantity=Decimal("1"),
        stop_price=Decimal("9"),
        target_price=None,
        observed_at=RECENT,
        correlation_id="corr-partial",
    )
    plan = manager.on_fill(fill, no_stop, as_of=AS_OF)
    assert plan.status is PlanStatus.NEEDS_REVIEW
    assert "POSITION_WOULD_BE_UNPROTECTED" in plan.reason_codes
    assert manager.unprotected_positions() == ("pos-AAPL",)

    # Reconciliation turns the same gap into an entry-blocking finding.
    drill = ExecutionDrill()
    report = drill.reconcile(positions={"AAPL": Decimal("1")}, protected={})
    kinds = {f.kind for f in report.findings}  # type: ignore[attr-defined]
    assert FindingKind.POSITION_MISMATCH in kinds or FindingKind.PROTECTION_MISMATCH in kinds
    assert report.entry_block_required is True  # type: ignore[attr-defined]


def test_reconciliation_does_not_remove_a_protective_exit() -> None:
    # A matched position with matched protection reconciles clean; the engine
    # only ever recommends — it never issues a cancel of the protective leg.
    drill = ExecutionDrill()
    report = drill.reconcile(positions={"AAPL": Decimal("0")}, protected={"AAPL": Decimal("0")})
    assert report.reconciled is True  # type: ignore[attr-defined]
    # The engine exposes no mutating method; recovery is advisory only.
    assert not hasattr(drill.reconciliation, "cancel_order")
    assert not hasattr(drill.reconciliation, "submit_order")


# ---------------------------------------------------------------------------
# Market-data loss and stale data — the deterministic final pre-submit gate.
# ---------------------------------------------------------------------------
PRESUBMIT_POLICY = PreSubmitPolicy(
    policy_version="chaos-pre-submit-1",
    max_price_slippage_pct=Decimal("0.01"),
    max_risk_increase_pct=Decimal("0.10"),
    market_max_staleness_seconds=30,
    account_max_staleness_seconds=60,
    decision_max_age_seconds=300,
    approval_max_age_seconds=600,
    require_manual_approval=False,
    price_change_action=ChangeAction.RESIZE,
    risk_change_action=ChangeAction.RESIZE,
    stale_data_action=StaleAction.NEEDS_REVIEW,
)
PRESUBMIT = FinalPreSubmitCheck(PRESUBMIT_POLICY)


def _risk() -> RiskResult:
    return RiskResult(
        outcome=RiskDecisionOutcome.APPROVE,
        approved_quantity=Decimal("100"),
        approved_risk_amount=Decimal("100"),
        max_loss_amount=Decimal("100"),
        risk_budget_equity=Decimal("50000"),
        decision_ref="decision:d1@sha256:" + "a" * 64,
        policy_ref="risk-policy:r1",
        engine_version="risk-engine-1.0.0",
        checks=(),
        reason_codes=(),
        input_refs=("observation:a-1@src-1",),
    )


def _decision() -> EngineDecision:
    result = DecisionResult(
        decision_id=UUID("00000000-0000-0000-0000-0000000000d1"),
        candidate_id=UUID("00000000-0000-0000-0000-0000000000c1"),
        outcome=DecisionOutcome.APPROVE,
        action_tier=ActionTier.AUTO_FULL,
        strategy_version="1.0.0",
        decision_policy_version="1.0.0",
        decided_at=RECENT,
        decision_hash="sha256:" + "b" * 64,
    )
    return EngineDecision(
        result=result,
        explanation=DecisionExplanation(
            fusion_ref="fusion:s@1.0.0",
            manifest_ref="manifest:s@1.0.0",
            policy_ref="decision-policy:1.0.0",
            engine_version="decision-engine-1.0.0",
            reason_codes=(),
            blocking_reasons=(),
            input_refs=("observation:a-1@src-1",),
            validation_note="FIXTURES_VALIDATED_ONLY",
        ),
    )


def _allocation() -> CandidateAllocation:
    return CandidateAllocation(
        candidate_id="cand-1",
        action=PortfolioAction.ALLOW,
        requested_quantity=Decimal("100"),
        allocated_quantity=Decimal("100"),
        allocated_risk=Decimal("100"),
        allocated_notional=Decimal("1000"),
        decision_ref="decision:d1@sha256:" + "a" * 64,
        risk_ref="risk:r1:APPROVE",
        policy_ref="portfolio-policy:p1",
        caps_evaluated=(),
        reason_codes=(),
        input_refs=("observation:a-1@src-1",),
    )


def _request() -> PreSubmitRequest:
    return PreSubmitRequest(
        allocation=_allocation(),
        risk_result=_risk(),
        decision=_decision(),
        symbol="AAPL",
        strategy_id="strat-1",
        source_id="src-1",
        environment=Environment.PAPER,
        planned_entry_price=Decimal("10"),
        planned_stop_price=Decimal("9"),
    )


def _run_presubmit(
    *,
    market: RefreshedMarket,
    account: RefreshedAccount,
    context: ExecutionContext | None = None,
) -> PreSubmitCheckResult:
    return PRESUBMIT.check(
        _request(),
        market,
        account,
        ApprovalState(approved=True, approved_at=RECENT),
        context if context is not None else ExecutionContext(),
        as_of=AS_OF,
    )


def _assert_no_authorization(result: PreSubmitCheckResult) -> None:
    assert result.action in (PreSubmitAction.NEEDS_REVIEW, PreSubmitAction.CANCEL)
    assert result.authorization is None  # nothing that could ever be submitted


def test_missing_market_data_mints_no_authorization() -> None:
    _assert_no_authorization(
        _run_presubmit(
            market=RefreshedMarket(price=None, observed_at=RECENT),
            account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=RECENT),
        )
    )


def test_stale_market_data_mints_no_authorization() -> None:
    stale = AS_OF - timedelta(seconds=120)  # older than the 30s window
    _assert_no_authorization(
        _run_presubmit(
            market=RefreshedMarket(price=Decimal("10"), observed_at=stale),
            account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=RECENT),
        )
    )


def test_stale_account_data_mints_no_authorization() -> None:
    stale = AS_OF - timedelta(seconds=120)  # older than the 60s window
    _assert_no_authorization(
        _run_presubmit(
            market=RefreshedMarket(price=Decimal("10"), observed_at=RECENT),
            account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=stale),
        )
    )


def test_reconciliation_required_blocks_submission() -> None:
    # After any recovery the broker/reconciliation flag forces a fail-closed
    # outcome even when data is fresh: nothing may submit until reconciled.
    _assert_no_authorization(
        _run_presubmit(
            market=RefreshedMarket(price=Decimal("10"), observed_at=RECENT),
            account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=RECENT),
            context=ExecutionContext(reconciliation_required=True),
        )
    )


def test_broker_unhealthy_blocks_submission() -> None:
    _assert_no_authorization(
        _run_presubmit(
            market=RefreshedMarket(price=Decimal("10"), observed_at=RECENT),
            account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=RECENT),
            context=ExecutionContext(broker_healthy=False),
        )
    )


def test_healthy_fresh_state_still_authorizes() -> None:
    # Control: the gate is not simply always-refusing. With fresh data, a
    # healthy broker and no reconciliation flag, a valid plan is authorized.
    result = _run_presubmit(
        market=RefreshedMarket(price=Decimal("10"), observed_at=RECENT),
        account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=RECENT),
    )
    assert result.action in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE)
    assert result.authorization is not None


# ---------------------------------------------------------------------------
# UI outage — the kill-switch / halt gate is deterministic without UI or LLM.
# ---------------------------------------------------------------------------
def _owner() -> Principal:
    return Principal(principal_id="op-1", roles=frozenset({Role.OWNER}), mfa_verified=True)


def _halt_service() -> EmergencyControlService:
    return EmergencyControlService(InMemoryEmergencyCommandStore(), InMemoryAuditSink())


def test_halt_gate_blocks_entries_without_any_ui_or_llm() -> None:
    service = _halt_service()
    service.activate_halt(
        _owner(),
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=Environment.PAPER,
        reason_code="SEV1",
        reason_text="ui/dashboard unavailable — operator halted from CLI",
    )
    context = EntryContext(environment=Environment.PAPER, account_ref="A1", symbol="AAPL")
    for checkpoint in EntryCheckpoint:
        decision = service.evaluate_entry(context, checkpoint)
        assert decision.blocked
        assert decision.blocking_scope is HaltScope.GLOBAL_ENTRY_HALT


def test_protective_exit_is_never_blocked_by_a_halt() -> None:
    service = _halt_service()
    service.activate_halt(
        _owner(),
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=Environment.PAPER,
        reason_code="SEV1",
        reason_text="ui outage",
    )
    context = EntryContext(environment=Environment.PAPER, account_ref="A1", symbol="AAPL")
    assert service.protective_exit_allowed(context) is True


def test_execution_agent_halt_gate_needs_no_ui_and_never_submits() -> None:
    # The agent consults the halt records directly at queue consumption; a UI
    # outage cannot re-enable a halted entry, and nothing reaches the broker.
    drill = ExecutionDrill()
    halt = HaltRecord(
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=Environment.PAPER,
        target_ref=None,
        reason_code="SEV1",
        keep_protection=True,
    )
    outcome = drill.agent.process(drill.request("ord-halt"), active_halts=(halt,))
    assert outcome.status is ExecutionStatus.CANCELLED_BY_POLICY
    assert outcome.receipt is None
    assert drill.broker.get_open_orders("paper-main") == ()


# -- cross-cutting: no submission during ANY unhealthy state ----------------


@pytest.mark.parametrize(
    "make_context",
    [
        pytest.param(lambda: ExecutionContext(broker_healthy=False), id="broker-down"),
        pytest.param(
            lambda: ExecutionContext(reconciliation_required=True), id="reconcile-required"
        ),
        pytest.param(
            lambda: ExecutionContext(
                active_halts=(
                    HaltRecord(
                        scope=HaltScope.GLOBAL_ENTRY_HALT,
                        environment=Environment.PAPER,
                        target_ref=None,
                        reason_code="SEV1",
                        keep_protection=True,
                    ),
                )
            ),
            id="halt-active",
        ),
    ],
)
def test_no_authorization_is_minted_in_any_unhealthy_state(
    make_context: object,
) -> None:
    result = _run_presubmit(
        market=RefreshedMarket(price=Decimal("10"), observed_at=RECENT),
        account=RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=RECENT),
        context=make_context(),  # type: ignore[operator]
    )
    _assert_no_authorization(result)


def test_no_ibkr_sdk_or_network_import_in_this_module() -> None:
    from pathlib import Path

    source = Path(__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            for forbidden in ("ib" + "api", "trading_broker_ibkr", "httpx", "requests", "socket"):
                assert forbidden not in stripped, f"forbidden import: {stripped}"


# -- callback loss / delayed / disconnect-after-accept (IMP-1303 gaps) ------


def test_lost_fill_callback_surfaces_as_missing_fill_event() -> None:
    """A broker fill event that the internal inbox never processed (a lost or
    dropped callback) must surface at reconciliation as MISSING_FILL_EVENT —
    it is detected, never silently ignored."""
    drill = ExecutionDrill()
    drill.agent.process(drill.request("ord-lost"))  # broker emits an ACK event
    # Reconcile with an EMPTY processed set: the internal side never saw the
    # broker's event (the callback was lost).
    report = drill.reconcile(
        open_orders=(
            InternalOrderExpectation(
                client_order_id="ord-lost", expected_state=OrderState.ACKNOWLEDGED
            ),
        ),
        processed_event_ids=frozenset(),
    )
    kinds = {f.kind for f in report.findings}  # type: ignore[attr-defined]
    assert FindingKind.MISSING_FILL_EVENT in kinds
    assert report.reconciled is False  # type: ignore[attr-defined]


def test_duplicate_callback_is_detected_not_double_counted() -> None:
    """A redelivered broker event (duplicate callback) is a no-op for order
    state — the broker's idempotency means no second order and the event
    stream still resolves to one logical order."""
    drill = ExecutionDrill()
    request = drill.request("ord-dupcb")
    drill.agent.process(request)
    drill.agent.process(request)  # duplicate delivery
    acks = [
        e
        for e in drill.broker.order_events()
        if e.client_order_id == "ord-dupcb" and e.state is OrderState.ACKNOWLEDGED
    ]
    assert len(acks) == 1
    assert len(drill.broker.get_open_orders("paper-main")) == 1


def test_disconnect_after_accepted_order_blocks_until_reconciled() -> None:
    """An order is accepted, then the broker disconnects. A new submit while
    disconnected fails closed (no blind retry), and recovery is a
    reconciliation over the still-open accepted order — never a resend."""
    drill = ExecutionDrill()
    first = drill.agent.process(drill.request("ord-acc"))
    assert first.status is ExecutionStatus.SUBMITTED

    drill.broker.disconnect()  # disconnect AFTER the order was accepted
    blocked = drill.agent.process(drill.request("ord-after"))
    assert blocked.status in (ExecutionStatus.FAILED, ExecutionStatus.NEEDS_REVIEW)
    assert blocked.receipt is None

    drill.broker.connect()  # reconnect; recover via reconciliation
    report = drill.reconcile(
        open_orders=(
            InternalOrderExpectation(
                client_order_id="ord-acc", expected_state=OrderState.ACKNOWLEDGED
            ),
        ),
        trigger=ReconciliationTrigger.STARTUP,
    )
    assert report.reconciled is True  # type: ignore[attr-defined]
    assert len(drill.broker.get_open_orders("paper-main")) == 1  # nothing re-sent
