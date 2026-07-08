"""Strategy manifest evaluator: roles, veto dominance, no global gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.strategy_manifest import (
    ManifestEntry,
    ManifestError,
    ManifestEvaluation,
    ManifestRole,
    StrategyManifest,
    StrategyManifestEvaluator,
)
from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    SignalDirection,
    SignalFamily,
)

AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=5)
INSTRUMENT = UUID("00000000-0000-0000-0000-000000000021")

EVALUATOR = StrategyManifestEvaluator()


def assessment(
    *,
    family: SignalFamily = SignalFamily.TECHNICAL,
    status: AssessmentStatus = AssessmentStatus.TRIGGERED,
    score: str = "0.9",
    calibration: CalibrationStatus = CalibrationStatus.HEURISTIC,
    observed_at: datetime = EARLIER,
) -> SignalAssessment:
    return SignalAssessment(
        instrument_id=INSTRUMENT,
        signal_family=family,
        model_id="fixture-engine",
        model_version="1.0.0",
        direction=SignalDirection.LONG,
        status=status,
        score=Decimal(score),
        calibrated_probability=None,
        calibration_status=calibration,
        observed_at=observed_at,
        available_at=observed_at,
    )


MANIFEST = StrategyManifest(
    strategy_id="technical.breakout_news_guard",
    version="1.0.0",
    entries=(
        ManifestEntry(
            "technical.breakout",
            ManifestRole.REQUIRED,
            min_score=Decimal("0.75"),
            required_status=AssessmentStatus.TRIGGERED,
        ),
        ManifestEntry(
            "news.material_negative",
            ManifestRole.VETO,
            required_status=AssessmentStatus.TRIGGERED,
        ),
        ManifestEntry("fundamental.quality", ManifestRole.SOFT_SCORE, weight=Decimal("5")),
        ManifestEntry("tradingview.alert", ManifestRole.BONUS, weight=Decimal("2")),
        ManifestEntry("sentiment_summary", ManifestRole.CONTEXT),
    ),
)


def evaluate(assessments: dict[str, SignalAssessment]) -> ManifestEvaluation:
    return EVALUATOR.evaluate(MANIFEST, assessments, as_of=AS_OF)


# -- REQUIRED behavior ----------------------------------------------------------


def test_required_satisfied_passes_gate() -> None:
    evaluation = evaluate({"technical.breakout": assessment()})
    assert evaluation.requirements_satisfied
    assert evaluation.gate_passed
    assert "REQUIRED_OK_technical.breakout" in evaluation.reason_codes


def test_required_missing_fails_gate() -> None:
    evaluation = evaluate({})
    assert not evaluation.requirements_satisfied
    assert not evaluation.gate_passed
    assert "REQUIRED_MISSING_technical.breakout" in evaluation.reason_codes


def test_required_condition_unmet_fails_gate() -> None:
    weak = assessment(score="0.5")  # below the manifest's 0.75 bar
    evaluation = evaluate({"technical.breakout": weak})
    assert not evaluation.requirements_satisfied
    assert "REQUIRED_UNMET_technical.breakout" in evaluation.reason_codes


# -- VETO dominance --------------------------------------------------------------


def test_veto_fires_and_dominates_soft_bonus() -> None:
    evaluation = evaluate(
        {
            "technical.breakout": assessment(),
            "news.material_negative": assessment(
                family=SignalFamily.NEWS, status=AssessmentStatus.TRIGGERED
            ),
            "fundamental.quality": assessment(family=SignalFamily.FUNDAMENTAL),
            "tradingview.alert": assessment(family=SignalFamily.EXTERNAL),
        }
    )
    assert evaluation.requirements_satisfied  # requirements were met...
    assert evaluation.vetoes_fired == ("news.material_negative",)
    assert not evaluation.gate_passed  # ...but the veto dominates (31.6)
    assert evaluation.soft_score == Decimal("5")  # contributions recorded, not deciding


def test_absent_veto_assessment_does_not_fire() -> None:
    evaluation = evaluate({"technical.breakout": assessment()})
    assert evaluation.vetoes_fired == ()
    assert evaluation.gate_passed


# -- SOFT/BONUS/CONTEXT -----------------------------------------------------------


def test_soft_and_bonus_contribute_without_gating() -> None:
    evaluation = evaluate(
        {
            "technical.breakout": assessment(),
            "fundamental.quality": assessment(family=SignalFamily.FUNDAMENTAL),
            "tradingview.alert": assessment(family=SignalFamily.EXTERNAL),
        }
    )
    assert evaluation.soft_score == Decimal("5")
    assert evaluation.bonus_score == Decimal("2")
    assert evaluation.gate_passed
    # and their ABSENCE does not gate anything:
    bare = evaluate({"technical.breakout": assessment()})
    assert bare.gate_passed and bare.soft_score == Decimal("0")


def test_context_never_approves_by_itself() -> None:
    evaluation = evaluate({"sentiment_summary": assessment(family=SignalFamily.NEWS)})
    assert evaluation.context_notes == ("sentiment_summary",)
    assert not evaluation.gate_passed  # required still missing


def test_no_global_all_model_agreement_gate() -> None:
    """DoD: only the manifest's own REQUIRED entries gate — a strategy that
    requires ONLY technical passes without fundamental/news/ML/social."""
    evaluation = evaluate({"technical.breakout": assessment()})
    assert evaluation.gate_passed
    families_present = {SignalFamily.TECHNICAL}
    assert families_present != set(SignalFamily)  # most families absent, still passes


# -- calibration authority ---------------------------------------------------------


def test_unverified_model_gets_no_forbidden_authority() -> None:
    manifest = StrategyManifest(
        strategy_id="ml.gated",
        version="1.0.0",
        entries=(
            ManifestEntry(
                "ml.ranker",
                ManifestRole.REQUIRED,
                min_calibration=CalibrationStatus.CALIBRATED,  # 31.5
            ),
        ),
    )
    unverified = assessment(
        family=SignalFamily.QUANT_ML, calibration=CalibrationStatus.UNVERIFIED
    )
    evaluation = EVALUATOR.evaluate(manifest, {"ml.ranker": unverified}, as_of=AS_OF)
    assert not evaluation.requirements_satisfied
    assert "REQUIRED_UNMET_ml.ranker" in evaluation.reason_codes


# -- fail-closed manifest validation -------------------------------------------------


def test_invalid_manifest_fails_closed() -> None:
    with pytest.raises(ManifestError, match="at least one REQUIRED"):
        StrategyManifest(
            strategy_id="s",
            version="1",
            entries=(ManifestEntry("a", ManifestRole.CONTEXT),),
        )
    with pytest.raises(ManifestError, match="positive weight"):
        ManifestEntry("a", ManifestRole.SOFT_SCORE)
    with pytest.raises(ManifestError, match="must not carry a weight"):
        ManifestEntry("a", ManifestRole.REQUIRED, weight=Decimal("1"))
    with pytest.raises(ManifestError, match="no gating conditions"):
        ManifestEntry("a", ManifestRole.CONTEXT, min_score=Decimal("1"))
    with pytest.raises(ValueError):
        ManifestRole("SOMETHING_ELSE")  # unknown role is not constructible
    with pytest.raises(ManifestError, match="duplicate"):
        StrategyManifest(
            strategy_id="s",
            version="1",
            entries=(
                ManifestEntry("a", ManifestRole.REQUIRED),
                ManifestEntry("a", ManifestRole.REQUIRED),
            ),
        )
    with pytest.raises(TypeError):
        StrategyManifest(strategy_id="s")  # type: ignore[call-arg]


# -- determinism / point-in-time ------------------------------------------------------


def test_same_inputs_same_output() -> None:
    inputs = {
        "technical.breakout": assessment(),
        "fundamental.quality": assessment(family=SignalFamily.FUNDAMENTAL),
    }
    assert evaluate(inputs) == evaluate(inputs)


def test_future_assessment_rejected() -> None:
    future = assessment(observed_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(ManifestError, match="lookahead"):
        evaluate({"technical.breakout": future})


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ManifestError, match="timezone-aware"):
        EVALUATOR.evaluate(
            MANIFEST,
            {"technical.breakout": assessment()},
            as_of=datetime(2026, 7, 8, 21, 0),  # noqa: DTZ001
        )


def test_evidence_and_entry_explanations_present() -> None:
    evaluation = evaluate({"technical.breakout": assessment()})
    assert "manifest:technical.breakout_news_guard@1.0.0" in evaluation.evidence_refs
    assert any(ref.startswith("assessment:technical.breakout@") for ref in evaluation.evidence_refs)
    assert all(entry.reason for entry in evaluation.entries)  # dashboard/debug


def test_evaluator_creates_no_candidate_decision_or_order() -> None:
    import trading_application.strategy_manifest as module

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
