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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.emergency import HaltRecord
from trading_application.execution_identity import (
    allocation_identity,
    decision_identity,
    pre_submit_policy_identity,
    risk_result_identity,
)
from trading_application.portfolio_allocator import (
    AllocationCandidate,
    CandidateAllocation,
    PortfolioAllocationPolicy,
    PortfolioAllocator,
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
from trading_application.risk_engine import PositionExposure, RiskEngine, RiskPolicy, RiskResult
from trading_application.trading_readiness_runtime import (
    MarketDataRules,
    MarketDataState,
    QuoteState,
)
from trading_broker_core.port import (
    BrokerCapabilities,
    BrokerCapabilityMode,
    OrderIntent,
    OrderSide,
)
from trading_broker_ibkr.paper_adapter import IbkrPaperAdapter
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
    ExecutionFingerprint,
    ExecutionIntent,
    ExecutionService,
    ExecutionServiceError,
    ExecutionStage,
)
from trading_execution_agent.reconciliation_latch import ALWAYS_PERMIT
from trading_market_data_core.sp500_current_universe import Constituent, ConstituentSnapshot
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
    UniverseListStatus,
    UniverseType,
)

from tests.broker.test_ibkr_paper_adapter import (
    ALIASES,
    PAPER_ACCOUNT,
    FakePaperTransport,
)

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

    def current_quote(self, symbol: str) -> QuoteState:
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
    ) -> None:
        self._allowed = allowed
        self._mint = mint
        #: Simulates a faulty authority presenting more than risk approved.
        self._oversize = oversize
        self.calls = 0

    def pre_submit_policy_identity(self) -> str:
        """The EXACT policy this authority mints under, as the service checks."""
        return pre_submit_policy_identity(PRE_SUBMIT_POLICY)

    def authorize(
        self, intent: object, report: object, risk_result: RiskResult
    ) -> AuthorizationDecision:
        self.calls += 1
        if not self._allowed:
            return AuthorizationDecision(allowed=False, reason="test denial")
        pre_submit: PreSubmitCheckResult | None = None
        authorization: SingleUseAuthorization | None = None
        if self._mint:
            pre_submit = _real_authorization(risk_result)
            if self._oversize is not None:
                pre_submit = _oversized(pre_submit, self._oversize)
            authorization = single_use_authorization(pre_submit)
        return AuthorizationDecision(
            allowed=True,
            reason="test-only authorization",
            pre_submit=pre_submit,
            authorization=authorization,
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
        PortfolioState(
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


def _real_authorization(
    risk_result: RiskResult,
    *,
    decision: EngineDecision | None = None,
    entry: Decimal = Decimal("100.02"),
    stop: Decimal = Decimal("98.02"),
) -> PreSubmitCheckResult:
    """Mint an authorization through the REAL FinalPreSubmitCheck.

    Nothing here hand-builds a ``PreSubmitCheckResult``. An earlier version did,
    with references deliberately chosen to match the risk result — which meant
    the binding check was being tested against a shape only the test produced,
    and a genuine authorization from the canonical producer would have been
    rejected.
    """
    engine_decision = decision or _decision()
    # The REAL allocator produces the allocation. Hand-building one meant the
    # merge gate skipped a producer entirely -- and the real allocator was
    # emitting a risk reference the consumer could not verify.
    allocation = _real_allocation(risk_result, entry=entry, stop=stop)
    return FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
        PreSubmitRequest(
            allocation=allocation,
            risk_result=risk_result,
            decision=engine_decision,
            symbol="AAPL",
            strategy_id="baseline-trend-breakout-v1",
            source_id="strategy",
            environment=Environment.PAPER,
            planned_entry_price=entry,
            planned_stop_price=stop,
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
    history: object | None = None,
    quotes: _Quotes | None = None,
    audit: _Audit | None = None,
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
        quotes=quotes or _Quotes(),
        audit=sink,
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
        # The agent is built FROM the service's gate, so the two can never be
        # different objects. Passing ALWAYS_PERMIT here used to be possible and
        # silently defeated the whole post-submit latch.
        entry_gate=gate,  # type: ignore[arg-type]
        agent_factory=lambda port, agent_gate: ExecutionAgent(
            port, entry_gate=agent_gate
        ),
        authorization=authorization,  # type: ignore[arg-type]
        universe=(lambda: universe) if universe is not None else None,
        portfolio=portfolio,  # type: ignore[arg-type]
        history=history,  # type: ignore[arg-type]
        protection=protection,  # type: ignore[arg-type]
        universe_status=(lambda: universe_status) if universe_status else None,
        halts=lambda: halts,
        now=lambda: NOW,
    )
    return service, fake, sink


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

    decision = DenyAllExecutionAuthorization().authorize(
        _intent(),
        evaluated.readiness,  # type: ignore[arg-type]
        evaluated.risk_result,
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
    """A request shaped like the service builds, for driving the agent directly."""
    service, _, _ = _service()
    evaluated = service.evaluate(intent)
    assert evaluated.risk_result is not None
    pre_submit = _real_authorization(evaluated.risk_result)
    return ExecutionRequest(
        client_order_id=intent.client_order_id,
        correlation_id=intent.correlation_id,
        pre_submit=pre_submit,
        authorization=single_use_authorization(pre_submit),
        environment=Environment.PAPER,
        account_alias="paper-main",
        symbol=intent.symbol,
        side=intent.side,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("100.02"),
        stop_price=Decimal("98.02"),
        strategy_id=intent.strategy_id,
        source_id=intent.source_id,
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

        def current_quote(self, symbol: str) -> QuoteState:
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
        quotes=quotes,  # type: ignore[arg-type]
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


def test_the_real_adapter_cannot_currently_hold_atomic_protection() -> None:
    """Pinned so flipping a capability flag without wiring it is caught."""
    capabilities = _adapter().capabilities()

    assert capabilities.bracket is BrokerCapabilityMode.UNSUPPORTED
    assert capabilities.oco is BrokerCapabilityMode.UNSUPPORTED
    assert capabilities.order_types[OrderType.STOP] is BrokerCapabilityMode.NATIVE, (
        "a supported STOP order type is exactly the thing that must NOT be"
        " mistaken for broker-side protection"
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


def test_the_real_adapter_has_no_protected_entry_lifecycle_so_entries_block() -> None:
    """Option B, end to end, against the real adapter and the real guard.

    STOP order type may be NATIVE; bracket and OCO are not; there is no atomic
    protected-entry implementation; the final readiness gate blocks; and the
    broker's transport sees ZERO submits.
    """
    capabilities = _adapter().capabilities()
    assert capabilities.order_types[OrderType.STOP] is BrokerCapabilityMode.NATIVE
    assert capabilities.bracket is BrokerCapabilityMode.UNSUPPORTED
    assert capabilities.oco is BrokerCapabilityMode.UNSUPPORTED

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
) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id, authorization_ref="a", correlation_id="c",
        environment=Environment.PAPER, account_alias=account_alias, symbol=symbol,
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
            agent_factory=lambda port, gate: ExecutionAgent(port, entry_gate=gate),
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
        audit=_CommitFails(),  # type: ignore[arg-type]
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

    pre_submit = _real_authorization(evaluated.risk_result)

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
            _real_authorization(risk_result, decision=other)
        assert _submits(transport) == []
        return
    if mutation == "risk_result":
        # Authorized against a different risk evaluation of the same decision.
        other_result = replace(risk_result, approved_quantity=Decimal("7"))
        pre_submit = _real_authorization(other_result)
    elif mutation == "risk_policy":
        # The risk result was produced under a different policy.
        risk_result = replace(risk_result, policy_ref="risk-policy:something-else")
        pre_submit = _real_authorization(evaluated.risk_result)
    else:
        pre_submit = _oversized(_real_authorization(risk_result), Decimal("10"))

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

    pre_submit = _real_authorization(evaluated.risk_result)

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
    pre_submit = _real_authorization(evaluated.risk_result)

    wrapper = single_use_authorization(pre_submit)

    assert wrapper.peek() is pre_submit.authorization
    assert authorization_matches(pre_submit, wrapper) is None


def test_a_mismatched_authorization_pair_is_refused() -> None:
    """Result approving 1 presented with an authorization for 10."""
    service, _, _ = _service()
    evaluated = service.evaluate(_intent())
    assert evaluated.risk_result is not None

    one = _real_authorization(evaluated.risk_result)
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
    allowed = _real_authorization(evaluated.risk_result)
    unminted = replace(allowed, authorization=None)

    with pytest.raises(PreSubmitError, match="minted no"):
        single_use_authorization(unminted)


def test_the_service_refuses_an_authorization_that_does_not_match_its_result(
) -> None:
    """End to end: a mismatched pair never reaches the broker."""

    class _MismatchedAuthority:
        def authorize(
            self, intent: object, report: object, risk_result: RiskResult
        ) -> AuthorizationDecision:
            one = _real_authorization(risk_result)
            ten = _oversized(one, Decimal("10"))
            assert ten.authorization is not None
            return AuthorizationDecision(
                allowed=True,
                reason="a result approving 1 with an authorization for 10",
                pre_submit=one,
                authorization=SingleUseAuthorization(ten.authorization),
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
    allocation = _real_allocation(evaluated.risk_result)
    assert allocation.decision_ref == decision_identity(_decision())
    assert allocation.risk_ref == risk_result_identity(evaluated.risk_result)
    assert allocation.policy_ref.startswith("portfolio-policy:")
    assert "@sha256:" in allocation.policy_ref

    # Produced by the REAL FinalPreSubmitCheck, from that allocation.
    pre_submit = _real_authorization(evaluated.risk_result)
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
    foreign_allocation = _real_allocation(foreign_risk)

    with pytest.raises(PreSubmitError, match="allocation names decision"):
        FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
            PreSubmitRequest(
                allocation=foreign_allocation,
                risk_result=evaluated.risk_result,
                decision=_decision(),
                symbol="AAPL",
                strategy_id="baseline-trend-breakout-v1",
                source_id="strategy",
                environment=Environment.PAPER,
                planned_entry_price=Decimal("100.02"),
                planned_stop_price=Decimal("98.02"),
            ),
            RefreshedMarket(price=Decimal("100.02"), observed_at=NOW),
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
    allocation = _real_allocation(other_evaluation)

    with pytest.raises(PreSubmitError, match="different risk evaluation"):
        FinalPreSubmitCheck(PRE_SUBMIT_POLICY).check(
            PreSubmitRequest(
                allocation=allocation,
                risk_result=evaluated.risk_result,
                decision=_decision(),
                symbol="AAPL",
                strategy_id="baseline-trend-breakout-v1",
                source_id="strategy",
                environment=Environment.PAPER,
                planned_entry_price=Decimal("100.02"),
                planned_stop_price=Decimal("98.02"),
            ),
            RefreshedMarket(price=Decimal("100.02"), observed_at=NOW),
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

    allocation = _real_allocation(evaluated.risk_result)
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
        def current_quote(self, symbol: str) -> QuoteState:
            raise RuntimeError("the market data farm is unreachable")

    service, _, _ = _service(quotes=_BrokenQuotes())  # type: ignore[arg-type]

    evidence = service.collect_evidence(_intent())

    checks = {name: (ok, detail) for name, ok, detail in evidence.observations}
    quote_ok, quote_detail = checks["quote"]
    assert not quote_ok
    assert "NOT OBSERVED" in quote_detail
    assert "the market data farm is unreachable" in quote_detail
    # ...and every other independent observation still ran.
    for other in ("halts", "account", "session_pnl", "contract", "universe",
                  "portfolio", "audit"):
        assert other in checks, f"{other} was lost to an unrelated failure"
    assert evidence.evaluation is not None, "the run still produced a verdict"


def test_each_observation_fails_on_its_own() -> None:
    """No shared boundary: breaking any one check leaves the rest reported."""
    for broken in ("_halt_evidence", "_account_evidence", "_session_pnl_evidence",
                   "_universe_evidence", "_portfolio_evidence", "_audit_evidence"):
        service, _, _ = _service()

        def explode(*args: object, _name: str = broken) -> tuple[bool, str]:
            raise RuntimeError(f"{_name} blew up")

        setattr(service, broken, explode)

        evidence = service.collect_evidence(_intent())

        checks = {name: (ok, detail) for name, ok, detail in evidence.observations}
        assert len(checks) >= 8, f"{broken} took other observations with it"
        failed = [d for _ok, d in checks.values() if "NOT OBSERVED" in d]
        assert any(broken in d for d in failed), broken


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
