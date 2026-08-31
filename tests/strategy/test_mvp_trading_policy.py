"""End-to-end + adversarial tests for the MVP trading policy orchestrator.

Proves the mission's conceptual path and its ownership boundaries: a valid setup
becomes a sealed canonical execution intent; a NO-TRADE never does; risk/safety
and unknown-state gates block new entries; risk only reduces, never increases;
and every approved intent flows through the canonical decision/risk/pre-submit
lineage.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from trading_application.emergency import HaltRecord
from trading_application.execution_identity import decision_identity, risk_result_identity
from trading_application.mvp_trading_policy import (
    MvpAccountSnapshot,
    MvpPolicyOutcome,
    MvpResult,
    MvpStage,
    MvpTradingPolicyError,
)
from trading_application.position_attribution import BrokerHeldPosition, SystemPositionRecord
from trading_broker_core.port import SessionPnlStatus
from trading_contracts.enums import (
    DecisionOutcome,
    Environment,
    HaltScope,
    RiskDecisionOutcome,
)
from trading_market_data_core.types import BarSeries

RunPolicy = Callable[..., MvpPolicyOutcome]
AsOf = Callable[[BarSeries], datetime]
Builder = Callable[..., BarSeries]
Spec = list[tuple[float, float]]


def _pair(
    con_id: int,
    symbol: str,
    *,
    open_risk: str = "10",
    notional: str = "1000",
    sector: str = "FIN",
    cluster: str = "BANK",
) -> tuple[BrokerHeldPosition, SystemPositionRecord]:
    held = BrokerHeldPosition(
        con_id=con_id, symbol=symbol, quantity=Decimal(1),
        notional=Decimal(notional), observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    record = SystemPositionRecord(
        con_id=con_id, symbol=symbol, strategy_id="baseline-trend-breakout-v1",
        source_id="scanner", sector=sector, cluster=cluster,
        open_risk_amount=Decimal(open_risk), notional=Decimal(notional),
    )
    return held, record


def _account(
    observed_at: datetime,
    *,
    broker_net_liquidation_value: Decimal | None = Decimal(1_000_000),
    simulation_equity_cap: Decimal | None = Decimal(1_000_000),
    session_pnl: Decimal | None = Decimal("-100"),
    session_pnl_status: SessionPnlStatus | None = SessionPnlStatus.OK,
) -> MvpAccountSnapshot:
    return MvpAccountSnapshot(
        broker_net_liquidation_value=broker_net_liquidation_value,
        simulation_equity_cap=simulation_equity_cap,
        session_pnl=session_pnl,
        session_pnl_status=session_pnl_status,
        observed_at=observed_at,
    )


# -- the happy path: a sealed canonical execution intent -----------------------
def test_valid_setup_produces_execution_intent(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    out = run_policy(triggered_series)
    assert out.result is MvpResult.EXECUTION_INTENT
    assert out.stage is MvpStage.EXECUTION_INTENT
    plan = out.executable_plan
    assert plan is not None
    assert plan.side == "BUY"
    assert plan.environment is Environment.PAPER
    assert plan.quantity > 0
    assert plan.stop_price is not None and plan.limit_price is not None
    assert plan.stop_price < plan.limit_price


def test_execution_intent_flows_through_the_canonical_lineage(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    out = run_policy(triggered_series)
    assert out.decision is not None and out.risk_result is not None
    assert out.allocation is not None and out.pre_submit is not None
    plan = out.executable_plan
    assert plan is not None
    # The sealed plan names THIS decision and THIS risk evaluation.
    assert plan.decision_ref == decision_identity(out.decision)
    assert plan.risk_ref == risk_result_identity(out.risk_result)
    assert out.decision.result.outcome is DecisionOutcome.APPROVE


def test_deterministic_same_inputs_same_intent(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    first = run_policy(triggered_series)
    second = run_policy(triggered_series)
    assert first.executable_plan == second.executable_plan
    assert first.decision is not None and second.decision is not None
    assert first.decision.result.decision_hash == second.decision.result.decision_hash


# -- NO TRADE can never become an execution intent -----------------------------
def test_no_breakout_is_no_trade_not_an_intent(
    run_policy: RunPolicy, build_series: Builder, base_spec: Spec
) -> None:
    base_spec[170] = (165.5, 2.2)  # below resistance
    out = run_policy(build_series(base_spec))
    assert out.result is MvpResult.NO_TRADE
    assert out.stage is MvpStage.DECISION
    assert out.executable_plan is None
    assert out.pre_submit is None
    assert out.risk_result is None  # never even reached the risk engine
    assert out.decision is not None
    assert out.decision.result.outcome is not DecisionOutcome.APPROVE


# -- position attribution gate -------------------------------------------------
def test_unattributable_position_blocks_entry(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    stray = BrokerHeldPosition(
        con_id=777, symbol="STRAY", quantity=Decimal(5),
        notional=Decimal(5000), observed_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    out = run_policy(triggered_series, held_positions=(stray,), system_records=())
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.POSITION_ATTRIBUTION
    assert out.risk_result is None
    assert any(r.startswith("POSITION_UNATTRIBUTABLE") for r in out.reason_codes)


def test_same_symbol_existing_position_vetoes(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    held, record = _pair(555, "MVPX")  # same symbol as the candidate
    out = run_policy(triggered_series, held_positions=(held,), system_records=(record,))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.RISK
    assert out.risk_result is not None
    assert out.risk_result.outcome is RiskDecisionOutcome.VETO
    assert "SYMBOL_ALREADY_OPEN" in out.reason_codes


def test_max_concurrent_positions_vetoes(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    held: list[BrokerHeldPosition] = []
    records: list[SystemPositionRecord] = []
    for i in range(8):
        h, r = _pair(600 + i, f"SYM{i}")
        held.append(h)
        records.append(r)
    out = run_policy(triggered_series, held_positions=tuple(held), system_records=tuple(records))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.RISK
    assert "MAX_POSITIONS_REACHED" in out.reason_codes


# -- unknown account state fails closed ----------------------------------------
def test_unknown_session_pnl_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, as_of_after: AsOf
) -> None:
    account = _account(as_of_after(triggered_series), session_pnl=None, session_pnl_status=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE
    assert "SESSION_PNL_UNKNOWN" in out.reason_codes
    assert out.risk_result is None  # never interpreted as zero


def test_stale_session_pnl_status_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, as_of_after: AsOf
) -> None:
    account = _account(
        as_of_after(triggered_series), session_pnl=None,
        session_pnl_status=SessionPnlStatus.STALE,
    )
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE


def test_unknown_nlv_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, as_of_after: AsOf
) -> None:
    account = _account(as_of_after(triggered_series), broker_net_liquidation_value=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "NLV_UNKNOWN" in out.reason_codes


def test_unknown_equity_cap_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, as_of_after: AsOf
) -> None:
    account = _account(as_of_after(triggered_series), simulation_equity_cap=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "EQUITY_CAP_UNKNOWN" in out.reason_codes


# -- halts and daily loss ------------------------------------------------------
def test_daily_hard_loss_halt_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, as_of_after: AsOf
) -> None:
    # A session loss beyond 1.5% of equity trips the hard halt.
    account = _account(as_of_after(triggered_series), session_pnl=Decimal("-20000"))
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.RISK
    assert "DAILY_HARD_HALT" in out.reason_codes


def test_active_entry_halt_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    halt = HaltRecord(
        scope=HaltScope.GLOBAL_ENTRY_HALT, environment=Environment.PAPER,
        target_ref=None, reason_code="TEST_HALT",
    )
    out = run_policy(triggered_series, active_halts=(halt,))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.RISK
    assert "HALT_ACTIVE" in out.reason_codes


# -- market-data / pre-submit safety -------------------------------------------
def test_do_not_chase_price_slippage_cancels(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    # A refreshed price far above the marketable-limit cap cancels (no chase).
    out = run_policy(triggered_series, market_price=Decimal("200"))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.PRE_SUBMIT
    assert "PRICE_SLIPPAGE_EXCEEDED" in out.reason_codes


def test_broker_unhealthy_cancels(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    out = run_policy(triggered_series, broker_healthy=False)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.PRE_SUBMIT
    assert "BROKER_UNHEALTHY" in out.reason_codes


def test_reconciliation_required_cancels(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    out = run_policy(triggered_series, reconciliation_required=True)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "RECONCILIATION_REQUIRED" in out.reason_codes


def test_missing_manual_approval_needs_review(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    out = run_policy(triggered_series, approved=False)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.PRE_SUBMIT


# -- sizing: risk decides, only reduces ----------------------------------------
def test_insufficient_risk_budget_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, as_of_after: AsOf
) -> None:
    tiny = _account(
        as_of_after(triggered_series),
        broker_net_liquidation_value=Decimal(1000),
        simulation_equity_cap=Decimal(1000),
    )
    out = run_policy(triggered_series, account=tiny)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.SIZING
    assert "INSUFFICIENT_RISK_BUDGET_FOR_ONE_SHARE" in out.reason_codes


def test_risk_reduces_but_never_increases_quantity(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    # An existing position consuming almost the whole open-risk envelope forces
    # the Risk Engine to REDUCE the candidate below its fixed-fractional request.
    held, record = _pair(555, "OTHER", open_risk="19900", notional="1000")
    out = run_policy(triggered_series, held_positions=(held,), system_records=(record,))
    assert out.result is MvpResult.EXECUTION_INTENT
    assert out.risk_result is not None
    assert out.risk_result.outcome is RiskDecisionOutcome.REDUCE
    plan = out.executable_plan
    assert plan is not None
    assert out.allocation is not None
    # allocation/plan never exceed the risk-approved quantity.
    assert out.allocation.allocated_quantity <= out.risk_result.approved_quantity
    assert plan.quantity <= out.risk_result.approved_quantity


# -- environment safety --------------------------------------------------------
def test_live_environment_is_refused(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    try:
        run_policy(triggered_series, environment=Environment.LIVE)
    except MvpTradingPolicyError:
        return
    raise AssertionError("a LIVE environment must be refused")
