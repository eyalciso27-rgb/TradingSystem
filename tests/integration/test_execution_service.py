"""One decision, through the canonical path, against the real components.

The pieces were all tested individually and none of it was joined up, so this
drives an actual decision from business intent to the submission boundary using
the real ``IbkrPaperAdapter``, the real ``RiskEngine``, the real
``ReadinessGuardedBroker`` and the real ``ExecutionAgent`` — with the seam at
the transport, one layer below the port.

The property most of these tests assert is the same one: **the broker's
transport never saw a submit**. Asserting on a returned status would pass just
as happily if the order had gone out and the refusal came afterwards.

Authorization is DENIED by production default. Exactly two tests enable it,
through an explicit test-only seam, to prove the submit path works at all and
reaches the broker exactly once.

Offline and deterministic: no ibapi, no socket, no network, no real order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.emergency import HaltRecord
from trading_application.execution_identity import (
    allocation_identity,
    authorization_identity,
    decision_identity,
    plan_identity,
    pre_submit_policy_identity,
    risk_request_identity,
    risk_result_identity,
)
from trading_application.execution_plan import ExecutablePlan
from trading_application.portfolio_allocator import (
    AllocationCandidate,
    CandidateAllocation,
    PortfolioAllocationPolicy,
    PortfolioAllocator,
    PortfolioPosition,
    PortfolioState,
)
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
    authorization_matches,
    single_use_authorization,
)
from trading_application.readiness_guarded_broker import QuoteSource
from trading_application.risk_engine import (
    AccountState,
    PositionExposure,
    RiskEngine,
    RiskPolicy,
    RiskResult,
    TradeRiskRequest,
)
from trading_application.trading_readiness_runtime import (
    MarketDataRules,
    MarketDataState,
    QuoteState,
)
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    BrokerInstrument,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
    ProtectiveBracket,
    SessionPnlStatus,
)
from trading_broker_ibkr.paper_adapter import IbkrPaperAdapter, decode_instrument_ref
from trading_contracts.decision import DecisionResult
from trading_contracts.enums import (
    ActionTier,
    DecisionOutcome,
    Environment,
    HaltScope,
    OrderType,
    PortfolioAction,
)
from trading_execution_agent.agent import (
    ExecutionAgent,
    ExecutionRequest,
    ExecutionStatus,
)
from trading_execution_agent.execution_service import (
    AuthorizationDecision,
    DecisionRiskRegistry,
    DenyAllExecutionAuthorization,
    ExecutionConfig,
    ExecutionEvaluation,
    ExecutionFingerprint,
    ExecutionIntent,
    ExecutionService,
    ExecutionServiceError,
    ExecutionStage,
    PlanContext,
)
from trading_execution_agent.reconciliation_latch import ALWAYS_PERMIT
from trading_market_data_core.sp500_current_universe import Constituent, ConstituentSnapshot
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
    UniverseListStatus,
    UniverseType,
)
from trading_persistence.execution_audit import (
    REQUIRED_IDENTITY_FIELDS,
    REQUIRED_PREPARED_FIELDS,
    ExecutionHistoryRecord,
    ExecutionHistoryUnreadableError,
    _require_readable,
    require_consistent_lifecycle,
)

from tests.broker.test_ibkr_paper_adapter import (
    ALIASES,
    PAPER_ACCOUNT,
    FakePaperTransport,
)
from tests.integration.test_production_quote_binding import RecordingMarketData

ADAPTER_NOW = datetime(2026, 8, 19, 15, 30, tzinfo=UTC)
NOW = ADAPTER_NOW + timedelta(seconds=2)

POLICY = RiskPolicy(
    policy_version="risk-policy-execution-test-1",
    risk_per_trade_pct=Decimal("0.0025"),
    max_total_open_risk_pct=Decimal("0.0100"),
    daily_soft_halt_pct=Decimal("0.0075"),
    daily_hard_halt_pct=Decimal("0.0100"),
    max_concurrent_positions=4,
    max_symbol_notional_pct=Decimal("0.10"),
    max_single_order_notional_pct=Decimal("0.075"),
    max_sector_notional_pct=Decimal("0.25"),
    max_correlated_cluster_pct=Decimal("0.30"),
    max_source_open_risk_share=Decimal("0.50"),
)


DEFAULT_DECISION_ID = UUID("00000000-0000-0000-0000-0000000000d1")


def _decision(
    outcome: DecisionOutcome = DecisionOutcome.APPROVE,
    decision_id: UUID = DEFAULT_DECISION_ID,
) -> EngineDecision:
    """A canonical strategy decision. Business intent, never a safety verdict."""
    return EngineDecision(
        result=DecisionResult(
            decision_id=decision_id,
            candidate_id=UUID("00000000-0000-0000-0000-0000000000c1"),
            outcome=outcome,
            action_tier=(
                ActionTier.AUTO_FULL
                if outcome is DecisionOutcome.APPROVE
                else ActionTier.WATCH
            ),
            strategy_version="1.0.0",
            decision_policy_version="1.0.0",
            decided_at=ADAPTER_NOW - timedelta(minutes=1),
            decision_hash="sha256:" + "a" * 64,
        ),
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


class _Quotes:
    """A quote source. Deliberately no richer than the real one exposes."""

    def __init__(self, quote: QuoteState | None = None) -> None:
        self.quote = quote or QuoteState(
            bid=Decimal("100.00"),
            ask=Decimal("100.05"),
            observed_at=NOW - timedelta(seconds=1),
            bid_size=500,
            ask_size=500,
            market_data_state=MarketDataState.REALTIME,
        )

    def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
        return self.quote


class _Audit:
    """Records what was persisted, in order, so ordering can be asserted."""

    def __init__(self, *, available: bool = True, raises: bool = False) -> None:
        self.records: list[Mapping[str, object]] = []
        self.prepared: list[Mapping[str, object]] = []
        self.outcomes: list[Mapping[str, object]] = []
        self._available = available
        self._raises = raises

    def can_persist(self) -> bool:
        return self._available

    def persist(self, record: Mapping[str, object]) -> str:
        if self._raises:
            raise RuntimeError("audit store unavailable")
        self.records.append(record)
        return f"audit-{len(self.records)}"

    def persist_prepared(self, record: Mapping[str, object]) -> str:
        self.prepared.append(record)
        return f"prepared-{len(self.prepared)}"

    def persist_outcome(self, record: Mapping[str, object]) -> str:
        self.outcomes.append(record)
        return f"outcome-{len(self.outcomes)}"


class _TestOnlyAuthorization:
    """Explicit test seam. There is no production equivalent, on purpose."""

    def __init__(
        self,
        *,
        allowed: bool = True,
        mint: bool = True,
        oversize: Decimal | None = None,
        checks_instead: Callable[[PlanContext], PlanContext] | None = None,
        portfolio: PortfolioState | None = None,
    ) -> None:
        self._allowed = allowed
        self._mint = mint
        #: Simulates a faulty authority presenting more than risk approved.
        self._oversize = oversize
        #: Simulates an authority that checks an order OTHER than the one the
        #: service will send -- the substitution the seal exists to refuse.
        self._context_override = checks_instead
        #: A portfolio state that constrains the allocator, so the portfolio
        #: cap can be made materially tighter than the risk ceiling.
        self._portfolio_state = portfolio
        self.calls = 0
        self.last_context: PlanContext | None = None

    def pre_submit_policy_identity(self) -> str:
        """The EXACT policy this authority mints under, as the service checks."""
        return pre_submit_policy_identity(PRE_SUBMIT_POLICY)

    def authorize(
        self,
        intent: object,
        report: object,
        risk_result: RiskResult,
        plan: PlanContext,
    ) -> AuthorizationDecision:
        self.calls += 1
        self.last_context = plan
        if not self._allowed:
            return AuthorizationDecision(allowed=False, reason="test denial")
        pre_submit: PreSubmitCheckResult | None = None
        authorization: SingleUseAuthorization | None = None
        allocation: CandidateAllocation | None = None
        if self._mint:
            # THE runtime facts, not a fixture price. The authority is handed
            # the order the service intends to send and checks THAT order --
            # which is the whole point: an authorization minted against an
            # entry of 100.02 while the service plans from a live quote is an
            # authorization for a different order.
            # A faulty authority checks a DIFFERENT order than the one it was
            # handed. Derived from the real context so only the substituted
            # fact differs -- the lineage stays genuine, which is what makes
            # the refusal a statement about the ORDER.
            context = (
                self._context_override(plan)
                if self._context_override is not None
                else plan
            )
            allocation = _real_allocation(
                risk_result,
                entry=context.entry_price,
                stop=context.stop_price or context.entry_price - Decimal("2"),
                portfolio=self._portfolio_state,
            )
            pre_submit = _real_authorization(
                risk_result, allocation=allocation, context=context
            )
            if self._oversize is not None:
                pre_submit = _oversized(pre_submit, self._oversize)
            authorization = single_use_authorization(pre_submit)
        return AuthorizationDecision(
            allowed=True,
            reason="test-only authorization",
            pre_submit=pre_submit,
            authorization=authorization,
            allocation=allocation,
        )


PORTFOLIO_POLICY = PortfolioAllocationPolicy(
    policy_version="portfolio-policy-execution-test-1",
    max_total_open_risk_pct=Decimal("0.0100"),
    max_symbol_notional_pct=Decimal("0.10"),
    max_sector_notional_pct=Decimal("0.25"),
    max_theme_notional_pct=Decimal("0.20"),
    max_strategy_risk_share=Decimal("0.50"),
    max_source_risk_share=Decimal("0.50"),
    max_cluster_risk_share=Decimal("0.40"),
    max_concurrent_positions=4,
    insufficient_capacity_action=PortfolioAction.REJECT,
)


def _real_allocation(
    risk_result: RiskResult,
    *,
    entry: Decimal = Decimal("100.02"),
    stop: Decimal = Decimal("98.02"),
    portfolio: PortfolioState | None = None,
) -> CandidateAllocation:
    """The allocation the REAL PortfolioAllocator produces for this risk result.

    Nothing here invents a reference. The allocator emits the canonical
    decision, risk-result and portfolio-policy identities itself, which is what
    lets FinalPreSubmitCheck verify the allocation belongs to this decision and
    this exact evaluation.
    """
    plan = PortfolioAllocator(PORTFOLIO_POLICY).allocate(
        [
            AllocationCandidate(
                candidate_id="cand-1",
                risk_result=risk_result,
                symbol="AAPL",
                sector="tech",
                theme="megacap",
                cluster="megacap",
                source_id="strategy",
                strategy_id="baseline-trend-breakout-v1",
                entry_price=entry,
                per_share_risk=entry - stop,
                priority_score=Decimal("1"),
            )
        ],
        portfolio
        or PortfolioState(
            risk_budget_equity=Decimal("50000"),
            open_positions=(),
            observed_at=NOW,
        ),
        as_of=NOW,
    )
    return plan.allocations[0]


def _oversized(pre_submit: PreSubmitCheckResult, quantity: Decimal) -> PreSubmitCheckResult:
    """A REAL authorization, then corrupted the way a faulty authority would.

    The real chain will not simply mint a bigger authorization -- the allocator
    sizes it from risk -- so an oversize is modelled by mutating a genuine
    result, which is exactly the buggy-authority case the binding must catch.
    """

    assert pre_submit.authorization is not None
    return replace(
        pre_submit,
        authorized_quantity=quantity,
        authorization=replace(pre_submit.authorization, authorized_quantity=quantity),
    )


PRE_SUBMIT_POLICY = PreSubmitPolicy(
    policy_version="pre-submit-policy-execution-test-1",
    max_price_slippage_pct=Decimal("0.01"),
    max_risk_increase_pct=Decimal("0.10"),
    market_max_staleness_seconds=30,
    account_max_staleness_seconds=60,
    decision_max_age_seconds=3600,
    approval_max_age_seconds=3600,
    require_manual_approval=False,
    price_change_action=ChangeAction.RESIZE,
    risk_change_action=ChangeAction.RESIZE,
    stale_data_action=StaleAction.NEEDS_REVIEW,
)


def _pre_submit_request(
    allocation: CandidateAllocation,
    risk_result: RiskResult,
    context: PlanContext,
) -> PreSubmitRequest:
    """A pre-submit request built from the REAL runtime order facts."""
    return PreSubmitRequest(
        allocation=allocation,
        risk_result=risk_result,
        decision=_decision(),
        risk_request=context.risk_request,
        symbol=context.symbol,
        strategy_id=context.strategy_id,
        source_id=context.source_id,
        environment=context.environment,
        planned_entry_price=context.entry_price,
        planned_stop_price=context.stop_price or context.entry_price,
        side=context.side,
        account_alias=context.account_alias,
        order_type=context.order_type,
        time_in_force=context.time_in_force,
        client_order_id=context.client_order_id,
        broker_instrument_ref=context.instrument.broker_instrument_ref,
        broker_exchange=context.instrument.exchange,
        broker_currency=context.instrument.currency,
    )


def _authorization_for(
    evaluation: ExecutionEvaluation, **kw: object
) -> PreSubmitCheckResult:
    """The authorization for THE order this evaluation would send.

    Every caller goes through the evaluation's own plan context, so no test can
    mint an authorization against a price, side or account the service is not
    actually using -- which is exactly what the previous fixture did, with an
    entry of 100.02 against a service that plans from a midpoint of 100.025.
    """
    assert evaluation.risk_result is not None
    assert evaluation.plan_context is not None
    return _real_authorization(
        evaluation.risk_result, context=evaluation.plan_context, **kw  # type: ignore[arg-type]
    )


def _allocation_for(evaluation: ExecutionEvaluation) -> CandidateAllocation:
    """The allocation for THE order this evaluation would send."""
    assert evaluation.risk_result is not None
    assert evaluation.plan_context is not None
    context = evaluation.plan_context
    return _real_allocation(
        evaluation.risk_result,
        entry=context.entry_price,
        stop=context.stop_price or context.entry_price,
    )


def _real_authorization(
    risk_result: RiskResult,
    *,
    context: PlanContext,
    decision: EngineDecision | None = None,
    allocation: CandidateAllocation | None = None,
) -> PreSubmitCheckResult:
    """Mint an authorization through the REAL FinalPreSubmitCheck.

    Nothing here hand-builds a ``PreSubmitCheckResult``. An earlier version did,
    with references deliberately chosen to match the risk result — which meant
    the binding check was being tested against a shape only the test produced,
    and a genuine authorization from the canonical producer would have been
    rejected.
    """
    engine_decision = decision or _decision()
    # The REAL runtime facts when the service supplies them. A fixture price
    # here was the actual defect: the check approved an order at 100.02 while
    # the service planned from its live quote, and the order still went out.
    facts = context
    entry = facts.entry_price
    stop = facts.stop_price if facts.stop_price is not None else entry
    # The REAL allocator produces the allocation. Hand-building one meant the
    # merge gate skipped a producer entirely -- and the real allocator was
    # emitting a risk reference the consumer could not verify.
    allocation = (
        allocation
        if allocation is not None
        else _real_allocation(risk_result, entry=entry, stop=stop)
    )
    return FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
        PreSubmitRequest(
            allocation=allocation,
            risk_result=risk_result,
            decision=engine_decision,
            risk_request=facts.risk_request,
            symbol=facts.symbol,
            strategy_id=facts.strategy_id,
            source_id=facts.source_id,
            environment=facts.environment,
            planned_entry_price=entry,
            planned_stop_price=stop,
            side=facts.side,
            account_alias=facts.account_alias,
            order_type=facts.order_type,
            time_in_force=facts.time_in_force,
            client_order_id=facts.client_order_id,
            broker_instrument_ref=facts.instrument.broker_instrument_ref,
            broker_exchange=facts.instrument.exchange,
            broker_currency=facts.instrument.currency,
        ),
        RefreshedMarket(price=entry, observed_at=NOW),
        RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=NOW),
        ApprovalState(approved=True, approved_at=NOW),
        ExecutionContext(),
        as_of=NOW,
    )


def PnlAwareTransport(  # noqa: N802 - a factory named like the type it makes
    *, daily_pnl: Decimal | None = Decimal("0"), **kw: object
) -> FakePaperTransport:
    """The canonical fake transport with a daily P&L configured.

    Not a subclass and not a richer shape: ``session_pnl`` is a real
    ``PaperTransport`` operation that the production SDK transport implements
    through ``reqPnL``, so the fake answers the same operation. Its default is
    ``None`` -- the honest common case, where the gateway serves no daily figure.
    """
    return FakePaperTransport(daily_pnl=daily_pnl, **kw)  # type: ignore[arg-type]


def _classified_portfolio() -> tuple[PositionExposure, ...]:
    """A real portfolio authority: positions classified as THEMSELVES.

    Deliberately a different sector, cluster and source from the candidate, so a
    regression that relabels existing exposure with the new decision's metadata
    shows up as a changed risk verdict rather than passing silently.
    """
    return (
        PositionExposure(
            symbol="MSFT", sector="software", cluster="megacap-software",
            source_id="other-source", strategy_id="other-strategy",
            open_risk_amount=Decimal("50"), notional=Decimal("4000"),
        ),
    )


def _production_quote_source() -> tuple[QuoteSource, RecordingMarketData]:
    """The REAL production quote chain, with only the SDK stubbed.

    Imported from the dedicated binding test so there is exactly one such
    harness: a second copy could drift from the first, and the whole point is
    that the merge gate exercises the production implementation.
    """
    from tests.integration.test_production_quote_binding import _production_chain

    return _production_chain()


def _adapter(transport: FakePaperTransport | None = None) -> IbkrPaperAdapter:
    fake = transport or PnlAwareTransport()
    adapter = IbkrPaperAdapter(
        lambda: fake,
        account_aliases=dict(ALIASES),
        host="127.0.0.1",
        port=4002,
        clock=lambda: ADAPTER_NOW,
    )
    adapter.connect()
    return adapter


def _universe(tickers: Sequence[str] = ("AAPL",)) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_id="SP500",
        as_of=ADAPTER_NOW.date(),
        fetched_at=ADAPTER_NOW,
        source_id="wikipedia:List_of_S&P_500_companies",
        source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(ticker=ticker, name=f"{ticker} Inc", security_id=f"sec-cik:{i:010d}")
            for i, ticker in enumerate(tickers, start=1)
        ),
    )


def _service(
    *,
    transport: FakePaperTransport | None = None,
    portfolio: object | None = _classified_portfolio,
    entry_latch: object | None = None,
    protection: object | None = None,
    protection_required: bool = False,
    # Never None. Durable history is mandatory for an execution-capable
    # service, so a test that omits it gets a real in-memory one rather than a
    # service with no restart-safe idempotency.
    history: object | None = None,
    # Deliberately `object`: the service takes protocols, and a test fake that
    # implements the protocol is a valid quote source or audit sink whether or
    # not it inherits from this file's default fixture.
    quotes: object | None = None,
    audit: object | None = None,
    authorization: object | None = None,
    requirements: StrategyDataRequirements | None = None,
    rules: MarketDataRules | None = None,
    halts: Sequence[HaltRecord] = (),
    universe: ConstituentSnapshot | None = None,
    universe_status: UniverseListStatus | None = None,
    adapter: IbkrPaperAdapter | None = None,
) -> tuple[ExecutionService, FakePaperTransport, _Audit]:
    fake = transport or PnlAwareTransport()
    broker = adapter or _adapter(fake)
    sink = audit or _Audit()
    # ONE gate for this service AND its agent.
    gate = entry_latch or _PermissiveLatch()
    service = ExecutionService(
        broker=broker,
        quotes=quotes or _Quotes(),  # type: ignore[arg-type]
        audit=sink,  # type: ignore[arg-type]
        risk_engine=RiskEngine(POLICY),
        config=ExecutionConfig(
            mode=TradingMode.PAPER,
            environment=Environment.PAPER,
            account_alias="paper-main",
            requirements=requirements
            or StrategyDataRequirements(strategy_id="baseline-trend-breakout-v1"),
            rules=rules
            or MarketDataRules(max_spread_fraction=Decimal("0.01"), min_price=Decimal("1")),
            risk_policy=POLICY,
            simulation_equity_cap=Decimal("50000"),
            order_type=OrderType.LIMIT,
            sector="tech",
            cluster="megacap",
            # Most tests here are about the paths BEFORE protection. The real
            # paper adapter cannot establish broker-side protection atomically
            # (bracket and OCO are UNSUPPORTED), so a protected entry is
            # correctly impossible through it -- proven by its own test below.
            protection_required=protection_required,
        ),
        # The service builds its own agent from this gate. There is no
        # parameter through which a different one could be installed -- an
        # agent_factory used to exist, and `lambda port, gate: ExecutionAgent(
        # port, entry_gate=ALWAYS_PERMIT)` silently defeated the whole latch.
        entry_gate=gate,  # type: ignore[arg-type]
        authorization=authorization,  # type: ignore[arg-type]
        universe=(lambda: universe) if universe is not None else None,
        portfolio=portfolio,  # type: ignore[arg-type]
        history=history if history is not None else _History(),  # type: ignore[arg-type]
        protection=protection,  # type: ignore[arg-type]
        universe_status=(lambda: universe_status) if universe_status else None,
        halts=lambda: halts,
        now=lambda: NOW,
    )
    return service, fake, sink  # type: ignore[return-value]


class _PermissiveLatch:
    """The canonical gate shape, permitting entry until something latches it.

    Supplied automatically to any execution-capable test service, because
    construction now REFUSES an authority that can say ALLOW without one — the
    service promises to latch new entries on a post-submit audit failure and
    cannot keep that promise with no latch.
    """

    def __init__(self) -> None:
        self.required: list[str] = []

    def require(self, reason: str) -> None:
        self.required.append(reason)

    def entry_blocked(self) -> tuple[bool, str]:
        return (bool(self.required), self.required[0] if self.required else "")


def _intent(**kw: object) -> ExecutionIntent:
    fields: dict[str, object] = {
        "strategy_id": "baseline-trend-breakout-v1",
        "decision": _decision(),
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "requested_quantity": Decimal("1"),
        "correlation_id": "corr-1",
        "client_order_id": "exec-ord-1",
    }
    fields.update(kw)
    return ExecutionIntent(**fields)  # type: ignore[arg-type]


def _submits(transport: FakePaperTransport) -> list[str]:
    return [call for call in transport.calls if call.startswith("submit:")]


# == the happy path stops at authorization ====================================


def test_a_fully_healthy_decision_is_ready_and_still_not_authorized() -> None:
    """Everything green, and it STILL does not submit. That is the design."""
    service, transport, audit = _service()

    evaluation = service.evaluate(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert evaluation.readiness is not None
    assert evaluation.readiness.readiness.value == "PAPER_READY"
    assert _submits(transport) == [], "evaluation must never reach the broker"
    assert audit.records, "the decision was recorded"


def test_the_production_authorization_is_deny() -> None:
    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    assert evaluated.plan_context is not None
    decision = DenyAllExecutionAuthorization().authorize(
        _intent(),
        evaluated.readiness,  # type: ignore[arg-type]
        evaluated.risk_result,
        evaluated.plan_context,
    )
    assert decision.allowed is False
    assert decision.authorization is None
    assert "not enabled" in decision.reason


def test_execute_without_authorization_never_reaches_the_broker() -> None:
    service, transport, _ = _service()

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert evaluation.submitted is False
    assert _submits(transport) == []


def test_the_probe_path_uses_the_what_if_and_places_nothing() -> None:
    """Tradability is proven by preview_order, which submits nothing."""
    service, transport, _ = _service()

    service.evaluate(_intent())

    assert _submits(transport) == []


# == the observations are made, not supplied ==================================


def test_the_intent_cannot_carry_a_safety_verdict() -> None:
    """Structural: there is no field through which a caller could assert one."""
    from dataclasses import fields as dataclass_fields

    names = {f.name for f in dataclass_fields(ExecutionIntent)}
    forbidden = {
        "readiness", "risk_result", "quote_fresh", "broker_healthy", "tradable",
        "account_safe", "audit_ok", "universe_ok", "protection_ok", "authorized",
    }
    assert names.isdisjoint(forbidden)


def test_the_real_risk_engine_produced_the_result() -> None:
    service, _, _ = _service()
    evaluation = service.evaluate(_intent())

    assert evaluation.risk_result is not None
    assert evaluation.risk_result.engine_version
    assert evaluation.risk_result.policy_ref
    assert evaluation.approved_quantity is not None
    assert evaluation.approved_quantity > 0


def test_the_contract_came_from_the_broker_and_stays_opaque() -> None:
    service, _, audit = _service()
    evaluation = service.evaluate(_intent())

    assert evaluation.instrument is not None
    assert evaluation.instrument.broker_instrument_ref == "conid:265598"
    assert audit.records[0]["security_id"] == "conid:265598"
    # The application never decodes it into a broker-specific id.
    assert "con_id" not in audit.records[0]


# == every failure must prove the broker was not called =======================


@pytest.mark.parametrize(
    ("label", "kwargs", "expected"),
    [
        (
            "halt active",
            {
                "halts": (
                    HaltRecord(
                        scope=HaltScope.GLOBAL_ENTRY_HALT,
                        environment=Environment.PAPER,
                        target_ref=None,
                        reason_code="OWNER_DRILL",
                    ),
                )
            },
            ExecutionStage.HALTED,
        ),
        (
            "no quote",
            {"quotes": _Quotes(QuoteState(None, None, None))},
            ExecutionStage.NO_QUOTE,
        ),
        (
            "stale quote",
            {
                "quotes": _Quotes(
                    QuoteState(
                        Decimal("100"), Decimal("100.05"), NOW - timedelta(minutes=30),
                        bid_size=500, ask_size=500,
                        market_data_state=MarketDataState.REALTIME,
                    )
                )
            },
            ExecutionStage.READINESS_BLOCKED,
        ),
        (
            "delayed data under a realtime-only rule",
            {
                "quotes": _Quotes(
                    QuoteState(
                        Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1),
                        bid_size=500, ask_size=500,
                        market_data_state=MarketDataState.DELAYED,
                    )
                ),
                "rules": MarketDataRules(
                    allowed_market_data_states=(MarketDataState.REALTIME,)
                ),
            },
            ExecutionStage.READINESS_BLOCKED,
        ),
        (
            "unknown feed state",
            {
                "quotes": _Quotes(
                    QuoteState(
                        Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1),
                        bid_size=500, ask_size=500,
                    )
                )
            },
            ExecutionStage.READINESS_BLOCKED,
        ),
        (
            "crossed quote",
            {
                "quotes": _Quotes(
                    QuoteState(
                        Decimal("100.10"), Decimal("100.00"), NOW - timedelta(seconds=1),
                        bid_size=500, ask_size=500,
                        market_data_state=MarketDataState.REALTIME,
                    )
                )
            },
            ExecutionStage.READINESS_BLOCKED,
        ),
        (
            "unknown bid size under a configured minimum",
            {
                "quotes": _Quotes(
                    QuoteState(
                        Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1),
                        market_data_state=MarketDataState.REALTIME,
                    )
                ),
                "rules": MarketDataRules(min_bid_size=100),
            },
            ExecutionStage.READINESS_BLOCKED,
        ),
        (
            "index strategy with no valid universe",
            {
                "requirements": StrategyDataRequirements(
                    strategy_id="s", universe=UniverseType.SP500_CURRENT
                ),
                "universe_status": UniverseListStatus.CURRENT_LIST_UNAVAILABLE,
            },
            ExecutionStage.UNIVERSE_REFUSED,
        ),
        (
            "risk vetoes the decision",
            {},
            ExecutionStage.RISK_REFUSED,
        ),
        ("audit unavailable", {"audit": _Audit(available=False)}, ExecutionStage.AUDIT_UNAVAILABLE),
        ("audit raises", {"audit": _Audit(raises=True)}, ExecutionStage.AUDIT_UNAVAILABLE),
    ],
)
def test_a_failure_refuses_and_the_broker_is_never_called(
    label: str, kwargs: dict[str, object], expected: ExecutionStage
) -> None:
    intent_overrides: dict[str, object] = {}
    if label == "risk vetoes the decision":
        intent_overrides["decision"] = _decision(DecisionOutcome.REJECT)

    service, transport, _ = _service(**kwargs)  # type: ignore[arg-type]
    evaluation = service.execute(_intent(**intent_overrides))

    assert evaluation.stage is expected, f"{label}: {evaluation.reasons}"
    assert _submits(transport) == [], f"{label}: the broker must never be called"


def test_an_unresolvable_instrument_refuses() -> None:
    service, transport, _ = _service()
    evaluation = service.evaluate(_intent(symbol="NOSUCH", client_order_id="exec-ord-2"))

    assert evaluation.stage is ExecutionStage.CONTRACT_UNRESOLVED
    assert _submits(transport) == []


def test_a_symbol_outside_the_current_index_refuses() -> None:
    service, transport, _ = _service(
        requirements=StrategyDataRequirements(
            strategy_id="s", universe=UniverseType.SP500_CURRENT
        ),
        universe_status=UniverseListStatus.CURRENT_LIST_VALID,
        universe=_universe(("MSFT",)),
    )
    evaluation = service.evaluate(_intent())

    assert evaluation.stage is ExecutionStage.UNIVERSE_REFUSED
    assert "not a current S&P 500 constituent" in evaluation.reasons[0]
    assert _submits(transport) == []


def test_an_index_strategy_with_a_valid_list_proceeds() -> None:
    service, _, _ = _service(
        requirements=StrategyDataRequirements(
            strategy_id="s", universe=UniverseType.SP500_CURRENT
        ),
        universe_status=UniverseListStatus.CURRENT_LIST_VALID,
        universe=_universe(("AAPL",)),
    )
    evaluation = service.evaluate(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED


def test_an_all_equities_strategy_ignores_the_index_entirely() -> None:
    """ADR-0017: membership is irrelevant to a strategy that does not use it."""
    service, _, _ = _service(
        requirements=StrategyDataRequirements(
            strategy_id="s", universe=UniverseType.ALL_ELIGIBLE_US_EQUITIES
        ),
        universe_status=UniverseListStatus.CURRENT_LIST_UNAVAILABLE,
    )
    evaluation = service.evaluate(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED


def test_a_missing_protective_stop_refuses() -> None:
    service, transport, _ = _service()
    # A price low enough that the configured stop fraction rounds to no offset
    # leaves the plan unprotected, which must refuse rather than submit naked.
    quote = QuoteState(
        Decimal("0.02"), Decimal("0.02"), NOW - timedelta(seconds=1),
        bid_size=500, ask_size=500, market_data_state=MarketDataState.REALTIME,
    )
    service, transport, _ = _service(quotes=_Quotes(quote))
    evaluation = service.evaluate(_intent())

    assert evaluation.stage in (
        ExecutionStage.PLAN_REFUSED, ExecutionStage.READINESS_BLOCKED,
        ExecutionStage.RISK_REFUSED,
    )
    assert _submits(transport) == []


# == authorization enabled ONLY through the explicit test seam ================


def test_an_authorized_ready_decision_reaches_the_broker_exactly_once() -> None:
    authorization = _TestOnlyAuthorization()
    service, transport, audit = _service(authorization=authorization)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.outcome is not None
    assert evaluation.outcome.status is ExecutionStatus.SUBMITTED
    assert len(_submits(transport)) == 1, "exactly one order reached the broker"


def test_the_pre_submit_record_is_written_before_the_broker_sees_anything() -> None:
    authorization = _TestOnlyAuthorization()
    service, transport, audit = _service(authorization=authorization)

    order: list[str] = []
    original = audit.persist

    def tracking(record: Mapping[str, object]) -> str:
        order.append("audit")
        return original(record)

    audit.persist = tracking  # type: ignore[method-assign]
    original_submit = FakePaperTransport.submit

    service.execute(_intent())

    assert order and order[0] == "audit"
    assert audit.records, "an order never reaches the broker unrecorded"
    assert len(_submits(transport)) == 1
    del original_submit


def test_the_post_submit_outcome_is_appended_not_substituted() -> None:
    service, _, audit = _service(authorization=_TestOnlyAuthorization())

    service.execute(_intent())

    assert audit.records, "the pre-submit record still exists"
    assert audit.outcomes, "the outcome was recorded separately"
    assert audit.outcomes[0]["checkpoint"] == "POST_SUBMIT"
    assert audit.outcomes[0]["submission_outcome"] == "SUBMITTED"


def test_an_authorization_without_a_minted_authorization_never_submits() -> None:
    """ALLOW is not enough: the single-use authorization must actually exist."""
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(mint=False)
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert _submits(transport) == []


def test_a_repeated_decision_does_not_submit_twice() -> None:
    """Idempotency: the same client_order_id is the broker's own key."""
    authorization = _TestOnlyAuthorization()
    service, transport, _ = _service(authorization=authorization)

    first = service.execute(_intent())
    second = service.execute(_intent())

    assert first.stage is ExecutionStage.SUBMITTED
    assert len(_submits(transport)) == 1, "a retry must not create a second order"
    assert second.outcome is not None


