"""Fundamental engine v1 (IMP-0802).

Authority: Strategy_and_Decision_Framework_v0.5 section 13.2 (signal family
FUNDAMENTAL: valuation, growth, profitability, leverage, health) and section
7.7 FUNDAMENTAL_VALUE (core features: valuation, profitability, leverage,
growth, revisions, financial health); Decision_Contract 13.2 (canonical
``SignalAssessment``). The IMP-0802 factor families — quality (profitability/
health), growth, valuation and financial risk (leverage/health) — map onto
those canonical feature groups.

Inputs are FROZEN fundamentals fixtures only (ADR-0014): a set of
``FundamentalField`` values, each carrying full lineage (field name, value,
provider, available_at, fiscal period, freshness). No provider, no network,
no secrets.

Hard rules:

- **No hardcoded thresholds/weights**: every criterion (field, comparator,
  threshold, weight) and every mapping bound is a required
  ``FundamentalPolicy`` input. Missing policy fails closed at construction.
- **Missing data is explicit**: an absent required field is NEVER read as 0
  and never guessed; the affected factor becomes UNAVAILABLE with a reason
  code, the overall assessment INCONCLUSIVE — missing data cannot produce a
  positive signal.
- **Stale/unknown fundamentals** are treated exactly like missing data for
  signal purposes (no positive/triggered output from them).
- **Point-in-time**: a field whose ``available_at`` is after ``as_of`` is
  invisible (reported as FIELD_NOT_YET_AVAILABLE) — no lookahead.
- **Deterministic**: same fields + policy + as_of => identical assessment.
- Output is the canonical assessment plus per-factor results and the full
  field lineage for dashboards/debug. No Candidate/Decision/Order is created
  and no final trade score is computed (PHASE-9). ``calibration_status``
  stays UNVERIFIED — fixtures-validated only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    FreshnessClass,
    SignalDirection,
    SignalFamily,
)

ENGINE_MODEL_ID = "fundamental-engine"
ENGINE_MODEL_VERSION = "1.0.0"

# Factor families per the IMP-0802 scope, grounded in Strategy 13.2/7.7.
FACTOR_FAMILIES = ("quality", "growth", "valuation", "financial_risk")

_UNUSABLE_FRESHNESS = (FreshnessClass.STALE, FreshnessClass.UNKNOWN)


class FundamentalEngineError(Exception):
    """Fail-closed engine failure (bad policy/inputs)."""


@dataclass(frozen=True, slots=True)
class FundamentalField:
    """One fundamentals datum with FULL lineage (field-level attribution)."""

    name: str
    value: Decimal
    provider_id: str
    available_at: datetime
    fiscal_period: str | None = None
    freshness: FreshnessClass = FreshnessClass.UNKNOWN

    def __post_init__(self) -> None:
        if self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")
        if not self.name or not self.provider_id:
            raise FundamentalEngineError("field name and provider attribution are mandatory")

    def lineage_ref(self) -> str:
        fiscal = self.fiscal_period or "-"
        return (
            f"field:{self.name}={self.value}@{self.provider_id}"
            f"@{self.available_at.isoformat()}@{fiscal}"
        )


class Comparator(StrEnum):
    """Direction of a favorable criterion (policy input)."""

    GTE = "GTE"
    LTE = "LTE"


@dataclass(frozen=True, slots=True)
class Criterion:
    """One policy criterion: favorable when field <cmp> threshold."""

    field: str
    comparator: Comparator
    threshold: Decimal
    weight: Decimal

    def __post_init__(self) -> None:
        if self.weight <= 0:
            raise FundamentalEngineError("criterion weight must be positive")

    def satisfied_by(self, value: Decimal) -> bool:
        if self.comparator is Comparator.GTE:
            return value >= self.threshold
        return value <= self.threshold


@dataclass(frozen=True, slots=True)
class FundamentalPolicy:
    """All factor criteria and mapping bounds — configuration input only."""

    policy_version: str
    factors: tuple[tuple[str, tuple[Criterion, ...]], ...]
    min_positive_score: Decimal  # aggregate >= this => LONG
    max_negative_score: Decimal  # aggregate <= this => NEGATIVE

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise FundamentalEngineError("policy_version is mandatory")
        names = [name for name, _criteria in self.factors]
        if not names or len(names) != len(set(names)):
            raise FundamentalEngineError("policy must define unique factor families")
        for name, criteria in self.factors:
            if name not in FACTOR_FAMILIES:
                raise FundamentalEngineError(f"unknown factor family {name!r}")
            if not criteria:
                raise FundamentalEngineError(f"factor {name!r} has no criteria")
        if self.max_negative_score >= self.min_positive_score:
            raise FundamentalEngineError("direction bounds must not overlap")


FACTOR_OK = "OK"
FACTOR_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FactorResult:
    """One factor family's outcome with its reason codes (dashboard/debug)."""

    name: str
    status: str
    score: Decimal | None  # None iff UNAVAILABLE (never a silent 0)
    reason_codes: tuple[str, ...]
    fields_used: tuple[str, ...]

    def __post_init__(self) -> None:
        if (self.score is None) != (self.status == FACTOR_UNAVAILABLE):
            raise FundamentalEngineError("score must be None exactly when UNAVAILABLE")


