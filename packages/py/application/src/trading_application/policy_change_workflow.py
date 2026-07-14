"""Policy change workflow (IMP-1206) — proposal, replay, shadow, approval,
rollback.

Authority: Opportunity Calibration & Model Governance ("Promotion and
Rollback are documented HUMAN actions"; §10 "several changes at once
require a clear Change Set and ablation"), Decision Contract §version
lifecycle (a new version passes REPLAY -> SHADOW before any activation),
MVP-G9 (replay/challenger runs never change the active version),
Acceptance ("100% of policies/models in a decision point at a fixed
version"; "a starvation alert does not change the Active Policy").
Task scope: proposal, replay, shadow, approval and rollback.
DoD: no direct edit of the active immutable version.

Everything here is deterministic, in-memory, research/governance-only:

- ``PolicyVersion`` is IMMUTABLE. Registering the same component+version
  with an identical fingerprint is idempotent; with a different
  fingerprint it is rejected fail-closed. There is no update, edit,
  replace or delete API — a change is always a NEW version.
- The active pointer is an append-only chain of ``PolicyPointerRecord``
  entries. Approval and rollback APPEND a new logical record pointing at
  an existing immutable version; no prior record and no version is ever
  mutated or deleted. Every record is marked ``research_only=True`` and
  never touches live or paper state.
- Every stage transition (PROPOSED -> REPLAY_VERIFIED -> SHADOW_VERIFIED
  -> APPROVAL_PENDING -> APPROVED, or -> REJECTED) requires an EXPLICIT
  HUMAN actor — blank, system, scheduler, timer, metric or other default
  actors are rejected. Stages cannot be skipped. Metrics and reports can
  only recommend; no metric, timer or model result can move the pointer.
- Evidence is mandatory and exact: replay evidence to pass REPLAY,
  shadow/challenger evidence to pass SHADOW, and the full bundle to reach
  APPROVAL_PENDING. Every evidence ref must carry a non-empty result
  hash, the EXACT fingerprint of the proposed to_version, and a
  created_as_of that does not postdate the transition it supports — no
  future evidence, no fallback.
- The audit trail is append-only with tz-aware, monotonic as_of;
  identical duplicate transitions dedup idempotently, conflicting reuse
  of a transition id is rejected as corruption.

Nothing in this module starts trading, touches a broker, changes a live
runtime or performs a real registry write. APPROVED means "a documented
human decision now exists" — activation of anything real stays an
owner-governance action outside this codebase.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

WORKFLOW_VERSION = "policy-change-workflow-1.0.0"

#: Actor ids that can never authorize a transition — a HUMAN is required.
_NON_HUMAN_ACTORS = frozenset(
    {"", "system", "auto", "default", "scheduler", "timer", "metric", "cron", "bot"}
)


class WorkflowError(Exception):
    """Fail-closed policy-change failure (bad wiring, missing evidence)."""


def _require_aware(stamp: datetime, name: str) -> None:
    if stamp.tzinfo is None:
        raise WorkflowError(f"{name} must be timezone-aware")


def _require_human_actor(actor_id: str) -> None:
    if actor_id.strip().lower() in _NON_HUMAN_ACTORS:
        raise WorkflowError(
            f"actor_id {actor_id!r} is not an explicit human actor; promotion and"
            " rollback are documented human actions — fail closed"
        )


class EvidenceKind(StrEnum):
    REPLAY = "REPLAY"
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    CHALLENGER = "CHALLENGER"
    FUNNEL = "FUNNEL"


class WorkflowStage(StrEnum):
    PROPOSED = "PROPOSED"
    REPLAY_VERIFIED = "REPLAY_VERIFIED"
    SHADOW_VERIFIED = "SHADOW_VERIFIED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ROLLED_BACK = "ROLLED_BACK"


_NEXT_STAGE: dict[WorkflowStage, WorkflowStage] = {
    WorkflowStage.PROPOSED: WorkflowStage.REPLAY_VERIFIED,
    WorkflowStage.REPLAY_VERIFIED: WorkflowStage.SHADOW_VERIFIED,
    WorkflowStage.SHADOW_VERIFIED: WorkflowStage.APPROVAL_PENDING,
    WorkflowStage.APPROVAL_PENDING: WorkflowStage.APPROVED,
}

_TERMINAL_STAGES = frozenset(
    {WorkflowStage.REJECTED, WorkflowStage.ROLLED_BACK}
)


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    """One immutable, registered policy/model version."""

    component: str
    version: str
    content_fingerprint: str
    created_as_of: datetime

    def __post_init__(self) -> None:
        for name in ("component", "version", "content_fingerprint"):
            if not str(getattr(self, name)).strip():
                raise WorkflowError(f"PolicyVersion.{name} is mandatory")
        _require_aware(self.created_as_of, "created_as_of")

    def canonical(self) -> tuple[str, str, str, str]:
        return (
            self.component,
            self.version,
            self.content_fingerprint,
            self.created_as_of.isoformat(),
        )


@dataclass(frozen=True, slots=True)
class ChangeSetItem:
    """One explicit parameter change inside a proposal."""

    parameter: str
    old_value: str
    new_value: str
    ablation_note: str = ""

    def __post_init__(self) -> None:
        if not self.parameter.strip():
            raise WorkflowError("ChangeSetItem.parameter is mandatory")


@dataclass(frozen=True, slots=True)
class PolicyChangeProposal:
    """A frozen from->to change proposal with an explicit change set."""

    proposal_id: str
    component: str
    from_version: str
    to_version: str
    change_set: tuple[ChangeSetItem, ...]
    author_actor_id: str
    created_as_of: datetime
    rationale: str
    from_fingerprint: str
    to_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "proposal_id",
            "component",
            "from_version",
            "to_version",
            "rationale",
            "from_fingerprint",
            "to_fingerprint",
        ):
            if not str(getattr(self, name)).strip():
                raise WorkflowError(f"proposal field {name!r} is mandatory; vague or"
                                    " incomplete proposals are rejected")
        _require_human_actor(self.author_actor_id)
        _require_aware(self.created_as_of, "created_as_of")
        if self.from_version == self.to_version:
            raise WorkflowError("to_version must differ from from_version")
        if not self.change_set:
            raise WorkflowError("change_set must not be empty")
        if len(self.change_set) > 1:
            # Governance: several changes at once require a clear change
            # set AND explicit ablation notes.
            for item in self.change_set:
                if not item.ablation_note.strip():
                    raise WorkflowError(
                        f"multi-item change set requires an explicit ablation note"
                        f" for {item.parameter!r}; fail closed"
                    )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A frozen pointer to a deterministic research run's result."""

    kind: EvidenceKind
    run_id: str
    seed: str
    result_hash: str
    version_fingerprint: str
    created_as_of: datetime

    def __post_init__(self) -> None:
        for name in ("run_id", "seed", "result_hash", "version_fingerprint"):
            if not str(getattr(self, name)).strip():
                raise WorkflowError(
                    f"evidence {name} is mandatory — no empty hashes and no"
                    " fallback; fail closed"
                )
        _require_aware(self.created_as_of, "created_as_of")

    def canonical(self) -> tuple[str, ...]:
        return (
            self.kind.value,
            self.run_id,
            self.seed,
            self.result_hash,
            self.version_fingerprint,
            self.created_as_of.isoformat(),
        )


