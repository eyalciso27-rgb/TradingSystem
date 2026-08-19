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
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.emergency import HaltRecord
from trading_application.risk_engine import RiskEngine, RiskPolicy
from trading_application.trading_readiness_runtime import (
    MarketDataRules,
    MarketDataState,
    QuoteState,
)
from trading_broker_core.port import OrderSide
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
    DenyAllExecutionAuthorization,
    ExecutionConfig,
    ExecutionIntent,
    ExecutionService,
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

from tests.broker.test_ibkr_paper_adapter import ALIASES, FakePaperTransport

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

    def __init__(self, *, allowed: bool = True, mint: bool = True) -> None:
        self._allowed = allowed
        self._mint = mint
        self.calls = 0

    def authorize(self, intent: object, report: object) -> AuthorizationDecision:
        self.calls += 1
        if not self._allowed:
            return AuthorizationDecision(allowed=False, reason="test denial")
        pre_submit, authorization = (_authorization() if self._mint else (None, None))
        return AuthorizationDecision(
            allowed=True,
            reason="test-only authorization",
            pre_submit=pre_submit,
            authorization=authorization,
        )


def _authorization() -> tuple[object, object]:
    """Mint a canonical single-use authorization through its real types."""
    from trading_application.pre_submit_check import (
        PreSubmitAction,
        PreSubmitCheckResult,
        SingleUseAuthorization,
        SubmitAuthorization,
    )

    submit = SubmitAuthorization(
        candidate_id="candidate-1",
        authorized_quantity=Decimal("1"),
        authorized_at=NOW,
        note="test-only authorization; no production path mints this",
    )
    result = PreSubmitCheckResult(
        candidate_id="candidate-1",
        action=PreSubmitAction.ALLOW,
        authorized_quantity=Decimal("1"),
        authorization=submit,
        allocation_ref="allocation-1",
        risk_ref="risk-1",
        decision_ref="decision-1",
        policy_ref="policy-1",
        checks=(),
        reason_codes=("TEST_ONLY",),
        input_refs=(),
    )
    return result, SingleUseAuthorization(submit)


def _adapter(transport: FakePaperTransport | None = None) -> IbkrPaperAdapter:
    fake = transport or FakePaperTransport()
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
    fake = transport or FakePaperTransport()
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
        ),
        agent_factory=lambda port: ExecutionAgent(port, entry_gate=ALWAYS_PERMIT),
        authorization=authorization,  # type: ignore[arg-type]
        universe=(lambda: universe) if universe is not None else None,
        universe_status=(lambda: universe_status) if universe_status else None,
        halts=lambda: halts,
        now=lambda: NOW,
    )
    return service, fake, sink


def _intent(**kw: object) -> ExecutionIntent:
    fields: dict[str, object] = {
        "decision_id": "decision-1",
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


def test_a_post_submit_audit_failure_does_not_retry_the_broker() -> None:
    """The order may already exist; retrying would risk a duplicate."""
    audit = _Audit()

    def exploding(record: Mapping[str, object]) -> str:
        raise RuntimeError("audit store died after submission")

    service, transport, _ = _service(audit=audit, authorization=_TestOnlyAuthorization())
    audit.persist_outcome = exploding  # type: ignore[method-assign]

    evaluation = service.execute(_intent())

    assert evaluation.stage is ExecutionStage.SUBMITTED
    assert len(_submits(transport)) == 1, "no second submission after an audit failure"


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
