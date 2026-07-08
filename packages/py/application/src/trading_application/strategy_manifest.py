"""Strategy manifest evaluator (IMP-0901).

Authority: Strategy_and_Decision_Framework_v0.5 section 28 (the flexible
Strategy Manifest: each strategy declares which signal families are
REQUIRED, VETO, SOFT_SCORE, BONUS or CONTEXT; there is NO global gate that
demands technical+fundamental+news+ML+external+social together; duplicated
signals of the same event get no independent weight), section 31.2 (fusion
manifest shape: per-entry assessment reference + condition such as
``score >= 75``), section 31.5 (an ML model may act only with an approved
calibration status) and section 31.6 (conflict resolution: a missing
required assessment is never APPROVE; a hard veto dominates; hard gates beat
ML disagreement).

Deterministic, pure evaluation over FROZEN inputs (ADR-0014): a versioned
manifest (configuration — nothing here invents thresholds/weights/roles) and
a mapping of assessment-key -> canonical SignalAssessment fixtures.

Output is a *manifest evaluation* only — which requirements were satisfied,
which vetoes fired, what SOFT/BONUS evidence contributed and what CONTEXT
was noted — with reason codes for dashboards. No Candidate, Decision, Order
or broker action is produced (fusion/decision arrive with IMP-0902/0903),
and no final trade score is computed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import AssessmentStatus, CalibrationStatus

EVALUATOR_VERSION = "manifest-evaluator-1.0.0"

_CALIBRATION_RANK: dict[CalibrationStatus, int] = {
    CalibrationStatus.UNVERIFIED: 0,
    CalibrationStatus.HEURISTIC: 1,
    CalibrationStatus.CALIBRATED: 2,
}


class ManifestError(Exception):
    """Fail-closed manifest/evaluation failure."""


class ManifestRole(StrEnum):
    """Canonical manifest evidence roles (Strategy framework section 28)."""

    REQUIRED = "REQUIRED"
    VETO = "VETO"
    SOFT_SCORE = "SOFT_SCORE"
    BONUS = "BONUS"
    CONTEXT = "CONTEXT"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One manifest input: an assessment reference + its condition.

    Conditions (min_score / required_status / min_calibration) come from the
    manifest configuration (31.2 examples) — never from code. ``weight``
    applies to SOFT_SCORE/BONUS contributions only.
    """

    assessment_key: str
    role: ManifestRole
    min_score: Decimal | None = None
    required_status: AssessmentStatus | None = None
    min_calibration: CalibrationStatus | None = None
    weight: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.assessment_key.strip():
            raise ManifestError("assessment_key is mandatory")
        if self.role in (ManifestRole.SOFT_SCORE, ManifestRole.BONUS):
            if self.weight is None or self.weight <= 0:
                raise ManifestError(
                    f"{self.role.value} entry {self.assessment_key!r} needs a positive weight"
                )
        elif self.weight is not None:
            raise ManifestError(
                f"{self.role.value} entry {self.assessment_key!r} must not carry a weight"
            )
        if self.role is ManifestRole.CONTEXT and (
            self.min_score is not None or self.required_status is not None
        ):
            raise ManifestError("CONTEXT entries carry no gating conditions")

    def condition_met(self, assessment: SignalAssessment) -> bool:
        """Deterministic condition check against one frozen assessment."""
        if self.min_calibration is not None and (
            _CALIBRATION_RANK[assessment.calibration_status]
            < _CALIBRATION_RANK[self.min_calibration]
        ):
            return False  # 31.5: an uncalibrated model gets no extra authority
        if self.required_status is not None and assessment.status is not self.required_status:
            return False
        if self.min_score is not None and assessment.score < self.min_score:
            return False
        return True


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    """A frozen, versioned per-strategy manifest (configuration input)."""

    strategy_id: str
    version: str
    entries: tuple[ManifestEntry, ...]

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.version.strip():
            raise ManifestError("strategy_id and version are mandatory")
        if not any(entry.role is ManifestRole.REQUIRED for entry in self.entries):
            raise ManifestError("a manifest must declare at least one REQUIRED entry")
        keys = [(entry.assessment_key, entry.role) for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ManifestError("duplicate manifest entry (key, role)")


@dataclass(frozen=True, slots=True)
class EntryResult:
    """Outcome of one manifest entry (dashboard/debug explanation)."""

    assessment_key: str
    role: ManifestRole
    present: bool
    condition_met: bool
    contribution: Decimal | None  # weight granted (SOFT/BONUS only)
    reason: str


@dataclass(frozen=True, slots=True)
class ManifestEvaluation:
    """The evaluator's only output — never a Candidate/Decision/Order."""

    strategy_id: str
    manifest_version: str
    as_of: datetime
    requirements_satisfied: bool
    vetoes_fired: tuple[str, ...]
    soft_score: Decimal
    bonus_score: Decimal
    context_notes: tuple[str, ...]
    entries: tuple[EntryResult, ...]
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    @property
    def gate_passed(self) -> bool:
        """All REQUIRED satisfied and no veto fired (31.6: veto dominates)."""
        return self.requirements_satisfied and not self.vetoes_fired


class StrategyManifestEvaluator:
    """Deterministic evaluator: manifest + frozen assessments -> evaluation."""

    def evaluate(
        self,
        manifest: StrategyManifest,
        assessments: Mapping[str, SignalAssessment],
        *,
        as_of: datetime,
    ) -> ManifestEvaluation:
        if as_of.tzinfo is None:
            raise ManifestError("as_of must be timezone-aware")
        for key, candidate_assessment in assessments.items():
            if (
                candidate_assessment.available_at > as_of
                or candidate_assessment.observed_at > as_of
            ):
                raise ManifestError(
                    f"lookahead: assessment {key!r} observed/available after as_of"
                )

        results: list[EntryResult] = []
        reason_codes: list[str] = []
        vetoes_fired: list[str] = []
        soft_score = Decimal(0)
        bonus_score = Decimal(0)
        context_notes: list[str] = []
        requirements_satisfied = True

        for entry in manifest.entries:
            assessment = assessments.get(entry.assessment_key)
            present = assessment is not None
            met = entry.condition_met(assessment) if assessment is not None else False
            contribution: Decimal | None = None
            reason = ""

            if entry.role is ManifestRole.REQUIRED:
                if not present:
                    requirements_satisfied = False
                    reason = "required assessment missing (never APPROVE, 31.6)"
                    reason_codes.append(f"REQUIRED_MISSING_{entry.assessment_key}")
                elif not met:
                    requirements_satisfied = False
                    reason = "required condition not met"
                    reason_codes.append(f"REQUIRED_UNMET_{entry.assessment_key}")
                else:
                    reason = "required condition satisfied"
                    reason_codes.append(f"REQUIRED_OK_{entry.assessment_key}")
            elif entry.role is ManifestRole.VETO:
                # A veto FIRES when its referenced assessment is present and
                # its condition holds; absence of evidence never fires a veto
                # (31.4: absence of news is not neutral evidence either way).
                if present and met:
                    vetoes_fired.append(entry.assessment_key)
                    reason = "veto fired (dominates SOFT/BONUS, 31.6)"
                    reason_codes.append(f"VETO_FIRED_{entry.assessment_key}")
                else:
                    reason = "veto not fired"
            elif entry.role in (ManifestRole.SOFT_SCORE, ManifestRole.BONUS):
                # SOFT/BONUS contribute score only — they are never a gate
                # unless the manifest itself listed them as REQUIRED/VETO.
                if present and met:
                    assert entry.weight is not None
                    contribution = entry.weight
                    if entry.role is ManifestRole.SOFT_SCORE:
                        soft_score += entry.weight
                    else:
                        bonus_score += entry.weight
                    reason = f"{entry.role.value.lower()} evidence contributed"
                    reason_codes.append(f"{entry.role.value}_{entry.assessment_key}")
                else:
                    reason = "no contribution (missing or condition unmet; not a gate)"
            else:  # CONTEXT
                if present:
                    context_notes.append(entry.assessment_key)
                    reason = "context noted (never an approval by itself)"
                else:
                    reason = "context absent"

            results.append(
                EntryResult(
                    assessment_key=entry.assessment_key,
                    role=entry.role,
                    present=present,
                    condition_met=met,
                    contribution=contribution,
                    reason=reason,
                )
            )

        evidence = [
            f"manifest:{manifest.strategy_id}@{manifest.version}",
            f"evaluator:{EVALUATOR_VERSION}",
            *(
                f"assessment:{key}@{assessment.model_id}@{assessment.model_version}"
                for key, assessment in sorted(assessments.items())
            ),
        ]
        return ManifestEvaluation(
            strategy_id=manifest.strategy_id,
            manifest_version=manifest.version,
            as_of=as_of,
            requirements_satisfied=requirements_satisfied,
            vetoes_fired=tuple(vetoes_fired),
            soft_score=soft_score,
            bonus_score=bonus_score,
            context_notes=tuple(context_notes),
            entries=tuple(results),
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            evidence_refs=tuple(evidence),
        )


__all__ = [
    "EVALUATOR_VERSION",
    "EntryResult",
    "ManifestEntry",
    "ManifestError",
    "ManifestEvaluation",
    "ManifestRole",
    "StrategyManifest",
    "StrategyManifestEvaluator",
]