@dataclass(frozen=True, slots=True)
class WorkflowTransition:
    """One human-authorized stage transition request."""

    transition_id: str
    proposal_id: str
    from_stage: WorkflowStage
    to_stage: WorkflowStage
    actor_id: str
    as_of: datetime
    evidence: tuple[EvidenceRef, ...]
    reason: str

    def __post_init__(self) -> None:
        if not self.transition_id.strip():
            raise WorkflowError("transition_id is mandatory")
        if not self.proposal_id.strip():
            raise WorkflowError("proposal_id is mandatory")
        if not self.reason.strip():
            raise WorkflowError("a documented reason is mandatory for every transition")
        _require_human_actor(self.actor_id)
        _require_aware(self.as_of, "as_of")

    def canonical(self) -> tuple[object, ...]:
        return (
            self.transition_id,
            self.proposal_id,
            self.from_stage.value,
            self.to_stage.value,
            self.actor_id,
            self.as_of.isoformat(),
            tuple(ref.canonical() for ref in self.evidence),
            self.reason,
        )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """The documented human approval that closed a proposal."""

    proposal_id: str
    actor_id: str
    as_of: datetime
    evidence_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_human_actor(self.actor_id)
        _require_aware(self.as_of, "as_of")


@dataclass(frozen=True, slots=True)
class PolicyPointerRecord:
    """One append-only logical active-pointer record. Never mutated."""

    record_id: str
    component: str
    from_version: str
    to_version: str
    actor_id: str
    reason: str
    proposal_id: str
    as_of: datetime
    previous_pointer_record_id: str
    research_only: bool = True
    affects_live_state: bool = False
    affects_paper_state: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if (
            not self.research_only
            or self.affects_live_state
            or self.affects_paper_state
        ):
            raise WorkflowError(
                "a pointer record is a research/governance record only; it can"
                " never touch live or paper state — fail closed"
            )