@dataclass(frozen=True, slots=True)
class FundamentalEvaluation:
    """Canonical assessment + factor breakdown + full field lineage."""

    assessment: SignalAssessment
    factors: tuple[FactorResult, ...]
    lineage: tuple[str, ...]
    reason_codes: tuple[str, ...]


class FundamentalEngine:
    """Deterministic FUNDAMENTAL SignalAssessment producer over fixtures."""

    def __init__(self, policy: FundamentalPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        instrument_id: UUID,
        *,
        fields: tuple[FundamentalField, ...],
        as_of: datetime,
    ) -> FundamentalEvaluation:
        if as_of.tzinfo is None:
            raise FundamentalEngineError("as_of must be timezone-aware")
        policy = self._policy
        reason_codes: list[str] = []

        # Point-in-time: only fields already available at as_of are usable;
        # a future field is reported, never silently used (no lookahead).
        usable: dict[str, FundamentalField] = {}
        lineage: list[str] = []
        for field in fields:
            if field.available_at > as_of:
                reason_codes.append(f"FIELD_NOT_YET_AVAILABLE_{field.name.upper()}")
                continue
            if field.name in usable:
                # deterministic: latest available_at wins; ties keep first
                if field.available_at <= usable[field.name].available_at:
                    continue
            usable[field.name] = field
        for field in sorted(usable.values(), key=lambda item: item.name):
            lineage.append(field.lineage_ref())

        factor_results: list[FactorResult] = []
        weighted_total = Decimal(0)
        weight_sum = Decimal(0)
        any_unavailable = False
        for name, criteria in policy.factors:
            result = self._evaluate_factor(name, criteria, usable)
            factor_results.append(result)
            reason_codes.extend(result.reason_codes)
            if result.status == FACTOR_UNAVAILABLE:
                any_unavailable = True
            else:
                assert result.score is not None
                factor_weight = sum((c.weight for c in criteria), Decimal(0))
                weighted_total += result.score * factor_weight
                weight_sum += factor_weight

        if any_unavailable or weight_sum == 0:
            status = AssessmentStatus.INCONCLUSIVE
            direction = SignalDirection.NEUTRAL
            score = Decimal(0)
            reason_codes.append("FUNDAMENTALS_INCOMPLETE")
        else:
            score = weighted_total / weight_sum
            status = AssessmentStatus.ELIGIBLE
            if score >= policy.min_positive_score:
                direction = SignalDirection.LONG
                reason_codes.append("FUNDAMENTALS_FAVORABLE")
            elif score <= policy.max_negative_score:
                direction = SignalDirection.NEGATIVE
                reason_codes.append("FUNDAMENTALS_UNFAVORABLE")
            else:
                direction = SignalDirection.NEUTRAL
                reason_codes.append("FUNDAMENTALS_MIXED")

        observed_candidates = [field.available_at for field in usable.values()]
        observed_at = max(observed_candidates) if observed_candidates else as_of
        assessment = SignalAssessment(
            instrument_id=instrument_id,
            signal_family=SignalFamily.FUNDAMENTAL,
            model_id=ENGINE_MODEL_ID,
            model_version=ENGINE_MODEL_VERSION,
            direction=direction,
            status=status,
            score=score,
            calibrated_probability=None,
            calibration_status=CalibrationStatus.UNVERIFIED,  # fixtures-validated only
            observed_at=observed_at,
            available_at=as_of,
            evidence_refs=(f"policy:{policy.policy_version}", *lineage),
            risk_flags=tuple(dict.fromkeys(reason_codes)),
        )
        return FundamentalEvaluation(
            assessment=assessment,
            factors=tuple(factor_results),
            lineage=tuple(lineage),
            reason_codes=tuple(dict.fromkeys(reason_codes)),
        )

    def _evaluate_factor(
        self,
        name: str,
        criteria: tuple[Criterion, ...],
        usable: dict[str, FundamentalField],
    ) -> FactorResult:
        reasons: list[str] = []
        satisfied = Decimal(0)
        total = Decimal(0)
        fields_used: list[str] = []
        for criterion in criteria:
            field = usable.get(criterion.field)
            if field is None:
                # explicit missing-data state: never a silent zero
                reasons.append(f"MISSING_FIELD_{criterion.field.upper()}")
                continue
            if field.freshness in _UNUSABLE_FRESHNESS:
                reasons.append(f"FIELD_FRESHNESS_{criterion.field.upper()}")
                continue
            fields_used.append(criterion.field)
            total += criterion.weight
            if criterion.satisfied_by(field.value):
                satisfied += criterion.weight
        if len(fields_used) < len(criteria):
            return FactorResult(
                name=name,
                status=FACTOR_UNAVAILABLE,
                score=None,
                reason_codes=tuple(reasons),
                fields_used=tuple(fields_used),
            )
        return FactorResult(
            name=name,
            status=FACTOR_OK,
            score=satisfied / total,
            reason_codes=tuple(reasons),
            fields_used=tuple(fields_used),
        )


__all__ = [
    "ENGINE_MODEL_ID",
    "ENGINE_MODEL_VERSION",
    "FACTOR_FAMILIES",
    "FACTOR_OK",
    "FACTOR_UNAVAILABLE",
    "Comparator",
    "Criterion",
    "FactorResult",
    "FundamentalEngine",
    "FundamentalEngineError",
    "FundamentalEvaluation",
    "FundamentalField",
    "FundamentalPolicy",
]