def test_a_post_submit_audit_failure_blocks_the_next_new_entry() -> None:
    """THE test that matters, and the one the previous version faked.

    Asserting ``latch.required`` proves a method was called. It proves nothing
    about the next entry, because the service's latch and the agent's gate used
    to be different objects — the helper literally passed ALWAYS_PERMIT to the
    agent while latching something else. This drives a SECOND, different entry
    through the SAME service and checks the broker.
    """
    audit = _Audit()

    def exploding(record: Mapping[str, object]) -> str:
        raise RuntimeError("audit store died after submission")

    service, transport, _ = _service(
        audit=audit, authorization=_TestOnlyAuthorization()
    )
    audit.persist_outcome = exploding  # type: ignore[method-assign]

    first = service.execute(_intent())

    assert first.stage is ExecutionStage.POST_SUBMIT_AUDIT_FAILED
    assert first.outcome is not None and first.outcome.receipt is not None, (
        "the broker receipt is preserved"
    )
    assert len(_submits(transport)) == 1
    blocked, reason = service.entry_gate.entry_blocked()
    assert blocked, f"the agent's own gate is latched: {reason}"

    # A genuinely DIFFERENT entry, through the same service.
    second = service.execute(
        _intent(
            decision=_decision(
                decision_id=UUID("00000000-0000-0000-0000-0000000000e2")
            ),
            client_order_id="exec-ord-2",
        )
    )

    assert second.stage is ExecutionStage.POST_SUBMIT_AUDIT_FAILED
    assert len(_submits(transport)) == 1, "the second entry never reached the broker"


def test_the_agent_itself_refuses_the_next_entry_after_the_latch() -> None:
    """Belt and braces: the AGENT refuses, not only the service.

    Even if the service's own critical-state check were removed, the gate the
    agent consults is the one that was latched.
    """
    audit = _Audit()
    service, transport, _ = _service(
        audit=audit, authorization=_TestOnlyAuthorization()
    )
    audit.persist_outcome = _raise_after_submit  # type: ignore[method-assign]

    service.execute(_intent())
    assert len(_submits(transport)) == 1

    # Bypass the service entirely and hand the agent a fresh request.
    from trading_execution_agent.agent import ExecutionStatus

    outcome = service._agent.process(  # noqa: SLF001 - the agent's own gate
        _execution_request(_intent(client_order_id="exec-ord-3"))
    )

    assert outcome.status is ExecutionStatus.NEEDS_REVIEW
    assert len(_submits(transport)) == 1, "the agent refused at its own gate"


def test_a_latch_that_itself_fails_still_blocks_new_entries() -> None:
    """A broken latch must not be able to reopen entries.

    Logging the failure is not blocking, so the service keeps its own critical
    state independently of whether the gate accepted the instruction.
    """

    class _BrokenGate:
        def require(self, reason: str) -> None:
            raise RuntimeError("the latch store is unreachable")

        def entry_blocked(self) -> tuple[bool, str]:
            return (False, "")

    audit = _Audit()
    service, transport, _ = _service(
        audit=audit, authorization=_TestOnlyAuthorization(),
        entry_latch=_BrokenGate(),
    )
    audit.persist_outcome = _raise_after_submit  # type: ignore[method-assign]

    service.execute(_intent())
    assert service.critical_state is not None

    second = service.execute(_intent(client_order_id="exec-ord-4"))

    assert second.stage is ExecutionStage.POST_SUBMIT_AUDIT_FAILED
    assert len(_submits(transport)) == 1


def test_cancellation_remains_available_after_the_latch() -> None:
    """Reducing exposure must never be gated by an entry latch."""
    from trading_execution_agent.agent import ExecutionStatus

    audit = _Audit()
    service, transport, _ = _service(
        audit=audit, authorization=_TestOnlyAuthorization()
    )
    audit.persist_outcome = _raise_after_submit  # type: ignore[method-assign]

    service.execute(_intent())
    assert service.entry_gate.entry_blocked()[0]

    cancelled = service._agent.cancel(  # noqa: SLF001
        "exec-ord-1", correlation_id="corr-1"
    )

    assert cancelled.status is ExecutionStatus.CANCEL_SENT, (
        "cancellation is not gated by the entry latch"
    )


def test_the_service_and_its_agent_hold_the_same_gate_instance() -> None:
    """Structural: there is no way to install a second gate."""
    service, _, _ = _service(authorization=_TestOnlyAuthorization())

    assert service._agent._entry_gate is service.entry_gate  # noqa: SLF001


def _raise_after_submit(record: Mapping[str, object]) -> str:
    raise RuntimeError("audit store died after submission")


def _execution_request(intent: ExecutionIntent) -> ExecutionRequest:
    """A request shaped like the service builds, for driving the agent directly.

    The order comes from the SEALED plan the real pre-submit check minted, so
    this helper cannot smuggle in an order the check never approved.
    """
    service, _, _ = _service()
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.authorized_plan is not None
    return ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        plan=pre_submit.authorized_plan,
    )


def test_a_denied_test_authorization_still_never_submits() -> None:
    service, transport, _ = _service(authorization=_TestOnlyAuthorization(allowed=False))
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert _submits(transport) == []


# == the guard remains the last word ==========================================


def test_the_guard_re_checks_at_the_boundary_not_just_at_planning() -> None:
    """A market that moves between planning and submitting is caught late.

    The quote source returns a healthy quote while the service plans, then a
    stale one by the time the guard asks again at the submission boundary. The
    earlier report was satisfied; the order must still not go out.
    """

    class _MovingQuotes:
        def __init__(self) -> None:
            self.calls = 0

        def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
            self.calls += 1
            observed = (
                NOW - timedelta(seconds=1) if self.calls <= 1 else NOW - timedelta(hours=1)
            )
            return QuoteState(
                Decimal("100"), Decimal("100.05"), observed,
                bid_size=500, ask_size=500,
                market_data_state=MarketDataState.REALTIME,
            )

    quotes = _MovingQuotes()
    service, transport, _ = _service(
        quotes=quotes,
        authorization=_TestOnlyAuthorization(),
    )
    evaluation = service.execute(_intent())

    assert quotes.calls >= 2, "the guard must fetch its own quote at the boundary"
    assert evaluation.stage is not ExecutionStage.SUBMITTED
    assert _submits(transport) == []