@dataclass(frozen=True, slots=True)
class RollbackRecord:
    """A documented human rollback to a previously registered version."""

    rollback_id: str
    proposal_id: str
    actor_id: str
    reason: str
    to_version: str
    as_of: datetime

    def __post_init__(self) -> None:
        if not self.rollback_id.strip():
            raise WorkflowError("rollback_id is mandatory")
        if not self.reason.strip():
            raise WorkflowError(
                "rollback requires a documented reason; fail closed"
            )
        if not self.to_version.strip():
            raise WorkflowError("rollback requires an explicit target version")
        _require_human_actor(self.actor_id)
        _require_aware(self.as_of, "as_of")


@dataclass(frozen=True, slots=True)
class PolicyChangeAuditEntry:
    """One append-only audit line. Entries are never edited or deleted."""

    entry_index: int
    transition_id: str
    proposal_id: str
    stage_before: WorkflowStage
    stage_after: WorkflowStage
    actor_id: str
    reason: str
    as_of: datetime
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True, slots=True)
class PolicyChangeReport:
    """Deterministic governance report. Recommends only; activates nothing."""

    active: tuple[tuple[str, str], ...]  # (component, active version)
    proposals: tuple[tuple[str, str], ...]  # (proposal_id, stage)
    pointer_history: tuple[PolicyPointerRecord, ...]
    approvals: tuple[ApprovalRecord, ...]
    audit: tuple[PolicyChangeAuditEntry, ...]
    findings: tuple[str, ...]
    report_hash: str
    research_only: bool = True
    affects_live_state: bool = False
    affects_paper_state: bool = False

    def __post_init__(self) -> None:
        if (
            not self.research_only
            or self.affects_live_state
            or self.affects_paper_state
        ):
            raise WorkflowError("the report is research/governance only; fail closed")


