"""Opportunity funnel (IMP-1203) — stage counts + rejection attribution.

Task scope: stage counts and rejection reason attribution. DoD: every
candidate has terminal/stage history — nothing ever disappears from the
report: a candidate is either ACTIVE (explicitly pending) or TERMINAL, and
the conservation check ``total == active + terminal`` is built into the
report.

Pure aggregation over frozen ``StageTransition`` records. The funnel can
be fed directly or as a consumer of the IMP-1201 ``ReplayRunner`` (STAGE
events), in which case canonical ordering, as-of discipline, exact
versions and the deterministic result hash all come from the runner. The
module writes nothing anywhere (no DB, no model registry — aggregation
only), reads no clock, and has no randomness.

Fail-closed rules:

- A candidate's first transition must be at ``OBSERVED``; ADVANCED moves
  to the NEXT canonical stage only (no skipping, no regression).
- ``as_of`` never moves backwards for a candidate.
- After a terminal outcome no further transition is accepted.
- REJECTED/VETOED/EXPIRED/STALE require a reason code and an
  engine+version attribution — no silent fallback.
- An identical duplicate transition is deduplicated idempotently; the
  same transition id with different content is rejected as corruption.
- Transitions from different (run_id, split_id) groups never mix in one
  funnel without explicit grouping — a mismatching group is rejected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from trading_application.replay_runner import (
    Payload,
    ReplayEvent,
    ReplayRunManifest,
    ReplaySnapshot,
)

FUNNEL_VERSION = "opportunity-funnel-1.0.0"


class FunnelError(Exception):
    """Fail-closed funnel failure (ordering, attribution, corruption)."""


class FunnelStage(StrEnum):
    """Canonical pipeline stages, in order."""

    OBSERVED = "OBSERVED"
    ASSESSED = "ASSESSED"
    FUSED = "FUSED"
    DECIDED = "DECIDED"
    RISK_CHECKED = "RISK_CHECKED"
    ALLOCATED = "ALLOCATED"
    PRE_SUBMIT = "PRE_SUBMIT"
    APPROVED = "APPROVED"


STAGE_ORDER: tuple[FunnelStage, ...] = tuple(FunnelStage)
_STAGE_INDEX: Mapping[FunnelStage, int] = {stage: i for i, stage in enumerate(STAGE_ORDER)}


class TransitionOutcome(StrEnum):
    ADVANCED = "ADVANCED"
    REJECTED = "REJECTED"
    VETOED = "VETOED"
    EXPIRED = "EXPIRED"
    STALE = "STALE"


#: Outcomes that end a candidate's journey at the current stage.
TERMINAL_OUTCOMES = frozenset(
    {
        TransitionOutcome.REJECTED,
        TransitionOutcome.VETOED,
        TransitionOutcome.EXPIRED,
        TransitionOutcome.STALE,
    }
)


class CandidateLifecycleStatus(StrEnum):
    ACTIVE = "ACTIVE"  # explicitly pending — never hidden
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class StageTransition:
    """One immutable stage-transition record."""

    transition_id: str
    candidate_id: str
    stage: FunnelStage
    outcome: TransitionOutcome
    reason_code: str
    attributed_engine: str
    attributed_version: str
    as_of: datetime
    run_id: str = ""
    split_id: str = ""

    def __post_init__(self) -> None:
        if not self.transition_id.strip():
            raise FunnelError("transition_id is mandatory")
        if not self.candidate_id.strip():
            raise FunnelError("candidate_id is mandatory")
        if self.as_of.tzinfo is None:
            raise FunnelError("as_of must be timezone-aware")
        if self.outcome in TERMINAL_OUTCOMES:
            if not self.reason_code.strip():
                raise FunnelError(
                    f"{self.outcome.value} transition requires a reason_code"
                    " (no silent fallback)"
                )
            if not self.attributed_engine.strip() or not self.attributed_version.strip():
                raise FunnelError(
                    f"{self.outcome.value} transition requires engine+version"
                    " attribution (no silent fallback)"
                )

    def canonical(self) -> tuple[object, ...]:
        return (
            self.transition_id,
            self.candidate_id,
            self.stage.value,
            self.outcome.value,
            self.reason_code,
            self.attributed_engine,
            self.attributed_version,
            self.as_of.isoformat(),
            self.run_id,
            self.split_id,
        )


@dataclass(frozen=True, slots=True)
class CandidateHistory:
    candidate_id: str
    status: CandidateLifecycleStatus
    current_stage: FunnelStage
    terminal_outcome: TransitionOutcome | None
    transitions: tuple[StageTransition, ...]


@dataclass(frozen=True, slots=True)
class RejectionAttribution:
    stage: FunnelStage
    reason_code: str
    engine: str
    version: str
    count: int


@dataclass(frozen=True, slots=True)
class FunnelReport:
    """Deterministic funnel summary; conservation is enforced, not hoped."""

    group_key: str  # run_id|split_id this report covers
    total_candidates: int
    active_candidates: int
    terminal_success: int  # reached APPROVED
    terminal_rejected: int
    terminal_vetoed: int
    terminal_expired: int
    terminal_stale: int
    stage_counts: Mapping[str, int]  # candidates that REACHED each stage
    conversion: Mapping[str, str]  # "STAGE->NEXT": decimal string with denominator
    rejection_attribution: tuple[RejectionAttribution, ...]
    histories: tuple[CandidateHistory, ...]

    def __post_init__(self) -> None:
        terminal = (
            self.terminal_success
            + self.terminal_rejected
            + self.terminal_vetoed
            + self.terminal_expired
            + self.terminal_stale
        )
        if self.total_candidates != self.active_candidates + terminal:
            raise FunnelError(
                "conservation violated: total != active + terminal"
                f" ({self.total_candidates} != {self.active_candidates} + {terminal})"
            )


@dataclass(slots=True)
class _CandidateState:
    stage_index: int
    last_as_of: datetime
    terminal: TransitionOutcome | None
    transitions: list[StageTransition] = field(default_factory=list)


class OpportunityFunnel:
    """Pure per-group aggregator of stage transitions."""

    def __init__(self, *, run_id: str = "", split_id: str = "") -> None:
        self._run_id = run_id
        self._split_id = split_id
        self._candidates: dict[str, _CandidateState] = {}
        self._seen: dict[str, tuple[object, ...]] = {}

    @property
    def group_key(self) -> str:
        return f"{self._run_id}|{self._split_id}"

    def record(self, transition: StageTransition) -> None:
        # -- group isolation: runs/splits never mix without explicit grouping --
        if transition.run_id != self._run_id or transition.split_id != self._split_id:
            raise FunnelError(
                f"transition {transition.transition_id!r} belongs to group"
                f" ({transition.run_id!r}, {transition.split_id!r}) but this funnel"
                f" aggregates ({self._run_id!r}, {self._split_id!r}); fail closed"
            )
        # -- dedup / conflict ---------------------------------------------------
        previous = self._seen.get(transition.transition_id)
        if previous is not None:
            if previous == transition.canonical():
                return  # idempotent duplicate
            raise FunnelError(
                f"transition_id {transition.transition_id!r} appears with"
                " different content; corrupt input — fail closed"
            )
        state = self._candidates.get(transition.candidate_id)
        if state is None:
            if transition.stage is not FunnelStage.OBSERVED:
                raise FunnelError(
                    f"candidate {transition.candidate_id!r} must start at OBSERVED,"
                    f" got {transition.stage.value}"
                )
            state = _CandidateState(
                stage_index=0, last_as_of=transition.as_of, terminal=None
            )
            self._candidates[transition.candidate_id] = state
        else:
            if state.terminal is not None:
                raise FunnelError(
                    f"candidate {transition.candidate_id!r} is terminal"
                    f" ({state.terminal.value}); no further transitions"
                )
            if transition.as_of < state.last_as_of:
                raise FunnelError(
                    f"candidate {transition.candidate_id!r}: as_of moved backwards"
                )
            expected_index = state.stage_index + 1
            actual_index = _STAGE_INDEX[transition.stage]
            if transition.outcome is TransitionOutcome.ADVANCED:
                if actual_index != expected_index:
                    verb = "skips" if actual_index > expected_index else "regresses to"
                    raise FunnelError(
                        f"candidate {transition.candidate_id!r} {verb} stage"
                        f" {transition.stage.value} from"
                        f" {STAGE_ORDER[state.stage_index].value}; canonical order only"
                    )
            elif actual_index not in (state.stage_index, expected_index):
                raise FunnelError(
                    f"candidate {transition.candidate_id!r}: terminal outcome at"
                    f" non-adjacent stage {transition.stage.value}"
                )
        # -- apply ----------------------------------------------------------------
        state.stage_index = _STAGE_INDEX[transition.stage]
        state.last_as_of = transition.as_of
        state.transitions.append(transition)
        if transition.outcome in TERMINAL_OUTCOMES:
            state.terminal = transition.outcome
        self._seen[transition.transition_id] = transition.canonical()

    # -- ReplayRunner consumer ----------------------------------------------------
    def consume(
        self,
        event: ReplayEvent,
        snapshot: ReplaySnapshot,
        as_of: datetime,
        manifest: ReplayRunManifest,
    ) -> Payload:
        """Frozen STAGE events from the runner become transitions."""
        del snapshot, manifest
        if event.kind != "STAGE":
            raise FunnelError(f"unsupported event kind {event.kind!r}; fail closed")
        payload = event.payload

        def text(key: str, default: str = "") -> str:
            value = payload.get(key)
            return value if isinstance(value, str) else default

        self.record(
            StageTransition(
                transition_id=event.event_id,
                candidate_id=text("candidate_id"),
                stage=FunnelStage(text("stage")),
                outcome=TransitionOutcome(text("outcome")),
                reason_code=text("reason_code"),
                attributed_engine=text("engine"),
                attributed_version=text("version"),
                as_of=as_of,
                run_id=self._run_id,
                split_id=self._split_id,
            )
        )
        return {"recorded": event.event_id, "group": self.group_key}

    # -- report ---------------------------------------------------------------------
    def candidate_history(self, candidate_id: str) -> CandidateHistory:
        state = self._candidates.get(candidate_id)
        if state is None:
            raise FunnelError(f"unknown candidate {candidate_id!r}")
        return CandidateHistory(
            candidate_id=candidate_id,
            status=(
                CandidateLifecycleStatus.TERMINAL
                if state.terminal is not None or state.stage_index == len(STAGE_ORDER) - 1
                else CandidateLifecycleStatus.ACTIVE
            ),
            current_stage=STAGE_ORDER[state.stage_index],
            terminal_outcome=state.terminal,
            transitions=tuple(state.transitions),
        )

    def report(self) -> FunnelReport:
        histories = tuple(
            self.candidate_history(candidate_id)
            for candidate_id in sorted(self._candidates)
        )
        stage_counts = {
            stage.value: sum(
                1
                for state in self._candidates.values()
                if any(_STAGE_INDEX[t.stage] >= _STAGE_INDEX[stage] for t in state.transitions)
            )
            for stage in STAGE_ORDER
        }
        conversion: dict[str, str] = {}
        for i in range(len(STAGE_ORDER) - 1):
            reached = stage_counts[STAGE_ORDER[i].value]
            advanced = stage_counts[STAGE_ORDER[i + 1].value]
            rate = (
                (Decimal(advanced) / Decimal(reached)).quantize(Decimal("0.0001"))
                if reached
                else Decimal(0)
            )
            conversion[f"{STAGE_ORDER[i].value}->{STAGE_ORDER[i + 1].value}"] = (
                f"{rate} ({advanced}/{reached})"
            )
        attribution_counts: dict[tuple[FunnelStage, str, str, str], int] = {}
        for history in histories:
            for transition in history.transitions:
                if transition.outcome in TERMINAL_OUTCOMES:
                    key = (
                        transition.stage,
                        transition.reason_code,
                        transition.attributed_engine,
                        transition.attributed_version,
                    )
                    attribution_counts[key] = attribution_counts.get(key, 0) + 1
        attribution = tuple(
            RejectionAttribution(stage=k[0], reason_code=k[1], engine=k[2], version=k[3], count=v)
            for k, v in sorted(
                attribution_counts.items(), key=lambda item: [str(x) for x in item[0]]
            )
        )

        def count(outcome: TransitionOutcome) -> int:
            return sum(1 for h in histories if h.terminal_outcome is outcome)

        success = sum(
            1
            for h in histories
            if h.terminal_outcome is None and h.current_stage is FunnelStage.APPROVED
        )
        active = sum(
            1 for h in histories if h.status is CandidateLifecycleStatus.ACTIVE
        )
        return FunnelReport(
            group_key=self.group_key,
            total_candidates=len(histories),
            active_candidates=active,
            terminal_success=success,
            terminal_rejected=count(TransitionOutcome.REJECTED),
            terminal_vetoed=count(TransitionOutcome.VETOED),
            terminal_expired=count(TransitionOutcome.EXPIRED),
            terminal_stale=count(TransitionOutcome.STALE),
            stage_counts=stage_counts,
            conversion=conversion,
            rejection_attribution=attribution,
            histories=histories,
        )


__all__ = [
    "FUNNEL_VERSION",
    "STAGE_ORDER",
    "TERMINAL_OUTCOMES",
    "CandidateHistory",
    "CandidateLifecycleStatus",
    "FunnelError",
    "FunnelReport",
    "FunnelStage",
    "OpportunityFunnel",
    "RejectionAttribution",
    "StageTransition",
    "TransitionOutcome",
]
