"""Market regime engine v1: golden fixtures, explicit UNKNOWN, no lookahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.regime_engine import (
    ENGINE_MODEL_VERSION,
    INDICATOR_ATR_RATIO,
    INDICATOR_BREADTH,
    INDICATOR_CORRELATION,
    INDICATOR_TREND,
    MarketRegimeEngine,
    RegimeAssessment,
    RegimeEngineError,
    RegimeIndicator,
    RegimePolicy,
)
from trading_contracts.enums import CalibrationStatus, FreshnessClass, MarketRegime

AS_OF = datetime(2026, 7, 8, 20, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=10)

POLICY = RegimePolicy(
    policy_version="regime-policy-test-1",
    high_vol_min_atr_ratio=Decimal("0.05"),
    risk_off_max_breadth=Decimal("0.35"),
    risk_off_min_correlation=Decimal("0.8"),
    trend_min_change=Decimal("0.03"),
    trend_min_breadth=Decimal("0.6"),
    range_max_abs_change=Decimal("0.01"),
    validity_minutes=30,
)

ENGINE = MarketRegimeEngine(POLICY)


def indicator(
    name: str,
    value: str,
    *,
    freshness: FreshnessClass = FreshnessClass.NEAR_REALTIME,
    observed_at: datetime = EARLIER,
) -> RegimeIndicator:
    return RegimeIndicator(
        name=name,
        value=Decimal(value),
        provider_id="prov-regime-fixture",
        observed_at=observed_at,
        freshness=freshness,
    )


def indicators(
    *, breadth: str, trend: str, atr: str, correlation: str
) -> tuple[RegimeIndicator, ...]:
    return (
        indicator(INDICATOR_BREADTH, breadth),
        indicator(INDICATOR_TREND, trend),
        indicator(INDICATOR_ATR_RATIO, atr),
        indicator(INDICATOR_CORRELATION, correlation),
    )


def classify(
    inds: tuple[RegimeIndicator, ...], *, event_stress: bool = False
) -> RegimeAssessment:
    return ENGINE.classify(inds, as_of=AS_OF, event_stress_active=event_stress)


# -- golden fixtures -----------------------------------------------------------


def test_trend_regime_fixture() -> None:
    result = classify(indicators(breadth="0.75", trend="0.05", atr="0.02", correlation="0.4"))
    assert result.regime is MarketRegime.TRENDING_RISK_ON
    assert result.score == Decimal("1")
    assert "TREND_POSITIVE" in result.reason_codes
    assert "BREADTH_POSITIVE" in result.reason_codes
    assert result.model_version == ENGINE_MODEL_VERSION
    assert result.confidence_state is CalibrationStatus.HEURISTIC
    assert result.valid_until == AS_OF + timedelta(minutes=30)


def test_range_regime_fixture() -> None:
    result = classify(indicators(breadth="0.5", trend="0.005", atr="0.02", correlation="0.4"))
    assert result.regime is MarketRegime.RANGE_BOUND
    assert "TREND_WEAK_WITHIN_RANGE_BAND" in result.reason_codes


def test_high_volatility_dominates_trend() -> None:
    result = classify(indicators(breadth="0.75", trend="0.05", atr="0.08", correlation="0.4"))
    assert result.regime is MarketRegime.HIGH_VOLATILITY  # severity precedence
    assert "ATR_RATIO_ABOVE_BAND" in result.reason_codes


def test_risk_off_fixture() -> None:
    result = classify(indicators(breadth="0.2", trend="-0.02", atr="0.02", correlation="0.9"))
    assert result.regime is MarketRegime.RISK_OFF
    assert result.score == Decimal("1")  # both conditions met
    assert "BREADTH_NEGATIVE" in result.reason_codes
    assert "CORRELATION_ELEVATED" in result.reason_codes


def test_event_stress_input_dominates_everything() -> None:
    result = classify(
        indicators(breadth="0.75", trend="0.05", atr="0.08", correlation="0.9"),
        event_stress=True,
    )
    assert result.regime is MarketRegime.EVENT_STRESS
    assert "EVENT_STRESS_INPUT_ACTIVE" in result.reason_codes


# -- UNKNOWN handling (DoD) -------------------------------------------------------


def test_insufficient_data_is_explicit_unknown() -> None:
    result = ENGINE.classify((), as_of=AS_OF)
    assert result.regime is MarketRegime.UNKNOWN
    assert result.score == Decimal("0")
    assert "REGIME_UNDETERMINED" in result.reason_codes
    assert "INDICATOR_MISSING_BREADTH" in result.reason_codes


def test_stale_indicators_never_produce_a_positive_regime() -> None:
    stale = tuple(
        indicator(name, value, freshness=FreshnessClass.STALE)
        for name, value in (
            (INDICATOR_BREADTH, "0.75"),
            (INDICATOR_TREND, "0.05"),
            (INDICATOR_ATR_RATIO, "0.02"),
            (INDICATOR_CORRELATION, "0.4"),
        )
    )
    result = classify(stale)
    assert result.regime is MarketRegime.UNKNOWN
    assert "INDICATOR_FRESHNESS_BREADTH" in result.reason_codes


def test_contradictory_partial_data_is_unknown_not_guessed() -> None:
    # trend strong but breadth missing: the trend rule cannot fire; trend is
    # above the range band, so no rule concludes -> UNKNOWN.
    result = classify((indicator(INDICATOR_TREND, "0.05"),))
    assert result.regime is MarketRegime.UNKNOWN


# -- policy / point-in-time / determinism -------------------------------------------


def test_missing_or_invalid_policy_fails_closed() -> None:
    with pytest.raises(RegimeEngineError, match="policy_version"):
        RegimePolicy(
            policy_version=" ",
            high_vol_min_atr_ratio=Decimal("0.05"),
            risk_off_max_breadth=Decimal("0.35"),
            risk_off_min_correlation=Decimal("0.8"),
            trend_min_change=Decimal("0.03"),
            trend_min_breadth=Decimal("0.6"),
            range_max_abs_change=Decimal("0.01"),
            validity_minutes=30,
        )
    with pytest.raises(RegimeEngineError, match="range band"):
        RegimePolicy(
            policy_version="x",
            high_vol_min_atr_ratio=Decimal("0.05"),
            risk_off_max_breadth=Decimal("0.35"),
            risk_off_min_correlation=Decimal("0.8"),
            trend_min_change=Decimal("0.01"),
            trend_min_breadth=Decimal("0.6"),
            range_max_abs_change=Decimal("0.02"),  # overlaps trend threshold
            validity_minutes=30,
        )
    with pytest.raises(TypeError):
        RegimePolicy(policy_version="x")  # type: ignore[call-arg]


def test_lookahead_indicator_rejected() -> None:
    future = indicator(INDICATOR_BREADTH, "0.7", observed_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(RegimeEngineError, match="lookahead"):
        ENGINE.classify((future,), as_of=AS_OF)


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        indicator(INDICATOR_BREADTH, "0.7", observed_at=datetime(2026, 7, 8, 19, 0))  # noqa: DTZ001
    with pytest.raises(RegimeEngineError, match="timezone-aware"):
        ENGINE.classify((), as_of=datetime(2026, 7, 8, 20, 0))  # noqa: DTZ001


def test_duplicate_indicator_rejected() -> None:
    duplicated = (indicator(INDICATOR_BREADTH, "0.7"), indicator(INDICATOR_BREADTH, "0.8"))
    with pytest.raises(RegimeEngineError, match="duplicate indicator"):
        ENGINE.classify(duplicated, as_of=AS_OF)


def test_same_inputs_same_result_with_evidence() -> None:
    inds = indicators(breadth="0.75", trend="0.05", atr="0.02", correlation="0.4")
    first = classify(inds)
    second = classify(inds)
    assert first == second
    assert f"policy:{POLICY.policy_version}" in first.evidence
    assert any(
        ref.startswith("indicator:breadth=0.75@prov-regime-fixture") for ref in first.evidence
    )
    assert first.observed_at == EARLIER and first.as_of == AS_OF


def test_engine_creates_no_candidate_decision_or_order() -> None:
    import trading_application.regime_engine as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        forbidden_imports = (
            "trading_persistence", "httpx", "requests", "subprocess",
            "TradeCandidate", "DecisionResult", "OrderIntent", "broker",
        )
        for name in forbidden_imports:
            assert name not in line, line