def test_the_risk_result_is_scoped_to_this_decision() -> None:
    """A result for one decision must not authorise another."""
    from trading_execution_agent.execution_service import DecisionScopedRiskSource

    service, _, _ = _service()
    evaluation = service.evaluate(_intent())
    assert evaluation.risk_result is not None

    source = DecisionScopedRiskSource("exec-ord-1", "AAPL", evaluation.risk_result)
    from trading_broker_core.port import OrderIntent

    def order(client_order_id: str, symbol: str) -> OrderIntent:
        return OrderIntent(
            client_order_id=client_order_id, authorization_ref="a", correlation_id="c",
            environment=Environment.PAPER, account_alias="paper-main", symbol=symbol,
            broker_instrument_ref="conid:265598",
            side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=Decimal(1),
            limit_price=Decimal("100"), stop_price=Decimal("98"),
        )

    assert source.risk_result_for(order("exec-ord-1", "AAPL")) is not None
    assert source.risk_result_for(order("exec-ord-1", "MSFT")) is None
    assert source.risk_result_for(order("other", "AAPL")) is None


def test_cancel_is_not_gated_by_entry_readiness() -> None:
    """Reducing exposure must stay possible when a new entry would be refused."""
    transport = FakePaperTransport()
    adapter = _adapter(transport)
    service, _, _ = _service(adapter=adapter, transport=transport)
    del service

    agent = ExecutionAgent(adapter, entry_gate=ALWAYS_PERMIT)
    outcome = agent.cancel("never-submitted", correlation_id="corr-1")

    # Refused because there is no such order — NOT because readiness blocked.
    assert outcome.status is ExecutionStatus.REFUSED
    assert "CANCEL_UNKNOWN_ORDER" in outcome.reason_codes


# == the defects a deep review found, each with its own proof =================


def test_a_missing_session_pnl_is_never_treated_as_zero() -> None:
    """The daily halt limits are computed from session P&L.

    Substituting zero would present an account that lost money today as flat and
    silently disable both the soft and hard halts. The base transport reports no
    daily P&L, which is the honest default for this adapter.
    """
    service, transport, audit = _service(transport=PnlAwareTransport(daily_pnl=None))

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.ACCOUNT_STATE_INCOMPLETE
    assert "UNKNOWN is not zero" in evaluation.reasons[0]
    assert _submits(transport) == []
    assert audit.records, "the refusal is still recorded"


def test_a_max_double_session_pnl_cannot_pass_the_daily_halt_gate() -> None:
    """The safety blocker: IBKR's DBL_MAX unset sentinel is FINITE, so a naive
    check would hand the risk engine a ~1.8e308 'profit' and pass the halt gate
    on an UNKNOWN account. It must fail closed exactly as a missing value does."""
    import sys

    service, transport, _ = _service(
        transport=PnlAwareTransport(daily_pnl=Decimal(str(sys.float_info.max)))
    )

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.ACCOUNT_STATE_INCOMPLETE
    assert _submits(transport) == [], "a sentinel P&L must never enable a submit"


def test_a_positions_observation_failure_preserves_the_facts_and_audits() -> None:
    """Item 11/12: a broker positions failure in _observe must FAIL CLOSED as
    OBSERVATION_FAILED while KEEPING the contract and quote already observed,
    and it must be durably audited. The owner run instead saw the failure
    escape, and the summary read 'contract NOT RESOLVED / quote NONE'."""
    from trading_broker_core.port import BrokerErrorCode, BrokerPortError

    service, transport, audit = _service(portfolio=None)

    def _timeout(alias: str) -> object:
        raise BrokerPortError(BrokerErrorCode.BROKER_REJECTED, "timed out waiting for positions")

    service._broker.get_positions = _timeout  # type: ignore[assignment,method-assign] # noqa: SLF001

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.OBSERVATION_FAILED
    # The facts observed BEFORE the failure are preserved, not discarded.
    assert evaluation.instrument is not None, "contract was discarded"
    assert evaluation.quote is not None, "quote was discarded"
    assert evaluation.entry_price is not None, "entry price was discarded"
    # Fail closed, audited.
    assert _submits(transport) == []
    assert evaluation.audit_ref is not None, "the refusal was not durably audited"
    assert audit.records, "no audit record was written"
    # The safe reason names the observation class, not the raw broker text.
    reason = " ".join(evaluation.reasons)
    assert "positions observation" in reason
    assert "timed out waiting for positions" not in reason, "raw broker text leaked"


def test_a_reported_session_pnl_reaches_the_risk_engine() -> None:
    service, _, _ = _service(transport=PnlAwareTransport(daily_pnl=Decimal("-10")))

    evaluation = service.evaluate(_intent())

    assert evaluation.risk_result is not None
    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED


@pytest.mark.parametrize(
    ("label", "daily_pnl", "expect_veto"),
    [
        # equity = min(100k NLV, 50k cap) = 50k. soft = 0.75%, hard = 1.00%.
        ("below the soft threshold", Decimal("-100"), False),
        ("beyond the soft threshold", Decimal("-400"), True),
        ("beyond the hard threshold", Decimal("-600"), True),
    ],
)
def test_the_daily_loss_halt_actually_blocks(
    label: str, daily_pnl: Decimal, expect_veto: bool
) -> None:
    """A real loss, through the real engine, to a real refusal."""
    service, transport, _ = _service(
        transport=PnlAwareTransport(daily_pnl=daily_pnl)
    )

    evaluation = service.execute(_intent())

    if expect_veto:
        assert evaluation.stage is ExecutionStage.RISK_REFUSED, label
        assert _submits(transport) == [], label
    else:
        assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED, label


def test_no_open_risk_is_ever_assumed_without_an_exposure_authority() -> None:
    """Unknown protection must not become an assumed stop.

    The port carries no protective stop for an existing position, so its true
    open risk is genuinely unknown. Estimating it from notional times an assumed
    stop distance is the same class of fabrication as an assumed session P&L —
    it just looks more reasonable. Without an authority the answer is "nothing
    known", and the classification gate refuses the entry outright.
    """
    service, transport, _ = _service(portfolio=None)

    positions = service._open_positions(_intent())  # noqa: SLF001 - the point
    evaluation = service.execute(_intent())

    assert positions == (), "no invented exposure"
    assert evaluation.stage is ExecutionStage.PORTFOLIO_UNCLASSIFIED
    assert _submits(transport) == []


