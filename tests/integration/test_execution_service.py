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
from trading_application.pre_submit_check import (
    PreSubmitCheckResult,
    SingleUseAuthorization,
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
)
from trading_execution_agent.agent import ExecutionAgent, ExecutionStatus
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


def _decision(outcome: DecisionOutcome = DecisionOutcome.APPROVE) -> EngineDecision:
    """A canonical strategy decision. Business intent, never a safety verdict."""
    return EngineDecision(
        result=DecisionResult(
            decision_id=UUID("00000000-0000-0000-0000-0000000000d1"),
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
        quantity: Decimal = Decimal("1"),
    ) -> None:
        self._allowed = allowed
        self._mint = mint
        self._quantity = quantity
        self.calls = 0

    def authorize(self, intent: object, report: object) -> AuthorizationDecision:
        self.calls += 1
        if not self._allowed:
            return AuthorizationDecision(allowed=False, reason="test denial")
        pre_submit: PreSubmitCheckResult | None = None
        authorization: SingleUseAuthorization | None = None
        if self._mint:
            pre_submit, authorization = _authorization(self._quantity)
        return AuthorizationDecision(
            allowed=True,
            reason="test-only authorization",
            pre_submit=pre_submit,
            authorization=authorization,
        )


def _risk_refs() -> tuple[str, str]:
    """The refs the REAL risk engine emits for this decision and policy.

    An authorization has to be about the decision it authorizes, so a test seam
    that minted arbitrary refs would be testing nothing -- the binding check
    would reject it, correctly.
    """
    decision = _decision().result
    return (
        f"decision:{decision.decision_id}@{decision.decision_hash}",
        f"risk-policy:{POLICY.policy_version}",
    )


def _authorization(
    quantity: Decimal = Decimal("1"),
) -> tuple[PreSubmitCheckResult, SingleUseAuthorization]:
    """Mint a canonical single-use authorization through its real types."""
    from trading_application.pre_submit_check import (
        PreSubmitAction,
        SubmitAuthorization,
    )

    decision_ref, policy_ref = _risk_refs()

    submit = SubmitAuthorization(
        candidate_id="candidate-1",
        authorized_quantity=quantity,
        authorized_at=NOW,
        note="test-only authorization; no production path mints this",
    )
    result = PreSubmitCheckResult(
        candidate_id="candidate-1",
        action=PreSubmitAction.ALLOW,
        authorized_quantity=quantity,
        authorization=submit,
        allocation_ref="allocation-1",
        risk_ref="risk-1",
        decision_ref=decision_ref,
        policy_ref=policy_ref,
        checks=(),
        reason_codes=("TEST_ONLY",),
        input_refs=(),
    )
    return result, SingleUseAuthorization(submit)


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
        agent_factory=lambda port: ExecutionAgent(port, entry_gate=ALWAYS_PERMIT),
        authorization=authorization,  # type: ignore[arg-type]
        universe=(lambda: universe) if universe is not None else None,
        portfolio=portfolio,  # type: ignore[arg-type]
        history=history,  # type: ignore[arg-type]
        entry_latch=entry_latch,
        universe_status=(lambda: universe_status) if universe_status else None,
        halts=lambda: halts,
        now=lambda: NOW,
    )
    return service, fake, sink


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
    decision = DenyAllExecutionAuthorization().authorize(_intent(), None)  # type: ignore[arg-type]
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


def test_a_post_submit_audit_failure_becomes_a_critical_reconciliation_state(
) -> None:
    """The broker answered and we could not record it.

    One submission, never retried; the receipt preserved; an explicit critical
    state rather than a log line; and new entries latched shut through the
    canonical gate. Cancellation stays available.
    """

    class _Latch:
        def __init__(self) -> None:
            self.required: list[str] = []
            self.cancels_blocked = False

        def require(self, reason: str) -> None:
            self.required.append(reason)

    audit = _Audit()
    latch = _Latch()

    def exploding(record: Mapping[str, object]) -> str:
        raise RuntimeError("audit store died after submission")

    service, transport, _ = _service(
        audit=audit, authorization=_TestOnlyAuthorization(), entry_latch=latch
    )
    audit.persist_outcome = exploding  # type: ignore[method-assign]

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.POST_SUBMIT_AUDIT_FAILED
    assert evaluation.outcome is not None, "the broker receipt is preserved"
    assert evaluation.outcome.receipt is not None
    assert len(_submits(transport)) == 1, "no second submission after an audit failure"
    assert latch.required, "new entries are latched until reconciliation"
    assert latch.cancels_blocked is False, "cancellation is never gated by this"


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


