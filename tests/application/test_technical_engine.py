"""Technical engine v1: golden fixtures, fail-closed, determinism (IMP-0801)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.technical_engine import (
    TechnicalEngine,
    TechnicalEngineError,
    TechnicalEvaluation,
    TechnicalPolicy,
)
from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    FreshnessClass,
    MarketSessionStatus,
    SignalDirection,
    SignalFamily,
)
from trading_market_data_core import Bar, BarSeries, FeatureSnapshot, FeatureValue
from trading_market_data_core.snapshot import FEATURE_AVAILABLE, FEATURE_UNAVAILABLE

INSTRUMENT = UUID("00000000-0000-0000-0000-000000000001")
AS_OF = datetime(2026, 7, 6, 20, 0, tzinfo=UTC)

# Golden policy — configuration input; the engine has no built-in numbers.
POLICY = TechnicalPolicy(
    policy_version="tech-policy-test-1",
    level_lookback_bars=5,
    trend_lookback_bars=3,
    min_relative_volume=Decimal("1.5"),
    max_spread=Decimal("0.20"),
    max_atr_extension=Decimal("2"),
    min_trend_change=Decimal("0.02"),
    weight_breakout=Decimal("2"),
    weight_volume=Decimal("1"),
    weight_trend=Decimal("1"),
)


def bars_from_closes(closes: list[str], *, volume: str = "10000") -> BarSeries:
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        start = datetime(2026, 6, 20, 13, 30, tzinfo=UTC) + timedelta(days=index)
        close_d = Decimal(close)
        bars.append(
            Bar(
                instrument_id=str(INSTRUMENT),
                provider_id="prov-fixture",
                interval="1d",
                open=close_d,
                high=close_d + Decimal("1"),
                low=close_d - Decimal("1"),
                close=close_d,
                volume=Decimal(volume),
                start_at=start,
                end_at=start + timedelta(hours=6, minutes=30),
                observed_at=start + timedelta(hours=6, minutes=30),
                freshness=FreshnessClass.DELAYED,
            )
        )
    return BarSeries(
        instrument_id=str(INSTRUMENT),
        provider_id="prov-fixture",
        interval="1d",
        bars=tuple(bars),
    )


def feature(
    name: str,
    value: str,
    *,
    freshness: FreshnessClass = FreshnessClass.REALTIME,
    status: str = FEATURE_AVAILABLE,
) -> FeatureValue:
    return FeatureValue(
        name=name,
        status=status,
        formula_version=f"{name}-v1",
        value=Decimal(value) if status == FEATURE_AVAILABLE else None,
        source_provider_id="prov-fixture",
        source_observed_at=AS_OF - timedelta(minutes=5),
        freshness=freshness,
        reason="" if status == FEATURE_AVAILABLE else "fixture unavailable",
    )


def snapshot(
    *,
    rvol: str = "2.0",
    spread: str = "0.05",
    atr: str = "1.5",
    overrides: tuple[FeatureValue, ...] = (),
) -> FeatureSnapshot:
    named = {f.name: f for f in overrides}
    features = tuple(
        named.get(name, feature(name, default))
        for name, default in (
            ("relative_volume", rvol),
            ("spread", spread),
            ("atr", atr),
        )
    )
    return FeatureSnapshot(
        instrument_id=str(INSTRUMENT), as_of=AS_OF, features=features
    )


# Flat history 100 with final bar variations. Prior highs = 101 (close+1).
FLAT = ["100"] * 7


def evaluate(
    closes: list[str],
    snap: FeatureSnapshot | None = None,
    *,
    session: MarketSessionStatus | None = None,
) -> TechnicalEvaluation:
    engine = TechnicalEngine(POLICY)
    return engine.evaluate(
        INSTRUMENT,
        snapshot=snap if snap is not None else snapshot(),
        bars=bars_from_closes(closes),
        as_of=AS_OF,
        session=session,
    )


# -- golden fixtures -----------------------------------------------------------


def test_breakout_fixture_triggers_long() -> None:
    # last close 102 > prior resistance 101, rvol 2.0 >= 1.5
    result = evaluate([*FLAT, "102"])
    assessment = result.assessment
    assert assessment.status is AssessmentStatus.TRIGGERED
    assert assessment.direction is SignalDirection.LONG
    assert "BREAKOUT_CONFIRMED" in result.reason_codes
    assert assessment.signal_family is SignalFamily.TECHNICAL
    assert result.resistance_level == Decimal("101")
    assert result.support_level == Decimal("99")
    # score = (breakout 2 + volume 1 + 0 trend... trend: 102/100 = +2% >= 2% -> trend_up)
    assert assessment.score == Decimal("1")  # all components satisfied: (2+1+1)/4


def test_breakdown_fixture_triggers_negative() -> None:
    result = evaluate([*FLAT, "97"])  # below support 99, volume confirmed
    assert result.assessment.status is AssessmentStatus.TRIGGERED
    assert result.assessment.direction is SignalDirection.NEGATIVE
    assert "BREAKDOWN_CONFIRMED" in result.reason_codes


def test_volume_without_breakout_is_not_triggered() -> None:
    result = evaluate([*FLAT, "100.5"])  # inside range, rvol high, trend < 2%
    assert result.assessment.status is AssessmentStatus.ELIGIBLE
    assert result.assessment.direction is SignalDirection.NEUTRAL
    assert "NO_TECHNICAL_SIGNAL" in result.reason_codes
    assert dict(result.components)["volume_confirmed"] is True
    # only the volume component satisfied: 1/4
    assert result.assessment.score == Decimal("0.25")


def test_breakout_without_volume_stays_eligible() -> None:
    result = evaluate([*FLAT, "102"], snapshot(rvol="1.0"))
    assert result.assessment.status is AssessmentStatus.ELIGIBLE  # no volume confirm
    assert dict(result.components)["breakout_up"] is True
    assert "TREND_UP" in result.reason_codes  # +2% trend keeps LONG eligibility
    assert result.assessment.direction is SignalDirection.LONG


def test_trend_fixture_eligible_long() -> None:
    result = evaluate(["100", "100", "100", "100", "100", "100.5", "101"])
    # close 101 <= resistance 101.5: no breakout; trend vs 3 bars ago: 101/100 = +1%?
    # 101 / 100 (bars[-4].close=100) = +1% < 2% -> neutral; craft stronger trend:
    strong = evaluate(["96", "96", "96", "96", "100", "100.4", "100.9"])
    assert strong.assessment.status is AssessmentStatus.ELIGIBLE
    assert strong.assessment.direction is SignalDirection.LONG
    assert "TREND_UP" in strong.reason_codes
    assert result.assessment.status is AssessmentStatus.ELIGIBLE


def test_false_breakout_risk_blocks_trigger() -> None:
    # extension beyond resistance: close 106, resistance 101, atr 1.5 ->
    # extension 5/1.5 = 3.33 > max 2 -> FALSE_BREAKOUT_RISK, no trigger
    result = evaluate([*FLAT, "106"])
    assert "FALSE_BREAKOUT_RISK" in result.reason_codes
    assert result.assessment.status is AssessmentStatus.ELIGIBLE
    assert "FALSE_BREAKOUT_RISK" in result.assessment.risk_flags


def test_wide_spread_blocks() -> None:
    result = evaluate([*FLAT, "102"], snapshot(spread="0.50"))
    assert result.assessment.status is AssessmentStatus.BLOCKED
    assert "WIDE_SPREAD" in result.reason_codes


def test_non_regular_session_blocks() -> None:
    result = evaluate([*FLAT, "102"], session=MarketSessionStatus.CLOSED)
    assert result.assessment.status is AssessmentStatus.BLOCKED
    assert "SESSION_CLOSED" in result.reason_codes


# -- fail-closed ---------------------------------------------------------------


def test_missing_feature_is_inconclusive_never_positive() -> None:
    unavailable = FeatureValue(
        name="spread",
        status=FEATURE_UNAVAILABLE,
        formula_version="spread-v1",
        reason="quote unavailable",
    )
    result = evaluate([*FLAT, "102"], snapshot(overrides=(unavailable,)))
    assert result.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert result.assessment.direction is SignalDirection.NEUTRAL
    assert "FEATURE_UNAVAILABLE_SPREAD" in result.reason_codes


def test_stale_freshness_never_triggers() -> None:
    stale = feature("relative_volume", "5.0", freshness=FreshnessClass.STALE)
    result = evaluate([*FLAT, "102"], snapshot(overrides=(stale,)))
    assert result.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert "FEATURE_FRESHNESS_RELATIVE_VOLUME" in result.reason_codes


def test_insufficient_bars_inconclusive() -> None:
    result = evaluate(["100", "101"])
    assert result.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert "INSUFFICIENT_BARS" in result.reason_codes


def test_missing_policy_fails_closed() -> None:
    with pytest.raises(TechnicalEngineError, match="policy_version"):
        TechnicalPolicy(
            policy_version="  ",
            level_lookback_bars=5,
            trend_lookback_bars=3,
            min_relative_volume=Decimal("1.5"),
            max_spread=Decimal("0.2"),
            max_atr_extension=Decimal("2"),
            min_trend_change=Decimal("0.02"),
            weight_breakout=Decimal("2"),
            weight_volume=Decimal("1"),
            weight_trend=Decimal("1"),
        )
    with pytest.raises(TypeError):
        TechnicalPolicy(policy_version="x")  # type: ignore[call-arg]


# -- determinism / point-in-time ------------------------------------------------


def test_same_inputs_same_assessment() -> None:
    first = evaluate([*FLAT, "102"])
    second = evaluate([*FLAT, "102"])
    assert first.assessment == second.assessment
    assert first.reason_codes == second.reason_codes


def test_lookahead_bar_rejected() -> None:
    series = bars_from_closes([*FLAT, "102"])
    late_bar = series.bars[-1]
    object.__setattr__(late_bar, "observed_at", AS_OF + timedelta(minutes=1))
    with pytest.raises(TechnicalEngineError, match="lookahead"):
        TechnicalEngine(POLICY).evaluate(
            INSTRUMENT, snapshot=snapshot(), bars=series, as_of=AS_OF
        )


def test_future_snapshot_rejected() -> None:
    future_snapshot = FeatureSnapshot(
        instrument_id=str(INSTRUMENT),
        as_of=AS_OF + timedelta(minutes=1),
        features=snapshot().features,
    )
    with pytest.raises(TechnicalEngineError, match="future"):
        evaluate([*FLAT, "102"], future_snapshot)


def test_naive_as_of_rejected() -> None:
    with pytest.raises(TechnicalEngineError, match="timezone-aware"):
        TechnicalEngine(POLICY).evaluate(
            INSTRUMENT,
            snapshot=snapshot(),
            bars=bars_from_closes([*FLAT, "102"]),
            as_of=datetime(2026, 7, 6, 20, 0),  # noqa: DTZ001
        )


def test_instrument_mismatch_rejected() -> None:
    with pytest.raises(TechnicalEngineError, match="different instrument"):
        TechnicalEngine(POLICY).evaluate(
            UUID("00000000-0000-0000-0000-000000000002"),
            snapshot=snapshot(),
            bars=bars_from_closes([*FLAT, "102"]),
            as_of=AS_OF,
        )


# -- output shape ----------------------------------------------------------------


def test_assessment_is_canonical_and_explains_itself() -> None:
    result = evaluate([*FLAT, "102"])
    assessment = result.assessment
    assert assessment.instrument_id == INSTRUMENT
    assert assessment.model_id == "technical-engine"
    assert assessment.calibration_status is CalibrationStatus.UNVERIFIED
    assert assessment.available_at == AS_OF
    assert assessment.observed_at <= AS_OF
    assert f"policy:{POLICY.policy_version}" in assessment.evidence_refs
    assert any(ref.startswith("snapshot:") for ref in assessment.evidence_refs)
    assert any(ref.startswith("bars:prov-fixture") for ref in assessment.evidence_refs)
    assert result.components  # dashboard/debug explanation present


def test_engine_creates_no_candidate_decision_or_order() -> None:
    import trading_application.technical_engine as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden_import in ("trading_persistence", "httpx", "requests", "broker"):
            assert forbidden_import not in line, line
        # the engine may import the SignalAssessment contract but never the
        # candidate/decision/order construction machinery
        for forbidden_name in ("TradeCandidate", "DecisionResult", "OrderIntent"):
            assert forbidden_name not in line, line