def test_an_account_with_no_positions_has_a_known_zero_open_risk() -> None:
    """Zero positions is a FACT, not an unknown, and must stay tradeable."""
    transport = PnlAwareTransport()
    transport.positions = lambda account_id: ()  # type: ignore[method-assign]

    service, _, _ = _service(transport=transport, portfolio=None)
    evaluation = service.evaluate(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert evaluation.risk_result is not None


def test_the_execution_config_offers_no_switch_to_weaken_this() -> None:
    """There is no Paper/Live knob that turns the refusal off."""
    from dataclasses import fields as dataclass_fields

    names = {field.name for field in dataclass_fields(ExecutionConfig)}

    assert "require_classified_portfolio" not in names
    assert "assumed_open_risk_fraction" not in names


def test_an_unclassified_portfolio_refuses_rather_than_guessing() -> None:
    service, transport, _ = _service(portfolio=None)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.PORTFOLIO_UNCLASSIFIED
    assert "cannot be evaluated" in evaluation.reasons[0]
    assert _submits(transport) == []


def test_a_mixed_portfolio_keeps_each_position_its_own() -> None:
    """Positions from different sectors and sources stay different."""
    mixed = (
        PositionExposure(
            symbol="XOM", sector="energy", cluster="oil", source_id="src-a",
            strategy_id="strat-a", open_risk_amount=Decimal("10"),
            notional=Decimal("1000"),
        ),
        PositionExposure(
            symbol="JPM", sector="financials", cluster="banks", source_id="src-b",
            strategy_id="strat-b", open_risk_amount=Decimal("10"),
            notional=Decimal("1000"),
        ),
    )
    service, _, _ = _service(portfolio=lambda: mixed)

    positions = service._open_positions(_intent())  # noqa: SLF001

    assert {p.sector for p in positions} == {"energy", "financials"}
    assert {p.source_id for p in positions} == {"src-a", "src-b"}
    assert "tech" not in {p.sector for p in positions}


# == protection must be REAL broker-side protection ==========================


def test_a_required_protection_the_broker_cannot_hold_refuses_the_entry() -> None:
    """THE fail-closed proof.

    A supported STOP order TYPE is not a resting protective order. The entry is
    a single LIMIT order carrying a stop price, which the adapter maps to the
    order's aux price -- one order, nothing protecting it. The real paper
    adapter declares bracket and OCO UNSUPPORTED, so a protected entry is not
    currently possible through it, and the entry must not go out.
    """
    service, transport, _ = _service(
        protection_required=True, authorization=_TestOnlyAuthorization()
    )

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.PROTECTION_UNAVAILABLE
    assert "not a resting protective order" in evaluation.reasons[0]
    assert _submits(transport) == [], "an unprotected entry must never be sent"


def test_both_layers_use_the_same_protection_authority() -> None:
    """No split brain: the early report and the final gate ask one object.

    The service used to read bracket/oco capabilities while the guard read a
    lifecycle, so a capability flag could make the early answer disagree with
    the final one. Flipping every flag now changes neither.
    """

    class _AllFlagsOn(IbkrPaperAdapter):
        def capabilities(self) -> BrokerCapabilities:
            return replace(
                super().capabilities(),
                bracket=BrokerCapabilityMode.NATIVE,
                oco=BrokerCapabilityMode.NATIVE,
            )

    adapter = _AllFlagsOn(
        lambda: PnlAwareTransport(), account_aliases=dict(ALIASES),
        host="127.0.0.1", port=4002, clock=lambda: ADAPTER_NOW,
    )
    adapter.connect()
    service, transport, _ = _service(adapter=adapter, protection_required=True)

    early = service._protection_state(_order())  # noqa: SLF001
    guard_state = service.guarded_broker._observe_protection(_order())  # noqa: SLF001

    assert early.can_protect is False, "a flag cannot make the early answer yes"
    assert guard_state.protective_order_supported is False, "nor the final one"
    assert _submits(transport) == []


def test_a_real_protection_lifecycle_satisfies_both_layers_identically() -> None:
    """And when one exists, both layers agree that it does."""
    from trading_application.readiness_guarded_broker import ProtectionEvidence

    class _Lifecycle:
        def protection_for(self, intent: OrderIntent) -> ProtectionEvidence:
            return ProtectionEvidence(
                established=True,
                mechanism="native-bracket",
                protective_order_refs=("ibkr-paper:5001",),
                detail="stop resting at the broker",
            )

    service, _, _ = _service(protection_required=True, protection=_Lifecycle())

    early = service._protection_state(_order())  # noqa: SLF001
    guard_state = service.guarded_broker._observe_protection(_order())  # noqa: SLF001

    assert early.can_protect is True
    assert guard_state.protective_order_supported is True


def test_the_real_adapter_now_holds_atomic_protection_and_it_is_wired() -> None:
    """ADR-0019: bracket/oco are NATIVE AND backed by real wiring.

    The inverse of the old option-B guard: pinned so the flag can never be
    NATIVE without ``submit_order`` actually establishing BOTH legs atomically. A
    bare flag flip with no wiring would leave only one leg here and fail.
    """
    adapter = _adapter()
    capabilities = adapter.capabilities()
    assert capabilities.bracket is BrokerCapabilityMode.NATIVE
    assert capabilities.oco is BrokerCapabilityMode.NATIVE
    assert capabilities.order_types[OrderType.STOP] is BrokerCapabilityMode.NATIVE

    alias = next(iter(ALIASES))
    receipt = adapter.submit_order(
        OrderIntent(
            client_order_id="brk-real",
            authorization_ref="submit-auth:brk-real",
            correlation_id="corr-brk-real",
            environment=Environment.PAPER,
            account_alias=alias,
            symbol="AAPL",
            broker_instrument_ref="conid:265598",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("1"),
            limit_price=Decimal("190"),
            protective=ProtectiveBracket(
                stop_price=Decimal("180"), stop_client_order_id="brk-real:stop"
            ),
        )
    )
    assert receipt.client_order_id == "brk-real"
    legs = {o.client_order_id for o in adapter.get_open_orders(alias)}
    assert legs == {"brk-real", "brk-real:stop"}, (
        "a NATIVE bracket flag must be backed by BOTH legs actually resting"
    )


def test_the_final_guard_never_infers_protection_from_a_capability_flag() -> None:
    """The defect this replaced a bad test for.

    A previous test subclassed the adapter to flip ``bracket`` to NATIVE and
    asserted protection passed. That proved a flag can make a gate pass, which
    is the opposite of the invariant: it implemented no bracket order, no
    parent/child pair, no transmit semantics and nothing resting at the broker.

    Flipping every capability flag now changes nothing, because the final guard
    reads a protected-entry LIFECYCLE, not capabilities.
    """
    from trading_application.readiness_guarded_broker import (
        GuardConfig,
        ReadinessGuardedBroker,
    )

    class _AllFlagsOn(IbkrPaperAdapter):
        def capabilities(self) -> BrokerCapabilities:
            return replace(
                super().capabilities(),
                bracket=BrokerCapabilityMode.NATIVE,
                oco=BrokerCapabilityMode.NATIVE,
            )

    adapter = _AllFlagsOn(
        lambda: PnlAwareTransport(), account_aliases=dict(ALIASES),
        host="127.0.0.1", port=4002, clock=lambda: ADAPTER_NOW,
    )
    adapter.connect()
    guard = ReadinessGuardedBroker(
        adapter,
        config=GuardConfig(
            mode=TradingMode.PAPER,
            requirements=StrategyDataRequirements(strategy_id="s"),
            rules=MarketDataRules(),
            account_alias="paper-main",
            protection_required=True,
        ),
        quotes=_Quotes(), risk=_NoRisk(), audit=_Audit(), now=lambda: NOW,
        # No protected-entry lifecycle: nothing can prove protection.
    )

    state = guard._observe_protection(_order())  # noqa: SLF001

    assert state.protective_order_supported is False, (
        "capability flags must not be able to make protection pass"
    )
    assert "not a resting protective order" in state.detail


def test_protection_evidence_without_a_broker_reference_is_not_protection() -> None:
    """"Established" with nothing to point at is a promise, not protection."""
    from trading_application.readiness_guarded_broker import (
        GuardConfig,
        ProtectionEvidence,
        ReadinessGuardedBroker,
    )

    class _EmptyPromise:
        def protection_for(self, intent: OrderIntent) -> ProtectionEvidence:
            return ProtectionEvidence(
                established=True, mechanism="native-bracket",
                protective_order_refs=(),  # nothing at the broker
            )

    guard = ReadinessGuardedBroker(
        _adapter(),
        config=GuardConfig(
            mode=TradingMode.PAPER,
            requirements=StrategyDataRequirements(strategy_id="s"),
            rules=MarketDataRules(),
            account_alias="paper-main",
            protection_required=True,
        ),
        quotes=_Quotes(), risk=_NoRisk(), audit=_Audit(),
        protection=_EmptyPromise(), now=lambda: NOW,
    )

    state = guard._observe_protection(_order())  # noqa: SLF001

    assert state.protective_order_supported is False


def test_without_a_wired_protected_entry_lifecycle_entries_still_block() -> None:
    """Defence in depth: a NATIVE bracket capability is NOT enough on its own.

    The adapter can now hold a bracket, but the execution service is constructed
    here WITHOUT a ``ProtectedEntryLifecycle`` wired in. Protection is proven by
    a lifecycle, never inferred from a capability flag, so the final readiness
    gate still blocks and the broker's transport sees ZERO submits. Wiring the
    lifecycle is a separate, explicit step.
    """
    capabilities = _adapter().capabilities()
    assert capabilities.order_types[OrderType.STOP] is BrokerCapabilityMode.NATIVE
    assert capabilities.bracket is BrokerCapabilityMode.NATIVE

    service, transport, _ = _service(
        protection_required=True, authorization=_TestOnlyAuthorization()
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.PROTECTION_UNAVAILABLE
    assert _submits(transport) == []


def test_the_final_guard_blocks_a_protected_entry_even_past_the_service_gate() -> None:
    """The service's earlier gate is not what makes this safe.

    Bypassing the service and driving the guard directly -- the way a future
    caller holding the port would -- must still refuse.
    """
    from trading_application.readiness_guarded_broker import (
        GuardConfig,
        ReadinessBlockedError,
        ReadinessGuardedBroker,
    )

    transport = PnlAwareTransport()
    guard = ReadinessGuardedBroker(
        _adapter(transport),
        config=GuardConfig(
            mode=TradingMode.PAPER,
            requirements=StrategyDataRequirements(strategy_id="s"),
            rules=MarketDataRules(),
            account_alias="paper-main",
            protection_required=True,
        ),
        quotes=_Quotes(), risk=_NoRisk(), audit=_Audit(), now=lambda: NOW,
    )

    with pytest.raises(ReadinessBlockedError) as error:
        guard.submit_order(_order())

    blockers = error.value.report.blockers
    assert any("broker_side_protection" in blocker for blocker in blockers)
    assert _submits(transport) == []


class _NoRisk:
    """A risk source that answers nothing; these tests are about protection."""

    def risk_result_for(self, intent: OrderIntent) -> RiskResult | None:
        return None


# == the final quantity is bound to the real risk approval ===================


def test_an_authorization_larger_than_the_risk_approval_is_blocked() -> None:
    """Risk approves 1, the authority mints 10, nothing is submitted.

    The sizing gate only ever checked ``approved_quantity > 0``, and the final
    quantity comes from the authorization -- so this is the gap that mattered.
    """
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(oversize=Decimal("10"))
    )

    # The strategy asks for 1, so the engine approves at most 1. The authority
    # then presents 10 -- the exact mismatch a faulty authorization makes.
    evaluation = service.execute(_intent(requested_quantity=Decimal("1")))

    # Caught at the authorization binding, before the guard is even reached:
    # an authority may reduce, never raise.
    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert "may reduce, never raise" in " ".join(evaluation.reasons)
    assert _submits(transport) == [], "an oversized order must never reach the broker"


def test_the_final_guard_also_refuses_an_oversized_order() -> None:
    """Defence in depth: if the binding above were bypassed, the guard refuses.

    The risk registry is the last boundary, and it answers only for an order
    that is still the execution the engine approved.
    """
    service, _, _ = _service()
    evaluation = service.evaluate(_intent())
    assert evaluation.risk_result is not None
    assert evaluation.fingerprint is not None

    registry = DecisionRiskRegistry()
    registry.register(evaluation.fingerprint, evaluation.risk_result)
    approved = evaluation.risk_result.approved_quantity

    assert registry.risk_result_for(_order(quantity=approved)) is not None
    assert registry.risk_result_for(_order(quantity=approved * 10)) is None


def test_the_risk_registry_refuses_an_order_bigger_than_it_approved() -> None:
    from trading_execution_agent.execution_service import DecisionRiskRegistry

    service, _, _ = _service()
    evaluation = service.evaluate(_intent())
    assert evaluation.risk_result is not None
    approved = evaluation.risk_result.approved_quantity

    fingerprint = ExecutionFingerprint(
        decision_id=_intent().decision_id, client_order_id="exec-ord-1",
        strategy_id="baseline-trend-breakout-v1", source_id="strategy",
        symbol="AAPL", side="BUY", environment="PAPER",
        account_alias="paper-main", requested_quantity="1",
        configured_order_type="LIMIT", configured_time_in_force="DAY",
    )
    registry = DecisionRiskRegistry()
    registry.register(fingerprint, evaluation.risk_result)

    assert registry.risk_result_for(_order(quantity=approved)) is not None
    assert registry.risk_result_for(_order(quantity=approved + 1)) is None
    assert registry.risk_result_for(_order(symbol="MSFT")) is None
    assert registry.risk_result_for(_order(client_order_id="other")) is None
    assert registry.risk_result_for(_order(side=OrderSide.SELL)) is None, (
        "the opposite side is a different execution"
    )
    assert registry.risk_result_for(_order(account_alias="another")) is None


def test_a_second_decision_cannot_spend_the_first_decisions_risk_approval() -> None:
    """Sharing a client order id and symbol is not being the same execution."""
    from trading_execution_agent.execution_service import ExecutionFingerprint

    service, _, _ = _service()
    evaluation = service.evaluate(_intent())
    assert evaluation.risk_result is not None

    first = ExecutionFingerprint(
        decision_id="decision-one", client_order_id="shared-id",
        strategy_id="s", source_id="strategy", symbol="AAPL", side="BUY",
        environment="PAPER", account_alias="paper-main",
    )
    registry = DecisionRiskRegistry()
    registry.register(first, evaluation.risk_result)

    # A DIFFERENT decision re-registering the same id replaces it rather than
    # inheriting; and an order for a different execution shape gets nothing.
    assert registry.risk_result_for(_order(client_order_id="shared-id")) is not None
    assert registry.risk_result_for(
        _order(client_order_id="shared-id", symbol="MSFT")
    ) is None


def _order(
    *,
    client_order_id: str = "exec-ord-1",
    symbol: str = "AAPL",
    quantity: Decimal = Decimal("1"),
    side: OrderSide = OrderSide.BUY,
    account_alias: str = "paper-main",
    broker_instrument_ref: str = "conid:265598",
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id, authorization_ref="a", correlation_id="c",
        environment=Environment.PAPER, account_alias=account_alias, symbol=symbol,
        broker_instrument_ref=broker_instrument_ref,
        side=side, order_type=OrderType.LIMIT, quantity=quantity,
        limit_price=Decimal("100"), stop_price=Decimal("98"),
    )


# == configuration invariants ================================================


def test_a_mode_environment_mismatch_is_refused_at_construction() -> None:
    with pytest.raises(ExecutionServiceError, match="requires environment"):
        ExecutionConfig(
            mode=TradingMode.PAPER,
            environment=Environment.LIVE,
            account_alias="paper-main",
            requirements=StrategyDataRequirements(strategy_id="s"),
            rules=MarketDataRules(),
            risk_policy=POLICY,
            simulation_equity_cap=Decimal("50000"),
        )


def test_a_risk_policy_mismatch_between_config_and_engine_is_refused() -> None:
    """Config claiming policy A while the engine applies policy B."""
    other = replace(POLICY, policy_version="a-different-policy")

    with pytest.raises(ExecutionServiceError, match="risk policy mismatch"):
        ExecutionService(
            broker=_adapter(),
            quotes=_Quotes(),
            audit=_Audit(),
            risk_engine=RiskEngine(other),
            config=ExecutionConfig(
                mode=TradingMode.PAPER,
                environment=Environment.PAPER,
                account_alias="paper-main",
                requirements=StrategyDataRequirements(strategy_id="s"),
                rules=MarketDataRules(),
                risk_policy=POLICY,
                simulation_equity_cap=Decimal("50000"),
            ),
            entry_gate=_PermissiveLatch(),
            history=_History(),
        )


# == restart-safe idempotency ================================================


@dataclass(frozen=True)
class _Record:
    """The canonical history record shape, as the reader returns it."""

    action: str
    payload: Mapping[str, object]


class _History:
    """The durable read side, as the audit chain would answer it.

    Records, not just action names: the caller has to be able to reconstruct
    what actually happened, and a terminal record may say REFUSED as easily as
    SUBMITTED.
    """

    def __init__(self, records: dict[str, list[_Record]] | None = None) -> None:
        self.records = records or {}
        self.orders: dict[str, list[_Record]] = {}

    def records_for(self, decision_id: str) -> tuple[_Record, ...]:
        return tuple(self.records.get(decision_id, ()))

    def records_for_order(self, client_order_id: str) -> tuple[_Record, ...]:
        """Indexed by ORDER id too, so a cross-decision collision is visible."""
        return tuple(self.orders.get(client_order_id, ()))

    def append(self, decision_id: str, action: str, **payload: object) -> None:
        record = _Record(action=action, payload=payload)
        self.records.setdefault(decision_id, []).append(record)
        fingerprint = payload.get("execution_fingerprint")
        if isinstance(fingerprint, dict):
            order_id = str(fingerprint.get("client_order_id", ""))
            if order_id:
                self.orders.setdefault(order_id, []).append(record)


def test_a_restart_does_not_resubmit_a_decision_that_already_completed() -> None:
    """The scenario in full: submit, lose the process, replay the decision."""
    transport = PnlAwareTransport()
    history = _History()

    first_service, _, _ = _service(
        transport=transport, authorization=_TestOnlyAuthorization(), history=history
    )
    first = first_service.execute(_intent())
    assert first.stage is ExecutionStage.SUBMITTED
    assert len(_submits(transport)) == 1

    # The durable chain recorded the lifecycle; the process now dies.
    identity = first.fingerprint
    assert identity is not None
    history.append(
        identity.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=identity.to_dict(),
    )
    history.append(
        identity.decision_id, "EXECUTION_OUTCOME",
        execution_fingerprint=identity.to_dict(),
        stage="SUBMITTED", submission_outcome="SUBMITTED",
    )
    del first_service

    # A brand-new service and a brand-new agent replay the same decision.
    second_service, _, _ = _service(
        transport=transport, authorization=_TestOnlyAuthorization(), history=history
    )
    second = second_service.execute(_intent())

    assert second.stage is ExecutionStage.SUBMITTED
    assert "already durably recorded" in second.reasons[0]
    assert len(_submits(transport)) == 1, "at most one order, across the restart"


def test_an_unresolved_pre_submit_after_restart_needs_reconciliation() -> None:
    """Committed the intent, never recorded the outcome: UNKNOWN, not a retry."""
    transport = PnlAwareTransport()
    history = _History()
    history.append(_intent().decision_id, "EXECUTION_SUBMIT_PREPARED")

    service, _, _ = _service(
        transport=transport, authorization=_TestOnlyAuthorization(), history=history
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.NEEDS_RECONCILIATION
    assert "never retry blindly" in evaluation.reasons[0]
    assert _submits(transport) == []


def test_unreadable_durable_history_blocks_rather_than_assuming_nothing_happened(
) -> None:
    class _Broken:
        def actions_for(self, decision_id: str) -> tuple[str, ...]:
            raise RuntimeError("database unreachable")

    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=_Broken()
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.NEEDS_RECONCILIATION
    assert _submits(transport) == []


def test_no_durable_history_of_this_decision_permits_execution() -> None:
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=_History()
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED
    assert len(_submits(transport)) == 1


# == a failed commit must never be followed by a submission ==================


def test_a_failed_audit_commit_means_zero_broker_submissions() -> None:
    """The invariant, at its sharpest.

    The sink raises the way a real COMMIT failure raises. Nothing reaches the
    broker, because an order the system cannot prove it decided to send is an
    order it must not send.
    """
    from trading_persistence.execution_audit import ExecutionAuditError

    class _CommitFails:
        def can_persist(self) -> bool:
            return True

        def persist(self, record: Mapping[str, object]) -> str:
            raise ExecutionAuditError("decision could not be committed (OperationalError)")

    service, transport, _ = _service(
        audit=_CommitFails(),
        authorization=_TestOnlyAuthorization(),
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUDIT_UNAVAILABLE
    assert _submits(transport) == []


def test_an_audit_that_cannot_persist_blocks_before_anything_is_planned() -> None:
    service, transport, _ = _service(
        audit=_Audit(available=False), authorization=_TestOnlyAuthorization()
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUDIT_UNAVAILABLE
    assert _submits(transport) == []


# == a terminal record is not automatically a submission =====================


@pytest.mark.parametrize(
    ("label", "stage", "outcome", "expected"),
    [
        ("prior submission", "SUBMITTED", "SUBMITTED", ExecutionStage.SUBMITTED),
        (
            "prior refusal", "SUBMISSION_REFUSED", "REFUSED",
            ExecutionStage.SUBMISSION_REFUSED,
        ),
        (
            "prior failure", "SUBMISSION_REFUSED", "FAILED",
            ExecutionStage.SUBMISSION_REFUSED,
        ),
        (
            "prior needs-review", "SUBMISSION_REFUSED", "NEEDS_REVIEW",
            ExecutionStage.SUBMISSION_REFUSED,
        ),
    ],
)
def test_a_prior_terminal_outcome_is_reconstructed_not_assumed_submitted(
    label: str, stage: str, outcome: str, expected: ExecutionStage
) -> None:
    """POST_SUBMIT does not mean SUBMITTED.

    A terminal record may say REFUSED, FAILED or NEEDS_REVIEW. Reading every one
    of them as SUBMITTED would invent an order that never existed — and would
    tell an operator a refusal had reached the market.
    """
    intent = _intent()
    history = _History()
    history.append(
        intent.decision_id, "EXECUTION_OUTCOME",
        stage=stage, submission_outcome=outcome,
    )
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is expected, label
    assert outcome in " ".join(evaluation.reasons)
    assert _submits(transport) == [], f"{label}: never resubmitted"


# == identity conflicts ======================================================


def test_the_same_decision_with_a_different_order_id_is_a_conflict() -> None:
    intent = _intent()
    history = _History()
    other = _fingerprint_dict(intent, client_order_id="a-different-order")
    history.append(
        intent.decision_id, "EXECUTION_OUTCOME",
        execution_fingerprint=other, stage="SUBMITTED",
        submission_outcome="SUBMITTED",
    )
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.IDEMPOTENCY_CONFLICT
    assert "client_order_id" in evaluation.reasons[0]
    assert _submits(transport) == []


def test_the_same_order_id_for_a_different_execution_is_a_conflict() -> None:
    """A familiar name is not the same execution."""
    intent = _intent()
    history = _History()
    history.append(
        intent.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(intent, symbol="MSFT", side="SELL"),
    )
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.IDEMPOTENCY_CONFLICT
    assert _submits(transport) == []


def test_a_matching_fingerprint_replays_rather_than_conflicting() -> None:
    intent = _intent()
    history = _History()
    history.append(
        intent.decision_id, "EXECUTION_OUTCOME",
        execution_fingerprint=_fingerprint_dict(intent),
        stage="SUBMITTED", submission_outcome="SUBMITTED",
    )
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.SUBMITTED
    assert _submits(transport) == []


def _fingerprint_dict(intent: ExecutionIntent, **overrides: str) -> dict[str, str]:
    base = {
        "decision_id": intent.decision_id,
        "client_order_id": intent.client_order_id,
        "strategy_id": intent.strategy_id,
        "source_id": intent.source_id,
        "symbol": intent.symbol,
        "side": intent.side.value,
        "environment": "PAPER",
        "account_alias": "paper-main",
        "requested_quantity": "1",
        "configured_order_type": "LIMIT",
        "configured_time_in_force": "DAY",
    }
    base.update(overrides)
    return base


# == an evaluation-only history is not an incident ===========================


def test_an_earlier_refusal_replays_as_a_refusal_not_a_reconciliation() -> None:
    """The defect the lifecycle actions exist to fix.

    A decision refused by a halt or a risk veto never went near the broker, so
    replaying it must reach the same refusal — not NEEDS_RECONCILIATION, which
    would turn every routine refusal into an operational incident.
    """
    intent = _intent()
    history = _History()
    history.append(intent.decision_id, "EXECUTION_EVALUATED", stage="RISK_REFUSED")
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is not ExecutionStage.NEEDS_RECONCILIATION
    assert evaluation.stage is ExecutionStage.SUBMITTED, (
        "an evaluation-only history does not block a fresh attempt"
    )
    assert len(_submits(transport)) == 1


# == broker recovery resolves an ambiguous durable state =====================


def _prepared_history(intent: ExecutionIntent, **order: str) -> _History:
    """Durable state where this execution was PREPARED and never resolved."""
    history = _History()
    shape = {
        "order_type": "LIMIT", "quantity": "1", "limit_price": "100.02",
        "stop_price": "98.02", "time_in_force": "DAY",
    }
    shape.update(order)
    history.append(
        intent.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(intent, **shape),
    )
    return history


def test_the_real_adapter_recovery_proves_identity_from_the_broker_order(
) -> None:
    """The REAL recovery path, with nothing fabricated.

    The adapter builds its recovery from the same-client ``reqOpenOrders`` row
    it genuinely receives — symbol, side, quantity, order type, prices. The
    previous version compared correlation ids, which IBKR never stores, so only
    a subclass inventing the original correlation could ever pass.
    """
    intent = _intent()
    transport = PnlAwareTransport()
    adapter = _adapter(transport)
    # A genuine same-client working order, submitted through the transport
    # exactly as a pre-crash execution would have left it.
    transport.submit(1001, PAPER_ACCOUNT, _order_intent_for(intent))

    service, _, _ = _service(
        adapter=adapter, transport=transport,
        authorization=_TestOnlyAuthorization(),
        history=_prepared_history(intent),
    )
    before = len(_submits(transport))

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert "recovered rather than duplicated" in " ".join(evaluation.reasons)
    assert len(_submits(transport)) == before, "recovery submits nothing"


def test_the_real_recovery_reports_the_broker_observed_order_shape() -> None:
    """No synthetic receipt: the fields come from the broker's own row."""
    intent = _intent()
    transport = PnlAwareTransport()
    adapter = _adapter(transport)
    transport.submit(1001, PAPER_ACCOUNT, _order_intent_for(intent))

    working = adapter.recover_working_orders("paper-main")

    assert len(working) == 1
    order = working[0]
    assert order.client_order_id == intent.client_order_id
    assert order.symbol == "AAPL"
    assert order.side is OrderSide.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.quantity == Decimal("1")
    assert order.limit_price == Decimal("100.02")
    assert not hasattr(order, "correlation_id"), (
        "the broker never stored our correlation id, so recovery must not"
        " claim to know it"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "99"),
        ("symbol", "MSFT"),
        ("side", "SELL"),
        ("limit_price", "250.00"),
        ("order_type", "MARKET"),
    ],
)
def test_a_broker_order_of_a_different_shape_is_not_recovered(
    field: str, value: str
) -> None:
    """Same order id, different order. Reconcile; never claim it."""
    intent = _intent()
    transport = PnlAwareTransport()
    adapter = _adapter(transport)
    transport.submit(1001, PAPER_ACCOUNT, _order_intent_for(intent))

    service, _, _ = _service(
        adapter=adapter, transport=transport,
        authorization=_TestOnlyAuthorization(),
        history=_prepared_history(intent, **{field: value}),
    )
    before = len(_submits(transport))

    evaluation = service.execute(intent)

    # symbol and side are execution IDENTITY, so they trip the conflict check
    # before recovery is even attempted; the order-shape fields are caught by
    # the broker comparison. Either way: blocked, nothing submitted.
    assert evaluation.stage in (
        ExecutionStage.NEEDS_RECONCILIATION, ExecutionStage.IDEMPOTENCY_CONFLICT
    ), field
    assert len(_submits(transport)) == before, field


def test_an_ambiguous_state_the_broker_cannot_resolve_needs_reconciliation(
) -> None:
    """The broker holds nothing with this identity, so nothing is claimed."""
    intent = _intent()
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=_prepared_history(intent)
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.NEEDS_RECONCILIATION
    assert "never retry blindly" in " ".join(evaluation.reasons)
    assert _submits(transport) == []


def _order_intent_for(intent: ExecutionIntent) -> OrderIntent:
    return OrderIntent(
        client_order_id=intent.client_order_id,
        authorization_ref="submit-auth:recovered",
        correlation_id=intent.correlation_id,
        environment=Environment.PAPER,
        account_alias="paper-main",
        symbol=intent.symbol,
        broker_instrument_ref="conid:265598",
        side=intent.side,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("100.02"),
        stop_price=Decimal("98.02"),
    )


# == the durable submit-prepared marker is the point of no return ============


def test_a_failure_to_durably_prepare_means_no_submission() -> None:
    """The marker is committed BEFORE the submission path is entered."""

    class _PrepareFails(_Audit):
        def persist_prepared(self, record: Mapping[str, object]) -> str:
            raise RuntimeError("database unreachable at the point of no return")

    service, transport, _ = _service(
        audit=_PrepareFails(), authorization=_TestOnlyAuthorization()
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUDIT_UNAVAILABLE
    assert _submits(transport) == []


def test_the_prepared_marker_is_written_before_the_broker_is_called() -> None:
    order: list[str] = []

    class _Ordered(_Audit):
        def persist_prepared(self, record: Mapping[str, object]) -> str:
            order.append("prepared")
            return "prepared-1"

    audit = _Ordered()
    service, transport, _ = _service(
        audit=audit, authorization=_TestOnlyAuthorization()
    )
    original = FakePaperTransport.submit

    def tracking(self: FakePaperTransport, *args: object, **kw: object) -> str:
        order.append("submit")
        return original(self, *args, **kw)  # type: ignore[arg-type]

    FakePaperTransport.submit = tracking  # type: ignore[method-assign]
    try:
        service.execute(_intent())
    finally:
        FakePaperTransport.submit = original  # type: ignore[method-assign]

    assert order == ["prepared", "submit"]


# == MERGE GATE: the canonical producer and the canonical consumer agree ======


def test_the_real_pre_submit_check_produces_an_authorization_this_service_accepts(
) -> None:
    """THE integration this package exists to make true.

    REAL EngineDecision -> REAL RiskEngine -> REAL CandidateAllocation ->
    REAL FinalPreSubmitCheck.check() -> REAL PreSubmitCheckResult ->
    REAL SingleUseAuthorization -> ExecutionService binding.

    Nothing here hand-builds a PreSubmitCheckResult. Against the previous head
    this failed: the pre-submit check emitted ``decision_ref = decision_hash``
    and ``risk_ref = "risk:APPROVE"`` while the risk engine emitted
    ``decision:{id}@{hash}`` and a risk-policy ref, so a genuine authorization
    was rejected and only a hand-tuned one passed.
    """
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert len(_submits(transport)) == 1


def test_the_canonical_chain_binds_without_any_hand_built_result() -> None:
    """The binding check itself, against a genuinely minted authorization."""
    service, _, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    pre_submit = _authorization_for(evaluated)

    assert pre_submit.action is PreSubmitAction.ALLOW
    assert pre_submit.authorization is not None
    # The producer's references are the canonical identities the consumer checks.
    assert pre_submit.decision_ref == decision_identity(_decision())
    assert pre_submit.risk_ref == risk_result_identity(evaluated.risk_result)
    assert pre_submit.policy_ref.startswith("pre-submit-policy:")

    assert service._authorization_binding(  # noqa: SLF001
        _intent(), pre_submit, evaluated.risk_result
    ) is None


@pytest.mark.parametrize(
    "mutation",
    ["decision", "risk_result", "risk_policy", "quantity"],
)
def test_each_individual_mismatch_fails_closed_and_submits_nothing(
    mutation: str,
) -> None:
    """One thing wrong at a time, each caught, each with zero submits."""
    service, transport, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None
    risk_result = evaluated.risk_result

    if mutation == "decision":
        # Authorized against a DIFFERENT decision. The canonical producer now
        # refuses this outright -- FinalPreSubmitCheck verifies that the
        # allocation and risk result belong to the decision being authorized,
        # so a mismatched authorization cannot even be minted.
        from trading_application.pre_submit_check import PreSubmitError

        other = _decision(decision_id=UUID("00000000-0000-0000-0000-0000000000d9"))
        with pytest.raises(PreSubmitError, match="but this request is for"):
            _authorization_for(evaluated, decision=other)
        assert _submits(transport) == []
        return
    if mutation == "risk_result":
        # Authorized against a different risk evaluation of the same decision.
        other_result = replace(risk_result, approved_quantity=Decimal("7"))
        assert evaluated.plan_context is not None
        pre_submit = _real_authorization(
            other_result, context=evaluated.plan_context
        )
    elif mutation == "risk_policy":
        # The risk result was produced under a different policy.
        risk_result = replace(risk_result, policy_ref="risk-policy:something-else")
        pre_submit = _authorization_for(evaluated)
    else:
        pre_submit = _oversized(_authorization_for(evaluated), Decimal("10"))

    reason = service._authorization_binding(  # noqa: SLF001
        _intent(), pre_submit, risk_result
    )

    assert reason is not None, f"{mutation} must be refused"
    assert _submits(transport) == []


def test_a_pre_submit_policy_ref_is_never_compared_to_the_risk_policy() -> None:
    """They are different facts governing different checks.

    Comparing them is precisely what made a valid authorization look invalid.
    """
    service, _, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    pre_submit = _authorization_for(evaluated)

    assert pre_submit.policy_ref != evaluated.risk_result.policy_ref
    assert pre_submit.policy_ref.startswith("pre-submit-policy:")
    assert evaluated.risk_result.policy_ref.startswith("risk-policy:")
    # And the binding accepts them anyway, because it compares each to the
    # right thing.
    assert service._authorization_binding(  # noqa: SLF001
        _intent(), pre_submit, evaluated.risk_result
    ) is None


def test_the_risk_reference_identifies_the_evaluation_not_the_outcome() -> None:
    """"risk:APPROVE" is shared by every approving evaluation in the system."""
    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    identity = risk_result_identity(evaluated.risk_result)
    other = risk_result_identity(replace(evaluated.risk_result, decision_ref="other"))

    assert identity.startswith("risk-result:sha256:")
    assert identity != "risk:APPROVE"
    assert identity != other, "two different evaluations, two different identities"


# == the authorization is sealed to the result that minted it ================


def test_the_single_use_authorization_is_derived_from_its_own_result() -> None:
    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)

    wrapper = single_use_authorization(pre_submit)

    assert wrapper.peek() is pre_submit.authorization
    assert authorization_matches(pre_submit, wrapper) is None


def test_a_mismatched_authorization_pair_is_refused() -> None:
    """Result approving 1 presented with an authorization for 10."""
    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    one = _authorization_for(evaluated)
    ten = _oversized(one, Decimal("10"))
    assert ten.authorization is not None

    mismatch = authorization_matches(one, SingleUseAuthorization(ten.authorization))

    assert mismatch is not None
    assert "does not wrap" in mismatch or "permits" in mismatch


def test_a_result_that_minted_nothing_cannot_produce_a_wrapper() -> None:
    from trading_application.pre_submit_check import PreSubmitError

    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None
    allowed = _authorization_for(evaluated)
    unminted = replace(allowed, authorization=None)

    with pytest.raises(PreSubmitError, match="minted no"):
        single_use_authorization(unminted)


def test_the_service_refuses_an_authorization_that_does_not_match_its_result(
) -> None:
    """End to end: a mismatched pair never reaches the broker."""

    class _MismatchedAuthority:
        def authorize(
            self,
            intent: object,
            report: object,
            risk_result: RiskResult,
            plan: PlanContext,
        ) -> AuthorizationDecision:
            one = _real_authorization(risk_result, context=plan)
            ten = _oversized(one, Decimal("10"))
            assert ten.authorization is not None
            return AuthorizationDecision(
                allowed=True,
                reason="a result approving 1 with an authorization for 10",
                pre_submit=one,
                authorization=SingleUseAuthorization(ten.authorization),
                allocation=_real_allocation(
                    risk_result,
                    entry=plan.entry_price,
                    stop=plan.stop_price or plan.entry_price,
                ),
            )

    service, transport, _ = _service(authorization=_MismatchedAuthority())

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert _submits(transport) == []


# == cross-decision collisions, the literal restart case =====================


def test_a_client_order_id_reused_by_a_different_decision_after_restart(
) -> None:
    """THE cross-decision case, tested literally.

    Decision A prepares order X. The process restarts. Decision B arrives
    reusing order X. Querying history under decision B finds nothing — so the
    order id must be indexed too, or the collision is invisible until the
    broker rejects it.
    """
    history = _History()
    first = _intent(client_order_id="shared-order")
    history.append(
        first.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(first),
    )

    # A genuinely DIFFERENT decision, reusing the same order id.
    second = _intent(
        decision=_decision(decision_id=UUID("00000000-0000-0000-0000-0000000000d7")),
        client_order_id="shared-order",
    )
    assert second.decision_id != first.decision_id
    assert history.records_for(second.decision_id) == (), (
        "the new decision's own history is empty; only the order index sees it"
    )

    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )
    evaluation = service.execute(second)

    assert evaluation.stage is ExecutionStage.IDEMPOTENCY_CONFLICT
    assert _submits(transport) == []


def test_the_same_decision_with_a_new_order_id_after_a_prepared_one() -> None:
    """A decision that already prepared an order may not prepare another."""
    history = _History()
    original = _intent(client_order_id="order-one")
    history.append(
        original.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(original),
    )

    retried = _intent(client_order_id="order-two")
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )
    evaluation = service.execute(retried)

    assert evaluation.stage is ExecutionStage.IDEMPOTENCY_CONFLICT
    assert "client_order_id" in evaluation.reasons[0]
    assert _submits(transport) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "99"),
        ("limit_price", "250.00"),
        ("stop_price", "1.00"),
        ("order_type", "MARKET"),
        ("time_in_force", "GTC"),
    ],
)
def test_a_changed_order_shape_is_a_conflict_not_a_replay(
    field: str, value: str
) -> None:
    """Same id, different order. That is a new execution, not the old one."""
    intent = _intent()
    history = _History()
    history.append(
        intent.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(
            intent,
            **{
                "order_type": "LIMIT", "quantity": "1", "limit_price": "100.02",
                "stop_price": "98.02", "time_in_force": "DAY",
                field: value,
            },
        ),
    )
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
    )

    evaluation = service.execute(intent)

    # A prepared record with no outcome means the broker MAY hold an order, and
    # that dominates: you must reconcile either way. What must never happen is
    # a submission, and the order-shape conflict is asserted directly below.
    assert evaluation.stage in (
        ExecutionStage.IDEMPOTENCY_CONFLICT, ExecutionStage.NEEDS_RECONCILIATION
    ), field
    assert _submits(transport) == [], field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "99"),
        ("limit_price", "250.00"),
        ("stop_price", "1.00"),
        ("order_type", "MARKET"),
        ("time_in_force", "GTC"),
    ],
)
def test_the_prepared_order_conflict_check_sees_a_changed_order(
    field: str, value: str
) -> None:
    """The check itself: same identity, different frozen order."""
    intent = _intent()
    history = _History()
    history.append(
        intent.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(
            intent,
            **{
                "order_type": "LIMIT", "quantity": "1", "limit_price": "100.02",
                "stop_price": "98.02", "time_in_force": "DAY",
            },
        ),
    )
    service, _, _ = _service(history=history)
    prepared = ExecutionFingerprint(
        decision_id=intent.decision_id, client_order_id=intent.client_order_id,
        strategy_id=intent.strategy_id, source_id=intent.source_id,
        symbol="AAPL", side="BUY", environment="PAPER",
        account_alias="paper-main", requested_quantity="1",
        configured_order_type="LIMIT", configured_time_in_force="DAY",
        order_type="LIMIT", quantity="1",
        limit_price="100.02", stop_price="98.02", time_in_force="DAY",
    )

    assert service._prepared_order_conflict(intent, prepared) is None  # noqa: SLF001

    changed = replace(prepared, **{field: value})
    conflict = service._prepared_order_conflict(intent, changed)  # noqa: SLF001

    assert conflict is not None, field
    assert field in conflict


