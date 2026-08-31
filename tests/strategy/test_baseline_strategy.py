"""Non-vacuous / adversarial tests for the baseline strategy evaluator.

Every mandatory predicate is proven load-bearing: from a single triggering
fixture, perturbing exactly ONE predicate flips the outcome from TRADE to NO
TRADE, and a NO-TRADE evaluation never emits a TRIGGERED assessment (so it can
never be approved downstream).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from trading_application.baseline_strategy import (
    BaselineEvaluation,
    BaselineStrategyError,
    BaselineTrendBreakoutStrategy,
)
from trading_application.baseline_strategy_config import baseline_trend_breakout_v1_policy
from trading_contracts.enums import (
    AssessmentStatus,
    FreshnessClass,
    MarketSessionStatus,
    SignalDirection,
)
from trading_market_data_core.types import BarSeries

Spec = list[tuple[float, float]]
AsOf = Callable[[BarSeries], datetime]
Builder = Callable[..., BarSeries]
Mutator = Callable[[Spec], None]


def _strategy() -> BaselineTrendBreakoutStrategy:
    return BaselineTrendBreakoutStrategy(baseline_trend_breakout_v1_policy())


def _evaluate(
    series: BarSeries,
    instrument_id: uuid.UUID,
    as_of_after: AsOf,
    session: MarketSessionStatus | None = MarketSessionStatus.REGULAR,
) -> BaselineEvaluation:
    return _strategy().evaluate(
        instrument_id, bars=series, as_of=as_of_after(series), session=session
    )


def _mutate_and_eval(
    base_spec: Spec,
    build_series: Builder,
    instrument_id: uuid.UUID,
    as_of_after: AsOf,
    mutate: Mutator,
) -> BaselineEvaluation:
    mutate(base_spec)
    series = build_series(base_spec)
    return _evaluate(series, instrument_id, as_of_after)


# -- the valid, complete setup -------------------------------------------------
def test_valid_complete_setup_triggers(
    triggered_series: BarSeries, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    result = _evaluate(triggered_series, instrument_id, as_of_after)
    assert result.is_trade
    assert result.assessment.status is AssessmentStatus.TRIGGERED
    assert result.assessment.direction is SignalDirection.LONG
    assert result.components.all_satisfied
    assert result.no_trade_reasons == ()
    signal = result.signal
    assert signal is not None
    # structural relationships hold exactly (Decimal, no float drift)
    assert signal.structural_stop < signal.breakout_close < signal.max_entry_price
    assert signal.per_share_risk == signal.max_entry_price - signal.structural_stop
    assert signal.resistance < signal.breakout_close


def test_no_trade_never_emits_a_triggered_assessment(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    # Break the breakout: a NO-TRADE must never be TRIGGERED.
    base_spec[170] = (165.5, 2.2)  # below resistance
    series = build_series(base_spec)
    result = _evaluate(series, instrument_id, as_of_after)
    assert not result.is_trade
    assert result.signal is None
    assert result.assessment.status is not AssessmentStatus.TRIGGERED


# -- each mandatory predicate is independently load-bearing (mutation) ---------
def test_declining_trend_blocks(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    # A declining trend into the base makes SMA150 fall (and close drop below
    # it): the trend predicate is load-bearing regardless of the base/breakout.
    def mutate(spec: Spec) -> None:
        for i in range(151):
            spec[i] = (300 - i * 0.9, 2.0)  # 300 -> ~165, SMA falling

    result = _mutate_and_eval(base_spec, build_series, instrument_id, as_of_after, mutate)
    assert not result.is_trade
    assert not result.components.trend_ok
    assert any(r.startswith("TREND_") for r in result.no_trade_reasons)


def test_close_below_sma_blocks(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    # Push the trend far above the breakout level so close < SMA150(today).
    def mutate(spec: Spec) -> None:
        for i in range(151):
            spec[i] = (100 + i * 1.5, 2.0)  # ends ~325, SMA far above 166.5

    result = _mutate_and_eval(base_spec, build_series, instrument_id, as_of_after, mutate)
    assert not result.is_trade
    assert not result.components.trend_ok


def test_no_contracting_base_blocks(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    # Invert the base contraction: the most recent 5 days become the WIDEST.
    def mutate(spec: Spec) -> None:
        for i in range(165, 170):
            spec[i] = (165.0, 4.0)

    result = _mutate_and_eval(base_spec, build_series, instrument_id, as_of_after, mutate)
    assert not result.is_trade
    assert not result.components.contracting_base_ok
    assert "NO_CONTRACTING_BASE" in result.no_trade_reasons


def test_no_breakout_blocks(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    def mutate(spec: Spec) -> None:
        spec[170] = (165.5, 2.2)  # completed close below the base resistance

    result = _mutate_and_eval(base_spec, build_series, instrument_id, as_of_after, mutate)
    assert not result.is_trade
    assert not result.components.breakout_trigger_ok
    assert "NO_BREAKOUT_CLOSE_NOT_ABOVE_RESISTANCE" in result.no_trade_reasons


def test_risk_per_share_over_cap_blocks(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    # A breakout far above resistance makes EntryPrice - Stop exceed 2.5*ATR20.
    def mutate(spec: Spec) -> None:
        spec[170] = (185.0, 2.2)

    result = _mutate_and_eval(base_spec, build_series, instrument_id, as_of_after, mutate)
    assert not result.is_trade
    assert not result.components.risk_within_cap
    assert "RISK_PER_SHARE_EXCEEDS_CAP" in result.no_trade_reasons


# -- data / session / history guards -------------------------------------------
def test_insufficient_history_is_inconclusive(
    base_spec: Spec, build_series: Builder, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    short = build_series(base_spec[:100])
    result = _evaluate(short, instrument_id, as_of_after)
    assert not result.is_trade
    assert result.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert any(r.startswith("INSUFFICIENT_HISTORY") for r in result.no_trade_reasons)


def test_stale_last_bar_blocks(
    build_series: Builder, base_spec: Spec, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    series = build_series(base_spec, last_freshness=FreshnessClass.STALE)
    result = _evaluate(series, instrument_id, as_of_after)
    assert not result.is_trade
    assert result.assessment.status is AssessmentStatus.BLOCKED
    assert any(r.startswith("DATA_STALE") for r in result.no_trade_reasons)


def test_unknown_freshness_blocks(
    build_series: Builder, base_spec: Spec, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    series = build_series(base_spec, last_freshness=FreshnessClass.UNKNOWN)
    result = _evaluate(series, instrument_id, as_of_after)
    assert not result.is_trade
    assert result.assessment.status is AssessmentStatus.BLOCKED


@pytest.mark.parametrize(
    "session",
    [MarketSessionStatus.PRE_MARKET, MarketSessionStatus.AFTER_HOURS, MarketSessionStatus.HALTED],
)
def test_non_rth_session_blocks(
    triggered_series: BarSeries,
    instrument_id: uuid.UUID,
    as_of_after: AsOf,
    session: MarketSessionStatus,
) -> None:
    result = _evaluate(triggered_series, instrument_id, as_of_after, session=session)
    assert not result.is_trade
    assert result.assessment.status is AssessmentStatus.BLOCKED
    assert any(r.startswith("SESSION_NOT_RTH") for r in result.no_trade_reasons)


def test_lookahead_bar_is_a_hard_error(
    triggered_series: BarSeries, instrument_id: uuid.UUID
) -> None:
    # as_of BEFORE the last bar's observation => lookahead, fail closed.
    early = triggered_series.bars[-1].observed_at - timedelta(days=1)
    with pytest.raises(BaselineStrategyError):
        _strategy().evaluate(instrument_id, bars=triggered_series, as_of=early)


def test_wrong_instrument_is_rejected(triggered_series: BarSeries, as_of_after: AsOf) -> None:
    other = uuid.uuid5(uuid.NAMESPACE_DNS, "some-other-instrument")
    with pytest.raises(BaselineStrategyError):
        _strategy().evaluate(other, bars=triggered_series, as_of=as_of_after(triggered_series))


# -- determinism ---------------------------------------------------------------
def test_deterministic_same_inputs_same_output(
    triggered_series: BarSeries, instrument_id: uuid.UUID, as_of_after: AsOf
) -> None:
    first = _evaluate(triggered_series, instrument_id, as_of_after)
    second = _evaluate(triggered_series, instrument_id, as_of_after)
    assert first.candidate_id == second.candidate_id
    assert first.signal == second.signal
    assert first.assessment == second.assessment
