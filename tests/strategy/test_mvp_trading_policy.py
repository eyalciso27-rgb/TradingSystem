"""End-to-end + adversarial tests for the MVP trading policy orchestrator.

Proves the mission's conceptual path and its ownership boundaries, and that
every owner-frozen new-entry rule is load-bearing in this policy path: universe
eligibility, next-RTH entry timing, conId ownership, unknown-state gates,
Start-of-Day daily loss, GICS-sector caps, reduce-only sizing, canonical
lineage, and NO-TRADE-never-an-intent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time
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
from trading_application.universe_eligibility import UniverseFacts
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
UniverseF = Callable[..., UniverseFacts]
AccountF = Callable[..., MvpAccountSnapshot]
AtDay = Callable[[int, time], datetime]

CANDIDATE_SECTOR = "Information Technology"


def _pair(
    con_id: int,
    symbol: str,
    *,
    open_risk: str = "10",
    notional: str = "1000",
    sector: str = "Financials",
    cluster: str = "BANK",
) -> tuple[BrokerHeldPosition, SystemPositionRecord]:
    held = BrokerHeldPosition(
        con_id=con_id, symbol=symbol, quantity=Decimal(1),
        notional=Decimal(notional), observed_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    record = SystemPositionRecord(
        con_id=con_id, symbol=symbol, strategy_id="baseline-trend-breakout-v1",
        source_id="scanner", sector=sector, cluster=cluster,
        open_risk_amount=Decimal(open_risk), notional=Decimal(notional),
    )
    return held, record


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
    assert out.universe is not None and out.universe.gics_sector == CANDIDATE_SECTOR
    plan = out.executable_plan
    assert plan is not None
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
    assert out.risk_result is None
    assert out.decision is not None
    assert out.decision.result.outcome is not DecisionOutcome.APPROVE


# -- B3: universe / liquidity / price / spread eligibility ---------------------
def test_non_sp500_member_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, is_sp500_member=False)
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.UNIVERSE
    assert "NOT_SP500_MEMBER" in out.reason_codes
    assert out.risk_result is None


def test_membership_unknown_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, is_sp500_member=None)
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.UNIVERSE
    assert "SP500_MEMBERSHIP_UNKNOWN" in out.reason_codes


def test_adv20_below_min_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series,
        universe=universe_facts(observed_at=observed, adv20_usd=Decimal("40000000")),
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "ADV20_BELOW_MIN" in out.reason_codes


def test_adv20_unknown_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, adv20_usd=None)
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "ADV20_UNKNOWN" in out.reason_codes


def test_price_below_min_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, last_price=Decimal("9"))
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "PRICE_BELOW_MIN" in out.reason_codes


def test_price_unknown_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, last_price=None)
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "PRICE_UNKNOWN" in out.reason_codes


def test_spread_too_wide_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, spread_bps=Decimal("11"))
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "SPREAD_TOO_WIDE" in out.reason_codes


def test_spread_unknown_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, spread_bps=None)
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "SPREAD_UNKNOWN" in out.reason_codes


def test_gics_sector_unknown_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series, universe=universe_facts(observed_at=observed, gics_sector=None)
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.UNIVERSE
    assert "GICS_SECTOR_UNKNOWN" in out.reason_codes


def test_membership_not_point_in_time_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, universe_facts: UniverseF
) -> None:
    observed = triggered_series.bars[-1].observed_at
    out = run_policy(
        triggered_series,
        universe=universe_facts(observed_at=observed, membership_is_point_in_time=False),
    )
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "MEMBERSHIP_NOT_POINT_IN_TIME" in out.reason_codes


# -- B7: next-RTH entry timing -------------------------------------------------
def test_after_close_same_day_is_blocked(
    run_policy: RunPolicy, triggered_series: BarSeries, at_trading_day: AtDay
) -> None:
    # After the breakout close, same day (after-hours) — the strategy's own RTH
    # session gate blocks a new-entry signal before entry timing is reached.
    # (Same-breakout-session RTH entry is not representable once bars complete at
    # the RTH close; the entry-timing ENTRY_SAME_BREAKOUT_SESSION path is covered
    # directly in tests/strategy/test_mvp_safety_gates.py.)
    after_close = at_trading_day(len(triggered_series.bars) - 1, time(16, 30))
    out = run_policy(triggered_series, as_of=after_close)
    assert out.result is MvpResult.NO_TRADE
    assert any(r.startswith("SESSION_NOT_RTH") for r in out.reason_codes)


def test_next_rth_session_is_eligible(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    out = run_policy(triggered_series)  # default as_of = next trading day 10:00 ET
    assert out.result is MvpResult.EXECUTION_INTENT


def test_two_sessions_later_is_blocked(
    run_policy: RunPolicy, triggered_series: BarSeries, at_trading_day: AtDay
) -> None:
    # The session AFTER the next one is no longer the NEXT RTH session.
    two_later = at_trading_day(len(triggered_series.bars) + 1, time(10, 0))
    out = run_policy(triggered_series, as_of=two_later)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ENTRY_TIMING
    assert "ENTRY_NOT_IN_NEXT_RTH_SESSION" in out.reason_codes


# -- position attribution gate -------------------------------------------------
def test_unattributable_position_blocks_entry(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    stray = BrokerHeldPosition(
        con_id=777, symbol="STRAY", quantity=Decimal(5),
        notional=Decimal(5000), observed_at=datetime(2024, 1, 1, tzinfo=UTC),
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
        h, r = _pair(600 + i, f"SYM{i}", sector=f"SECTOR{i}")
        held.append(h)
        records.append(r)
    out = run_policy(triggered_series, held_positions=tuple(held), system_records=tuple(records))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.RISK
    assert "MAX_POSITIONS_REACHED" in out.reason_codes


# -- B4/B5: GICS-sector caps ---------------------------------------------------
def test_two_same_sector_positions_still_allowed(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    held: list[BrokerHeldPosition] = []
    records: list[SystemPositionRecord] = []
    for i in range(2):  # 2 in the candidate's sector => count gate passes
        h, r = _pair(700 + i, f"IT{i}", sector=CANDIDATE_SECTOR, cluster=f"CL{i}", open_risk="50")
        held.append(h)
        records.append(r)
    out = run_policy(triggered_series, held_positions=tuple(held), system_records=tuple(records))
    assert out.result is MvpResult.EXECUTION_INTENT


def test_three_same_sector_positions_block(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    held: list[BrokerHeldPosition] = []
    records: list[SystemPositionRecord] = []
    for i in range(3):  # already 3 in the candidate's GICS sector
        h, r = _pair(710 + i, f"IT{i}", sector=CANDIDATE_SECTOR, cluster=f"CL{i}", open_risk="50")
        held.append(h)
        records.append(r)
    out = run_policy(triggered_series, held_positions=tuple(held), system_records=tuple(records))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.SECTOR
    assert "SECTOR_MAX_POSITIONS_REACHED" in out.reason_codes


def test_three_positions_in_other_sector_do_not_affect_candidate(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    held: list[BrokerHeldPosition] = []
    records: list[SystemPositionRecord] = []
    for i in range(3):
        h, r = _pair(720 + i, f"FN{i}", sector="Financials", cluster=f"BK{i}", open_risk="50")
        held.append(h)
        records.append(r)
    out = run_policy(triggered_series, held_positions=tuple(held), system_records=tuple(records))
    assert out.result is MvpResult.EXECUTION_INTENT


def test_sector_planned_risk_cap_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    # Existing same-sector risk (aggregating clusters) close to 0.75% of 1M
    # (=7500); the candidate's ~2500 approved risk pushes it over.
    h1, r1 = _pair(730, "IT_A", sector=CANDIDATE_SECTOR, cluster="CL_A", open_risk="3000")
    h2, r2 = _pair(731, "IT_B", sector=CANDIDATE_SECTOR, cluster="CL_B", open_risk="3000")
    out = run_policy(triggered_series, held_positions=(h1, h2), system_records=(r1, r2))
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.SECTOR
    assert "SECTOR_PLANNED_RISK_CAP_EXCEEDED" in out.reason_codes


# -- B2: P&L usable only with PNL_OK -------------------------------------------
def test_finite_pnl_without_status_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF,
    next_rth_as_of: AsOf,
) -> None:
    account = account_ok(next_rth_as_of(triggered_series), session_pnl_status=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE
    assert any(r.startswith("SESSION_PNL_NOT_OK") for r in out.reason_codes)


def test_finite_pnl_stale_status_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF,
    next_rth_as_of: AsOf,
) -> None:
    account = account_ok(
        next_rth_as_of(triggered_series), session_pnl_status=SessionPnlStatus.STALE
    )
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE


def test_zero_pnl_without_status_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF,
    next_rth_as_of: AsOf,
) -> None:
    account = account_ok(
        next_rth_as_of(triggered_series), session_pnl=Decimal("0"), session_pnl_status=None
    )
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE


def test_zero_pnl_with_ok_status_is_valid(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF,
    next_rth_as_of: AsOf,
) -> None:
    account = account_ok(
        next_rth_as_of(triggered_series), session_pnl=Decimal("0"),
        session_pnl_status=SessionPnlStatus.OK,
    )
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.EXECUTION_INTENT


def test_unknown_session_pnl_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    account = account_ok(
        next_rth_as_of(triggered_series), session_pnl=None, session_pnl_status=None
    )
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE
    assert "SESSION_PNL_UNKNOWN" in out.reason_codes


def test_unknown_nlv_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    account = account_ok(next_rth_as_of(triggered_series), broker_net_liquidation_value=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "NLV_UNKNOWN" in out.reason_codes


def test_unknown_equity_cap_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    account = account_ok(next_rth_as_of(triggered_series), simulation_equity_cap=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert "EQUITY_CAP_UNKNOWN" in out.reason_codes


# -- B6: daily loss vs Start-of-Day equity ------------------------------------
def test_unknown_start_of_day_equity_blocks(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    account = account_ok(next_rth_as_of(triggered_series), start_of_day_equity=None)
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.ACCOUNT_STATE
    assert "START_OF_DAY_EQUITY_UNKNOWN" in out.reason_codes


def test_daily_loss_uses_sod_equity_not_current_nlv(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    # 1.6% of SOD equity is a breach even though the loss is a tiny fraction of
    # a much larger CURRENT equity — proving the SOD denominator is used.
    account = account_ok(
        next_rth_as_of(triggered_series),
        session_pnl=Decimal("-16000"),
        start_of_day_equity=Decimal(1_000_000),
        broker_net_liquidation_value=Decimal(10_000_000),
        simulation_equity_cap=Decimal(10_000_000),
    )
    out = run_policy(triggered_series, account=account)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.DAILY_LOSS
    assert "DAILY_LOSS_LIMIT_REACHED_SOD" in out.reason_codes


def test_changing_current_nlv_does_not_move_sod_threshold(
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    # Loss is 1.0% of SOD (below 1.5%): the trade proceeds regardless of NLV.
    as_of = next_rth_as_of(triggered_series)
    for nlv in (Decimal(1_000_000), Decimal(5_000_000)):
        account = account_ok(
            as_of, session_pnl=Decimal("-10000"),
            start_of_day_equity=Decimal(1_000_000),
            broker_net_liquidation_value=nlv, simulation_equity_cap=nlv,
        )
        out = run_policy(triggered_series, account=account)
        assert out.result is MvpResult.EXECUTION_INTENT


# -- halts ---------------------------------------------------------------------
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
    run_policy: RunPolicy, triggered_series: BarSeries, account_ok: AccountF, next_rth_as_of: AsOf
) -> None:
    tiny = account_ok(
        next_rth_as_of(triggered_series),
        broker_net_liquidation_value=Decimal(1000),
        simulation_equity_cap=Decimal(1000),
        start_of_day_equity=Decimal(1000),
        session_pnl=Decimal("0"),  # no daily loss, so sizing is the binding gate
    )
    out = run_policy(triggered_series, account=tiny)
    assert out.result is MvpResult.NO_NEW_ENTRY
    assert out.stage is MvpStage.SIZING
    assert "INSUFFICIENT_RISK_BUDGET_FOR_ONE_SHARE" in out.reason_codes


def test_risk_reduces_but_never_increases_quantity(
    run_policy: RunPolicy, triggered_series: BarSeries
) -> None:
    # An existing OTHER-sector position consuming almost the whole open-risk
    # envelope forces the Risk Engine to REDUCE the candidate.
    held, record = _pair(555, "OTHER", open_risk="19900", notional="1000", sector="Energy")
    out = run_policy(triggered_series, held_positions=(held,), system_records=(record,))
    assert out.result is MvpResult.EXECUTION_INTENT
    assert out.risk_result is not None
    assert out.risk_result.outcome is RiskDecisionOutcome.REDUCE
    plan = out.executable_plan
    assert plan is not None
    assert out.allocation is not None
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