def test_a_normalised_number_is_not_a_changed_order() -> None:
    """1, 1.0 and 1.00 are the same quantity."""
    intent = _intent()
    history = _History()
    history.append(
        intent.decision_id, "EXECUTION_SUBMIT_PREPARED",
        execution_fingerprint=_fingerprint_dict(
            intent, **{"quantity": "1", "limit_price": "100.02"}
        ),
    )
    service, _, _ = _service(history=history)
    prepared = ExecutionFingerprint(
        decision_id=intent.decision_id, client_order_id=intent.client_order_id,
        strategy_id=intent.strategy_id, source_id=intent.source_id,
        symbol="AAPL", side="BUY", environment="PAPER",
        account_alias="paper-main", requested_quantity="1",
        configured_order_type="LIMIT", configured_time_in_force="DAY",
        quantity="1", limit_price="100.02",
    )

    assert service._prepared_order_conflict(intent, prepared) is None  # noqa: SLF001


def test_an_identical_order_shape_replays_rather_than_conflicting() -> None:
    """The control: nothing changed, so nothing is a conflict."""
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())

    service, transport, audit = _service(authorization=_TestOnlyAuthorization())
    first = service.execute(intent)

    assert first.stage is ExecutionStage.SUBMITTED
    assert audit.prepared, "a SUBMIT_PREPARED record was written"
    prepared = audit.prepared[0]["execution_fingerprint"]
    assert isinstance(prepared, dict)
    assert prepared["quantity"] == "1", "the frozen order is in the marker"
    assert prepared["order_type"] == "LIMIT"
    assert prepared["limit_price"]
    assert len(_submits(transport)) == 1


# == an unreadable durable payload must block ===============================


def test_an_unreadable_history_blocks_rather_than_reconstructing_a_refusal(
) -> None:
    """A terminal row whose payload cannot be read is NOT a refusal.

    The reader used to return an empty payload, so a missing object made a
    terminal record look like a decision with no outcome — reconstructed as
    SUBMISSION_REFUSED. That is a confident wrong answer about whether an order
    reached the market.
    """

    class _Unreadable:
        def records_for(self, decision_id: str) -> tuple[object, ...]:
            raise RuntimeError("payload object missing from the store")

        def records_for_order(self, client_order_id: str) -> tuple[object, ...]:
            raise RuntimeError("payload object missing from the store")

    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=_Unreadable()
    )
    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.NEEDS_RECONCILIATION, (
        "an unreadable payload must never be reconstructed as a refusal"
    )
    assert _submits(transport) == []


# == the FULL canonical producer chain, no hand-built objects ================


def test_the_full_real_producer_chain_reaches_the_broker_exactly_once() -> None:
    """REAL decision -> risk -> allocator -> pre-submit -> service -> broker.

    Every object in this chain is produced by its canonical producer. An earlier
    version hand-built the CandidateAllocation, which skipped the allocator
    entirely — and the real allocator was emitting `risk:{policy}:{outcome}`,
    a reference the consumer could not verify against an exact evaluation.
    """
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    # Produced by the REAL PortfolioAllocator.
    allocation = _allocation_for(evaluated)
    assert allocation.decision_ref == decision_identity(_decision())
    assert allocation.risk_ref == risk_result_identity(evaluated.risk_result)
    assert allocation.policy_ref.startswith("portfolio-policy:")
    assert "@sha256:" in allocation.policy_ref

    # Produced by the REAL FinalPreSubmitCheck, from that allocation.
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.allocation_ref == allocation_identity(allocation)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert len(_submits(transport)) == 1


def test_an_allocation_from_another_decision_cannot_authorize_this_one() -> None:
    """The pre-submit check verifies lineage instead of trusting the action."""
    from trading_application.pre_submit_check import PreSubmitError

    service, transport, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    other_decision = _decision(
        decision_id=UUID("00000000-0000-0000-0000-0000000000f1")
    )
    foreign_risk = replace(
        evaluated.risk_result, decision_ref=decision_identity(other_decision)
    )
    assert evaluated.plan_context is not None
    foreign_allocation = _real_allocation(
        foreign_risk,
        entry=evaluated.plan_context.entry_price,
        stop=evaluated.plan_context.stop_price or evaluated.plan_context.entry_price,
    )

    context = evaluated.plan_context
    assert context is not None
    with pytest.raises(PreSubmitError, match="allocation names decision"):
        FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
            _pre_submit_request(foreign_allocation, evaluated.risk_result, context),
            RefreshedMarket(price=context.entry_price, observed_at=NOW),
            RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=NOW),
            ApprovalState(approved=True, approved_at=NOW),
            ExecutionContext(),
            as_of=NOW,
        )

    assert _submits(transport) == []


def test_an_allocation_sized_against_another_evaluation_is_refused() -> None:
    """Same decision, different risk evaluation. Still not this one."""
    from trading_application.pre_submit_check import PreSubmitError

    service, transport, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    other_evaluation = replace(
        evaluated.risk_result, approved_quantity=Decimal("7")
    )
    assert evaluated.plan_context is not None
    allocation = _real_allocation(
        other_evaluation,
        entry=evaluated.plan_context.entry_price,
        stop=evaluated.plan_context.stop_price or evaluated.plan_context.entry_price,
    )

    context = evaluated.plan_context
    assert context is not None
    with pytest.raises(PreSubmitError, match="different risk evaluation"):
        FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
            _pre_submit_request(allocation, evaluated.risk_result, context),
            RefreshedMarket(price=context.entry_price, observed_at=NOW),
            RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=NOW),
            ApprovalState(approved=True, approved_at=NOW),
            ExecutionContext(),
            as_of=NOW,
        )

    assert _submits(transport) == []


def test_the_allocation_reference_identifies_the_exact_allocation() -> None:
    """Not `allocation:{candidate}@{action}` — many share both."""
    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    allocation = _allocation_for(evaluated)
    different = replace(allocation, allocated_quantity=Decimal("99"))

    assert allocation_identity(allocation).startswith("allocation:cand-1@sha256:")
    assert allocation_identity(allocation) != allocation_identity(different)


# == the probe: one broken read must not hide the others ====================


def test_a_failing_observation_does_not_abort_the_whole_probe() -> None:
    """The probe exists to say what is broken, so it must survive breakage.

    The quote read used to be unguarded: a raising quote source ended the run
    and the owner learned nothing about the contract, the universe, the
    portfolio or the audit sink -- precisely the facts a broken host needs.
    """

    class _BrokenQuotes:
        def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
            raise RuntimeError("the market data farm is unreachable")

    service, _, _ = _service(quotes=_BrokenQuotes())

    evidence = service.collect_evidence(_intent())

    checks = {name: (ok, detail) for name, ok, detail in evidence.observations}
    quote_ok, quote_detail = checks["quote"]
    assert not quote_ok
    assert "NOT OBSERVED" in quote_detail
    assert "RuntimeError" in quote_detail
    assert "the market data farm is unreachable" not in quote_detail, (
        "a foreign exception's text is not printed to the owner"
    )
    # ...and every other independent observation still ran.
    for other in ("halts", "account", "session_pnl", "contract", "universe",
                  "portfolio", "audit"):
        assert other in checks, f"{other} was lost to an unrelated failure"
    assert evidence.evaluation is not None, "the run still produced a verdict"


def test_collect_evidence_reads_positions_twice_without_a_second_failure() -> None:
    """The owner-shaped regression. The independent portfolio evidence reads
    positions, and the canonical evaluation reads them AGAIN on the same broker
    session. Both must succeed -- the owner run had the second time out because
    the fixed positions stream was retired by the first read.

    Here the broker is a counting fake: every get_positions call must return
    normally. The evidence must NOT show 'portfolio OK' beside 'evaluation timed
    out waiting for positions'."""
    service, _, _ = _service(portfolio=None)
    broker = service._broker  # noqa: SLF001
    calls = {"n": 0}

    def _counting(alias: str) -> object:
        calls["n"] += 1
        return ()  # no open positions -- exactly the owner-run case

    broker.get_positions = _counting  # type: ignore[assignment,method-assign] # noqa: SLF001

    evidence = service.collect_evidence(_intent())

    checks = {name: (ok, detail) for name, ok, detail in evidence.observations}
    assert checks["portfolio"][0] is True, "independent portfolio read failed"
    # The canonical evaluation ALSO read positions, and did not stop on them.
    assert calls["n"] >= 2, "the canonical evaluation did not re-read positions"
    assert evidence.evaluation is not None
    assert evidence.evaluation.stage is not ExecutionStage.OBSERVATION_FAILED, (
        "the second positions read failed -- the round-10 regression"
    )
    detail = " ".join(
        d for _ok, d in checks.values()
    ) + " " + " ".join(evidence.evaluation.reasons)
    assert "timed out waiting for positions" not in detail


def test_each_observation_fails_on_its_own() -> None:
    """No shared boundary: breaking any one check leaves the rest reported."""
    # The account/session-P&L pair shares one snapshot and its own internal
    # boundary; breaking their shared dependency is covered separately below.
    for broken in ("_halt_evidence", "_universe_evidence",
                   "_portfolio_evidence", "_audit_evidence"):
        service, _, _ = _service()

        def explode(*args: object, _name: str = broken) -> tuple[bool, str]:
            raise RuntimeError(f"{_name} blew up")

        setattr(service, broken, explode)

        evidence = service.collect_evidence(_intent())

        checks = {name: (ok, detail) for name, ok, detail in evidence.observations}
        assert len(checks) >= 8, f"{broken} took other observations with it"
        failed = [d for _ok, d in checks.values() if "NOT OBSERVED" in d]
        assert failed, broken


def test_account_and_session_pnl_share_one_snapshot() -> None:
    """Item 6: the diagnostic phase makes ONE account observation, not two.

    ``_account_evidence`` and ``_session_pnl_evidence`` used to call
    ``get_account_snapshot`` each -- two full reqPnL subscriptions before
    canonical evaluation even started. They now share one snapshot.
    """
    service, _, _ = _service()
    broker = service._broker  # noqa: SLF001 - white-box: count the real observations
    calls: list[str] = []
    original = broker.get_account_snapshot

    def counting(alias: str) -> object:
        calls.append(alias)
        return original(alias)

    broker.get_account_snapshot = counting  # type: ignore[assignment,method-assign]

    # The diagnostic account + session-P&L phase, in isolation: it must observe
    # the account exactly ONCE (it used to be twice -- one reqPnL per record).
    snapshot, account_ok, _note = service._isolated_account_snapshot()  # noqa: SLF001
    pnl_ok, _pnl = service._session_pnl_view(snapshot)  # noqa: SLF001

    assert len(calls) == 1, f"the diagnostic phase made {len(calls)} account snapshots"
    assert account_ok and pnl_ok, "both records derive from the one snapshot"


def _account_snapshot(
    *,
    session_pnl: object = None,
    status: SessionPnlStatus | None = None,
    detail: str | None = None,
) -> AccountSnapshot:
    from datetime import UTC, datetime
    from decimal import Decimal

    return AccountSnapshot(
        account_alias="paper-poi",
        environment=Environment.PAPER,
        net_liquidation_value=Decimal("100000"),
        cash=Decimal("60000"),
        buying_power=Decimal("120000"),
        observed_at=datetime(2026, 8, 24, tzinfo=UTC),
        session_pnl=session_pnl,  # type: ignore[arg-type]
        session_pnl_status=status,
        session_pnl_detail=detail,
    )


def test_the_pnl_view_surfaces_the_safe_broker_code(monkeypatch: object) -> None:
    """Item 4: a request-scoped broker code reaches owner evidence, so the next
    owner run does not need a code change to learn WHICH code occurred."""
    service, _, _ = _service()

    _ok, detail = service._session_pnl_view(  # noqa: SLF001
        _account_snapshot(
            status=SessionPnlStatus.REQUEST_ERROR, detail="reqPnL rejected (IBKR 321)"
        )
    )

    assert "PNL_REQUEST_ERROR" in detail
    assert "IBKR 321" in detail


def test_the_pnl_view_never_leaks_raw_text_when_it_carries_a_detail() -> None:
    """The detail on the snapshot is already code-only; the view must not add
    raw broker text or an account id even when a detail is present."""
    service, _, _ = _service()

    _ok, detail = service._session_pnl_view(  # noqa: SLF001
        _account_snapshot(
            status=SessionPnlStatus.REQUEST_ERROR, detail="reqPnL rejected (IBKR 321)"
        )
    )

    # The safe detail carries the code, not the raw text or an account id.
    assert "DU" not in detail  # no account-id-looking token
    assert "321" in detail