class PolicyChangeWorkflow:
    """Proposal -> replay -> shadow -> approval -> (rollback) governance.

    Versions are immutable, the pointer chain is append-only, every
    transition needs an explicit human actor, and nothing here can start
    or change live/paper trading.
    """

    def __init__(
        self,
        versions: Sequence[PolicyVersion],
        initial_active: Mapping[str, str],
        *,
        actor_id: str,
        as_of: datetime,
    ) -> None:
        _require_human_actor(actor_id)
        _require_aware(as_of, "as_of")
        self._versions: dict[tuple[str, str], PolicyVersion] = {}
        for version in versions:
            self.register_version(version)
        self._pointer_history: dict[str, list[PolicyPointerRecord]] = {}
        for component, version_id in sorted(initial_active.items()):
            if (component, version_id) not in self._versions:
                raise WorkflowError(
                    f"initial active version {version_id!r} for {component!r} is"
                    " not a registered immutable version; fail closed"
                )
            self._pointer_history[component] = [
                PolicyPointerRecord(
                    record_id=f"ptr:{component}:0",
                    component=component,
                    from_version="",
                    to_version=version_id,
                    actor_id=actor_id,
                    reason="bootstrap: frozen active-pointer snapshot at workflow start",
                    proposal_id="",
                    as_of=as_of,
                    previous_pointer_record_id="",
                )
            ]
        self._proposals: dict[str, PolicyChangeProposal] = {}
        self._stages: dict[str, WorkflowStage] = {}
        self._evidence: dict[str, list[EvidenceRef]] = {}
        self._approvals: list[ApprovalRecord] = []
        self._audit: list[PolicyChangeAuditEntry] = []
        self._seen_transitions: dict[str, tuple[object, ...]] = {}
        self._last_as_of: datetime = as_of

    # -- immutable version registry ------------------------------------------------
    def register_version(self, version: PolicyVersion) -> None:
        key = (version.component, version.version)
        existing = self._versions.get(key)
        if existing is not None:
            if existing.canonical() == version.canonical():
                return  # idempotent re-registration of the identical version
            raise WorkflowError(
                f"{version.component}:{version.version} is already registered with"
                " a different content fingerprint; versions are immutable — a"
                " change requires a NEW version; fail closed"
            )
        self._versions[key] = version

    def active_version(self, component: str) -> str:
        history = self._pointer_history.get(component)
        if not history:
            raise WorkflowError(f"component {component!r} has no active pointer")
        return history[-1].to_version

    # -- proposals -----------------------------------------------------------------
    def submit_proposal(self, proposal: PolicyChangeProposal) -> None:
        if proposal.proposal_id in self._proposals:
            if self._proposals[proposal.proposal_id] == proposal:
                return  # idempotent duplicate
            raise WorkflowError(
                f"proposal {proposal.proposal_id!r} already exists with different"
                " content; fail closed"
            )
        for version_id, fingerprint, label in (
            (proposal.from_version, proposal.from_fingerprint, "from_version"),
            (proposal.to_version, proposal.to_fingerprint, "to_version"),
        ):
            registered = self._versions.get((proposal.component, version_id))
            if registered is None:
                raise WorkflowError(
                    f"proposal {label} {version_id!r} is not a registered immutable"
                    " version; fail closed"
                )
            if registered.content_fingerprint != fingerprint:
                raise WorkflowError(
                    f"proposal {label} fingerprint does not match the registered"
                    " immutable version; fail closed"
                )
        active = self.active_version(proposal.component)
        if proposal.from_version != active:
            raise WorkflowError(
                f"proposal from_version {proposal.from_version!r} does not match"
                f" the frozen active-pointer snapshot ({active!r}); fail closed"
            )
        self._proposals[proposal.proposal_id] = proposal
        self._stages[proposal.proposal_id] = WorkflowStage.PROPOSED
        self._evidence[proposal.proposal_id] = []

    # -- transitions ------------------------------------------------------------------
    def advance(self, transition: WorkflowTransition) -> None:
        canonical = transition.canonical()
        previous = self._seen_transitions.get(transition.transition_id)
        if previous is not None:
            if previous == canonical:
                return  # idempotent duplicate
            raise WorkflowError(
                f"transition {transition.transition_id!r} appears with different"
                " content; corrupt input — fail closed"
            )
        proposal = self._proposals.get(transition.proposal_id)
        if proposal is None:
            raise WorkflowError(f"unknown proposal {transition.proposal_id!r}")
        current = self._stages[transition.proposal_id]
        if current in _TERMINAL_STAGES:
            raise WorkflowError(
                f"proposal {transition.proposal_id!r} is terminal ({current});"
                " no further transitions"
            )
        if current is WorkflowStage.APPROVED:
            raise WorkflowError(
                "an APPROVED proposal only supports an explicit rollback;"
                " fail closed"
            )
        if transition.from_stage is not current:
            raise WorkflowError(
                f"transition declares from_stage {transition.from_stage} but the"
                f" proposal is at {current}; fail closed"
            )
        if transition.as_of < self._last_as_of:
            raise WorkflowError("as_of must be monotonic; it never regresses")
        if transition.to_stage is not WorkflowStage.REJECTED:
            expected = _NEXT_STAGE[current]
            if transition.to_stage is not expected:
                raise WorkflowError(
                    f"stage skipping is forbidden: {current} may only advance to"
                    f" {expected} (or REJECTED), not {transition.to_stage}"
                )
        self._check_evidence(proposal, transition)

        self._seen_transitions[transition.transition_id] = canonical
        self._evidence[transition.proposal_id].extend(transition.evidence)
        self._stages[transition.proposal_id] = transition.to_stage
        self._last_as_of = transition.as_of
        self._audit.append(
            PolicyChangeAuditEntry(
                entry_index=len(self._audit),
                transition_id=transition.transition_id,
                proposal_id=transition.proposal_id,
                stage_before=current,
                stage_after=transition.to_stage,
                actor_id=transition.actor_id,
                reason=transition.reason,
                as_of=transition.as_of,
                evidence=transition.evidence,
            )
        )
        if transition.to_stage is WorkflowStage.APPROVED:
            self._approve(proposal, transition)

    def _check_evidence(
        self, proposal: PolicyChangeProposal, transition: WorkflowTransition
    ) -> None:
        to_fingerprint = self._versions[
            (proposal.component, proposal.to_version)
        ].content_fingerprint
        for ref in transition.evidence:
            if ref.version_fingerprint != to_fingerprint:
                raise WorkflowError(
                    f"evidence {ref.run_id!r} refers to a different version"
                    " fingerprint than the proposed to_version; fail closed"
                )
            if ref.created_as_of > transition.as_of:
                raise WorkflowError(
                    f"evidence {ref.run_id!r} postdates the transition it"
                    " supports; future evidence is forbidden — fail closed"
                )
        kinds = {ref.kind for ref in transition.evidence}
        accumulated = {
            ref.kind for ref in self._evidence[proposal.proposal_id]
        } | kinds
        if transition.to_stage is WorkflowStage.REPLAY_VERIFIED:
            if EvidenceKind.REPLAY not in kinds:
                raise WorkflowError(
                    "REPLAY_VERIFIED requires replay evidence for the exact"
                    " to_version; fail closed"
                )
        elif transition.to_stage is WorkflowStage.SHADOW_VERIFIED:
            if not kinds & {EvidenceKind.SHADOW, EvidenceKind.CHALLENGER}:
                raise WorkflowError(
                    "SHADOW_VERIFIED requires shadow or challenger evidence;"
                    " fail closed"
                )
        elif transition.to_stage is WorkflowStage.APPROVAL_PENDING:
            if EvidenceKind.REPLAY not in accumulated or not accumulated & {
                EvidenceKind.SHADOW,
                EvidenceKind.CHALLENGER,
            }:
                raise WorkflowError(
                    "APPROVAL_PENDING requires the full evidence bundle"
                    " (replay AND shadow/challenger); fail closed"
                )

    def _approve(
        self, proposal: PolicyChangeProposal, transition: WorkflowTransition
    ) -> None:
        # APPROVED == a documented human decision. It APPENDS a logical
        # pointer record; it never edits a version, never mutates an
        # earlier pointer and never activates a runtime, broker or
        # trading mode.
        self._approvals.append(
            ApprovalRecord(
                proposal_id=proposal.proposal_id,
                actor_id=transition.actor_id,
                as_of=transition.as_of,
                evidence_hashes=tuple(
                    ref.result_hash for ref in self._evidence[proposal.proposal_id]
                ),
            )
        )
        self._append_pointer(
            component=proposal.component,
            to_version=proposal.to_version,
            actor_id=transition.actor_id,
            reason=transition.reason,
            proposal_id=proposal.proposal_id,
            as_of=transition.as_of,
        )

    # -- rollback ---------------------------------------------------------------------
    def rollback(self, record: RollbackRecord) -> None:
        proposal = self._proposals.get(record.proposal_id)
        if proposal is None:
            raise WorkflowError(f"unknown proposal {record.proposal_id!r}")
        if self._stages[record.proposal_id] is not WorkflowStage.APPROVED:
            raise WorkflowError(
                "rollback is defined only from APPROVED; fail closed"
            )
        if (proposal.component, record.to_version) not in self._versions:
            raise WorkflowError(
                f"rollback target {record.to_version!r} is not a registered"
                " immutable version; fail closed"
            )
        if record.as_of < self._last_as_of:
            raise WorkflowError("as_of must be monotonic; it never regresses")
        # Rollback APPENDS a new pointer record to a previous version; it
        # deletes nothing — approvals, pointers and versions all remain.
        self._append_pointer(
            component=proposal.component,
            to_version=record.to_version,
            actor_id=record.actor_id,
            reason=record.reason,
            proposal_id=record.proposal_id,
            as_of=record.as_of,
        )
        self._stages[record.proposal_id] = WorkflowStage.ROLLED_BACK
        self._last_as_of = record.as_of
        self._audit.append(
            PolicyChangeAuditEntry(
                entry_index=len(self._audit),
                transition_id=f"rollback:{record.rollback_id}",
                proposal_id=record.proposal_id,
                stage_before=WorkflowStage.APPROVED,
                stage_after=WorkflowStage.ROLLED_BACK,
                actor_id=record.actor_id,
                reason=record.reason,
                as_of=record.as_of,
                evidence=(),
            )
        )

    def _append_pointer(
        self,
        *,
        component: str,
        to_version: str,
        actor_id: str,
        reason: str,
        proposal_id: str,
        as_of: datetime,
    ) -> None:
        history = self._pointer_history[component]
        previous = history[-1]
        history.append(
            PolicyPointerRecord(
                record_id=f"ptr:{component}:{len(history)}",
                component=component,
                from_version=previous.to_version,
                to_version=to_version,
                actor_id=actor_id,
                reason=reason,
                proposal_id=proposal_id,
                as_of=as_of,
                previous_pointer_record_id=previous.record_id,
            )
        )

    # -- report ------------------------------------------------------------------------
    def pointer_history(self, component: str) -> tuple[PolicyPointerRecord, ...]:
        return tuple(self._pointer_history.get(component, ()))

    def audit(self) -> tuple[PolicyChangeAuditEntry, ...]:
        return tuple(self._audit)

    def report(self) -> PolicyChangeReport:
        active = tuple(
            (component, history[-1].to_version)
            for component, history in sorted(self._pointer_history.items())
        )
        proposals = tuple(
            (proposal_id, self._stages[proposal_id].value)
            for proposal_id in sorted(self._proposals)
        )
        pointer_history = tuple(
            record
            for _, history in sorted(self._pointer_history.items())
            for record in history
        )
        findings = tuple(
            f"proposal {proposal_id}: stage {stage} — recommendation only; no"
            " automatic activation exists in this workflow"
            for proposal_id, stage in proposals
        )
        material = {
            "workflow": WORKFLOW_VERSION,
            "active": [list(pair) for pair in active],
            "proposals": [list(pair) for pair in proposals],
            "pointers": [
                [
                    record.record_id,
                    record.component,
                    record.from_version,
                    record.to_version,
                    record.actor_id,
                    record.proposal_id,
                    record.as_of.isoformat(),
                    record.previous_pointer_record_id,
                ]
                for record in pointer_history
            ],
            "audit": [
                [
                    entry.entry_index,
                    entry.transition_id,
                    entry.proposal_id,
                    entry.stage_before.value,
                    entry.stage_after.value,
                    entry.actor_id,
                    entry.reason,
                    entry.as_of.isoformat(),
                    [list(ref.canonical()) for ref in entry.evidence],
                ]
                for entry in self._audit
            ],
        }
        report_hash = "sha256:" + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return PolicyChangeReport(
            active=active,
            proposals=proposals,
            pointer_history=pointer_history,
            approvals=tuple(self._approvals),
            audit=tuple(self._audit),
            findings=findings,
            report_hash=report_hash,
        )


__all__ = [
    "WORKFLOW_VERSION",
    "ApprovalRecord",
    "ChangeSetItem",
    "EvidenceKind",
    "EvidenceRef",
    "PolicyChangeAuditEntry",
    "PolicyChangeProposal",
    "PolicyChangeReport",
    "PolicyChangeWorkflow",
    "PolicyPointerRecord",
    "PolicyVersion",
    "RollbackRecord",
    "WorkflowError",
    "WorkflowStage",
    "WorkflowTransition",
]
