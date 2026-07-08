"""Deterministic decision engine: gates, policy outcomes, explanation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.decision_engine import (
    DecisionEngine,
    DecisionEngineError,
    DecisionPolicy,
    EngineDecision,
)
from trading_application.signal_fusion import (
    FusionInput,
    FusionPolicy,
    FusionResult,
    SignalFusion,
)
from trading_application.strategy_manifest import (
    ManifestEntry,
    ManifestEvaluation,
    ManifestRole,
    StrategyManifest,
    StrategyManifestEvaluator,
)
from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    ActionTier,
    AssessmentStatus,
    CalibrationStatus,
    DecisionOutcome,
    SignalDirection,
    SignalFamily,
)

AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=5)
INSTRUMENT = UUID("00000000-0000-0000-0000-000000000041")
CANDIDATE = UUID("00000000-0000-0000-0000-000000000042")

DECISION_POLICY = DecisionPolicy(
    policy_version="1.0.0",
    tier_outcomes=(
        (ActionTier.AUTO_FULL, DecisionOutcome.APPROVE),
        (ActionTier.AUTO_REDUCED, DecisionOutcome.APPROVE),
        (ActionTier.MANUAL, DecisionOutcome.NEEDS_REVIEW),
        (ActionTier.SHADOW, DecisionOutcome.SHADOW),
        (ActionTier.WATCH, DecisionOutcome.WATCH),
        (ActionTier.REJECT, DecisionOutcome.REJECT),
    ),
    unsatisfied_gate_outcome=DecisionOutcome.WATCH,
    conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
)

FUSION_POLICY = FusionPolicy(
    policy_version="fusion-policy-test-1",
    family_weights=(
        (SignalFamily.TECHNICAL, Decimal("100")),
        (SignalFamily.NEWS, Decimal("30")),
    ),
    unverified_multiplier=Decimal("0.5"),
    action_tier_rules=(
        (ActionTier.AUTO_FULL, Decimal("85")),
        (ActionTier.MANUAL, Decimal("68")),
        (ActionTier.SHADOW, Decimal("40")),
    ),
)

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

ENGINE = DecisionEngine(DECISION_POLICY)


def assessment(
    *,
    family: SignalFamily = SignalFamily.TECHNICAL,
    direction: SignalDirection = SignalDirection.LONG,
    score: str = "0.9",
) -> SignalAssessment:
    return SignalAssessment(
        instrument_id=INSTRUMENT,
        signal_family=family,
        model_id="fixture-engine",
        model_version="1.0.0",
        direction=direction,
        status=AssessmentStatus.TRIGGERED,
        score=Decimal(score),
        calibrated_probability=None,
        calibration_status=CalibrationStatus.CALIBRATED,
        observed_at=EARLIER,
        available_at=EARLIER,
    )


def pipeline(
    *,
    technical_score: str = "0.9",
    with_veto: bool = False,
    with_conflict: bool = False,
    with_required: bool = True,
) -> tuple[FusionResult, ManifestEvaluation]:
    assessments: dict[str, SignalAssessment] = {}
    inputs: list[FusionInput] = [
        FusionInput(
            assessment_ref="a-1",
            assessment=assessment(score=technical_score),
            source_id="src-1",
            cluster_key="event-1",
        )
    ]
    if with_required:
        assessments["technical.breakout"] = assessment(score=technical_score)
    if with_veto:
        assessments["news.material_negative"] = assessment(family=SignalFamily.NEWS)
    if with_conflict:
        inputs.append(
            FusionInput(
                assessment_ref="n-1",
                assessment=assessment(
                    family=SignalFamily.NEWS, direction=SignalDirection.NEGATIVE, score="0.1"
                ),
                source_id="src-2",
                cluster_key="event-2",
            )
        )
    evaluation = StrategyManifestEvaluator().evaluate(MANIFEST, assessments, as_of=AS_OF)
    fusion = SignalFusion(FUSION_POLICY).fuse(tuple(inputs), evaluation, as_of=AS_OF)
    return fusion, evaluation


def decide(fusion: FusionResult, evaluation: ManifestEvaluation) -> EngineDecision:
    return ENGINE.decide(CANDIDATE, fusion, evaluation, as_of=AS_OF)


# -- DoD: same snapshot/version => same decision --------------------------------


def test_same_inputs_produce_identical_decision() -> None:
    fusion, evaluation = pipeline()
    first = decide(fusion, evaluation)
    second = decide(fusion, evaluation)
    assert first == second
    assert first.result.decision_id == second.result.decision_id  # derived, not random


def test_different_policy_version_changes_decision_identity() -> None:
    fusion, evaluation = pipeline()
    other_policy = DecisionPolicy(
        policy_version="1.0.1",
        tier_outcomes=DECISION_POLICY.tier_outcomes,
        unsatisfied_gate_outcome=DecisionOutcome.WATCH,
        conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    first = decide(fusion, evaluation)
    second = DecisionEngine(other_policy).decide(CANDIDATE, fusion, evaluation, as_of=AS_OF)
    assert first.result.decision_id != second.result.decision_id


# -- golden outcomes ---------------------------------------------------------------


def test_approve_fixture_expected_outcome() -> None:
    fusion, evaluation = pipeline(technical_score="0.9")  # 100*0.9=90 -> AUTO_FULL
    decision = decide(fusion, evaluation)
    assert decision.result.action_tier is ActionTier.AUTO_FULL
    assert decision.result.outcome is DecisionOutcome.APPROVE
    assert "RISK_EVALUATION_PENDING" in decision.explanation.reason_codes  # pre-risk
    assert decision.result.supporting_assessment_refs == ("a-1",)


def test_manual_tier_maps_to_needs_review_by_policy() -> None:
    fusion, evaluation = pipeline(technical_score="0.7")  # 70 -> MANUAL
    decision = decide(fusion, evaluation)
    assert decision.result.action_tier is ActionTier.MANUAL
    assert decision.result.outcome is DecisionOutcome.NEEDS_REVIEW


# -- gate dominance ------------------------------------------------------------------


def test_fused_veto_can_only_reject() -> None:
    fusion, evaluation = pipeline(with_veto=True)
    decision = decide(fusion, evaluation)
    assert decision.result.outcome is DecisionOutcome.REJECT
    assert decision.result.action_tier is ActionTier.REJECT
    assert decision.result.veto_reason_codes == ("VETO_NEWS_MATERIAL_NEGATIVE",)
    assert "REJECTED_BY_VETO" in decision.explanation.reason_codes
    assert "RISK_EVALUATION_PENDING" not in decision.explanation.reason_codes


def test_failed_manifest_gate_never_approves() -> None:
    fusion, evaluation = pipeline(with_required=False)
    decision = decide(fusion, evaluation)
    assert decision.result.outcome is DecisionOutcome.WATCH  # policy's unsatisfied outcome
    assert "MANIFEST_GATE_UNSATISFIED" in decision.explanation.blocking_reasons


def test_conflicting_fusion_never_a_silent_positive() -> None:
    fusion, evaluation = pipeline(technical_score="0.9", with_conflict=True)
    # score 90 - 3 = 87 -> still AUTO_FULL by tier, but conflicts exist
    decision = decide(fusion, evaluation)
    assert decision.result.outcome is DecisionOutcome.NEEDS_REVIEW  # policy conflict outcome
    assert "CONFLICT_ROUTED_BY_POLICY" in decision.explanation.reason_codes
    assert "DIRECTION_CONFLICT_PRESENT" in decision.explanation.blocking_reasons


# -- policy fail-closed ---------------------------------------------------------------


def test_invalid_policy_fails_closed() -> None:
    with pytest.raises(DecisionEngineError, match="approving outcome"):
        DecisionPolicy(
            policy_version="1.0.0",
            tier_outcomes=((ActionTier.SHADOW, DecisionOutcome.SHADOW),),
            unsatisfied_gate_outcome=DecisionOutcome.APPROVE,
            conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
        )
    with pytest.raises(DecisionEngineError, match="REJECT tier"):
        DecisionPolicy(
            policy_version="1.0.0",
            tier_outcomes=((ActionTier.REJECT, DecisionOutcome.APPROVE),),
            unsatisfied_gate_outcome=DecisionOutcome.WATCH,
            conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
        )
    with pytest.raises(TypeError):
        DecisionPolicy(policy_version="1.0.0")  # type: ignore[call-arg]


def test_unmapped_tier_fails_closed() -> None:
    narrow = DecisionPolicy(
        policy_version="1.0.0",
        tier_outcomes=((ActionTier.AUTO_FULL, DecisionOutcome.APPROVE),),
        unsatisfied_gate_outcome=DecisionOutcome.WATCH,
        conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
    )
    fusion, evaluation = pipeline(technical_score="0.7")  # -> MANUAL tier, unmapped
    with pytest.raises(DecisionEngineError, match="no outcome mapping"):
        DecisionEngine(narrow).decide(CANDIDATE, fusion, evaluation, as_of=AS_OF)


# -- point-in-time / hygiene -------------------------------------------------------------


def test_future_inputs_rejected() -> None:
    fusion, evaluation = pipeline()
    with pytest.raises(DecisionEngineError, match="lookahead"):
        ENGINE.decide(CANDIDATE, fusion, evaluation, as_of=AS_OF - timedelta(minutes=1))


def test_naive_as_of_rejected() -> None:
    fusion, evaluation = pipeline()
    with pytest.raises(DecisionEngineError, match="timezone-aware"):
        ENGINE.decide(CANDIDATE, fusion, evaluation, as_of=datetime(2026, 7, 8, 21, 0))  # noqa: DTZ001


def test_mismatched_strategy_versions_rejected() -> None:
    fusion, _evaluation = pipeline()
    other_manifest = StrategyManifest(
        strategy_id="other.strategy",
        version="1.0.0",
        entries=(ManifestEntry("technical.breakout", ManifestRole.REQUIRED),),
    )
    other_evaluation = StrategyManifestEvaluator().evaluate(
        other_manifest, {"technical.breakout": assessment()}, as_of=AS_OF
    )
    with pytest.raises(DecisionEngineError, match="different strategy"):
        ENGINE.decide(CANDIDATE, fusion, other_evaluation, as_of=AS_OF)


def test_explanation_contains_full_provenance() -> None:
    fusion, evaluation = pipeline()
    decision = decide(fusion, evaluation)
    explanation = decision.explanation
    assert explanation.fusion_ref.startswith("fusion:technical.breakout_news_guard@")
    assert explanation.manifest_ref.startswith("manifest:technical.breakout_news_guard@")
    assert explanation.policy_ref == "decision-policy:1.0.0"
    assert explanation.input_refs  # fusion evidence carried through
    assert "FIXTURES_VALIDATED_ONLY" in explanation.validation_note
    assert decision.result.decision_policy_version == "1.0.0"


def test_engine_creates_no_order_or_broker_action() -> None:
    import trading_application.decision_engine as module

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
