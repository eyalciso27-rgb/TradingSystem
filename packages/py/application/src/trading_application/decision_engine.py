"""Deterministic decision engine (IMP-0903).

Authority: Decision_Contract_v0.8 (DecisionResult: LLMs never determine
outcome, quantity or submission — this is a deterministic engine; canonical
DecisionOutcome and ActionTier), Strategy_and_Decision_Framework_v0.5
section 28 (action tiers are policy-declared), section 31.6 (a missing
required assessment is never APPROVE; a hard veto means REJECT; conflicts
are resolved by strategy purpose, never a hidden average; hard gates beat
ML disagreement) and the baseline invariant that the Risk Engine holds
ABSOLUTE veto downstream — nothing decided here bypasses risk (IMP-0904).

Pure decision over FROZEN inputs (ADR-0014): the IMP-0902 ``FusionResult``,
the IMP-0901 ``ManifestEvaluation``, a candidate reference and a
``DecisionPolicy``. Output is a canonical ``DecisionResult`` plus a full
explanation — never an Order/OrderIntent/broker action and never a real
PAPER_AUTO submission.

Hard rules:

- **Deterministic** (DoD): the decision_id is derived (uuid5) from the full
  decision content, so the same snapshot/version/policy/as_of produces the
  IDENTICAL decision object.
- **Gate dominance**: a fused REJECT (fired veto) can only be REJECT;
  a failed manifest gate can only be the policy's non-approving
  unsatisfied-outcome (never APPROVE); direction conflicts route to the
  policy's conflict outcome instead of a silent positive.
- **No invented rules**: the tier->outcome mapping, the unsatisfied-gate
  outcome and the conflict outcome are all ``DecisionPolicy`` inputs.
- **Risk is pending, not bypassed**: every non-rejecting decision carries an
  explicit RISK_EVALUATION_PENDING marker — approval here is pre-risk and
  the risk engine (IMP-0904) keeps absolute veto.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from trading_contracts.decision import DecisionResult
from trading_contracts.enums import ActionTier, DecisionOutcome

from trading_application.signal_fusion import FusionResult
from trading_application.strategy_manifest import ManifestEvaluation

ENGINE_VERSION = "decision-engine-1.0.0"

# Deterministic namespace for content-derived decision ids.
_DECISION_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-00000000d0c1")

_APPROVING_OUTCOMES = frozenset({DecisionOutcome.APPROVE})


class DecisionEngineError(Exception):
    """Fail-closed decision failure (bad policy/inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    """All outcome rules — policy/config input, nothing in code."""

    policy_version: str  # must match the canonical VERSION_PATTERN (semver)
    tier_outcomes: tuple[tuple[ActionTier, DecisionOutcome], ...]
    unsatisfied_gate_outcome: DecisionOutcome
    conflict_outcome: DecisionOutcome

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise DecisionEngineError("policy_version is mandatory")
        tiers = [tier for tier, _outcome in self.tier_outcomes]
        if len(tiers) != len(set(tiers)):
            raise DecisionEngineError("duplicate tier mapping")
        if not self.tier_outcomes:
            raise DecisionEngineError("tier outcome mapping is mandatory")
        for tier, outcome in self.tier_outcomes:
            if tier is ActionTier.REJECT and outcome is not DecisionOutcome.REJECT:
                raise DecisionEngineError("a REJECT tier may only map to REJECT")
        if self.unsatisfied_gate_outcome in _APPROVING_OUTCOMES:
            raise DecisionEngineError(
                "an unsatisfied gate may never map to an approving outcome (31.6)"
            )
        if self.conflict_outcome in _APPROVING_OUTCOMES:
            raise DecisionEngineError(
                "a direction conflict may never map straight to APPROVE"
            )

    def outcome_for(self, tier: ActionTier) -> DecisionOutcome:
        for candidate, outcome in self.tier_outcomes:
            if candidate is tier:
                return outcome
        raise DecisionEngineError(f"no outcome mapping for tier {tier.value} (fail closed)")


@dataclass(frozen=True, slots=True)
class DecisionExplanation:
    """Full dashboard/debug/audit explanation of one decision."""

    fusion_ref: str
    manifest_ref: str
    policy_ref: str
    engine_version: str
    reason_codes: tuple[str, ...]
    blocking_reasons: tuple[str, ...]
    input_refs: tuple[str, ...]
    validation_note: str


@dataclass(frozen=True, slots=True)
class EngineDecision:
    """Canonical DecisionResult + its explanation (never an order)."""

    result: DecisionResult
    explanation: DecisionExplanation


class DecisionEngine:
    """Deterministic outcome/action-tier engine over fused signals."""

    def __init__(self, policy: DecisionPolicy) -> None:
        self._policy = policy

    def decide(
        self,
        candidate_id: UUID,
        fusion: FusionResult,
        manifest_evaluation: ManifestEvaluation,
        *,
        as_of: datetime,
    ) -> EngineDecision:
        if as_of.tzinfo is None:
            raise DecisionEngineError("as_of must be timezone-aware")
        if fusion.as_of > as_of or manifest_evaluation.as_of > as_of:
            raise DecisionEngineError("lookahead: inputs are newer than as_of")
        if fusion.strategy_id != manifest_evaluation.strategy_id or (
            fusion.manifest_version != manifest_evaluation.manifest_version
        ):
            raise DecisionEngineError(
                "fusion and manifest evaluation belong to different strategy versions"
            )

        policy = self._policy
        reason_codes: list[str] = []
        blocking: list[str] = []

        # -- gate dominance: fusion arithmetic never overrides gates ----------
        if fusion.vetoes_fired or fusion.action_tier is ActionTier.REJECT:
            outcome = DecisionOutcome.REJECT
            action_tier = ActionTier.REJECT
            blocking.extend(f"VETO_{ref}" for ref in fusion.vetoes_fired)
            reason_codes.append("REJECTED_BY_VETO")
        elif not manifest_evaluation.requirements_satisfied:
            outcome = policy.unsatisfied_gate_outcome
            action_tier = ActionTier.WATCH
            blocking.append("MANIFEST_GATE_UNSATISFIED")
            reason_codes.append("GATE_UNSATISFIED_NEVER_APPROVE")
        else:
            action_tier = fusion.action_tier
            outcome = policy.outcome_for(action_tier)
            reason_codes.append(f"OUTCOME_{outcome.value}_BY_POLICY_TIER_{action_tier.value}")
            if fusion.conflicts and outcome in _APPROVING_OUTCOMES:
                # 31.6/req: conflicting evidence never becomes a silent positive.
                outcome = policy.conflict_outcome
                reason_codes.append("CONFLICT_ROUTED_BY_POLICY")
                blocking.append("DIRECTION_CONFLICT_PRESENT")

        if outcome not in (DecisionOutcome.REJECT,):
            # Risk engine (IMP-0904) holds absolute veto downstream; this
            # decision is explicitly pre-risk and never bypasses it.
            reason_codes.append("RISK_EVALUATION_PENDING")

        veto_reason_codes = tuple(
            f"VETO_{ref.upper().replace('.', '_').replace('-', '_')}"
            for ref in fusion.vetoes_fired
        )
        explanation = DecisionExplanation(
            fusion_ref=f"fusion:{fusion.strategy_id}@{fusion.manifest_version}",
            manifest_ref=(
                f"manifest:{manifest_evaluation.strategy_id}"
                f"@{manifest_evaluation.manifest_version}"
            ),
            policy_ref=f"decision-policy:{policy.policy_version}",
            engine_version=ENGINE_VERSION,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            blocking_reasons=tuple(dict.fromkeys(blocking)),
            input_refs=tuple(fusion.evidence_refs),
            validation_note="FIXTURES_VALIDATED_ONLY (ADR-0014); calibration UNVERIFIED "
            "authority limited by fusion/manifest policy",
        )

        # Deterministic decision id: derived from the full decision content,
        # so the same snapshot/version/policy/as_of yields the same decision.
        fingerprint = "|".join(
            (
                str(candidate_id),
                fusion.strategy_id,
                fusion.manifest_version,
                str(fusion.score),
                fusion.action_tier.value,
                outcome.value,
                policy.policy_version,
                as_of.isoformat(),
                ",".join(fusion.vetoes_fired),
                ",".join(explanation.reason_codes),
            )
        )
        decision_id = uuid.uuid5(_DECISION_NAMESPACE, fingerprint)
        decision_hash = "sha256:" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

        result = DecisionResult(
            decision_id=decision_id,
            candidate_id=candidate_id,
            outcome=outcome,
            action_tier=action_tier,
            strategy_version=fusion.manifest_version,
            decision_policy_version=policy.policy_version,
            supporting_assessment_refs=tuple(
                contribution.assessment_ref
                for contribution in fusion.contributions
                if not contribution.deduped
            ),
            veto_reason_codes=veto_reason_codes,
            decided_at=as_of,
            decision_hash=decision_hash,
        )
        return EngineDecision(result=result, explanation=explanation)


__all__ = [
    "ENGINE_VERSION",
    "DecisionEngine",
    "DecisionEngineError",
    "DecisionExplanation",
    "DecisionPolicy",
    "EngineDecision",
]