def test_the_pnl_view_distinguishes_timeout_error_and_unset() -> None:
    """Item 6: the owner probe can tell the four P&L outcomes apart, and never
    calls a bounded timeout a permission failure."""
    from decimal import Decimal

    service, _, _ = _service()
    view = service._session_pnl_view  # noqa: SLF001

    ok = view(_account_snapshot(session_pnl=Decimal("0"), status=SessionPnlStatus.OK))
    assert ok[0] is True and "PNL_OK" in ok[1]

    timeout = view(_account_snapshot(status=SessionPnlStatus.TIMEOUT))
    assert timeout[0] is False
    assert "PNL_TIMEOUT" in timeout[1]
    for claim in ("no permission", "lacks permission", "no p&l permission"):
        assert claim not in timeout[1].lower(), "a timeout is not a permission failure"

    request_error = view(_account_snapshot(status=SessionPnlStatus.REQUEST_ERROR))
    assert request_error[0] is False and "PNL_REQUEST_ERROR" in request_error[1]

    unset = view(_account_snapshot(status=SessionPnlStatus.UNSET))
    assert unset[0] is False and "PNL_UNSET" in unset[1]


def test_the_pnl_view_never_claims_permission_for_any_unknown() -> None:
    """No unknown status may be reported as 'no P&L permission'."""
    service, _, _ = _service()
    for status in (
        SessionPnlStatus.TIMEOUT,
        SessionPnlStatus.REQUEST_ERROR,
        SessionPnlStatus.UNSET,
        SessionPnlStatus.STALE,
        None,
    ):
        ok, detail = service._session_pnl_view(  # noqa: SLF001
            _account_snapshot(status=status)
        )
        assert ok is False, f"{status} must fail closed"
        low = detail.lower()
        for claim in ("no permission", "lacks permission", "no p&l permission"):
            assert claim not in low, f"{status} was falsely called a permission failure"


def test_the_pnl_view_reports_stale_explicitly() -> None:
    """A stale value fails closed and says so -- never used as if current."""
    service, _, _ = _service()
    ok, detail = service._session_pnl_view(  # noqa: SLF001
        _account_snapshot(status=SessionPnlStatus.STALE, detail="reqPnL last update 40s ago")
    )
    assert ok is False
    assert "PNL_STALE" in detail


def test_a_broken_account_snapshot_fails_both_records_not_the_rest() -> None:
    """They share a dependency, so a broken snapshot fails BOTH -- honestly --
    while every other observation still reports. The failure is caught inside
    the shared observation, so collect_evidence still completes."""
    service, _, _ = _service()
    broker = service._broker  # noqa: SLF001 - white-box: break the shared dependency

    def explode(alias: str) -> object:
        raise RuntimeError("account snapshot blew up")

    broker.get_account_snapshot = explode  # type: ignore[assignment,method-assign]

    evidence = service.collect_evidence(_intent())

    checks = {name: (ok, detail) for name, ok, detail in evidence.observations}
    assert checks["account"][0] is False
    assert checks["session_pnl"][0] is False
    for other in ("halts", "contract", "universe", "portfolio", "audit"):
        assert other in checks, f"{other} was lost to the shared-snapshot failure"


def test_a_failing_evaluation_still_returns_the_observations() -> None:
    """Even the decision path failing leaves the diagnosis intact."""
    service, _, _ = _service()

    def explode(intent: ExecutionIntent) -> object:
        raise RuntimeError("the decision path blew up")

    service._observe = explode  # type: ignore[assignment,method-assign] # noqa: SLF001

    evidence = service.collect_evidence(_intent())

    assert evidence.evaluation is not None
    assert evidence.evaluation.stage is ExecutionStage.OBSERVATION_FAILED
    assert len(evidence.observations) >= 9


# == the seal: what was checked IS what is sent ==============================
#
# The defect these close: FinalPreSubmitCheck proved a decision, a risk result,
# an allocation, a policy and a quantity -- and then the service built its own
# order from its own runtime fields and the agent built the broker's OrderIntent
# from separate, independently substitutable request fields. The merge gate even
# contained a live instance: the authorization was minted against an entry of
# 100.02 while the service planned from a midpoint of 100.025, and the order
# still went out.


def _mutating(**fields: object) -> Callable[[PlanContext], PlanContext]:
    """An authority that checks an order OTHER than the one it was handed."""

    def transform(context: PlanContext) -> PlanContext:
        return replace(context, **fields)  # type: ignore[arg-type]

    return transform


def test_the_authority_is_handed_the_order_the_service_will_actually_send(
) -> None:
    """The runtime facts, not a fixture price.

    The service plans its entry from the live quote midpoint (100.00/100.05 ->
    100.025), which is NOT the 100.02 the old fixture minted against.
    """
    authority = _TestOnlyAuthorization()
    service, transport, _ = _service(authorization=authority)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    context = authority.last_context
    assert context is not None
    assert context.entry_price == Decimal("100.025"), (
        "the authority checks the price the service actually planned"
    )
    assert context.stop_price == Decimal("98.025")
    assert len(_submits(transport)) == 1
    sent = transport.submitted_intents[0]
    assert sent.limit_price == context.entry_price
    assert sent.stop_price == context.stop_price


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("entry price", {"entry_price": Decimal("100.02")}),
        ("stop price", {"stop_price": Decimal("98.02")}),
        ("side", {"side": "SELL"}),
        ("order type", {"order_type": OrderType.MARKET}),
        ("time in force", {"time_in_force": "GTC"}),
        ("symbol", {"symbol": "MSFT"}),
        ("account", {"account_alias": "paper-other"}),
        ("environment", {"environment": Environment.LIVE}),
    ],
)
def test_an_authorization_for_a_different_order_never_reaches_the_broker(
    name: str, mutation: dict[str, object]
) -> None:
    """B through G: one substituted order fact at a time, all fail closed.

    The lineage stays genuine in every case -- same decision, same risk
    evaluation, same allocation, same policy -- so the ONLY thing wrong is the
    order. Every reference check the previous design performed would pass.
    """
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(checks_instead=_mutating(**mutation))
    )

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED, name
    reasons = " ".join(evaluation.reasons)
    # Symbol and environment are ALSO part of the risk question, so the
    # canonical pre-submit check refuses them even earlier -- by refusing to
    # mint at all. Either way the refusal is total.
    assert (
        "not the plan this execution would send" in reasons
        or "authority raised" in reasons
    ), f"{name}: {evaluation.reasons}"
    assert _submits(transport) == [], f"{name} reached the broker"


def test_a_faulty_authority_cannot_raise_the_portfolio_allocation() -> None:
    """A: risk approves more than the portfolio allocates.

    Risk sizes from the risk envelope; the PORTFOLIO then allocates less. An
    authority presenting a genuine-derived result for a size between the two
    passes every risk check -- a risk ceiling is not a portfolio allocation, and
    the previous binding checked only the ceiling.
    """
    intent = _intent(requested_quantity=Decimal("100"))
    # A portfolio already carrying open risk, so the allocator's caps bind
    # strictly below what risk alone would allow.
    constrained = PortfolioState(
        risk_budget_equity=Decimal("50000"),
        open_positions=(
            PortfolioPosition(
                symbol="MSFT", sector="software", theme="enterprise",
                cluster="megacap-software", source_id="other-source",
                strategy_id="other-strategy",
                open_risk_amount=Decimal("450"), notional=Decimal("35000"),
            ),
        ),
        observed_at=NOW,
    )
    honest, _, _ = _service(
        authorization=_TestOnlyAuthorization(portfolio=constrained)
    )
    evaluated = honest.evaluate(intent)
    assert evaluated.risk_result is not None
    assert evaluated.plan_context is not None
    allocated = _real_allocation(
        evaluated.risk_result,
        entry=evaluated.plan_context.entry_price,
        stop=evaluated.plan_context.stop_price or evaluated.plan_context.entry_price,
        portfolio=constrained,
    )
    risk_ceiling = evaluated.risk_result.approved_quantity
    assert allocated.allocated_quantity < risk_ceiling, (
        "the portfolio must allocate strictly less than risk approved, or this"
        " test proves nothing"
    )
    between = allocated.allocated_quantity + Decimal(1)
    assert between <= risk_ceiling, "the presented size is within the RISK limit"

    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(
            portfolio=constrained, oversize=between
        )
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert _submits(transport) == []


def test_an_authorization_without_its_allocation_is_refused() -> None:
    """The allocation is not optional: without it the cap cannot be checked."""

    class _NoAllocation:
        def pre_submit_policy_identity(self) -> str:
            return pre_submit_policy_identity(PRE_SUBMIT_POLICY)

        def authorize(
            self,
            intent: object,
            report: object,
            risk_result: RiskResult,
            plan: PlanContext,
        ) -> AuthorizationDecision:
            pre_submit = _real_authorization(risk_result, context=plan)
            return AuthorizationDecision(
                allowed=True,
                reason="genuine authorization, no allocation handed over",
                pre_submit=pre_submit,
                authorization=single_use_authorization(pre_submit),
            )

    service, transport, _ = _service(authorization=_NoAllocation())

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert "no portfolio allocation" in " ".join(evaluation.reasons)
    assert _submits(transport) == []


def test_the_agent_refuses_a_plan_that_is_not_the_one_authorized() -> None:
    """The agent checks the seal itself, before spending the single use.

    Even handed a genuine result and a genuine token, an order that is not the
    sealed one is refused by the component that would send it.
    """
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.authorized_plan is not None

    substituted = ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        # A different order, presented with a genuine authorization.
        plan=replace(pre_submit.authorized_plan, quantity=Decimal("99")),
    )

    outcome = service._agent.process(substituted)  # noqa: SLF001

    assert outcome.status is ExecutionStatus.REFUSED
    assert "PLAN_NOT_THE_ONE_CHECKED" in outcome.reason_codes
    assert _submits(transport) == []


def test_the_agent_builds_its_order_only_from_the_sealed_plan() -> None:
    """Structural: no order field on the request is independently settable."""
    from dataclasses import fields as dataclass_fields

    settable = {field.name for field in dataclass_fields(ExecutionRequest)}

    for order_fact in (
        "symbol", "side", "order_type", "limit_price", "stop_price",
        "time_in_force", "environment", "account_alias",
    ):
        assert order_fact not in settable, (
            f"{order_fact} is a settable field on ExecutionRequest, so an order"
            " fact can differ from the sealed plan"
        )
        assert hasattr(ExecutionRequest, order_fact), (
            f"{order_fact} must still be readable -- from the plan"
        )


def test_the_sealed_plan_is_the_order_the_broker_receives() -> None:
    """The whole chain, compared field for field at the transport."""
    authority = _TestOnlyAuthorization()
    service, transport, _ = _service(authorization=authority)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.authorization is not None
    pre_submit = evaluation.authorization.pre_submit
    assert pre_submit is not None and pre_submit.authorized_plan is not None
    plan = pre_submit.authorized_plan
    assert len(_submits(transport)) == 1
    order = transport.submitted_intents[0]

    assert order.symbol == plan.symbol
    assert order.side.value == plan.side
    assert order.quantity == plan.quantity
    assert order.order_type == plan.order_type
    assert order.limit_price == plan.limit_price
    assert order.stop_price == plan.stop_price
    assert order.time_in_force.value == plan.time_in_force
    assert order.account_alias == plan.account_alias
    assert order.environment == plan.environment


# == the risk identity names the QUESTION, not just the answer ===============


def _question(**overrides: object) -> TradeRiskRequest:
    """A risk request, with one fact varied at a time."""
    base = {
        "symbol": "AAPL",
        "sector": "tech",
        "cluster": "megacap",
        "source_id": "strategy",
        "strategy_id": "baseline-trend-breakout-v1",
        "environment": Environment.PAPER,
        "requested_quantity": Decimal("10"),
        "entry_price": Decimal("100"),
        "stop_price": Decimal("98"),
    }
    base.update(overrides)
    return TradeRiskRequest(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "overrides"),
    [
        ("symbol", {"symbol": "MSFT"}),
        ("sector", {"sector": "energy"}),
        ("cluster", {"cluster": "smallcap"}),
        ("source", {"source_id": "another-source"}),
        ("strategy", {"strategy_id": "another-strategy"}),
        ("environment", {"environment": Environment.LIVE}),
        ("quantity", {"requested_quantity": Decimal("11")}),
        ("entry", {"entry_price": Decimal("101")}),
        ("stop", {"stop_price": Decimal("97")}),
    ],
)
def test_two_different_risk_questions_never_share_an_identity(
    name: str, overrides: dict[str, object]
) -> None:
    """The defect: a RiskResult is output numbers, and outputs can coincide.

    Same decision, same account, same policy, same approved quantity and risk
    figures -- but a different symbol, sector, source or cluster where no cap
    happened to bind. The results were byte-identical, so ``risk_result_identity``
    gave them the SAME reference and an authorization derived from one was
    indistinguishable from an authorization for the other.
    """
    mine, theirs = _question(), _question(**overrides)

    assert risk_request_identity(mine) != risk_request_identity(theirs), name


def test_equal_looking_risk_outputs_still_get_different_identities() -> None:
    """Proof at the RESULT level, which is what an authorization carries.

    Two evaluations engineered to produce identical output numbers. Under the
    old contract their identities were equal; they must now differ, because the
    result carries the question it answered.
    """
    engine = RiskEngine(POLICY)
    decision = _decision()
    account = AccountState(
        broker_net_liquidation_value=Decimal("50000"),
        simulation_equity_cap=Decimal("50000"),
        session_pnl=Decimal("0"),
        open_positions=(),
        observed_at=NOW,
    )
    apple = engine.evaluate(decision, _question(), account, as_of=NOW)
    micro = engine.evaluate(
        decision, _question(symbol="MSFT", sector="software"), account, as_of=NOW
    )

    # Identical answers...
    assert apple.outcome is micro.outcome
    assert apple.approved_quantity == micro.approved_quantity
    assert apple.approved_risk_amount == micro.approved_risk_amount
    assert apple.risk_budget_equity == micro.risk_budget_equity
    assert apple.decision_ref == micro.decision_ref
    assert apple.policy_ref == micro.policy_ref
    # ...to different questions.
    assert apple.request_ref != micro.request_ref
    assert risk_result_identity(apple) != risk_result_identity(micro), (
        "an authorization for the MSFT evaluation must not be usable for AAPL"
    )


def test_a_risk_result_for_another_question_cannot_cross_the_chain() -> None:
    """Risk -> Allocator -> PreSubmit -> Service, with a foreign evaluation."""
    from trading_application.pre_submit_check import PreSubmitError

    service, transport, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None
    context = evaluated.plan_context
    assert context is not None

    # A genuine risk result -- of a different question. Its decision, policy and
    # numbers all line up; only the question differs.
    engine = RiskEngine(POLICY)
    account = AccountState(
        broker_net_liquidation_value=Decimal("50000"),
        simulation_equity_cap=Decimal("50000"),
        session_pnl=Decimal("0"),
        open_positions=(),
        observed_at=NOW,
    )
    foreign = engine.evaluate(
        _decision(), _question(symbol="MSFT", sector="software"), account, as_of=NOW
    )
    foreign_allocation = _real_allocation(
        foreign,
        entry=context.entry_price,
        stop=context.stop_price or context.entry_price,
    )

    with pytest.raises(PreSubmitError, match="different risk request"):
        FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
            _pre_submit_request(foreign_allocation, foreign, context),
            RefreshedMarket(price=context.entry_price, observed_at=NOW),
            RefreshedAccount(risk_budget_equity=Decimal("50000"), observed_at=NOW),
            ApprovalState(approved=True, approved_at=NOW),
            ExecutionContext(),
            as_of=NOW,
        )

    assert _submits(transport) == []


def test_the_service_verifies_the_engine_answered_its_own_question() -> None:
    """A result that names a different request is a service-level failure."""
    service, transport, _ = _service()

    class _WrongQuestion:
        policy = POLICY

        def evaluate(self, decision: object, request: object, account: object,
                     **kw: object) -> RiskResult:
            real = RiskEngine(POLICY)
            return replace(
                real.evaluate(decision, request, account, **kw),  # type: ignore[arg-type]
                request_ref="risk-request:SOMETHING-ELSE@sha256:" + "0" * 64,
            )

    service._risk_engine = _WrongQuestion()  # type: ignore[assignment] # noqa: SLF001

    evaluation = service.execute(_intent())

    assert evaluation.stage is not ExecutionStage.SUBMITTED
    assert _submits(transport) == []


# == the one gate is structural, not conventional ============================


def test_there_is_no_constructor_path_to_a_second_entry_gate() -> None:
    """The agent_factory is gone, and nothing replaced it.

    The wiring was correct before, but the CONTRACT allowed
    ``lambda port, gate: ExecutionAgent(port, entry_gate=ALWAYS_PERMIT)`` --
    a supplied gate the service never checked was used. No type could catch
    that, so the parameter had to go.
    """
    import inspect

    parameters = inspect.signature(ExecutionService.__init__).parameters
    assert "agent_factory" not in parameters
    for name, parameter in parameters.items():
        annotation = str(parameter.annotation)
        assert "ExecutionAgent" not in annotation, (
            f"{name} can supply an agent, so it can supply a different gate"
        )
    # A clock is a clock; a safety dependency is not injectable.
    assert "entry_gate" in parameters


def test_every_service_shares_its_gate_with_its_agent() -> None:
    """Whatever gate goes in, that is the gate the agent consults."""
    gate = _PermissiveLatch()
    service, _, _ = _service(entry_latch=gate)

    assert service.entry_gate is gate
    assert service._agent._entry_gate is gate  # noqa: SLF001


# == a late boundary refusal is RESOLVED, not left ambiguous =================


class _LateBoundaryQuotes:
    """Healthy while the service plans, stale by the time the guard re-asks."""

    def __init__(self) -> None:
        self.calls = 0

    def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
        self.calls += 1
        observed = (
            NOW - timedelta(seconds=1)
            if self.calls <= 2
            else NOW - timedelta(hours=1)
        )
        return QuoteState(
            Decimal("100.00"), Decimal("100.05"), observed,
            bid_size=500, ask_size=500,
            market_data_state=MarketDataState.REALTIME,
        )


class _LinkedStore:
    """One durable store, written by the sink and read on replay.

    The restart tests used to hand-append the records they then asserted on,
    which tests the reader against whatever the test believes the writer does.
    This is the writer's OWN output, replayed.
    """

    def __init__(self) -> None:
        self.history = _History()

    # -- the sink side ----------------------------------------------------
    def can_persist(self) -> bool:
        return True

    def persist(self, record: Mapping[str, object]) -> str:
        return self._append("EXECUTION_EVALUATED", record)

    def persist_prepared(self, record: Mapping[str, object]) -> str:
        return self._append("EXECUTION_SUBMIT_PREPARED", record)

    def persist_outcome(self, record: Mapping[str, object]) -> str:
        return self._append("EXECUTION_OUTCOME", record)

    def _append(self, action: str, record: Mapping[str, object]) -> str:
        payload = dict(record)
        decision_id = str(payload.get("decision_id", ""))
        stored = _Record(action=action, payload=payload)
        self.history.records.setdefault(decision_id, []).append(stored)
        fingerprint = payload.get("execution_fingerprint")
        if isinstance(fingerprint, dict):
            order_id = str(fingerprint.get("client_order_id", ""))
            if order_id:
                self.history.orders.setdefault(order_id, []).append(stored)
        return f"{action}-{len(self.history.records.get(decision_id, ()))}"

    # -- the reader side --------------------------------------------------
    def records_for(self, decision_id: str) -> tuple[_Record, ...]:
        return self.history.records_for(decision_id)

    def records_for_order(self, client_order_id: str) -> tuple[_Record, ...]:
        return self.history.records_for_order(client_order_id)


