"""E2E shadow scenarios (IMP-1301) — full pipeline, shadow only.

Authority: Integration spec ("End-to-End Shadow: a real source to Decision
without an order"), Traceability Matrix (TR-001 scanner-only discovery,
TR-002 non-blocking social, TR-008/009 fusion grammar, TR-010 stale data
blocks, TR-011 action ladder, TR-024 every candidate has a funnel stage),
PHASE-13 ("full system without free automation"). Task scope: all source
types, independent discovery, failures and recovery. DoD: the
traceability acceptance suite passes.

This module is a THIN, deterministic ORCHESTRATION of the existing
canonical engines — it re-implements none of them:

    frozen source event -> canonical normalization/provenance
    (trading_contracts.social dedup) -> candidate -> OpportunityFunnel
    OBSERVED -> frozen SignalAssessments -> StrategyManifestEvaluator ->
    SignalFusion -> DecisionEngine -> RiskEngine -> PortfolioAllocator ->
    FinalPreSubmitCheck (evaluation ONLY) -> counterfactual shadow
    tracking -> funnel terminal/active state -> trace report.

Shadow-only, by construction:

- The harness is a consumer of the IMP-1201 ``ReplayRunner`` over frozen
  fixture events. No adapter, connector, network, broker or database is
  ever touched; a source marked unavailable is recorded DEGRADED — never
  CONNECTED and never a PoI pass.
- The pre-submit stage calls the canonical EVALUATION-ONLY API
  (``FinalPreSubmitCheck.evaluate_only``): the same deterministic gate
  logic, but no authorization object of any kind is constructed anywhere
  on this path — ALLOW/RESIZE in a shadow scenario is a research
  outcome, not an execution permission. Nothing here imports an
  execution agent or a broker package.
- Every failure produces an explicit, attributed, traceable state: a
  candidate never disappears (funnel conservation) and never lingers in
  an ambiguous state — it is ACTIVE at a named stage or terminal with a
  reason code and engine/version attribution.

Traceability: every step appends a ``TraceEntry`` carrying trace_id,
source refs, candidate_id, run/split group, as_of, stage, engine +
version and input/output fingerprints. Identifiers are DERIVED from the
input (sha256 / uuid5) — nothing is randomly generated. ``report()``
fails closed if any lineage link is missing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    DecisionOutcome,
    Environment,
    PortfolioAction,
    RiskDecisionOutcome,
    SignalDirection,
    SignalFamily,
    SourceType,
)
from trading_contracts.social import (
    SOCIAL_SOURCE_TYPES,
    CanonicalSocialMessage,
    compute_social_message_dedup_key,
)

from trading_application.counterfactual_shadow import (
    CounterfactualShadowTracker,
    ShadowCandidate,
    ShadowOrigin,
    ShadowPolicy,
    ShadowReport,
)
from trading_application.decision_engine import (
    ENGINE_VERSION,
    DecisionEngine,
    DecisionPolicy,
)
from trading_application.opportunity_funnel import (
    FunnelReport,
    FunnelStage,
    OpportunityFunnel,
    StageTransition,
    TransitionOutcome,
)
from trading_application.portfolio_allocator import (
    ALLOCATOR_VERSION,
    AllocationCandidate,
    PortfolioAllocationPolicy,
    PortfolioAllocator,
    PortfolioState,
)
from trading_application.pre_submit_check import (
    PRE_SUBMIT_VERSION,
    ApprovalState,
    ExecutionContext,
    FinalPreSubmitCheck,
    PreSubmitAction,
    PreSubmitPolicy,
    PreSubmitRequest,
    RefreshedAccount,
    RefreshedMarket,
)
from trading_application.risk_engine import (
    RISK_ENGINE_VERSION,
    AccountState,
    RiskEngine,
    RiskPolicy,
    TradeRiskRequest,
)
from trading_application.signal_fusion import (
    FUSION_VERSION,
    FusionInput,
    FusionPolicy,
    SignalFusion,
)
from trading_application.strategy_manifest import (
    EVALUATOR_VERSION,
    StrategyManifest,
    StrategyManifestEvaluator,
)

if TYPE_CHECKING:
    from trading_application.replay_runner import (
        Payload,
        ReplayEvent,
        ReplayRunManifest,
        ReplaySnapshot,
    )

E2E_SHADOW_VERSION = "e2e-shadow-1.0.0"

_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000e2e01")


class E2EShadowError(Exception):
    """Fail-closed E2E-shadow failure (corrupt input, broken lineage)."""


def _short_hash(material: str) -> str:
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class TraceEntry:
    """One lineage link. Research-only, deterministic, never mutated."""

    trace_id: str
    candidate_id: str
    source_event_refs: tuple[str, ...]
    run_id: str
    split_id: str
    as_of: datetime
    stage: str
    engine: str
    engine_version: str
    input_fingerprint: str
    output_fingerprint: str
    outcome: str
    finding: str = ""
    research_only: bool = True

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise E2EShadowError("as_of must be timezone-aware")
        if not self.research_only:
            raise E2EShadowError("trace entries are research-only; fail closed")
        if not self.input_fingerprint or not self.output_fingerprint:
            raise E2EShadowError("trace fingerprints are mandatory; fail closed")


@dataclass(frozen=True, slots=True)
class E2EScenarioConfig:
    """Frozen scenario wiring: all engine policies + portfolio fixtures."""

    manifest: StrategyManifest
    fusion_policy: FusionPolicy
    decision_policy: DecisionPolicy
    risk_policy: RiskPolicy
    account: AccountState
    allocation_policy: PortfolioAllocationPolicy
    portfolio: PortfolioState
    pre_submit_policy: PreSubmitPolicy
    shadow_policy: ShadowPolicy
    run_id: str = ""
    split_id: str = ""

    def versions(self) -> dict[str, str]:
        """Exact component versions for the replay dataset manifest."""
        return {
            "e2e-shadow": E2E_SHADOW_VERSION,
            "manifest": f"{self.manifest.strategy_id}@{self.manifest.version}",
            "fusion-policy": self.fusion_policy.policy_version,
            "decision-policy": self.decision_policy.policy_version,
            "risk-policy": self.risk_policy.policy_version,
            "allocation-policy": self.allocation_policy.policy_version,
            "pre-submit-policy": self.pre_submit_policy.policy_version,
            "shadow-policy": self.shadow_policy.policy_version,
        }


@dataclass(frozen=True, slots=True)
class E2EShadowReport:
    """Deterministic E2E trace report. Fails closed on any missing link."""

    run_id: str
    split_id: str
    trace: tuple[TraceEntry, ...]
    funnel: FunnelReport
    shadow: ShadowReport
    total_candidates: int
    active_candidates: int
    terminal_candidates: int
    lineage_complete: bool
    findings: tuple[str, ...]
    report_hash: str
    research_only: bool = True
    affects_live_state: bool = False
    affects_paper_state: bool = False

    def __post_init__(self) -> None:
        if not self.research_only or self.affects_live_state or self.affects_paper_state:
            raise E2EShadowError("the E2E report is research-only; fail closed")
        if not self.lineage_complete:
            raise E2EShadowError("lineage incomplete; the acceptance suite fails closed")
        if self.total_candidates != self.active_candidates + self.terminal_candidates:
            raise E2EShadowError("conservation violated: total != active + terminal")


@dataclass(slots=True)
class _Candidate:
    candidate_id: str
    symbol: str
    direction: SignalDirection
    independent: bool
    source_refs: list[str] = field(default_factory=list)
    assessments: dict[str, tuple[str, SignalAssessment, str, str]] = field(default_factory=dict)
    engine_failures: list[str] = field(default_factory=list)


def _text(payload: Payload, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _decimal(payload: Payload, key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise E2EShadowError(f"payload[{key!r}] must be a string decimal")
    return Decimal(value)


class E2EShadowHarness:
    """ReplayRunner consumer orchestrating the full shadow pipeline.

    Thin composition of the canonical engines. No execution surface, no
    connector, no authorization — everything virtual and traceable.
    """

    def __init__(self, config: E2EScenarioConfig) -> None:
        self._config = config
        self._evaluator = StrategyManifestEvaluator()
        self._fusion = SignalFusion(config.fusion_policy)
        self._decision = DecisionEngine(config.decision_policy)
        self._risk = RiskEngine(config.risk_policy)
        self._allocator = PortfolioAllocator(config.allocation_policy)
        self._pre_submit = FinalPreSubmitCheck(config.pre_submit_policy)
        self._funnel = OpportunityFunnel(run_id=config.run_id, split_id=config.split_id)
        self._shadow = CounterfactualShadowTracker(
            config.shadow_policy, run_id=config.run_id, split_id=config.split_id
        )
        self._candidates: dict[str, _Candidate] = {}
        self._sources: dict[str, str] = {}  # dedup_key -> content_hash
        self._trace: list[TraceEntry] = []

    # -- the injected consumer ------------------------------------------------------
    def consume(
        self,
        event: ReplayEvent,
        snapshot: ReplaySnapshot,
        as_of: datetime,
        manifest: ReplayRunManifest,
    ) -> Payload:
        payload = event.payload
        if event.kind != "BAR" and (
            _text(payload, "run_id"),
            _text(payload, "split_id"),
        ) != (self._config.run_id, self._config.split_id):
            raise E2EShadowError(
                f"event {event.event_id!r} belongs to a different (run_id,"
                " split_id) group; replay runs never mix — fail closed"
            )
        if event.kind == "SCAN":
            note = self._on_scan(event, as_of)
        elif event.kind == "SOURCE":
            note = self._on_source(event, as_of)
        elif event.kind == "ASSESSMENT":
            note = self._on_assessment(event, as_of)
        elif event.kind == "ENGINE_FAILURE":
            note = self._on_engine_failure(event, as_of)
        elif event.kind == "EVALUATE":
            note = self._on_evaluate(event, as_of)
        elif event.kind == "BAR":
            # Price bars feed ONLY the counterfactual shadow tracker.
            inner = self._shadow.consume(event, snapshot, as_of, manifest)
            note = str(inner.get("note") or "bar")
        else:
            raise E2EShadowError(f"unsupported event kind {event.kind!r}; fail closed")
        return {
            "virtual": True,
            "executable": False,
            "as_of": as_of.isoformat(),
            "note": f"SHADOW:{note}",
        }

    # -- tracing helpers ---------------------------------------------------------------
    def _record(
        self,
        candidate_id: str,
        stage: str,
        engine: str,
        engine_version: str,
        as_of: datetime,
        *,
        source_refs: tuple[str, ...],
        inputs: str,
        outputs: str,
        outcome: str,
        finding: str = "",
    ) -> None:
        self._trace.append(
            TraceEntry(
                trace_id=_short_hash(f"{candidate_id}|{stage}|{as_of.isoformat()}|{outputs}"),
                candidate_id=candidate_id,
                source_event_refs=source_refs,
                run_id=self._config.run_id,
                split_id=self._config.split_id,
                as_of=as_of,
                stage=stage,
                engine=engine,
                engine_version=engine_version,
                input_fingerprint=_short_hash(inputs),
                output_fingerprint=_short_hash(outputs),
                outcome=outcome,
                finding=finding,
            )
        )

    def _advance(self, candidate_id: str, stage: FunnelStage, as_of: datetime) -> None:
        self._funnel.record(
            StageTransition(
                transition_id=f"{candidate_id}:{stage.value}",
                candidate_id=candidate_id,
                stage=stage,
                outcome=TransitionOutcome.ADVANCED,
                reason_code="",
                attributed_engine="",
                attributed_version="",
                as_of=as_of,
                run_id=self._config.run_id,
                split_id=self._config.split_id,
            )
        )

    def _terminal(
        self,
        candidate_id: str,
        stage: FunnelStage,
        outcome: TransitionOutcome,
        reason: str,
        engine: str,
        version: str,
        as_of: datetime,
    ) -> None:
        self._funnel.record(
            StageTransition(
                transition_id=f"{candidate_id}:terminal:{stage.value}",
                candidate_id=candidate_id,
                stage=stage,
                outcome=outcome,
                reason_code=reason,
                attributed_engine=engine,
                attributed_version=version,
                as_of=as_of,
                run_id=self._config.run_id,
                split_id=self._config.split_id,
            )
        )

    def _track_shadow(
        self,
        candidate: _Candidate,
        origin: ShadowOrigin,
        reason: str,
        engine: str,
        version: str,
        as_of: datetime,
        payload: Payload,
    ) -> None:
        """Track an untaken candidate counterfactually (research only)."""
        entry_raw = _text(payload, "entry_price")
        quantity_raw = _text(payload, "quantity")
        if not entry_raw.strip() or not quantity_raw.strip():
            return  # no price fixture -> nothing measurable, stays in funnel/trace
        self._shadow.track(
            ShadowCandidate(
                candidate_id=candidate.candidate_id,
                origin=origin,
                reason_code=reason,
                attributed_engine=engine,
                attributed_version=version,
                decision_as_of=as_of,
                symbol=candidate.symbol,
                direction="BUY" if candidate.direction is SignalDirection.LONG else "SELL",
                virtual_quantity=Decimal(quantity_raw),
                reference_price=Decimal(entry_raw),
                run_id=self._config.run_id,
                split_id=self._config.split_id,
            )
        )

    # -- discovery / sources -------------------------------------------------------------
    def _spawn_candidate(
        self,
        candidate_id: str,
        symbol: str,
        direction: str,
        source_ref: str,
        as_of: datetime,
        *,
        independent: bool,
    ) -> _Candidate:
        candidate = _Candidate(
            candidate_id=candidate_id,
            symbol=symbol,
            direction=SignalDirection(direction or "LONG"),
            independent=independent,
        )
        candidate.source_refs.append(source_ref)
        self._candidates[candidate_id] = candidate
        self._advance(candidate_id, FunnelStage.OBSERVED, as_of)
        return candidate

    def _on_scan(self, event: ReplayEvent, as_of: datetime) -> str:
        # TR-001: autonomous discovery — a candidate born from a scan run
        # only, with no social source involved at all.
        payload = event.payload
        candidate_id = _text(payload, "candidate_id")
        if not candidate_id.strip():
            raise E2EShadowError("SCAN event requires candidate_id")
        source_ref = f"scan:{event.event_id}"
        self._spawn_candidate(
            candidate_id,
            _text(payload, "symbol"),
            _text(payload, "direction"),
            source_ref,
            as_of,
            independent=True,
        )
        self._record(
            candidate_id, "DISCOVERED", "scanner", E2E_SHADOW_VERSION, as_of,
            source_refs=(source_ref,), inputs=event.event_id,
            outputs=f"candidate:{candidate_id}", outcome="OBSERVED",
            finding="independent discovery; no social source involved",
        )
        return f"scan:{candidate_id}"

    def _on_source(self, event: ReplayEvent, as_of: datetime) -> str:
        payload = event.payload
        raw_type = _text(payload, "source_type")
        try:
            source_type = SourceType(raw_type)
        except ValueError:
            raise E2EShadowError(
                f"source_type {raw_type!r} is not a canonical SourceType;"
                " no parallel schema — fail closed"
            ) from None
        if _text(payload, "status") == "UNAVAILABLE":
            # TR-002: a down source degrades — it is never CONNECTED, never
            # a PoI pass, and never stops independent discovery.
            self._record(
                "", "SOURCE_UNAVAILABLE", source_type.value, E2E_SHADOW_VERSION, as_of,
                source_refs=(event.event_id,), inputs=event.event_id,
                outputs="degraded", outcome="DEGRADED",
                finding=f"{source_type.value} unavailable: not CONNECTED, not a"
                " PoI pass; other sources and scanners continue",
            )
            return f"source-unavailable:{source_type.value}"

        channel = _text(payload, "channel")
        message_id = _text(payload, "external_message_id") or None
        content = _text(payload, "content")
        content_hash = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
        dedup_key = compute_social_message_dedup_key(
            source_type, channel, message_id, content_hash
        )
        if source_type in SOCIAL_SOURCE_TYPES:
            # Canonical normalization contract — validates the dedup key and
            # the social shape; user-mode and bot-mode observations of the
            # same post collapse to one canonical message.
            CanonicalSocialMessage(
                source_type=source_type,
                channel_external_id=channel,
                external_message_id=message_id,
                content_text=content,
                content_hash=content_hash,
                dedup_key=dedup_key,
            )
        previous = self._sources.get(dedup_key)
        if previous is not None:
            if previous == content_hash:
                self._record(
                    _text(payload, "candidate_id"), "SOURCE_DEDUPED",
                    source_type.value, E2E_SHADOW_VERSION, as_of,
                    source_refs=(event.event_id,), inputs=dedup_key,
                    outputs="dedup", outcome="DEDUPED",
                    finding="identical observation (cross-mode) deduplicated",
                )
                return f"source-dedup:{dedup_key[:15]}"
            raise E2EShadowError(
                f"source {dedup_key!r} appears with conflicting content;"
                " corrupt input — fail closed"
            )
        self._sources[dedup_key] = content_hash

        candidate_id = _text(payload, "candidate_id")
        source_ref = f"{source_type.value.lower()}:{event.event_id}"
        if candidate_id.strip():
            existing = self._candidates.get(candidate_id)
            if existing is None:
                self._spawn_candidate(
                    candidate_id,
                    _text(payload, "symbol"),
                    _text(payload, "direction"),
                    source_ref,
                    as_of,
                    independent=False,
                )
            else:
                existing.source_refs.append(source_ref)
        self._record(
            candidate_id, "SOURCE_NORMALIZED", source_type.value,
            E2E_SHADOW_VERSION, as_of,
            source_refs=(source_ref,), inputs=f"{dedup_key}|{content_hash}",
            outputs=f"normalized:{dedup_key}", outcome="NORMALIZED",
            finding=f"provenance: {source_type.value} via canonical contract",
        )
        return f"source:{source_type.value}"

    # -- assessments / failures -----------------------------------------------------------
    def _require_candidate(self, payload: Payload) -> _Candidate:
        candidate_id = _text(payload, "candidate_id")
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise E2EShadowError(f"unknown candidate {candidate_id!r}; fail closed")
        return candidate

    def _on_assessment(self, event: ReplayEvent, as_of: datetime) -> str:
        payload = event.payload
        candidate = self._require_candidate(payload)
        key = _text(payload, "key")
        valid_raw = _text(payload, "valid_until")
        assessment = SignalAssessment(
            instrument_id=uuid.uuid5(_NAMESPACE, candidate.symbol),
            signal_family=SignalFamily(_text(payload, "family")),
            model_id=_text(payload, "model_id"),
            model_version=_text(payload, "model_version"),
            direction=SignalDirection(_text(payload, "direction")),
            status=AssessmentStatus(_text(payload, "status")),
            score=_decimal(payload, "score"),
            calibration_status=CalibrationStatus(_text(payload, "calibration")),
            observed_at=event.observed_at,
            available_at=event.available_at,
            valid_until=datetime.fromisoformat(valid_raw) if valid_raw.strip() else None,
            evidence_refs=tuple(candidate.source_refs),
        )
        ref = _text(payload, "ref") or f"assessment:{event.event_id}"
        candidate.assessments[key] = (
            ref,
            assessment,
            _text(payload, "source_id") or "internal",
            _text(payload, "cluster_key") or ref,
        )
        self._record(
            candidate.candidate_id, "ASSESSMENT_RECORDED",
            assessment.model_id, assessment.model_version, as_of,
            source_refs=tuple(candidate.source_refs), inputs=event.event_id,
            outputs=f"{key}:{assessment.direction.value}:{assessment.score}",
            outcome="RECORDED",
        )
        return f"assessment:{candidate.candidate_id}:{key}"

    def _on_engine_failure(self, event: ReplayEvent, as_of: datetime) -> str:
        # A model/engine failure degrades explicitly — no crash, no silent
        # completion, and the candidate never disappears.
        payload = event.payload
        candidate = self._require_candidate(payload)
        engine = _text(payload, "engine")
        version = _text(payload, "version")
        reason = _text(payload, "reason") or "ENGINE_FAILURE"
        candidate.engine_failures.append(f"{engine}@{version}:{reason}")
        self._record(
            candidate.candidate_id, "ENGINE_FAILURE", engine, version, as_of,
            source_refs=tuple(candidate.source_refs), inputs=event.event_id,
            outputs=reason, outcome="DEGRADED",
            finding=f"{engine}@{version} failed ({reason}); pipeline continues"
            " degraded — missing evidence can gate but never crashes",
        )
        return f"engine-failure:{candidate.candidate_id}:{engine}"

    # -- the evaluation chain ----------------------------------------------------------------
    def _on_evaluate(self, event: ReplayEvent, as_of: datetime) -> str:
        payload = event.payload
        candidate = self._require_candidate(payload)
        cid = candidate.candidate_id
        refs = tuple(candidate.source_refs)
        config = self._config

        # -- staleness gate (TR-010: stale data blocks) -----------------------
        required_keys = {
            entry.assessment_key
            for entry in config.manifest.entries
            if entry.role.value == "REQUIRED"
        }
        fresh: dict[str, SignalAssessment] = {}
        for key, (_ref, assessment, _src, _cluster) in candidate.assessments.items():
            if assessment.valid_until is not None and assessment.valid_until < as_of:
                if key in required_keys:
                    self._terminal(
                        cid, FunnelStage.ASSESSED, TransitionOutcome.EXPIRED,
                        "DATA_STALE", "e2e-shadow-harness", E2E_SHADOW_VERSION, as_of,
                    )
                    self._record(
                        cid, "STALE_BLOCKED", "e2e-shadow-harness",
                        E2E_SHADOW_VERSION, as_of,
                        source_refs=refs, inputs=key, outputs="DATA_STALE",
                        outcome="EXPIRED",
                        finding=f"required assessment {key!r} expired before as_of;"
                        " blocked fail-closed, never reaches execution",
                    )
                    self._track_shadow(
                        candidate, ShadowOrigin.REJECTED, "DATA_STALE",
                        "e2e-shadow-harness", E2E_SHADOW_VERSION, as_of, payload,
                    )
                    return f"stale-blocked:{cid}"
                continue  # non-required stale evidence is dropped, with a trace
            fresh[key] = assessment

        # -- manifest + fusion --------------------------------------------------
        evaluation = self._evaluator.evaluate(config.manifest, fresh, as_of=as_of)
        self._advance(cid, FunnelStage.ASSESSED, as_of)
        self._record(
            cid, "MANIFEST_EVALUATED", "manifest-evaluator", EVALUATOR_VERSION, as_of,
            source_refs=refs, inputs=",".join(sorted(fresh)),
            outputs=f"gate={evaluation.gate_passed}|vetoes={evaluation.vetoes_fired}",
            outcome="ASSESSED",
            finding="; ".join(candidate.engine_failures),
        )
        fusion_inputs = tuple(
            FusionInput(
                assessment_ref=ref, assessment=assessment,
                source_id=source_id, cluster_key=cluster,
            )
            for key, (ref, assessment, source_id, cluster) in sorted(
                candidate.assessments.items()
            )
            if key in fresh
        )
        fusion = self._fusion.fuse(fusion_inputs, evaluation, as_of=as_of)
        self._advance(cid, FunnelStage.FUSED, as_of)
        self._record(
            cid, "FUSED", "signal-fusion", FUSION_VERSION, as_of,
            source_refs=refs, inputs=",".join(i.assessment_ref for i in fusion_inputs),
            outputs=f"score={fusion.score}|tier={fusion.action_tier.value}",
            outcome="FUSED",
            finding=f"conflicts={len(fusion.conflicts)}" if fusion.conflicts else "",
        )

        # -- deterministic decision ------------------------------------------------
        decision = self._decision.decide(
            uuid.uuid5(_NAMESPACE, cid), fusion, evaluation, as_of=as_of
        )
        outcome = decision.result.outcome
        self._record(
            cid, "DECIDED", "decision-engine", ENGINE_VERSION, as_of,
            source_refs=refs, inputs=decision.result.decision_hash,
            outputs=f"outcome={outcome.value}|tier={decision.result.action_tier.value}",
            outcome=outcome.value,
        )
        if outcome is DecisionOutcome.REJECT:
            reason = (
                decision.explanation.blocking_reasons[0]
                if decision.explanation.blocking_reasons
                else "REJECTED_BY_POLICY"
            )
            self._terminal(
                cid, FunnelStage.DECIDED, TransitionOutcome.REJECTED,
                reason, "decision-engine", ENGINE_VERSION, as_of,
            )
            self._track_shadow(
                candidate, ShadowOrigin.REJECTED, reason,
                "decision-engine", ENGINE_VERSION, as_of, payload,
            )
            return f"rejected:{cid}"
        if outcome is not DecisionOutcome.APPROVE:
            # WATCH / SHADOW / NEEDS_REVIEW: explicitly ACTIVE, never vague,
            # never a blind retry — a human or a later event moves it.
            self._advance(cid, FunnelStage.DECIDED, as_of)
            origin = (
                ShadowOrigin.MANUAL
                if outcome is DecisionOutcome.NEEDS_REVIEW
                else ShadowOrigin.WATCH
            )
            self._track_shadow(
                candidate, origin, f"DECISION_{outcome.value}",
                "decision-engine", ENGINE_VERSION, as_of, payload,
            )
            return f"active:{cid}:{outcome.value}"
        self._advance(cid, FunnelStage.DECIDED, as_of)

        # -- risk (absolute veto) -----------------------------------------------------
        entry_price = _decimal(payload, "entry_price")
        stop_price = _decimal(payload, "stop_price")
        risk = self._risk.evaluate(
            decision,
            TradeRiskRequest(
                symbol=candidate.symbol,
                sector=_text(payload, "sector") or "UNKNOWN",
                cluster=_text(payload, "cluster") or "UNKNOWN",
                source_id=_text(payload, "source_id") or "internal",
                strategy_id=config.manifest.strategy_id,
                environment=Environment.RESEARCH,
                requested_quantity=_decimal(payload, "quantity"),
                entry_price=entry_price,
                stop_price=stop_price,
            ),
            config.account,
            as_of=as_of,
        )
        self._record(
            cid, "RISK_CHECKED", "risk-engine", RISK_ENGINE_VERSION, as_of,
            source_refs=refs, inputs=decision.result.decision_hash,
            outputs=f"outcome={risk.outcome.value}|qty={risk.approved_quantity}",
            outcome=risk.outcome.value,
        )
        if risk.outcome is RiskDecisionOutcome.VETO:
            reason = risk.reason_codes[0] if risk.reason_codes else "RISK_VETO"
            self._terminal(
                cid, FunnelStage.RISK_CHECKED, TransitionOutcome.VETOED,
                reason, "risk-engine", RISK_ENGINE_VERSION, as_of,
            )
            self._track_shadow(
                candidate, ShadowOrigin.REJECTED, reason,
                "risk-engine", RISK_ENGINE_VERSION, as_of, payload,
            )
            return f"vetoed:{cid}"
        self._advance(cid, FunnelStage.RISK_CHECKED, as_of)

        # -- portfolio allocation --------------------------------------------------------
        plan = self._allocator.allocate(
            [
                AllocationCandidate(
                    candidate_id=cid,
                    risk_result=risk,
                    symbol=candidate.symbol,
                    sector=_text(payload, "sector") or "UNKNOWN",
                    theme=_text(payload, "theme") or "UNKNOWN",
                    cluster=_text(payload, "cluster") or "UNKNOWN",
                    source_id=_text(payload, "source_id") or "internal",
                    strategy_id=config.manifest.strategy_id,
                    entry_price=entry_price,
                    per_share_risk=abs(entry_price - stop_price),
                    priority_score=fusion.score,
                )
            ],
            config.portfolio,
            as_of=as_of,
        )
        allocation = plan.allocations[0]
        self._record(
            cid, "ALLOCATED", "portfolio-allocator", ALLOCATOR_VERSION, as_of,
            source_refs=refs, inputs=f"risk-qty={risk.approved_quantity}",
            outputs=f"action={allocation.action.value}|qty={allocation.allocated_quantity}",
            outcome=allocation.action.value,
        )
        if allocation.action is PortfolioAction.REJECT or (
            allocation.allocated_quantity <= 0
            and allocation.action is not PortfolioAction.MANUAL
        ):
            reason = (
                allocation.reason_codes[0]
                if allocation.reason_codes
                else "ALLOCATION_REJECTED"
            )
            self._terminal(
                cid, FunnelStage.ALLOCATED, TransitionOutcome.REJECTED,
                reason, "portfolio-allocator", ALLOCATOR_VERSION, as_of,
            )
            self._track_shadow(
                candidate, ShadowOrigin.REJECTED, reason,
                "portfolio-allocator", ALLOCATOR_VERSION, as_of, payload,
            )
            return f"allocation-rejected:{cid}"
        self._advance(cid, FunnelStage.ALLOCATED, as_of)
        if allocation.action is PortfolioAction.MANUAL:
            self._track_shadow(
                candidate, ShadowOrigin.MANUAL, "ALLOCATION_MANUAL",
                "portfolio-allocator", ALLOCATOR_VERSION, as_of, payload,
            )
            return f"active:{cid}:ALLOCATION_MANUAL"

        # -- final pre-submit: EVALUATION ONLY ----------------------------------------------
        market_raw = _text(payload, "market_price")
        gate = self._pre_submit.evaluate_only(
            PreSubmitRequest(
                allocation=allocation,
                risk_result=risk,
                decision=decision,
                symbol=candidate.symbol,
                strategy_id=config.manifest.strategy_id,
                source_id=_text(payload, "source_id") or "internal",
                environment=Environment.RESEARCH,
                planned_entry_price=entry_price,
                planned_stop_price=stop_price,
            ),
            RefreshedMarket(
                price=Decimal(market_raw) if market_raw.strip() else entry_price,
                observed_at=as_of,
            ),
            RefreshedAccount(
                risk_budget_equity=config.portfolio.risk_budget_equity,
                observed_at=as_of,
            ),
            ApprovalState(approved=True, approved_at=as_of),
            ExecutionContext(),
            as_of=as_of,
        )
        # SHADOW ONLY: evaluate_only never constructs an authorization —
        # ALLOW / RESIZE here is a research outcome, not an execution
        # permission.
        action = gate.action
        self._record(
            cid, "PRE_SUBMIT_EVALUATED", "pre-submit-check", PRE_SUBMIT_VERSION, as_of,
            source_refs=refs, inputs=f"alloc-qty={allocation.allocated_quantity}",
            outputs=f"action={action.value}|qty={gate.evaluated_quantity}",
            outcome=f"SHADOW_{action.value}",
            finding="research outcome only; evaluation-only API — no"
            " authorization exists, nothing submitted",
        )
        self._advance(cid, FunnelStage.PRE_SUBMIT, as_of)
        if action in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE):
            self._advance(cid, FunnelStage.APPROVED, as_of)
            return f"shadow-approved:{cid}:{action.value}"
        if action is PreSubmitAction.NEEDS_REVIEW:
            self._track_shadow(
                candidate, ShadowOrigin.MANUAL, "PRE_SUBMIT_NEEDS_REVIEW",
                "pre-submit-check", PRE_SUBMIT_VERSION, as_of, payload,
            )
            return f"active:{cid}:NEEDS_REVIEW"
        reason = gate.reason_codes[0] if gate.reason_codes else "PRE_SUBMIT_CANCEL"
        self._terminal(
            cid, FunnelStage.PRE_SUBMIT, TransitionOutcome.REJECTED,
            reason, "pre-submit-check", PRE_SUBMIT_VERSION, as_of,
        )
        self._track_shadow(
            candidate, ShadowOrigin.REJECTED, reason,
            "pre-submit-check", PRE_SUBMIT_VERSION, as_of, payload,
        )
        return f"pre-submit-cancelled:{cid}"

    # -- report --------------------------------------------------------------------------------
    def report(self) -> E2EShadowReport:
        funnel_report = self._funnel.report()
        shadow_report = self._shadow.report()
        findings: list[str] = []

        # Lineage must be CONTINUOUS: every funnel candidate has trace
        # entries, starts from a source/discovery link with source refs,
        # and every entry carries fingerprints (enforced per entry).
        traced = {entry.candidate_id for entry in self._trace if entry.candidate_id}
        lineage_complete = True
        for history in funnel_report.histories:
            entries = [e for e in self._trace if e.candidate_id == history.candidate_id]
            if not entries or not entries[0].source_event_refs:
                lineage_complete = False
                findings.append(f"{history.candidate_id}: missing lineage link")
        for candidate_id in traced:
            if candidate_id not in {h.candidate_id for h in funnel_report.histories}:
                lineage_complete = False
                findings.append(f"{candidate_id}: traced but absent from the funnel")

        terminal = funnel_report.total_candidates - funnel_report.active_candidates
        material = {
            "version": E2E_SHADOW_VERSION,
            "run": [self._config.run_id, self._config.split_id],
            "trace": [
                [
                    entry.trace_id, entry.candidate_id, list(entry.source_event_refs),
                    entry.stage, entry.engine, entry.engine_version,
                    entry.input_fingerprint, entry.output_fingerprint,
                    entry.outcome, entry.finding, entry.as_of.isoformat(),
                ]
                for entry in self._trace
            ],
            "funnel_totals": [
                funnel_report.total_candidates,
                funnel_report.active_candidates,
            ],
            "shadow": shadow_report.policy_ref,
        }
        report_hash = "sha256:" + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return E2EShadowReport(
            run_id=self._config.run_id,
            split_id=self._config.split_id,
            trace=tuple(self._trace),
            funnel=funnel_report,
            shadow=shadow_report,
            total_candidates=funnel_report.total_candidates,
            active_candidates=funnel_report.active_candidates,
            terminal_candidates=terminal,
            lineage_complete=lineage_complete,
            findings=tuple(findings),
            report_hash=report_hash,
        )


__all__ = [
    "E2E_SHADOW_VERSION",
    "E2EScenarioConfig",
    "E2EShadowError",
    "E2EShadowHarness",
    "E2EShadowReport",
    "TraceEntry",
]
