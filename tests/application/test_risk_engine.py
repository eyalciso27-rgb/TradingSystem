"""Risk engine: absolute veto, envelope limits, boundary/property (IMP-0904)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.emergency import HaltRecord
from trading_application.risk_engine import (
    AccountState,
    PositionExposure,
    RiskEngine,
    RiskEngineError,
    RiskPolicy,
    RiskResult,
    TradeRiskRequest,
)
from trading_contracts.decision import DecisionResult
from trading_contracts.enums import (
    ActionTier,
    DecisionOutcome,
    Environment,
    HaltScope,
    RiskDecisionOutcome,
)

AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=5)

# Envelope values from Policy_Parameters section 3 — supplied as CONFIG here.
POLICY = RiskPolicy(
    policy_version="risk-policy-test-1",
    risk_per_trade_pct=Decimal("0.0025"),
    max_total_open_risk_pct=Decimal("0.0100"),
    daily_soft_halt_pct=Decimal("0.0075"),
    daily_hard_halt_pct=Decimal("0.0100"),
    max_concurrent_positions=4,
    max_symbol_notional_pct=Decimal("0.10"),
    max_single_order_notional_pct=Decimal("0.075"),
    max_sector_notional_pct=Decimal("0.25"),
    # test-fixture value chosen so a single max-budget trade fits the cluster
    # cap (all limits are config; production values come from the catalog)
    max_correlated_cluster_pct=Decimal("0.30"),
    max_source_open_risk_share=Decimal("0.50"),
)
ENGINE = RiskEngine(POLICY)

# equity = min(100k NLV, 50k cap) = 50k -> per-trade budget 125; per-share risk 1
EQUITY = Decimal("50000")


def decision(outcome: DecisionOutcome = DecisionOutcome.APPROVE) -> EngineDecision:
    result = DecisionResult(
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
        decided_at=EARLIER,
        decision_hash="sha256:" + "a" * 64,
    )
    explanation = DecisionExplanation(
        fusion_ref="fusion:s@1.0.0",
        manifest_ref="manifest:s@1.0.0",
        policy_ref="decision-policy:1.0.0",
        engine_version="decision-engine-1.0.0",
        reason_codes=("RISK_EVALUATION_PENDING",),
        blocking_reasons=(),
        input_refs=("observation:a-1@src-1",),
        validation_note="FIXTURES_VALIDATED_ONLY",
    )
    return EngineDecision(result=result, explanation=explanation)


def request(
    *,
    quantity: str = "100",
    entry: str = "100",
    stop: str | None = "99",
    symbol: str = "AAPL",
    sector: str = "tech",
    cluster: str = "megacap",
    source: str = "src-1",
) -> TradeRiskRequest:
    return TradeRiskRequest(
        symbol=symbol,
        sector=sector,
        cluster=cluster,
        source_id=source,
        strategy_id="strat-1",
        environment=Environment.PAPER,
        requested_quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        stop_price=Decimal(stop) if stop is not None else None,
    )


def account(
    *,
    nlv: str | None = "100000",
    cap: str | None = "50000",
    session_pnl: str = "0",
    positions: tuple[PositionExposure, ...] = (),
) -> AccountState:
    return AccountState(
        broker_net_liquidation_value=Decimal(nlv) if nlv else None,
        simulation_equity_cap=Decimal(cap) if cap else None,
        session_pnl=Decimal(session_pnl),
        open_positions=positions,
        observed_at=EARLIER,
    )


def position(
    *,
    symbol: str = "MSFT",
    sector: str = "other",
    cluster: str = "other",
    source: str = "src-9",
    risk: str = "0",
    notional: str = "0",
) -> PositionExposure:
    return PositionExposure(
        symbol=symbol,
        sector=sector,
        cluster=cluster,
        source_id=source,
        strategy_id="strat-9",
        open_risk_amount=Decimal(risk),
        notional=Decimal(notional),
    )


def evaluate(
    req: TradeRiskRequest,
    acct: AccountState,
    *,
    active_halts: tuple[HaltRecord, ...] = (),
) -> RiskResult:
    return ENGINE.evaluate(decision(), req, acct, active_halts=active_halts, as_of=AS_OF)


# -- happy path -----------------------------------------------------------------


def test_approve_within_envelope() -> None:
    # entry $10 / stop $9: risk $1/share; 100 shares -> risk 100 <= 125 budget,
    # notional 1000 well under every cap
    result = evaluate(request(quantity="100", entry="10", stop="9"), account())
    assert result.outcome is RiskDecisionOutcome.APPROVE
    assert result.approved_quantity == Decimal("100")
    assert result.approved_risk_amount == Decimal("100")
    assert result.risk_budget_equity == EQUITY
    assert "WITHIN_SAFETY_ENVELOPE" in result.reason_codes


# -- boundary tests (DoD) ----------------------------------------------------------


def test_boundary_exactly_at_per_trade_budget_approves() -> None:
    # budget = 0.0025 * 50000 = 125; per-share risk 1 -> exactly 125 shares
    result = evaluate(request(quantity="125", entry="10", stop="9"), account())
    assert result.outcome is RiskDecisionOutcome.APPROVE
    assert result.approved_quantity == Decimal("125")


def test_boundary_one_over_budget_reduces_rounded_down() -> None:
    result = evaluate(request(quantity="126", entry="10", stop="9"), account())
    assert result.outcome is RiskDecisionOutcome.REDUCE
    assert result.approved_quantity == Decimal("125")  # floor, never exceed
    assert "REDUCED_PER_TRADE_RISK" in result.reason_codes


def test_fractional_budget_rounds_down_then_notional_caps() -> None:
    # per-share risk 0.30 -> 125/0.3 = 416.67 -> 416; then order notional
    # 3750/$10 = 375 caps further (both reductions explained)
    result = evaluate(request(quantity="1000", entry="10", stop="9.70"), account())
    assert result.outcome is RiskDecisionOutcome.REDUCE
    assert result.approved_quantity == Decimal("375")
    assert result.approved_risk_amount <= Decimal("125")
    assert "REDUCED_PER_TRADE_RISK" in result.reason_codes
    assert "REDUCED_ORDER_NOTIONAL" in result.reason_codes


# -- property tests (DoD) ------------------------------------------------------------


def test_property_reduce_only_and_budget_never_exceeded() -> None:
    budget = POLICY.risk_per_trade_pct * EQUITY
    for quantity in ("1", "50", "124", "125", "126", "500", "10000"):
        result = evaluate(request(quantity=quantity), account())
        assert result.approved_quantity <= Decimal(quantity)  # reduce-only (3.1)
        assert result.approved_risk_amount <= budget  # budget never exceeded
        assert result.approved_quantity == result.approved_quantity.to_integral_value()
        # deterministic
        again = evaluate(request(quantity=quantity), account())
        assert again.approved_quantity == result.approved_quantity


# -- veto paths -----------------------------------------------------------------------


def test_daily_hard_halt_vetoes() -> None:
    result = evaluate(request(), account(session_pnl="-500"))  # -1% of 50k
    assert result.outcome is RiskDecisionOutcome.VETO
    assert "DAILY_HARD_HALT" in result.reason_codes


def test_daily_soft_halt_vetoes() -> None:
    result = evaluate(request(), account(session_pnl="-380"))  # -0.76%
    assert result.outcome is RiskDecisionOutcome.VETO
    assert "DAILY_SOFT_HALT" in result.reason_codes


def test_active_kill_switch_blocks_absolutely() -> None:
    halt = HaltRecord(
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=Environment.PAPER,
        target_ref=None,
        reason_code="SEV1",
    )
    result = ENGINE.evaluate(
        decision(), request(), account(), active_halts=(halt,), as_of=AS_OF
    )
    assert result.outcome is RiskDecisionOutcome.VETO
    assert "HALT_ACTIVE" in result.reason_codes


def test_missing_account_data_fails_closed() -> None:
    result = evaluate(request(), account(nlv=None))
    assert result.outcome is RiskDecisionOutcome.VETO
    assert "ACCOUNT_DATA_MISSING" in result.reason_codes
    result = evaluate(request(), account(cap=None))  # cap is mandatory (section 2)
    assert result.outcome is RiskDecisionOutcome.VETO


def test_missing_protective_stop_vetoes() -> None:
    result = evaluate(request(stop=None), account())
    assert result.outcome is RiskDecisionOutcome.VETO
    assert "NO_PROTECTIVE_STOP" in result.reason_codes


def test_non_approved_decision_never_allows() -> None:
    for outcome in (DecisionOutcome.REJECT, DecisionOutcome.NEEDS_REVIEW, DecisionOutcome.WATCH):
        result = ENGINE.evaluate(decision(outcome), request(), account(), as_of=AS_OF)
        assert result.outcome is RiskDecisionOutcome.VETO
        assert "DECISION_NOT_APPROVED" in result.reason_codes


def test_duplicate_symbol_and_position_count_veto() -> None:
    open_same = account(positions=(position(symbol="AAPL"),))
    assert "SYMBOL_ALREADY_OPEN" in evaluate(request(), open_same).reason_codes
    full = account(positions=tuple(position(symbol=f"P{i}") for i in range(4)))
    assert "MAX_POSITIONS_REACHED" in evaluate(request(), full).reason_codes


# -- exposure caps ----------------------------------------------------------------------


def test_total_open_risk_cap_reduces_or_vetoes() -> None:
    # envelope = 1% * 50k = 500; existing open risk 450 -> room for 50 shares
    positions = (position(risk="450"),)
    result = evaluate(request(quantity="100", entry="10", stop="9"), account(positions=positions))
    assert result.outcome is RiskDecisionOutcome.REDUCE
    assert result.approved_quantity == Decimal("50")
    assert "REDUCED_TOTAL_OPEN_RISK" in result.reason_codes
    # no room at all -> VETO
    exhausted = (position(risk="500"),)
    result = evaluate(request(quantity="100", entry="10", stop="9"), account(positions=exhausted))
    assert result.outcome is RiskDecisionOutcome.VETO
    assert "NO_ROOM_WITHIN_ENVELOPE" in result.reason_codes


def test_single_order_notional_boundary_exact() -> None:
    # allowed notional 3750 at $100 -> exactly 37 shares (floor of 37.5)
    result = evaluate(request(quantity="50"), account())
    assert result.approved_quantity == Decimal("37")
    assert "REDUCED_ORDER_NOTIONAL" in result.reason_codes


def test_sector_cluster_source_caps() -> None:
    # sector cap 25% * 50k = 12500; existing tech notional 12000 -> room 500 -> 5 shares
    positions = (position(sector="tech", notional="12000"),)
    result = evaluate(request(quantity="37"), account(positions=positions))
    assert result.approved_quantity == Decimal("5")
    assert "REDUCED_SECTOR_NOTIONAL" in result.reason_codes
    # cluster cap: 30% of envelope(500) = 150 risk; existing cluster risk 149 -> 1 share
    cluster_positions = (position(cluster="megacap", risk="149"),)
    result = evaluate(request(quantity="37"), account(positions=cluster_positions))
    assert result.approved_quantity == Decimal("1")
    assert "REDUCED_CLUSTER_RISK" in result.reason_codes
    # source share: 50% of envelope = 250; existing source risk 249 -> 1 share
    source_positions = (position(source="src-1", risk="249"),)
    result = evaluate(request(quantity="37"), account(positions=source_positions))
    assert result.approved_quantity == Decimal("1")
    assert "REDUCED_SOURCE_RISK" in result.reason_codes


# -- policy / hygiene ---------------------------------------------------------------------


def test_invalid_policy_fails_closed() -> None:
    with pytest.raises(RiskEngineError, match="policy_version"):
        RiskPolicy(
            policy_version=" ",
            risk_per_trade_pct=Decimal("0.0025"),
            max_total_open_risk_pct=Decimal("0.01"),
            daily_soft_halt_pct=Decimal("0.0075"),
            daily_hard_halt_pct=Decimal("0.01"),
            max_concurrent_positions=4,
            max_symbol_notional_pct=Decimal("0.1"),
            max_single_order_notional_pct=Decimal("0.075"),
            max_sector_notional_pct=Decimal("0.25"),
            max_correlated_cluster_pct=Decimal("0.2"),
            max_source_open_risk_share=Decimal("0.5"),
        )
    with pytest.raises(TypeError):
        RiskPolicy(policy_version="x")  # type: ignore[call-arg]


def test_lookahead_and_naive_rejected() -> None:
    with pytest.raises(RiskEngineError, match="lookahead"):
        ENGINE.evaluate(decision(), request(), account(), as_of=EARLIER - timedelta(minutes=1))
    with pytest.raises(RiskEngineError, match="timezone-aware"):
        ENGINE.evaluate(decision(), request(), account(), as_of=datetime(2026, 7, 8))  # noqa: DTZ001


def test_explanation_contains_full_trace() -> None:
    result = evaluate(request(quantity="126"), account())
    assert result.decision_ref.startswith("decision:")
    assert "sha256:" in result.decision_ref
    assert result.policy_ref == "risk-policy:risk-policy-test-1"
    assert result.input_refs == ("observation:a-1@src-1",)
    names = [check.name for check in result.checks]
    assert "decision_gate" in names and "per_trade_risk" in names
    assert any(not check.passed and check.effect == "REDUCE" for check in result.checks)


def test_engine_creates_no_order_or_broker_action() -> None:
    import trading_application.risk_engine as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        forbidden_imports = (
            "trading_persistence", "httpx", "requests", "subprocess",
            "OrderIntent", "broker",
        )
        for name in forbidden_imports:
            assert name not in line, line