def test_a_late_readiness_refusal_is_durably_resolved_as_not_sent() -> None:
    """The guard refused BEFORE the broker call, so nothing was sent.

    Leaving the SUBMIT_PREPARED marker unresolved would mean a restart reads
    "the broker may hold this order" about an order the guard proved was never
    sent -- an incident invented out of a working safety check.
    """
    store = _LinkedStore()
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(),
        audit=store, history=store,
        quotes=_LateBoundaryQuotes(),
    )

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.READINESS_BLOCKED, evaluation.reasons
    assert _submits(transport) == [], "the guard refused before the broker call"
    records = store.records_for(_intent().decision_id)
    actions = [record.action for record in records]
    assert "EXECUTION_SUBMIT_PREPARED" in actions, "the marker is NOT rewritten"
    assert "EXECUTION_OUTCOME" in actions, "and it IS resolved, beside it"
    resolved = next(r for r in records if r.action == "EXECUTION_OUTCOME")
    assert resolved.payload["submission_outcome"] == "NOT_SENT"
    assert resolved.payload["broker_order_ref"] is None
    prepared = next(r for r in records if r.action == "EXECUTION_SUBMIT_PREPARED")
    assert (
        resolved.payload["execution_fingerprint"]
        == prepared.payload["execution_fingerprint"]
    ), "the SAME full prepared fingerprint is preserved"


def test_a_restart_after_a_late_refusal_reconstructs_not_sent() -> None:
    """The whole point: a NEW service, a fresh replay, no false incident."""
    intent = _intent()
    store = _LinkedStore()
    first, first_transport, _ = _service(
        authorization=_TestOnlyAuthorization(), audit=store, history=store,
        quotes=_LateBoundaryQuotes(),
    )
    assert first.execute(intent).stage is ExecutionStage.READINESS_BLOCKED
    assert _submits(first_transport) == []
    del first

    # The process dies. A brand new service replays the same execution.
    second, second_transport, _ = _service(
        authorization=_TestOnlyAuthorization(), audit=store, history=store
    )

    replayed = second.execute(intent)

    assert replayed.stage is ExecutionStage.READINESS_BLOCKED, replayed.reasons
    assert _submits(second_transport) == [], "still nothing sent"


def test_an_unrecordable_not_sent_resolution_fails_closed() -> None:
    """If the store cannot prove it, a restart cannot either."""
    store = _LinkedStore()
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), audit=store, history=store,
        quotes=_LateBoundaryQuotes(),
    )
    store.persist_outcome = _raise_after_submit  # type: ignore[method-assign]

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.POST_SUBMIT_AUDIT_FAILED
    assert _submits(transport) == [], "nothing was ever sent"
    assert service.critical_state is not None
    assert service.entry_gate.entry_blocked()[0], "new entries are latched"


# == PREPARED and OUTCOME must agree about which order it was ================
#
# The writer carries the prepared fingerprint into the outcome record, which is
# correct -- and not something the reader may DEPEND on. A store whose
# correctness rests on the current writer always behaving cannot detect the
# writer misbehaving.


def _prepared_payload(**order: str) -> dict[str, object]:
    shape = {
        "decision_id": "decision-1",
        "client_order_id": "ord-1",
        "strategy_id": "strat-1",
        "source_id": "src-1",
        "symbol": "AAPL",
        "side": "BUY",
        "environment": "PAPER",
        "account_alias": "paper-main",
        "requested_quantity": "10",
        "configured_order_type": "LIMIT",
        "configured_time_in_force": "DAY",
        "order_type": "LIMIT",
        "quantity": "10",
        "limit_price": "100.02",
        "stop_price": "98.02",
        "time_in_force": "DAY",
    }
    shape.update(order)
    return {"execution_fingerprint": shape}


def _history_record(action: str, payload: dict[str, object]) -> ExecutionHistoryRecord:
    return ExecutionHistoryRecord(
        action=action, chain_hash="sha256:" + "0" * 64, payload=payload
    )


def _lifecycle(**outcome_order: str) -> tuple[ExecutionHistoryRecord, ...]:
    prepared = _history_record("EXECUTION_SUBMIT_PREPARED", _prepared_payload())
    terminal_payload = _prepared_payload(**outcome_order)
    terminal_payload["submission_outcome"] = "SUBMITTED"
    return (prepared, _history_record("EXECUTION_OUTCOME", terminal_payload))


def test_a_matching_prepared_outcome_pair_reads_normally() -> None:
    """The control: an honest lifecycle is readable."""
    require_consistent_lifecycle(_lifecycle())


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("quantity", {"quantity": "11"}),
        ("order type", {"order_type": "MARKET"}),
        ("time in force", {"time_in_force": "GTC"}),
        ("limit price", {"limit_price": "101.00"}),
        ("stop price", {"stop_price": "97.00"}),
    ],
)
def test_an_outcome_resolving_a_different_order_is_unreadable(
    name: str, mutation: dict[str, str]
) -> None:
    """PREPARED says one order, OUTCOME says another. Reconcile, never replay."""
    with pytest.raises(ExecutionHistoryUnreadableError, match="DIFFERENT order"):
        require_consistent_lifecycle(_lifecycle(**mutation))


@pytest.mark.parametrize(
    ("name", "missing"),
    [
        ("prepared quantity", "quantity"),
        ("order type", "order_type"),
        ("time in force", "time_in_force"),
    ],
)
def test_a_terminal_record_missing_the_prepared_order_is_unreadable(
    name: str, missing: str
) -> None:
    """A record that resolves a submission must say WHICH order it resolved."""
    payload = _prepared_payload(**{missing: ""})
    payload["submission_outcome"] = "SUBMITTED"

    with pytest.raises(ExecutionHistoryUnreadableError, match="frozen order"):
        _require_readable("EXECUTION_OUTCOME", "sha256:x", payload)


@pytest.mark.parametrize(
    "missing",
    ["source_id", "configured_order_type", "configured_time_in_force"],
)
def test_a_record_missing_a_request_identity_field_is_unreadable(
    missing: str,
) -> None:
    """The request identity is the writer's contract, enforced by the reader."""
    payload = _prepared_payload(**{missing: ""})
    payload["submission_outcome"] = "SUBMITTED"

    with pytest.raises(ExecutionHistoryUnreadableError, match="incomplete"):
        _require_readable("EXECUTION_OUTCOME", "sha256:x", payload)


def test_the_reader_and_the_writer_share_one_field_contract() -> None:
    """Structural: neither side restates the field list.

    Two tuples that happen to match today are not a contract. A field added to
    the writer's identity and forgotten in the reader would mean the reader
    validated an identity the writer no longer produced.
    """
    from trading_contracts.execution_fingerprint import (
        MANDATORY_PREPARED_FIELDS,
        PREPARED_ORDER_FIELDS,
        REQUEST_IDENTITY_FIELDS,
    )

    assert ExecutionFingerprint.IDENTITY_FIELDS is REQUEST_IDENTITY_FIELDS
    assert ExecutionFingerprint.ORDER_FIELDS is PREPARED_ORDER_FIELDS
    assert REQUIRED_IDENTITY_FIELDS is REQUEST_IDENTITY_FIELDS
    assert REQUIRED_PREPARED_FIELDS is MANDATORY_PREPARED_FIELDS
    # And the writer really does emit every field the reader demands.
    written = set(
        ExecutionFingerprint(
            decision_id="d", client_order_id="o", strategy_id="s", source_id="src",
            symbol="AAPL", side="BUY", environment="PAPER", account_alias="a",
        ).to_dict()
    )
    assert set(REQUEST_IDENTITY_FIELDS) <= written
    assert set(PREPARED_ORDER_FIELDS) <= written


def test_an_inconsistent_lifecycle_blocks_the_service_with_zero_submits() -> None:
    """End to end: unreadable history means reconcile, not submit."""

    class _Inconsistent:
        def records_for(self, decision_id: str) -> tuple[object, ...]:
            raise ExecutionHistoryUnreadableError(
                "terminal record resolves a DIFFERENT order than the one prepared"
            )

        def records_for_order(self, client_order_id: str) -> tuple[object, ...]:
            return ()

    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=_Inconsistent()
    )

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.NEEDS_RECONCILIATION
    assert _submits(transport) == []


# == THE merge gate: one uninterrupted identity chain =======================


def test_the_merge_gate_one_identity_from_decision_to_broker_order() -> None:
    """Every link, real, and the same runtime facts throughout.

    REAL EngineDecision -> REAL TradeRiskRequest -> REAL RiskEngine ->
    request-bound RiskResult -> REAL PortfolioAllocator -> exact
    CandidateAllocation -> REAL FinalPreSubmitCheck -> sealed ExecutablePlan ->
    REAL SingleUseAuthorization -> ExecutionService -> ExecutionAgent ->
    ReadinessGuardedBroker -> the real IbkrPaperAdapter over a deterministic
    transport.

    Nothing here is hand-built: not a risk ref, not an allocation, not an
    allocation bound, not a plan ref, not a PreSubmitCheckResult, not a token.
    The gate is that the plan FinalPreSubmit approved is bit-for-bit the
    OrderIntent the transport received.
    """
    intent = _intent()
    authority = _TestOnlyAuthorization()
    transport = PnlAwareTransport()
    service, _, _ = _service(
        transport=transport, adapter=_adapter(transport), authorization=authority
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.authorization is not None
    pre_submit = evaluation.authorization.pre_submit
    allocation = evaluation.authorization.allocation
    risk_result = evaluation.risk_result
    context = evaluation.plan_context
    assert pre_submit is not None and allocation is not None
    assert risk_result is not None and context is not None
    plan = pre_submit.authorized_plan
    assert plan is not None

    # -- every link names the link before it, by canonical identity --------
    assert risk_result.decision_ref == decision_identity(intent.decision)
    assert risk_result.request_ref == risk_request_identity(context.risk_request)
    assert allocation.decision_ref == risk_result.decision_ref
    assert allocation.risk_ref == risk_result_identity(risk_result)
    assert pre_submit.allocation_ref == allocation_identity(allocation)
    assert pre_submit.risk_ref == allocation.risk_ref
    assert pre_submit.decision_ref == risk_result.decision_ref
    assert pre_submit.policy_ref == pre_submit_policy_identity(PRE_SUBMIT_POLICY)

    # -- the plan seals all of it, the order, its id AND its security -------
    assert plan.client_order_id == intent.client_order_id
    assert evaluation.instrument is not None
    resolved_security = evaluation.instrument.broker_instrument_ref
    assert resolved_security, "the broker resolved a security"
    assert plan.broker_instrument_ref == resolved_security
    assert context.instrument.broker_instrument_ref == resolved_security
    assert plan.decision_ref == risk_result.decision_ref
    assert plan.risk_request_ref == risk_result.request_ref
    assert plan.risk_ref == pre_submit.risk_ref
    assert plan.allocation_ref == pre_submit.allocation_ref
    assert pre_submit.plan_ref == plan_identity(plan)
    assert pre_submit.authorization is not None
    assert pre_submit.authorization.plan_ref == pre_submit.plan_ref

    # -- the authorized size respects BOTH ceilings ------------------------
    assert pre_submit.authorized_quantity <= risk_result.approved_quantity
    assert pre_submit.authorized_quantity <= allocation.allocated_quantity

    # -- and the broker received exactly that plan, once -------------------
    assert len(_submits(transport)) == 1
    order = transport.submitted_intents[0]
    assert order.client_order_id == plan.client_order_id
    assert order.broker_instrument_ref == resolved_security, (
        "the OrderIntent names the resolved security, not a ticker"
    )
    assert order.authorization_ref == authorization_identity(
        pre_submit.authorization
    ), "the canonical authorization identity, naming the exact plan"
    assert order.symbol == plan.symbol
    assert order.side.value == plan.side
    assert order.quantity == plan.quantity
    assert order.order_type == plan.order_type
    assert order.limit_price == plan.limit_price
    assert order.stop_price == plan.stop_price
    assert order.time_in_force.value == plan.time_in_force
    assert order.account_alias == plan.account_alias
    assert order.environment == plan.environment
    # The same runtime quote drove the whole chain.
    assert plan.limit_price == context.entry_price == Decimal("100.025")

    # -- and the real adapter sent it as the resolved contract --------------
    from trading_broker_ibkr.paper_adapter import decode_instrument_ref

    assert decode_instrument_ref(order.broker_instrument_ref) == 265598, (
        "the IBKR package translates its own opaque reference to a real conId"
    )
    # The durable record names the security too, so a restart cannot mistake a
    # different security for a replay.
    assert evaluation.fingerprint is not None
    assert evaluation.fingerprint.broker_instrument_ref == resolved_security

    # -- one authorization can never become two broker orderRefs -----------
    replayed = service.execute(intent)
    assert replayed.stage is ExecutionStage.SUBMITTED
    assert "already durably recorded" in " ".join(replayed.reasons) or (
        "idempotent retry" in " ".join(replayed.reasons)
    )
    assert len(_submits(transport)) == 1
    assert {order.client_order_id for order in transport.submitted_intents} == {
        plan.client_order_id
    }

    # -- and production still refuses ---------------------------------------
    production, production_transport, _ = _service()
    refused = production.execute(_intent(client_order_id="prod-1"))
    assert refused.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert _submits(production_transport) == []


# == the probe reports what broke, not who we are ============================


def test_the_probe_never_prints_foreign_exception_text() -> None:
    """Broker/database/filesystem messages carry DSNs, paths and account ids.

    Probe output is read by an operator and pasted into issues and chats, so a
    third party's exception text is not printed at all -- the type is, and a
    canonical code when there is one.
    """

    class _LeakyQuotes:
        def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
            raise ConnectionError(
                "could not connect to postgres://tws:hunter2@10.0.0.4:5432/ib"
                " for account DU1234567 (C:\\Users\\owner\\ibkr\\session.log)"
            )

    service, _, _ = _service(quotes=_LeakyQuotes())

    evidence = service.collect_evidence(_intent())

    detail = next(d for name, _ok, d in evidence.observations if name == "quote")
    assert "ConnectionError" in detail
    for secret in ("hunter2", "postgres://", "DU1234567", "10.0.0.4", "C:\\Users"):
        assert secret not in detail, f"{secret!r} leaked into owner output"


def test_a_known_application_error_still_reports_its_safe_code() -> None:
    """Being safe must not mean being useless."""
    from trading_broker_core.port import BrokerErrorCode, BrokerPortError

    class _CodedQuotes:
        def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
            raise BrokerPortError(
                BrokerErrorCode.BROKER_UNHEALTHY,
                "no market data for account DU1234567",
            )

    service, _, _ = _service(quotes=_CodedQuotes())

    evidence = service.collect_evidence(_intent())

    detail = next(d for name, _ok, d in evidence.observations if name == "quote")
    assert "BROKER_UNHEALTHY" in detail, "the safe reason code IS reported"
    assert "DU1234567" not in detail, "the account id is not"


# == OUR idempotency key is INSIDE the seal =================================
#
# The hole this closes: ExecutablePlan fixed the symbol, side, size, type,
# prices, TIF and account -- and `client_order_id` sat outside it as an
# independently settable field on ExecutionRequest.
#
# Note the wording, because it matters: this is OUR idempotency key, not the
# broker's. IBKR does not deduplicate on the orderRef it travels in, so two
# orders carrying the same one are two live orders. Nothing at the broker
# would have caught a substituted id -- the protection is entirely ours, from
# the durable history and the agent's outcome map, and that is why the id has
# to be sealed rather than merely conventional.


def test_one_authorization_cannot_become_two_broker_orders() -> None:
    """THE regression. On the previous head this produced two submits.

    One genuine pre-submit result, one genuine sealed plan, and two attempts to
    send it under different order ids. The second must be unrepresentable or
    refused, and the broker must never see two orders.
    """
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.authorized_plan is not None
    plan = pre_submit.authorized_plan

    first = ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        plan=plan,
    )
    # A SECOND independent single-use wrapper around the same genuine result --
    # the public API permits it, so the defence cannot rely on it not happening.
    second = ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        plan=plan,
    )

    assert first.client_order_id == second.client_order_id == plan.client_order_id, (
        "there is no way to give the same sealed plan a different order id"
    )

    service._agent.process(first)  # noqa: SLF001
    service._agent.process(second)  # noqa: SLF001

    submitted = transport.submitted_intents
    assert len(submitted) <= 1, "one authorization, at most one broker order"
    assert len({order.client_order_id for order in submitted}) <= 1, (
        "one authorization can never produce two distinct broker orderRefs"
    )


def test_a_substituted_order_id_is_refused_at_the_agent_boundary() -> None:
    """The order id is sealed, so changing it alone breaks the seal."""
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.authorized_plan is not None

    substituted = ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        # Everything else identical; only OUR idempotency key differs.
        plan=replace(pre_submit.authorized_plan, client_order_id="order-B"),
    )

    outcome = service._agent.process(substituted)  # noqa: SLF001

    assert outcome.status is ExecutionStatus.REFUSED
    assert "PLAN_NOT_THE_ONE_CHECKED" in outcome.reason_codes
    assert "NOT_SENT" in outcome.reason_codes
    assert _submits(transport) == []


def test_the_order_id_is_part_of_the_plan_identity() -> None:
    """Two plans differing only by order id are two different plans."""
    service, _, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.authorized_plan is not None
    plan = pre_submit.authorized_plan

    assert plan_identity(plan) != plan_identity(
        replace(plan, client_order_id="order-B")
    )
    assert pre_submit.authorization is not None
    assert pre_submit.authorization.plan_ref == plan_identity(plan), (
        "the token authorizes the order id too"
    )


def test_the_agent_takes_the_order_id_from_the_sealed_plan() -> None:
    """Not from the request, because the request no longer has one."""
    authority = _TestOnlyAuthorization()
    service, transport, _ = _service(authorization=authority)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.authorization is not None
    pre_submit = evaluation.authorization.pre_submit
    assert pre_submit is not None and pre_submit.authorized_plan is not None
    assert transport.submitted_intents[0].client_order_id == (
        pre_submit.authorized_plan.client_order_id
    )


def test_no_broker_bound_order_field_is_independently_settable() -> None:
    """Structural, and now including our idempotency key and the security.

    The previous version of this test omitted `client_order_id`, which is
    exactly how the remaining substitution stayed hidden: every field it named
    was sealed, and the one it did not name was not.
    """
    from dataclasses import fields as dataclass_fields

    settable = {field.name for field in dataclass_fields(ExecutionRequest)}
    sealed = {field.name for field in dataclass_fields(ExecutablePlan)}

    for order_fact in (
        "client_order_id", "symbol", "side", "quantity", "order_type",
        "limit_price", "stop_price", "time_in_force", "environment",
        "account_alias",
    ):
        assert order_fact not in settable, (
            f"{order_fact} is a settable field on ExecutionRequest, so an order"
            " fact can differ from the sealed plan"
        )
        assert order_fact in sealed, f"{order_fact} is not inside the seal"
        assert hasattr(ExecutionRequest, order_fact), (
            f"{order_fact} must still be readable -- from the plan"
        )

    # Only the correlation id and the sealed objects remain settable. A
    # correlation id is a tracing label; it is not a broker-bound order fact.
    assert settable == {"correlation_id", "pre_submit", "authorization", "plan"}


def test_the_broker_receipt_carries_the_canonical_authorization_identity() -> None:
    """One authorization identity format, content-addressed, naming the plan."""
    authority = _TestOnlyAuthorization()
    service, transport, _ = _service(authorization=authority)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.authorization is not None
    pre_submit = evaluation.authorization.pre_submit
    assert pre_submit is not None and pre_submit.authorization is not None
    expected = authorization_identity(pre_submit.authorization)

    assert transport.submitted_intents[0].authorization_ref == expected
    assert evaluation.outcome is not None and evaluation.outcome.receipt is not None
    assert evaluation.outcome.receipt.authorization_ref == expected
    assert "@sha256:" in expected, "content-addressed, not a timestamp"
    # ...and it really does identify the plan: a different plan_ref is a
    # different authorization identity.
    assert expected != authorization_identity(
        replace(pre_submit.authorization, plan_ref="plan:OTHER@sha256:" + "0" * 64)
    )


