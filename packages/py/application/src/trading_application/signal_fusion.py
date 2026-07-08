"""Signal fusion (IMP-0902).

Authority: Strategy_and_Decision_Framework_v0.5 section 28 (action tiers are
declared by policy/manifest — ``auto_full: score >= 85`` etc. — and signals
that are copies of the SAME event get no independent weight), section 31.2
(fusion manifest), section 31.6 (conflict resolution: hard veto dominates;
technical/fundamental conflict is resolved by strategy purpose — never a
hidden average; hard gates beat ML disagreement; a missing required
assessment is never APPROVE), section 31.5 (an uncalibrated model gets no
extra authority) and the Data Model fusion_runs/fusion_evidence lineage
(every fusion references its exact assessments and manifest).

Deterministic, pure fusion over FROZEN inputs (ADR-0014): observation-level
``FusionInput`` items (assessment + source + cluster key), the IMP-0901
``ManifestEvaluation`` and a ``FusionPolicy``. The output is a fused result
only — full provenance, explicit conflicts, a policy-mapped action tier —
never a Candidate/Decision/Order (the decision engine is IMP-0903).

Hard rules:

- **No double counting** (DoD): observations sharing a ``cluster_key``
  collapse into ONE weighted contribution (deterministic representative:
  earliest observed_at, then lowest ref); the duplicates stay visible as
  DEDUPED provenance with zero weight.
- **Conflicts are explicit**: opposing directions among counted
  contributions raise a CONFLICT reason + conflict pairs — nothing is
  silently averaged away.
- **Veto/gate dominance**: a fired manifest veto forces REJECT; unsatisfied
  requirements force WATCH — fusion arithmetic can never override them.
- **No invented numbers**: family weights, the unverified-model multiplier
  and every action-tier threshold come from ``FusionPolicy``/manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    ActionTier,
    CalibrationStatus,
    SignalDirection,
    SignalFamily,
)

from trading_application.strategy_manifest import ManifestEvaluation

FUSION_VERSION = "signal-fusion-1.0.0"


class FusionError(Exception):
    """Fail-closed fusion failure (bad policy/inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class FusionInput:
    """One observation-level input: assessment + provenance + event cluster."""

    assessment_ref: str
    assessment: SignalAssessment
    source_id: str
    cluster_key: str  # observations of the same event share a cluster

    def __post_init__(self) -> None:
        if not self.assessment_ref or not self.source_id or not self.cluster_key:
            raise FusionError("assessment_ref, source_id and cluster_key are mandatory")


@dataclass(frozen=True, slots=True)
class FusionPolicy:
    """All fusion numbers — policy/config input, nothing in code."""

    policy_version: str
    family_weights: tuple[tuple[SignalFamily, Decimal], ...]
    unverified_multiplier: Decimal  # authority granted to non-CALIBRATED models
    # Descending (tier, min_score) rules per section 28 action_tiers.
    action_tier_rules: tuple[tuple[ActionTier, Decimal], ...]

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise FusionError("policy_version is mandatory")
        families = [family for family, _w in self.family_weights]
        if len(families) != len(set(families)):
            raise FusionError("duplicate family weight")
        if not (0 <= self.unverified_multiplier <= 1):
            raise FusionError("unverified_multiplier must be within [0, 1]")
        if not self.action_tier_rules:
            raise FusionError("action tier rules are mandatory")
        thresholds = [threshold for _tier, threshold in self.action_tier_rules]
        if thresholds != sorted(thresholds, reverse=True):
            raise FusionError("action tier thresholds must be strictly descending")

    def weight_for(self, family: SignalFamily) -> Decimal | None:
        for candidate, weight in self.family_weights:
            if candidate is family:
                return weight
        return None


@dataclass(frozen=True, slots=True)
class FusionContribution:
    """Provenance of one observation in the fused result."""

    assessment_ref: str
    source_id: str
    cluster_key: str
    signal_family: SignalFamily
    model_id: str
    model_version: str
    calibration_status: CalibrationStatus
    direction: SignalDirection
    weight_applied: Decimal
    deduped: bool
    deduped_into: str | None = None


@dataclass(frozen=True, slots=True)
class FusionResult:
    """Fused signal only — the decision engine (IMP-0903) consumes this."""

    strategy_id: str
    manifest_version: str
    as_of: datetime
    score: Decimal
    action_tier: ActionTier
    gate_passed: bool
    vetoes_fired: tuple[str, ...]
    conflicts: tuple[tuple[str, str], ...]
    contributions: tuple[FusionContribution, ...]
    reason_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class SignalFusion:
    """Deterministic fusion of frozen observations under one manifest."""

    def __init__(self, policy: FusionPolicy) -> None:
        self._policy = policy

    def fuse(
        self,
        inputs: tuple[FusionInput, ...],
        manifest_evaluation: ManifestEvaluation,
        *,
        as_of: datetime,
    ) -> FusionResult:
        if as_of.tzinfo is None:
            raise FusionError("as_of must be timezone-aware")
        policy = self._policy
        refs = [item.assessment_ref for item in inputs]
        if len(refs) != len(set(refs)):
            raise FusionError("duplicate assessment_ref (same evidence under two entries)")
        for item in inputs:
            assessment = item.assessment
            if assessment.available_at > as_of or assessment.observed_at > as_of:
                raise FusionError(
                    f"lookahead: observation {item.assessment_ref!r} after as_of"
                )

        reason_codes: list[str] = []

        # -- cluster dedup (DoD + section 28: copies of one event get no
        #    independent weight). Deterministic representative per cluster.
        by_cluster: dict[str, list[FusionInput]] = {}
        for item in sorted(
            inputs, key=lambda x: (x.assessment.observed_at, x.assessment_ref)
        ):
            by_cluster.setdefault(item.cluster_key, []).append(item)

        contributions: list[FusionContribution] = []
        counted: list[tuple[FusionInput, Decimal]] = []
        for cluster_key, members in sorted(by_cluster.items()):
            representative = members[0]
            for member in members:
                is_representative = member is representative
                weight = Decimal(0)
                if is_representative:
                    family_weight = policy.weight_for(member.assessment.signal_family)
                    if family_weight is None:
                        reason_codes.append(
                            f"FAMILY_UNWEIGHTED_{member.assessment.signal_family.value}"
                        )
                    else:
                        weight = family_weight
                        if (
                            member.assessment.calibration_status
                            is not CalibrationStatus.CALIBRATED
                        ):
                            # 31.5: uncalibrated authority is policy-limited.
                            weight *= policy.unverified_multiplier
                        counted.append((member, weight))
                else:
                    reason_codes.append(f"DEDUPED_{member.assessment_ref}")
                contributions.append(
                    FusionContribution(
                        assessment_ref=member.assessment_ref,
                        source_id=member.source_id,
                        cluster_key=cluster_key,
                        signal_family=member.assessment.signal_family,
                        model_id=member.assessment.model_id,
                        model_version=member.assessment.model_version,
                        calibration_status=member.assessment.calibration_status,
                        direction=member.assessment.direction,
                        weight_applied=weight,
                        deduped=not is_representative,
                        deduped_into=None if is_representative else representative.assessment_ref,
                    )
                )

        # -- explicit conflicts (31.6: never a hidden average) -----------------
        conflicts: list[tuple[str, str]] = []
        longs = [m for m, w in counted if m.assessment.direction is SignalDirection.LONG and w > 0]
        shorts = [
            m for m, w in counted if m.assessment.direction is SignalDirection.NEGATIVE and w > 0
        ]
        for long_item in longs:
            for short_item in shorts:
                conflicts.append((long_item.assessment_ref, short_item.assessment_ref))
        if conflicts:
            reason_codes.append("DIRECTION_CONFLICT")

        # -- directional weighted score + manifest soft/bonus ------------------
        score = Decimal(0)
        for member, weight in counted:
            direction = member.assessment.direction
            sign = (
                Decimal(1)
                if direction is SignalDirection.LONG
                else Decimal(-1)
                if direction is SignalDirection.NEGATIVE
                else Decimal(0)
            )
            score += sign * weight * member.assessment.score
        score += manifest_evaluation.soft_score + manifest_evaluation.bonus_score

        # -- gate dominance (31.6): fusion arithmetic never overrides gates ----
        if manifest_evaluation.vetoes_fired:
            action_tier = ActionTier.REJECT
            reason_codes.append("VETO_DOMINATES")
        elif not manifest_evaluation.requirements_satisfied:
            action_tier = ActionTier.WATCH
            reason_codes.append("REQUIRED_UNSATISFIED_NEVER_APPROVE")
        else:
            action_tier = ActionTier.WATCH  # below every policy threshold
            for tier, min_score in self._policy.action_tier_rules:
                if score >= min_score:
                    action_tier = tier
                    reason_codes.append(f"TIER_{tier.value}_BY_POLICY")
                    break
            else:
                reason_codes.append("BELOW_ALL_TIER_THRESHOLDS")

        evidence = [
            f"policy:{policy.policy_version}",
            f"fusion:{FUSION_VERSION}",
            f"manifest:{manifest_evaluation.strategy_id}@{manifest_evaluation.manifest_version}",
            *(f"observation:{item.assessment_ref}@{item.source_id}" for item in inputs),
        ]
        return FusionResult(
            strategy_id=manifest_evaluation.strategy_id,
            manifest_version=manifest_evaluation.manifest_version,
            as_of=as_of,
            score=score,
            action_tier=action_tier,
            gate_passed=manifest_evaluation.gate_passed,
            vetoes_fired=manifest_evaluation.vetoes_fired,
            conflicts=tuple(conflicts),
            contributions=tuple(contributions),
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            evidence_refs=tuple(evidence),
        )


__all__ = [
    "FUSION_VERSION",
    "FusionContribution",
    "FusionError",
    "FusionInput",
    "FusionPolicy",
    "FusionResult",
    "SignalFusion",
]
