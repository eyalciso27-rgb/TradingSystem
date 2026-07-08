"""Signal fusion: dedup, conflicts, veto dominance, policy tiers (IMP-0902)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.signal_fusion import (
    FusionError,
    FusionInput,
    FusionPolicy,
    FusionResult,
    SignalFusion,
)
from trading_application.strategy_manifest import (
    ManifestEntry,
    ManifestRole,
    StrategyManifest,
    StrategyManifestEvaluator,
)
from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    ActionTier,
    AssessmentStatus,
    CalibrationStatus,
    SignalDirection,
    SignalFamily,
)

AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=5)
INSTRUMENT = UUID("00000000-0000-0000-0000-000000000031")

POLICY = FusionPolicy(
    policy_version="fusion-policy-test-1",
    family_weights=(
        (SignalFamily.TECHNICAL, Decimal("50")),
        (SignalFamily.NEWS, Decimal("30")),
        (SignalFamily.SOCIAL, Decimal("20")),
    ),
    unverified_multiplier=Decimal("0.5"),
    action_tier_rules=(
        (ActionTier.AUTO_FULL, Decimal("85")),
        (ActionTier.AUTO_REDUCED, Decimal("78")),
        (ActionTier.MANUAL, Decimal("68")),
        (ActionTier.SHADOW, Decimal("40")),
    ),
)

FUSION = SignalFusion(POLICY)

MANIFEST = StrategyManifest(
    strategy_id="technical.breakout_news_guard",
    version="1.0.0",
    entries=(
        ManifestEntry(
            "technical.breakout",
            ManifestRole.REQUIRED,
            required_status=AssessmentStatus.TRIGGERED,
        ),
        ManifestEntry(
            "news.material_negative",
            ManifestRole.VETO,
            required_status=AssessmentStatus.TRIGGERED,
        ),
    ),
)


def assessment(
    *,
    family: SignalFamily = SignalFamily.TECHNICAL,
    direction: SignalDirection = SignalDirection.LONG,
    status: AssessmentStatus = AssessmentStatus.TRIGGERED,
    score: str = "0.9",
    calibration: CalibrationStatus = CalibrationStatus.CALIBRATED,
    observed_at: datetime = EARLIER,
) -> SignalAssessment:
    return SignalAssessment(
        instrument_id=INSTRUMENT,
        signal_family=family,
        model_id="fixture-engine",
        model_version="1.0.0",
        direction=direction,
        status=status,
        score=Decimal(score),
        calibrated_probability=None,
        calibration_status=calibration,
        observed_at=observed_at,
        available_at=observed_at,
    )


def observation(
    ref: str,
    *,
    source: str = "src-1",
    cluster: str = "event-1",
    **kwargs: object,
) -> FusionInput:
    return FusionInput(
        assessment_ref=ref,
        assessment=assessment(**kwargs),  # type: ignore[arg-type]
        source_id=source,
        cluster_key=cluster,
    )


def manifest_evaluation(
    assessments: dict[str, SignalAssessment],
) -> object:
    return StrategyManifestEvaluator().evaluate(MANIFEST, assessments, as_of=AS_OF)


def fuse(inputs: tuple[FusionInput, ...], assessments: dict[str, SignalAssessment]) -> FusionResult:
    evaluation = StrategyManifestEvaluator().evaluate(MANIFEST, assessments, as_of=AS_OF)
    return FUSION.fuse(inputs, evaluation, as_of=AS_OF)


PASSING = {"technical.breakout": assessment()}


# -- golden fusion -----------------------------------------------------------


def test_valid_fusion_expected_result() -> None:
    result = fuse((observation("a-1"),), PASSING)
    # 50 (technical weight) * 0.9 (score) = 45 -> SHADOW (>= 40)
    assert result.score == Decimal("45.0")
    assert result.action_tier is ActionTier.SHADOW
    assert result.gate_passed
    assert "TIER_SHADOW_BY_POLICY" in result.reason_codes
    contribution = result.contributions[0]
    assert contribution.source_id == "src-1"
    assert contribution.model_id == "fixture-engine"
    assert contribution.calibration_status is CalibrationStatus.CALIBRATED
    assert contribution.weight_applied == Decimal("50")


def test_duplicate_source_observations_do_not_double_count() -> None:
    """DoD: two observations of the same event cluster count once."""
    single = fuse((observation("a-1"),), PASSING)
    doubled = fuse(
        (
            observation("a-1"),
            observation("a-2", source="src-2", cluster="event-1"),
        ),
        PASSING,
    )
    assert doubled.score == single.score  # no double counting
    deduped = [c for c in doubled.contributions if c.deduped]
    assert len(deduped) == 1
    assert deduped[0].assessment_ref == "a-2"
    assert deduped[0].weight_applied == Decimal("0")
    assert deduped[0].deduped_into == "a-1"
    assert "DEDUPED_a-2" in doubled.reason_codes


def test_distinct_events_do_count_separately() -> None:
    result = fuse(
        (
            observation("a-1", cluster="event-1"),
            observation(
                "n-1",
                cluster="event-2",
                family=SignalFamily.NEWS,
                score="1.0",
            ),
        ),
        PASSING,
    )
    # 50*0.9 + 30*1.0 = 75 -> MANUAL (>= 68)
    assert result.score == Decimal("75.0")
    assert result.action_tier is ActionTier.MANUAL


def test_same_evidence_under_two_refs_rejected() -> None:
    with pytest.raises(FusionError, match="duplicate assessment_ref"):
        fuse((observation("a-1"), observation("a-1")), PASSING)


# -- conflicts ------------------------------------------------------------------


def test_direction_conflict_is_explicit_not_averaged_away() -> None:
    result = fuse(
        (
            observation("a-1", cluster="event-1"),
            observation(
                "n-1",
                cluster="event-2",
                family=SignalFamily.NEWS,
                direction=SignalDirection.NEGATIVE,
                score="1.0",
            ),
        ),
        PASSING,
    )
    assert result.conflicts == (("a-1", "n-1"),)
    assert "DIRECTION_CONFLICT" in result.reason_codes
    # arithmetic still deterministic: 45 - 30 = 15
    assert result.score == Decimal("15.0")


# -- veto/gate dominance ------------------------------------------------------------


def test_fired_veto_forces_reject_regardless_of_score() -> None:
    assessments = {
        "technical.breakout": assessment(),
        "news.material_negative": assessment(family=SignalFamily.NEWS),
    }
    result = fuse((observation("a-1"),), assessments)
    assert result.vetoes_fired == ("news.material_negative",)
    assert result.action_tier is ActionTier.REJECT
    assert "VETO_DOMINATES" in result.reason_codes


def test_unsatisfied_requirements_force_watch_never_approve() -> None:
    result = fuse((observation("a-1"),), {})  # required assessment missing
    assert result.action_tier is ActionTier.WATCH
    assert "REQUIRED_UNSATISFIED_NEVER_APPROVE" in result.reason_codes


def test_no_global_all_model_agreement() -> None:
    """Only the manifest gates; one technical family alone can reach a tier."""
    result = fuse((observation("a-1"),), PASSING)
    assert result.gate_passed
    assert result.action_tier is not ActionTier.REJECT


# -- calibration authority / unweighted families --------------------------------------


def test_unverified_model_weight_is_policy_limited() -> None:
    result = fuse(
        (observation("a-1", calibration=CalibrationStatus.UNVERIFIED),),
        PASSING,
    )
    # 50 * 0.5 (unverified multiplier) * 0.9 = 22.5 -> below SHADOW threshold
    assert result.score == Decimal("22.5")
    assert result.action_tier is ActionTier.WATCH
    assert "BELOW_ALL_TIER_THRESHOLDS" in result.reason_codes


def test_unweighted_family_contributes_nothing_and_is_flagged() -> None:
    result = fuse(
        (observation("f-1", family=SignalFamily.FUNDAMENTAL, cluster="event-9"),),
        PASSING,
    )
    assert result.score == Decimal("0")
    assert "FAMILY_UNWEIGHTED_FUNDAMENTAL" in result.reason_codes


# -- policy / determinism / point-in-time ----------------------------------------------


def test_invalid_policy_fails_closed() -> None:
    with pytest.raises(FusionError, match="policy_version"):
        FusionPolicy(
            policy_version=" ",
            family_weights=(),
            unverified_multiplier=Decimal("0.5"),
            action_tier_rules=((ActionTier.SHADOW, Decimal("40")),),
        )
    with pytest.raises(FusionError, match="descending"):
        FusionPolicy(
            policy_version="x",
            family_weights=(),
            unverified_multiplier=Decimal("0.5"),
            action_tier_rules=(
                (ActionTier.SHADOW, Decimal("40")),
                (ActionTier.AUTO_FULL, Decimal("85")),
            ),
        )
    with pytest.raises(TypeError):
        FusionPolicy(policy_version="x")  # type: ignore[call-arg]


def test_same_inputs_same_result() -> None:
    inputs = (observation("a-1"),)
    assert fuse(inputs, PASSING) == fuse(inputs, PASSING)


def test_future_observation_rejected() -> None:
    future = observation("a-1", observed_at=AS_OF + timedelta(seconds=1))
    evaluation = StrategyManifestEvaluator().evaluate(MANIFEST, PASSING, as_of=AS_OF)
    with pytest.raises(FusionError, match="lookahead"):
        FUSION.fuse((future,), evaluation, as_of=AS_OF)


def test_naive_as_of_rejected() -> None:
    evaluation = StrategyManifestEvaluator().evaluate(MANIFEST, PASSING, as_of=AS_OF)
    with pytest.raises(FusionError, match="timezone-aware"):
        FUSION.fuse((observation("a-1"),), evaluation, as_of=datetime(2026, 7, 8, 21, 0))  # noqa: DTZ001


def test_provenance_preserved_for_every_component() -> None:
    result = fuse(
        (
            observation("a-1"),
            observation("a-2", source="src-2", cluster="event-1"),
        ),
        PASSING,
    )
    for contribution in result.contributions:
        assert contribution.assessment_ref and contribution.source_id
        assert contribution.model_id and contribution.model_version
        assert contribution.cluster_key == "event-1"
    assert "observation:a-1@src-1" in result.evidence_refs
    assert "observation:a-2@src-2" in result.evidence_refs
    assert f"policy:{POLICY.policy_version}" in result.evidence_refs


def test_fusion_creates_no_candidate_decision_or_order() -> None:
    import trading_application.signal_fusion as module

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
