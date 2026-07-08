"""Fundamental engine v1: lineage, missing-data, golden fixtures (IMP-0802)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.fundamental_engine import (
    FACTOR_OK,
    FACTOR_UNAVAILABLE,
    Comparator,
    Criterion,
    FactorResult,
    FundamentalEngine,
    FundamentalEngineError,
    FundamentalEvaluation,
    FundamentalField,
    FundamentalPolicy,
)
from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    FreshnessClass,
    SignalDirection,
    SignalFamily,
)

INSTRUMENT = UUID("00000000-0000-0000-0000-000000000011")
AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
AVAILABLE = AS_OF - timedelta(days=3)

# Golden policy — configuration input; the engine holds no numbers.
POLICY = FundamentalPolicy(
    policy_version="fund-policy-test-1",
    factors=(
        (
            "quality",
            (
                Criterion("roe", Comparator.GTE, Decimal("0.15"), Decimal("1")),
                Criterion("gross_margin", Comparator.GTE, Decimal("0.40"), Decimal("1")),
            ),
        ),
        ("growth", (Criterion("revenue_growth", Comparator.GTE, Decimal("0.10"), Decimal("2")),)),
        ("valuation", (Criterion("pe_ratio", Comparator.LTE, Decimal("25"), Decimal("1")),)),
        (
            "financial_risk",
            (Criterion("debt_to_equity", Comparator.LTE, Decimal("1.0"), Decimal("1")),),
        ),
    ),
    min_positive_score=Decimal("0.75"),
    max_negative_score=Decimal("0.25"),
)


def field(
    name: str,
    value: str,
    *,
    available_at: datetime = AVAILABLE,
    freshness: FreshnessClass = FreshnessClass.NEAR_REALTIME,
    provider: str = "prov-fund-fixture",
    fiscal: str | None = "2026Q2",
) -> FundamentalField:
    return FundamentalField(
        name=name,
        value=Decimal(value),
        provider_id=provider,
        available_at=available_at,
        fiscal_period=fiscal,
        freshness=freshness,
    )


GOOD_FIELDS = (
    field("roe", "0.22"),
    field("gross_margin", "0.55"),
    field("revenue_growth", "0.18"),
    field("pe_ratio", "18"),
    field("debt_to_equity", "0.4"),
)


def evaluate(fields: tuple[FundamentalField, ...]) -> FundamentalEvaluation:
    return FundamentalEngine(POLICY).evaluate(INSTRUMENT, fields=fields, as_of=AS_OF)


def factor(evaluation: FundamentalEvaluation, name: str) -> FactorResult:
    return next(f for f in evaluation.factors if f.name == name)


# -- golden fixtures -----------------------------------------------------------


def test_all_factors_favorable_gives_long_eligible() -> None:
    evaluation = evaluate(GOOD_FIELDS)
    assessment = evaluation.assessment
    assert assessment.signal_family is SignalFamily.FUNDAMENTAL
    assert assessment.status is AssessmentStatus.ELIGIBLE
    assert assessment.direction is SignalDirection.LONG
    assert assessment.score == Decimal("1")
    assert "FUNDAMENTALS_FAVORABLE" in evaluation.reason_codes
    for name in ("quality", "growth", "valuation", "financial_risk"):
        result = factor(evaluation, name)
        assert result.status == FACTOR_OK and result.score == Decimal("1")


def test_quality_fixture_partial_score() -> None:
    fields = (
        field("roe", "0.22"),
        field("gross_margin", "0.20"),  # fails the 0.40 bar
        field("revenue_growth", "0.18"),
        field("pe_ratio", "18"),
        field("debt_to_equity", "0.4"),
    )
    evaluation = evaluate(fields)
    assert factor(evaluation, "quality").score == Decimal("0.5")
    # aggregate: quality 0.5*2 + growth 1*2 + valuation 1*1 + risk 1*1 = 5 / 6
    assert evaluation.assessment.score == Decimal(5) / Decimal(6)


def test_growth_and_valuation_fixtures_negative_direction() -> None:
    fields = (
        field("roe", "0.05"),
        field("gross_margin", "0.10"),
        field("revenue_growth", "-0.10"),
        field("pe_ratio", "80"),
        field("debt_to_equity", "3.5"),
    )
    evaluation = evaluate(fields)
    assert evaluation.assessment.direction is SignalDirection.NEGATIVE
    assert evaluation.assessment.score == Decimal("0")
    assert "FUNDAMENTALS_UNFAVORABLE" in evaluation.reason_codes
    assert factor(evaluation, "financial_risk").score == Decimal("0")


def test_financial_risk_fixture_expected() -> None:
    fields = (*GOOD_FIELDS[:4], field("debt_to_equity", "2.0"))
    evaluation = evaluate(fields)
    assert factor(evaluation, "financial_risk").score == Decimal("0")
    assert evaluation.assessment.direction is SignalDirection.LONG  # 5/6 > 0.75


# -- missing / stale data --------------------------------------------------------


def test_missing_field_is_explicit_not_silent_zero() -> None:
    fields = tuple(f for f in GOOD_FIELDS if f.name != "pe_ratio")
    evaluation = evaluate(fields)
    valuation = factor(evaluation, "valuation")
    assert valuation.status == FACTOR_UNAVAILABLE
    assert valuation.score is None  # never a silent 0
    assert "MISSING_FIELD_PE_RATIO" in valuation.reason_codes
    assert evaluation.assessment.status is AssessmentStatus.INCONCLUSIVE
    assert evaluation.assessment.direction is SignalDirection.NEUTRAL
    assert "FUNDAMENTALS_INCOMPLETE" in evaluation.reason_codes


def test_stale_or_unknown_field_never_positive() -> None:
    for freshness in (FreshnessClass.STALE, FreshnessClass.UNKNOWN):
        fields = (*GOOD_FIELDS[:4], field("debt_to_equity", "0.4", freshness=freshness))
        evaluation = evaluate(fields)
        assert evaluation.assessment.status is AssessmentStatus.INCONCLUSIVE
        assert evaluation.assessment.direction is SignalDirection.NEUTRAL
        assert "FIELD_FRESHNESS_DEBT_TO_EQUITY" in evaluation.reason_codes


def test_future_available_at_is_invisible_no_lookahead() -> None:
    future = field("pe_ratio", "18", available_at=AS_OF + timedelta(hours=1))
    fields = (*tuple(f for f in GOOD_FIELDS if f.name != "pe_ratio"), future)
    evaluation = evaluate(fields)
    assert "FIELD_NOT_YET_AVAILABLE_PE_RATIO" in evaluation.reason_codes
    assert factor(evaluation, "valuation").status == FACTOR_UNAVAILABLE
    assert evaluation.assessment.status is AssessmentStatus.INCONCLUSIVE


# -- policy handling ---------------------------------------------------------------


def test_missing_or_invalid_policy_fails_closed() -> None:
    with pytest.raises(FundamentalEngineError, match="policy_version"):
        FundamentalPolicy(
            policy_version=" ",
            factors=POLICY.factors,
            min_positive_score=Decimal("0.75"),
            max_negative_score=Decimal("0.25"),
        )
    with pytest.raises(FundamentalEngineError, match="unknown factor family"):
        FundamentalPolicy(
            policy_version="x",
            factors=(("momentum", (Criterion("roe", Comparator.GTE, Decimal(1), Decimal(1)),)),),
            min_positive_score=Decimal("0.75"),
            max_negative_score=Decimal("0.25"),
        )
    with pytest.raises(TypeError):
        FundamentalPolicy(policy_version="x")  # type: ignore[call-arg]


# -- determinism / hygiene ----------------------------------------------------------


def test_same_inputs_same_assessment() -> None:
    first = evaluate(GOOD_FIELDS)
    second = evaluate(GOOD_FIELDS)
    assert first.assessment == second.assessment
    assert first.lineage == second.lineage


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        field("roe", "0.2", available_at=datetime(2026, 7, 5, 12, 0))  # noqa: DTZ001
    with pytest.raises(FundamentalEngineError, match="timezone-aware"):
        FundamentalEngine(POLICY).evaluate(
            INSTRUMENT, fields=GOOD_FIELDS, as_of=datetime(2026, 7, 8, 21, 0)  # noqa: DTZ001
        )


def test_field_lineage_preserved_per_field() -> None:
    evaluation = evaluate(GOOD_FIELDS)
    assert len(evaluation.lineage) == len(GOOD_FIELDS)
    for ref in evaluation.lineage:
        assert ref.startswith("field:")
        assert "@prov-fund-fixture@" in ref  # provider attribution
        assert "@2026Q2" in ref  # fiscal period
        assert "2026-07-05" in ref  # available_at
    assert f"policy:{POLICY.policy_version}" in evaluation.assessment.evidence_refs
    # every lineage ref also lands on the canonical assessment evidence
    for ref in evaluation.lineage:
        assert ref in evaluation.assessment.evidence_refs


def test_output_is_canonical_and_fixtures_validated_only() -> None:
    evaluation = evaluate(GOOD_FIELDS)
    assert evaluation.assessment.model_id == "fundamental-engine"
    assert evaluation.assessment.calibration_status is CalibrationStatus.UNVERIFIED
    assert evaluation.assessment.available_at == AS_OF
    assert evaluation.assessment.observed_at <= AS_OF


def test_engine_creates_no_candidate_decision_or_order() -> None:
    import trading_application.fundamental_engine as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in ("trading_persistence", "httpx", "requests", "broker"):
            assert forbidden not in line, line
        for forbidden_name in ("TradeCandidate", "DecisionResult", "OrderIntent"):
            assert forbidden_name not in line, line