def test_the_agent_refuses_a_token_minted_by_another_result() -> None:
    """The final boundary verifies the exact authorization PAIR."""
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    mine = _authorization_for(evaluated)
    # A genuine token from a genuinely different authorization. Two byte-equal
    # authorizations ARE the same authorization -- same plan, same size, same
    # instant -- so the comparison that matters is by value, and this is a
    # materially different one.
    theirs = _oversized(_authorization_for(evaluated), Decimal("9"))
    assert mine.authorized_plan is not None
    assert theirs.authorization is not None

    crossed = ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=mine,
        authorization=SingleUseAuthorization(theirs.authorization),
        plan=mine.authorized_plan,
    )

    outcome = service._agent.process(crossed)  # noqa: SLF001

    assert outcome.status is ExecutionStatus.REFUSED
    assert "AUTHORIZATION_IS_NOT_THE_ONE_MINTED" in outcome.reason_codes
    assert "NOT_SENT" in outcome.reason_codes
    assert _submits(transport) == []


# == durable history is not optional for an execution-capable service ========


def test_an_execution_service_cannot_be_built_without_durable_history() -> None:
    """Structural: there is no submission-capable object without a history.

    `history` used to default to None and `_durable_verdict` simply returned
    None when it was absent -- so supplying an authority was enough to make a
    service submission-capable with no restart-safe idempotency at all.
    """
    import inspect

    parameter = inspect.signature(ExecutionService.__init__).parameters["history"]
    assert parameter.default is inspect.Parameter.empty, (
        "history has a default again, so a service can be built without one"
    )

    with pytest.raises(ExecutionServiceError, match="durable execution history"):
        ExecutionService(
            broker=_adapter(),
            quotes=_Quotes(),
            audit=_Audit(),
            risk_engine=RiskEngine(POLICY),
            config=ExecutionConfig(
                mode=TradingMode.PAPER,
                environment=Environment.PAPER,
                account_alias="paper-main",
                requirements=StrategyDataRequirements(strategy_id="s"),
                rules=MarketDataRules(),
                risk_policy=POLICY,
                simulation_equity_cap=Decimal("50000"),
            ),
            entry_gate=_PermissiveLatch(),
            history=None,  # type: ignore[arg-type]
            authorization=_TestOnlyAuthorization(),
        )


# == single-flight: two concurrent calls cannot both submit ==================


def test_a_re_entrant_duplicate_cannot_submit_a_second_order() -> None:
    """The exact interleaving, deterministically.

    The second attempt runs from INSIDE the first, at the moment the first has
    read an empty history and not yet written anything -- which is precisely the
    window where the durable check cannot help, because there is nothing durable
    to see yet.
    """
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    duplicates: list[ExecutionStage] = []

    original = service._quotes.current_quote  # noqa: SLF001
    entered = False

    def racing(instrument: BrokerInstrument) -> QuoteState:
        nonlocal entered
        if not entered:
            entered = True
            # Re-enter with the SAME identity, before the first attempt has
            # prepared or submitted anything.
            duplicates.append(service.execute(intent).stage)
        return original(instrument)

    service._quotes.current_quote = racing  # type: ignore[method-assign] # noqa: SLF001

    first = service.execute(intent)

    assert duplicates == [ExecutionStage.ALREADY_IN_FLIGHT]
    assert first.stage is ExecutionStage.SUBMITTED, first.reasons
    assert len(_submits(transport)) == 1, "exactly one order reached the broker"


def test_two_threads_racing_the_same_execution_submit_at_most_once() -> None:
    """Real threads, released together by a barrier."""
    import threading

    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    start = threading.Barrier(2)
    stages: list[ExecutionStage] = []
    guard = threading.Lock()

    def run() -> None:
        start.wait(timeout=5)
        evaluation = service.execute(intent)
        with guard:
            stages.append(evaluation.stage)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(stages) == 2
    assert len(_submits(transport)) <= 1, "at most one broker order"
    assert ExecutionStage.SUBMITTED in stages or ExecutionStage.ALREADY_IN_FLIGHT in stages


def test_competing_order_ids_for_one_decision_produce_one_winner() -> None:
    """Same decision, two different client order ids. Never two orders."""
    store = _LinkedStore()
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), audit=store, history=store
    )

    first = service.execute(_intent(client_order_id="race-A"))
    second = service.execute(_intent(client_order_id="race-B"))

    assert first.stage is ExecutionStage.SUBMITTED, first.reasons
    assert second.stage is not ExecutionStage.SUBMITTED, second.reasons
    assert len(_submits(transport)) == 1, "one decision, one order"


def test_the_single_flight_claim_is_released_after_a_refusal() -> None:
    """A refused attempt must not wedge the identity permanently."""
    service, transport, _ = _service(authorization=_TestOnlyAuthorization(allowed=False))

    first = service.execute(_intent())
    second = service.execute(_intent())

    assert first.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert second.stage is ExecutionStage.AUTHORIZATION_DENIED, (
        "the claim was released, so this is a fresh refusal rather than the"
        " identity being wedged as permanently in flight"
    )
    assert _submits(transport) == []


def test_the_single_flight_scope_is_documented_as_single_process() -> None:
    """We do not claim multi-process ownership we do not have.

    A durable claim with a uniqueness constraint would be needed for that, and
    the canonical execution path does not have one yet. Saying so is the point.
    """
    from trading_execution_agent import execution_service as module

    doc = module._SingleFlight.__doc__ or ""  # noqa: SLF001

    assert "single-PROCESS" in doc
    assert "does NOT provide multi-process" in doc


# == the SECURITY is sealed, not the ticker =================================
#
# A ticker is not a security. Two share classes under one root, a US listing
# and its foreign line, a symbol re-used after a corporate action -- these agree
# as strings and differ as instruments. Before this, the system could resolve
# security X, price a ticker-shaped contract, authorize a ticker-shaped plan and
# submit a third ticker-shaped contract, with nothing proving all four were the
# same security.


def test_the_plan_seals_the_resolved_security() -> None:
    """The authorization names the instrument, not just the symbol."""
    authority = _TestOnlyAuthorization()
    service, transport, _ = _service(authorization=authority)

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.instrument is not None
    assert evaluation.authorization is not None
    pre_submit = evaluation.authorization.pre_submit
    assert pre_submit is not None and pre_submit.authorized_plan is not None
    plan = pre_submit.authorized_plan

    resolved = evaluation.instrument.broker_instrument_ref
    assert resolved, "the broker resolved a security"
    assert plan.broker_instrument_ref == resolved
    assert plan.broker_exchange == evaluation.instrument.exchange
    assert plan.broker_currency == evaluation.instrument.currency
    # ...and it is inside the hash, so a different security is a different plan.
    assert plan_identity(plan) != plan_identity(
        replace(plan, broker_instrument_ref="conid:999999")
    )


def test_the_quote_is_asked_for_the_resolved_security() -> None:
    """Not for a ticker the feed might resolve to something else."""
    asked: list[BrokerInstrument] = []

    class _RecordingQuotes:
        def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
            asked.append(instrument)
            return QuoteState(
                Decimal("100.00"), Decimal("100.05"), NOW - timedelta(seconds=1),
                bid_size=500, ask_size=500,
                market_data_state=MarketDataState.REALTIME,
            )

    service, _, _ = _service(
        quotes=_RecordingQuotes(), authorization=_TestOnlyAuthorization()
    )

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert asked, "the quote source was consulted"
    assert evaluation.instrument is not None
    for instrument in asked:
        assert instrument.broker_instrument_ref == (
            evaluation.instrument.broker_instrument_ref
        ), "every quote was for the resolved security"


def test_a_substituted_security_never_reaches_the_broker() -> None:
    """Same ticker, different instrument. The seal refuses it."""
    intent = _intent()
    service, transport, _ = _service(authorization=_TestOnlyAuthorization())
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    pre_submit = _authorization_for(evaluated)
    assert pre_submit.authorized_plan is not None

    substituted = ExecutionRequest(
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        # Same AAPL, a different security.
        plan=replace(pre_submit.authorized_plan, broker_instrument_ref="conid:999999"),
    )

    outcome = service._agent.process(substituted)  # noqa: SLF001

    assert outcome.status is ExecutionStatus.REFUSED
    assert "PLAN_NOT_THE_ONE_CHECKED" in outcome.reason_codes
    assert _submits(transport) == []


def test_the_durable_fingerprint_records_which_security_was_prepared() -> None:
    """Same decision, same order id, same ticker, DIFFERENT security."""
    store = _LinkedStore()
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), audit=store, history=store
    )

    first = service.execute(_intent())
    assert first.stage is ExecutionStage.SUBMITTED, first.reasons
    assert first.fingerprint is not None
    assert first.fingerprint.broker_instrument_ref, "the security is recorded"

    prepared = next(
        record for record in store.records_for(_intent().decision_id)
        if record.action == "EXECUTION_SUBMIT_PREPARED"
    )
    fingerprint = prepared.payload["execution_fingerprint"]
    assert isinstance(fingerprint, dict)
    assert fingerprint["broker_instrument_ref"] == (
        first.fingerprint.broker_instrument_ref
    )

    # A replay of the same identity against a DIFFERENT security is a conflict,
    # not a replay. Ticker equality is not security equality.
    stored = ExecutionFingerprint.from_dict(prepared.payload)
    assert stored is not None
    other = replace(stored, broker_instrument_ref="conid:999999")
    assert "broker_instrument_ref" in stored.differences(other)
    assert len(_submits(transport)) == 1


# == the resolver itself fails closed =======================================


def test_a_contract_without_a_usable_id_is_unresolved() -> None:
    """conid:0 is truthy and is not a security."""
    from trading_broker_ibkr.paper_adapter import encode_instrument_ref

    for unusable in (0, -1, None, "", "abc"):
        assert encode_instrument_ref(unusable) == "", unusable
    assert encode_instrument_ref(265598) == "conid:265598"


def test_an_opaque_reference_is_only_decoded_by_its_own_package() -> None:
    from trading_broker_ibkr.paper_adapter import decode_instrument_ref

    assert decode_instrument_ref("conid:265598") == 265598
    for unusable in ("", "conid:0", "conid:-3", "conid:", "conid:abc", "sec:265598"):
        assert decode_instrument_ref(unusable) is None, unusable


def test_an_ambiguous_contract_is_not_resolved_to_an_arbitrary_one() -> None:
    """Several materially different contracts is no answer, not the first one."""
    from trading_broker_core.port import InstrumentQuery
    from trading_broker_ibkr.paper_adapter import _resolve_one_contract

    class _Contract:
        def __init__(self, con_id: int, currency: str) -> None:
            self.conId = con_id
            self.symbol = "BRK"
            self.exchange = "SMART"
            self.currency = currency

    class _Detail:
        def __init__(self, contract: object) -> None:
            self.contract = contract

    query = InstrumentQuery(symbol="BRK")
    requested = _Contract(0, "USD")

    ambiguous = [_Detail(_Contract(1, "USD")), _Detail(_Contract(2, "GBP"))]
    assert _resolve_one_contract(ambiguous, query, requested) is None

    unusable = [_Detail(_Contract(0, "USD"))]
    assert _resolve_one_contract(unusable, query, requested) is None

    exact = [_Detail(_Contract(265598, "USD"))]
    resolved = _resolve_one_contract(exact, query, requested)
    assert resolved is not None
    assert resolved.broker_instrument_ref == "conid:265598"
    # The same answer twice: the reference is stable.
    again = _resolve_one_contract(exact, query, requested)
    assert again is not None
    assert again.broker_instrument_ref == resolved.broker_instrument_ref


def test_a_resolved_instrument_must_carry_a_reference() -> None:
    """The type itself refuses an unidentified security."""
    with pytest.raises(ValueError, match="broker reference"):
        BrokerInstrument(
            broker_instrument_ref="", symbol="AAPL", exchange="SMART", currency="USD"
        )


def test_an_order_must_name_a_security() -> None:
    """OrderIntent refuses a ticker-only order at the port boundary."""
    with pytest.raises(ValueError, match="broker_instrument_ref is mandatory"):
        _order(client_order_id="ord-no-security", broker_instrument_ref="")


# == two services in ONE process cannot both submit =========================


def test_two_service_instances_share_one_single_flight_registry() -> None:
    """The claim was per-INSTANCE, so two services could not see each other.

    Each ExecutionService constructed its own _SingleFlight, which meant two of
    them in one process held different locks and different claimed sets -- and
    the code described the guarantee as process-wide. The registry is now one
    per process, with no constructor parameter that could install a second.
    """
    import inspect

    from trading_execution_agent import execution_service as module

    first, _, _ = _service(authorization=_TestOnlyAuthorization())
    second, _, _ = _service(authorization=_TestOnlyAuthorization())

    assert first._in_flight is second._in_flight  # noqa: SLF001
    assert first._in_flight is module._PROCESS_SINGLE_FLIGHT  # noqa: SLF001
    parameters = inspect.signature(ExecutionService.__init__).parameters
    for name, parameter in parameters.items():
        assert "SingleFlight" not in str(parameter.annotation), (
            f"{name} can supply a second registry"
        )


def test_two_services_racing_one_identity_submit_at_most_once() -> None:
    """Deterministic: the second service runs from INSIDE the first's window.

    Both share the durable history and the broker seam, and the second starts
    at the moment the first has read an empty history and written nothing --
    the exact interleaving a per-instance claim could not see.
    """
    intent = _intent()
    transport = PnlAwareTransport()
    store = _LinkedStore()
    first, _, _ = _service(
        transport=transport, adapter=_adapter(transport),
        authorization=_TestOnlyAuthorization(), audit=store, history=store,
    )
    second, _, _ = _service(
        transport=transport, adapter=_adapter(transport),
        authorization=_TestOnlyAuthorization(), audit=store, history=store,
    )
    stages: list[ExecutionStage] = []

    original = first._quotes.current_quote  # noqa: SLF001
    entered = False

    def racing(instrument: BrokerInstrument) -> QuoteState:
        nonlocal entered
        if not entered:
            entered = True
            # A DIFFERENT service object, same process, same identity.
            stages.append(second.execute(intent).stage)
        return original(instrument)

    first._quotes.current_quote = racing  # type: ignore[method-assign] # noqa: SLF001

    winner = first.execute(intent)

    assert stages == [ExecutionStage.ALREADY_IN_FLIGHT], stages
    assert winner.stage is ExecutionStage.SUBMITTED, winner.reasons
    assert len(_submits(transport)) == 1, "two services, at most one order"


def test_two_services_with_competing_order_ids_produce_one_winner() -> None:
    """Same decision, different order ids, two service objects. One order."""
    transport = PnlAwareTransport()
    store = _LinkedStore()
    first, _, _ = _service(
        transport=transport, adapter=_adapter(transport),
        authorization=_TestOnlyAuthorization(), audit=store, history=store,
    )
    second, _, _ = _service(
        transport=transport, adapter=_adapter(transport),
        authorization=_TestOnlyAuthorization(), audit=store, history=store,
    )

    a = first.execute(_intent(client_order_id="two-svc-A"))
    b = second.execute(_intent(client_order_id="two-svc-B"))

    assert a.stage is ExecutionStage.SUBMITTED, a.reasons
    assert b.stage is not ExecutionStage.SUBMITTED, b.reasons
    assert len(_submits(transport)) == 1, "one decision, one order"


def test_threads_across_two_service_instances_submit_at_most_once() -> None:
    """Real threads, two service objects, one barrier."""
    import threading

    intent = _intent(client_order_id="two-svc-threads")
    transport = PnlAwareTransport()
    store = _LinkedStore()
    services = [
        _service(
            transport=transport, adapter=_adapter(transport),
            authorization=_TestOnlyAuthorization(), audit=store, history=store,
        )[0]
        for _ in range(2)
    ]
    start = threading.Barrier(2)
    stages: list[ExecutionStage] = []
    guard = threading.Lock()

    def run(service: ExecutionService) -> None:
        start.wait(timeout=5)
        evaluation = service.execute(intent)
        with guard:
            stages.append(evaluation.stage)

    threads = [threading.Thread(target=run, args=(svc,)) for svc in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(stages) == 2
    assert len(_submits(transport)) <= 1, "at most one broker order"


def test_the_single_flight_scope_names_the_multi_process_limit() -> None:
    """We still do not claim ownership we do not have."""
    from trading_execution_agent import execution_service as module

    doc = module._SingleFlight.__doc__ or ""  # noqa: SLF001

    assert "single-PROCESS" in doc
    assert "does NOT provide multi-process" in doc


# == THE merge gate, with the PRODUCTION quote implementation ================


def test_the_merge_gate_one_security_through_the_real_quote_path() -> None:
    """The identity chain including the REAL production market-data leg.

    The other merge gate uses a fake quote source, which is what let the quote
    leg drift to a ticker without any test noticing. This one runs the actual
    ``IbkrQuoteSource`` -> ``IbkrMarketDataSession`` ->
    ``IbkrMarketDataRawClient`` -> stub-SDK transport chain, so the assertion
    that the price and the order name the SAME security is made against the
    production code and not against a test double.

    conid:265598, from resolution to reqMktData to placeOrder.
    """
    intent = _intent()
    authority = _TestOnlyAuthorization()
    transport = PnlAwareTransport()
    adapter = _adapter(transport)

    resolved = adapter.resolve_instrument(InstrumentQuery(symbol="AAPL"))
    assert resolved.broker_instrument_ref == "conid:265598"

    quotes, market_data = _production_quote_source()
    service, _, _ = _service(
        transport=transport, adapter=adapter, authorization=authority, quotes=quotes
    )

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.SUBMITTED, evaluation.reasons
    assert evaluation.authorization is not None
    pre_submit = evaluation.authorization.pre_submit
    assert pre_submit is not None and pre_submit.authorized_plan is not None
    plan = pre_submit.authorized_plan

    # -- resolved == quoted --------------------------------------------------
    assert evaluation.instrument is not None
    security = evaluation.instrument.broker_instrument_ref
    assert security == resolved.broker_instrument_ref
    assert market_data.contracts, "the production quote path made a request"
    for contract in market_data.contracts:
        assert contract.conId == 265598, (
            "every reqMktData named the resolved security"
        )
    assert not any(
        call.startswith("reqMktData_by_symbol") for call in market_data.calls
    ), market_data.calls

    # -- quoted == planned == authorized ------------------------------------
    assert evaluation.plan_context is not None
    assert evaluation.plan_context.instrument.broker_instrument_ref == security
    assert plan.broker_instrument_ref == security
    assert evaluation.quote is not None
    assert f"requested_for={security}" in evaluation.quote.detail

    # -- authorized == prepared == submitted --------------------------------
    assert evaluation.fingerprint is not None
    assert evaluation.fingerprint.broker_instrument_ref == security
    sent = transport.submitted_intents
    assert len(sent) == 1
    assert sent[0].broker_instrument_ref == security
    assert decode_instrument_ref(sent[0].broker_instrument_ref) == 265598, (
        "the IBKR package translated its own reference to the real conId"
    )

    # -- and the entry price really came from that quote --------------------
    assert plan.limit_price == evaluation.quote.midpoint

    # -- production still refuses -------------------------------------------
    production, production_transport, _ = _service()
    refused = production.execute(_intent(client_order_id="prod-quote-1"))
    assert refused.stage is ExecutionStage.AUTHORIZATION_DENIED
    assert _submits(production_transport) == []