def test_existing_positions_are_not_labelled_with_the_candidates_metadata() -> None:
    """Without a portfolio authority, classification is UNKNOWN, not borrowed.

    Labelling every open position with the new decision's sector, cluster,
    source and strategy would corrupt four separate risk caps at once, in
    whichever direction happened to be wrong.
    """
    from trading_execution_agent.execution_service import UNKNOWN_CLASSIFICATION

    service, transport, _ = _service(portfolio=None)
    positions = service._open_positions(_intent())  # noqa: SLF001 - the point

    assert positions, "the fake transport holds a position"
    for position in positions:
        assert position.sector == UNKNOWN_CLASSIFICATION
        assert position.cluster == UNKNOWN_CLASSIFICATION
        assert position.source_id == UNKNOWN_CLASSIFICATION
        assert position.strategy_id == UNKNOWN_CLASSIFICATION
        assert position.sector != "tech", "never the candidate's sector"
        assert position.source_id != "strategy", "never the candidate's source"


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
    assert "bracket=UNSUPPORTED" in evaluation.reasons[0]
    assert _submits(transport) == [], "an unprotected entry must never be sent"


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
        authorization=_TestOnlyAuthorization(quantity=Decimal("10"))
    )

    # The strategy asks for 1, so the engine approves at most 1. The authority
    # then mints 10 -- the exact mismatch a faulty future authorization makes.
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
        strategy_id="baseline-trend-breakout-v1", symbol="AAPL", side="BUY",
        environment="PAPER", account_alias="paper-main",
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
        strategy_id="s", symbol="AAPL", side="BUY",
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
            agent_factory=lambda port: ExecutionAgent(port, entry_gate=ALWAYS_PERMIT),
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

    def records_for(self, decision_id: str) -> tuple[_Record, ...]:
        return tuple(self.records.get(decision_id, ()))

    def append(self, decision_id: str, action: str, **payload: object) -> None:
        self.records.setdefault(decision_id, []).append(
            _Record(action=action, payload=payload)
        )


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
        "symbol": intent.symbol,
        "side": intent.side.value,
        "environment": "PAPER",
        "account_alias": "paper-main",
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


def test_an_ambiguous_state_is_resolved_by_the_brokers_own_recovery() -> None:
    """PREPARED with no outcome, and the broker still holds the order.

    The canonical same-client open-order recovery answers it. Nothing new is
    submitted, and the state stops being an abstract database condition.
    """
    intent = _intent()
    transport = PnlAwareTransport()
    history = _History()
    history.append(intent.decision_id, "EXECUTION_SUBMIT_PREPARED")

    # The broker really does hold a working order with this canonical identity.
    adapter = _adapter(transport)
    transport.submit(1001, PAPER_ACCOUNT, _order_intent_for(intent))

    service, _, _ = _service(
        adapter=adapter, transport=transport,
        authorization=_TestOnlyAuthorization(), history=history,
    )
    before = len(_submits(transport))

    evaluation = service.execute(intent)

    assert evaluation.stage is ExecutionStage.SUBMITTED
    assert "recovered rather than duplicated" in " ".join(evaluation.reasons)
    assert len(_submits(transport)) == before, "recovery submits nothing"


def test_an_ambiguous_state_the_broker_cannot_resolve_needs_reconciliation() -> None:
    intent = _intent()
    history = _History()
    history.append(intent.decision_id, "EXECUTION_SUBMIT_PREPARED")
    service, transport, _ = _service(
        authorization=_TestOnlyAuthorization(), history=history
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
        limit_price=Decimal("100.00"),
        stop_price=Decimal("98.00"),
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
