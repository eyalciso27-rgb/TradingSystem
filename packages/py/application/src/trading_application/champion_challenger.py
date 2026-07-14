"""Champion/Challenger arena (IMP-1205) — same events, isolated state.

Authority: Opportunity Calibration & Model Governance §10 ("Champion and
Challenger receive the same events, snapshots and deadlines"; "comparison
covers coverage, risk, drawdown and operational failures, not only P&L";
"the challenger gets no execution access before Gate and approval"),
MVP-G9 (run champion/challenger and replay WITHOUT changing the active
version), STR-AC-12. Task scope: same events, isolated state, comparison
report. DoD: the challenger cannot execute or change the active pointer.

The arena is a CONSUMER injected into the IMP-1201 ``ReplayRunner`` — it
sees only ``(event, snapshot, as_of, manifest)`` over frozen events, so
canonical ordering, as-of snapshots, exact versions, dedup and the
deterministic result hash all come from the runner and cannot be
bypassed. Every candidate event is delivered to BOTH participants with
the same event_id and the same as_of.

No execution, no promotion (the DoD, by construction):

- Neither participant — champion included — has any execution surface
  here: no order, no authorization, no submit/cancel, no broker import.
  Every decision is ``virtual_only=True``, ``executable=False`` and never
  touches live or paper state.
- The active pointer enters the arena as a frozen READ-ONLY snapshot that
  records who was champion when the run started. There is no setter, no
  update, no promote and no swap anywhere in this module; a snapshot that
  does not point at the arena's champion is rejected fail-closed. The
  report may carry findings — never a promotion action.

State isolation:

- Each participant's working state is built fresh from its own frozen
  initial state; the two sides never share a mutable object, policy
  callable or cache, and sharing one is rejected fail-closed at
  construction.
- Policies receive a read-only view of the event payload — an attempt to
  mutate it fails and is recorded as that side's operational failure; it
  never leaks to the other side and is never silently patched over.
- The evaluation order (champion-first vs challenger-first) cannot change
  the report or the result hash: outputs are keyed by role, and each
  policy can reach nothing but its own state.

Comparison rules: every shared candidate stays in the report — rejected,
vetoed, watch, unfilled and failed evaluations included; coverage and
agreement carry explicit denominators; a side that produced nothing is a
missing/failed evaluation, never an agreement; no winner is declared by
P&L alone — performance, coverage and failures are reported separately.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType

from trading_application.replay_runner import (
    Payload,
    PayloadValue,
    ReplayEvent,
    ReplayRunManifest,
    ReplaySnapshot,
)

ARENA_VERSION = "champion-challenger-1.0.0"

_RATE = Decimal("0.0001")


class ArenaError(Exception):
    """Fail-closed champion/challenger failure (bad wiring, corrupt input)."""


class ParticipantRole(StrEnum):
    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"


#: A deterministic decision policy: (read-only payload, as_of, OWN state)
#: -> decision code (e.g. APPROVE/REJECT/WATCH). It can mutate only its
#: own state dict; the payload view is read-only.
DecisionPolicy = Callable[[Mapping[str, PayloadValue], datetime, dict[str, str]], str]


@dataclass(frozen=True, slots=True)
class ChallengerParticipant:
    """One frozen participant: exact versions + a deterministic policy."""

    participant_id: str
    role: ParticipantRole
    versions: Mapping[str, str]  # model/policy/engine -> exact version
    policy: DecisionPolicy
    initial_state: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.participant_id.strip():
            raise ArenaError("participant_id is mandatory")
        if not self.versions:
            raise ArenaError(
                f"participant {self.participant_id!r} declares no exact versions;"
                " fail closed (every decision must point at a fixed version)"
            )
        for component, version in self.versions.items():
            if not component.strip() or not version.strip():
                raise ArenaError(
                    f"participant {self.participant_id!r} has a missing/blank exact"
                    f" version for {component!r}; fail closed (no silent fallback)"
                )

    def fingerprint(self) -> str:
        material = json.dumps(
            {
                "participant_id": self.participant_id,
                "role": self.role.value,
                "versions": dict(sorted(self.versions.items())),
                "initial_state": dict(sorted(self.initial_state.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "participant:" + hashlib.sha256(material.encode("ascii")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ParticipantState:
    """A frozen, read-only copy of one participant's final working state."""

    participant_id: str
    role: ParticipantRole
    values: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class VirtualParticipantDecision:
    """One virtual decision. Research-only; can never become an order."""

    participant_id: str
    role: ParticipantRole
    event_id: str
    candidate_id: str
    as_of: datetime
    decision: str
    virtual_only: bool = True
    executable: bool = False
    affects_live_state: bool = False
    affects_paper_state: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.as_of, "as_of")
        if not self.decision.strip():
            raise ArenaError("decision code must be non-blank")
        if (
            not self.virtual_only
            or self.executable
            or self.affects_live_state
            or self.affects_paper_state
        ):
            raise ArenaError(
                "a participant decision is virtual/non-executable by definition;"
                " fail closed"
            )


class ComparisonClassification(StrEnum):
    AGREEMENT = "AGREEMENT"
    DIVERGENCE = "DIVERGENCE"
    CHAMPION_FAILED = "CHAMPION_FAILED"
    CHALLENGER_FAILED = "CHALLENGER_FAILED"
    BOTH_FAILED = "BOTH_FAILED"


@dataclass(frozen=True, slots=True)
class CandidateComparison:
    """One shared candidate, both sides' virtual decisions, classified."""

    candidate_id: str
    champion: VirtualParticipantDecision | None
    challenger: VirtualParticipantDecision | None
    classification: ComparisonClassification


@dataclass(frozen=True, slots=True)
class ComparisonMetrics:
    """Coverage/agreement/failures with EXPLICIT denominators; PnL apart."""

    total_shared_events: int
    total_candidates: int
    champion_coverage: str  # "rate (evaluated/total)"
    challenger_coverage: str
    agreement: str  # "rate (agree/both-evaluated)"
    divergence: str  # "rate (diverge/both-evaluated)"
    champion_failures: int
    challenger_failures: int
    champion_virtual_net_pnl: str | None
    challenger_virtual_net_pnl: str | None
    champion_virtual_costs: str | None
    challenger_virtual_costs: str | None
    champion_virtual_max_drawdown: str | None
    challenger_virtual_max_drawdown: str | None


@dataclass(frozen=True, slots=True)
class ActivePointerSnapshot:
    """READ-ONLY record of who was champion when the run started.

    There is deliberately no setter, no update, no promote and no swap —
    neither here nor anywhere else in this module. Changing the active
    pointer is an owner-governance action outside this arena.
    """

    active_participant_id: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        if not self.active_participant_id.strip():
            raise ArenaError("active_participant_id is mandatory")
        _require_aware(self.recorded_at, "recorded_at")


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """Research-only comparison. Findings, never a promotion action."""

    champion_id: str
    challenger_id: str
    champion_versions: tuple[tuple[str, str], ...]
    challenger_versions: tuple[tuple[str, str], ...]
    champion_fingerprint: str
    challenger_fingerprint: str
    active_pointer: ActivePointerSnapshot
    metrics: ComparisonMetrics
    comparisons: tuple[CandidateComparison, ...]
    operational_failures: tuple[str, ...]
    findings: tuple[str, ...]
    final_states: tuple[ParticipantState, ...]
    run_fingerprint: str
    seed: str
    group_run_id: str
    group_split_id: str
    report_hash: str
    virtual_only: bool = True
    executable: bool = False
    affects_live_state: bool = False
    affects_paper_state: bool = False

    def __post_init__(self) -> None:
        if (
            not self.virtual_only
            or self.executable
            or self.affects_live_state
            or self.affects_paper_state
        ):
            raise ArenaError("a comparison report is research-only; fail closed")
        by_class = dict.fromkeys(ComparisonClassification, 0)
        for comparison in self.comparisons:
            by_class[comparison.classification] += 1
        if self.metrics.total_candidates != len(self.comparisons) or sum(
            by_class.values()
        ) != len(self.comparisons):
            raise ArenaError(
                "conservation violated: every shared candidate must appear in"
                " the report exactly once"
            )


def _require_aware(stamp: datetime, name: str) -> None:
    if stamp.tzinfo is None:
        raise ArenaError(f"{name} must be timezone-aware")


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return f"n/a (0/{denominator})"
    rate = (Decimal(numerator) / Decimal(denominator)).quantize(_RATE)
    return f"{rate} ({numerator}/{denominator})"


@dataclass(slots=True)
class _SideResult:
    decision: VirtualParticipantDecision | None = None
    failure: str | None = None


class ChampionChallengerArena:
    """ReplayRunner consumer feeding both participants the same events.

    Holds no execution surface, no promotion surface and no shared state
    between the two sides.
    """

    def __init__(
        self,
        champion: ChallengerParticipant,
        challenger: ChallengerParticipant,
        active_pointer: ActivePointerSnapshot,
        *,
        run_id: str = "",
        split_id: str = "",
        evaluation_order: tuple[ParticipantRole, ...] = (
            ParticipantRole.CHAMPION,
            ParticipantRole.CHALLENGER,
        ),
    ) -> None:
        if champion.role is not ParticipantRole.CHAMPION:
            raise ArenaError("champion participant must carry the CHAMPION role")
        if challenger.role is not ParticipantRole.CHALLENGER:
            raise ArenaError("challenger participant must carry the CHALLENGER role")
        if champion.participant_id == challenger.participant_id:
            raise ArenaError("participants must have distinct ids")
        if champion.policy is challenger.policy:
            raise ArenaError(
                "champion and challenger share the same policy callable; shared"
                " mutable dependencies between the sides are forbidden — fail closed"
            )
        if champion.initial_state is challenger.initial_state and isinstance(
            champion.initial_state, dict
        ):
            raise ArenaError(
                "champion and challenger share the same mutable initial-state"
                " object; fail closed"
            )
        if active_pointer.active_participant_id != champion.participant_id:
            raise ArenaError(
                f"active pointer references {active_pointer.active_participant_id!r}"
                f" but the arena champion is {champion.participant_id!r}; the arena"
                " never moves the active pointer — fail closed"
            )
        if sorted(role.value for role in evaluation_order) != sorted(
            role.value for role in ParticipantRole
        ):
            raise ArenaError("evaluation_order must contain each role exactly once")
        self._participants: dict[ParticipantRole, ChallengerParticipant] = {
            ParticipantRole.CHAMPION: champion,
            ParticipantRole.CHALLENGER: challenger,
        }
        self._active_pointer = active_pointer
        self._run_id = run_id
        self._split_id = split_id
        self._evaluation_order = evaluation_order
        # Fresh, per-side working state — never the same object identity.
        self._states: dict[ParticipantRole, dict[str, str]] = {
            role: dict(participant.initial_state)
            for role, participant in self._participants.items()
        }
        self._results: dict[str, dict[ParticipantRole, _SideResult]] = {}
        self._order: list[str] = []
        self._seen: dict[str, tuple[tuple[str, PayloadValue], ...]] = {}
        self._failures: list[str] = []
        self._event_count = 0
        self._manifest_run_id = ""
        self._manifest_seed = ""

    # -- the injected consumer ----------------------------------------------------
    def consume(
        self,
        event: ReplayEvent,
        snapshot: ReplaySnapshot,
        as_of: datetime,
        manifest: ReplayRunManifest,
    ) -> Payload:
        del snapshot  # ordering/visibility are the runner's concern
        self._manifest_run_id = manifest.run_id
        self._manifest_seed = manifest.seed
        if event.kind != "CANDIDATE":
            raise ArenaError(f"unsupported event kind {event.kind!r}; fail closed")
        payload = event.payload
        if (str(payload.get("run_id") or ""), str(payload.get("split_id") or "")) != (
            self._run_id,
            self._split_id,
        ):
            raise ArenaError(
                f"event {event.event_id!r} belongs to a different"
                " (run_id, split_id) group than this arena; replay runs and"
                " walk-forward splits never mix without explicit grouping;"
                " fail closed"
            )
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id.strip():
            raise ArenaError(f"event {event.event_id!r} lacks candidate_id")
        canonical = tuple(sorted(payload.items()))
        previous = self._seen.get(candidate_id)
        if previous is not None:
            if previous == canonical:
                return self._output(as_of, candidate_id, note=f"dedup:{candidate_id}")
            raise ArenaError(
                f"candidate {candidate_id!r} appears with different content;"
                " corrupt input — fail closed"
            )
        self._seen[candidate_id] = canonical
        self._event_count += 1
        self._order.append(candidate_id)

        # SAME EVENT TO BOTH SIDES: identical frozen payload view, same
        # event_id, same as_of. Each policy touches only its own state.
        readonly = MappingProxyType(dict(payload))
        sides: dict[ParticipantRole, _SideResult] = {}
        for role in self._evaluation_order:
            participant = self._participants[role]
            result = _SideResult()
            try:
                code = participant.policy(readonly, as_of, self._states[role])
                result.decision = VirtualParticipantDecision(
                    participant_id=participant.participant_id,
                    role=role,
                    event_id=event.event_id,
                    candidate_id=candidate_id,
                    as_of=as_of,
                    decision=code,
                )
            except Exception as error:  # noqa: BLE001 - recorded, never hidden
                # Operational failure of ONE side: recorded explicitly and
                # kept; it never hides the other side's result and is never
                # silently patched over.
                result.failure = f"{type(error).__name__}: {error}"
                self._failures.append(
                    f"{role.value}:{participant.participant_id}:{candidate_id}:"
                    f"{result.failure}"
                )
            sides[role] = result
        self._results[candidate_id] = sides
        return self._output(as_of, candidate_id, note=f"evaluated:{candidate_id}")

    def _output(self, as_of: datetime, candidate_id: str, *, note: str) -> Payload:
        # Keyed by ROLE (never by evaluation order) so champion-first and
        # challenger-first runs produce byte-identical outputs.
        payload: dict[str, PayloadValue] = {
            "virtual": True,
            "executable": False,
            "as_of": as_of.isoformat(),
            "note": f"RESEARCH:{note}",
            "arena": self._combined_fingerprint(),
        }
        sides = self._results[candidate_id]
        for role in ParticipantRole:
            side = sides[role]
            if side.decision is not None:
                payload[role.value.lower()] = side.decision.decision
            else:
                payload[role.value.lower()] = f"FAILED:{side.failure}"
        return payload

    def _combined_fingerprint(self) -> str:
        material = "|".join(
            [
                self._participants[ParticipantRole.CHAMPION].fingerprint(),
                self._participants[ParticipantRole.CHALLENGER].fingerprint(),
                self._active_pointer.active_participant_id,
                ARENA_VERSION,
            ]
        )
        return "arena:" + hashlib.sha256(material.encode("ascii")).hexdigest()[:16]

    # -- report --------------------------------------------------------------------
    def report(self) -> ComparisonReport:
        comparisons: list[CandidateComparison] = []
        agree = diverge = champion_failed = challenger_failed = both_failed = 0
        for candidate_id in self._order:
            sides = self._results[candidate_id]
            champion_side = sides[ParticipantRole.CHAMPION]
            challenger_side = sides[ParticipantRole.CHALLENGER]
            if champion_side.decision is None and challenger_side.decision is None:
                classification = ComparisonClassification.BOTH_FAILED
                both_failed += 1
            elif champion_side.decision is None:
                classification = ComparisonClassification.CHAMPION_FAILED
                champion_failed += 1
            elif challenger_side.decision is None:
                classification = ComparisonClassification.CHALLENGER_FAILED
                challenger_failed += 1
            elif champion_side.decision.decision == challenger_side.decision.decision:
                classification = ComparisonClassification.AGREEMENT
                agree += 1
            else:
                classification = ComparisonClassification.DIVERGENCE
                diverge += 1
            comparisons.append(
                CandidateComparison(
                    candidate_id=candidate_id,
                    champion=champion_side.decision,
                    challenger=challenger_side.decision,
                    classification=classification,
                )
            )

        total = len(comparisons)
        both_evaluated = agree + diverge
        findings: list[str] = []
        metrics = ComparisonMetrics(
            total_shared_events=self._event_count,
            total_candidates=total,
            champion_coverage=_rate(total - champion_failed - both_failed, total),
            challenger_coverage=_rate(total - challenger_failed - both_failed, total),
            agreement=_rate(agree, both_evaluated),
            divergence=_rate(diverge, both_evaluated),
            champion_failures=champion_failed + both_failed,
            challenger_failures=challenger_failed + both_failed,
            champion_virtual_net_pnl=self._state_metric(
                ParticipantRole.CHAMPION, "virtual_net_pnl", findings
            ),
            challenger_virtual_net_pnl=self._state_metric(
                ParticipantRole.CHALLENGER, "virtual_net_pnl", findings
            ),
            champion_virtual_costs=self._state_metric(
                ParticipantRole.CHAMPION, "virtual_costs", findings
            ),
            challenger_virtual_costs=self._state_metric(
                ParticipantRole.CHALLENGER, "virtual_costs", findings
            ),
            champion_virtual_max_drawdown=self._state_metric(
                ParticipantRole.CHAMPION, "virtual_max_drawdown", findings
            ),
            challenger_virtual_max_drawdown=self._state_metric(
                ParticipantRole.CHALLENGER, "virtual_max_drawdown", findings
            ),
        )
        if diverge:
            findings.append(
                f"{diverge} divergent candidate(s); review performance, risk,"
                " coverage and failures SEPARATELY — no winner is declared by"
                " P&L alone and no promotion action is taken here"
            )
        champion = self._participants[ParticipantRole.CHAMPION]
        challenger = self._participants[ParticipantRole.CHALLENGER]
        final_states = tuple(
            ParticipantState(
                participant_id=self._participants[role].participant_id,
                role=role,
                values=tuple(sorted(self._states[role].items())),
            )
            for role in ParticipantRole
        )
        material = {
            "champion": champion.fingerprint(),
            "challenger": challenger.fingerprint(),
            "active_pointer": self._active_pointer.active_participant_id,
            "run": self._manifest_run_id,
            "seed": self._manifest_seed,
            "group": [self._run_id, self._split_id],
            "comparisons": [
                {
                    "candidate_id": comparison.candidate_id,
                    "classification": comparison.classification.value,
                    "champion": (
                        comparison.champion.decision if comparison.champion else None
                    ),
                    "challenger": (
                        comparison.challenger.decision if comparison.challenger else None
                    ),
                }
                for comparison in comparisons
            ],
            "failures": list(self._failures),
            "states": [
                [state.participant_id, list(map(list, state.values))]
                for state in final_states
            ],
        }
        report_hash = "sha256:" + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        return ComparisonReport(
            champion_id=champion.participant_id,
            challenger_id=challenger.participant_id,
            champion_versions=tuple(sorted(champion.versions.items())),
            challenger_versions=tuple(sorted(challenger.versions.items())),
            champion_fingerprint=champion.fingerprint(),
            challenger_fingerprint=challenger.fingerprint(),
            active_pointer=self._active_pointer,
            metrics=metrics,
            comparisons=tuple(comparisons),
            operational_failures=tuple(self._failures),
            findings=tuple(findings),
            final_states=final_states,
            run_fingerprint=self._manifest_run_id,
            seed=self._manifest_seed,
            group_run_id=self._run_id,
            group_split_id=self._split_id,
            report_hash=report_hash,
        )

    def _state_metric(
        self, role: ParticipantRole, key: str, findings: list[str]
    ) -> str | None:
        raw = self._states[role].get(key)
        if raw is None:
            findings.append(
                f"{role.value}: {key} not reported by the participant policy;"
                " shown as missing, not fabricated"
            )
            return None
        try:
            return str(Decimal(raw))
        except InvalidOperation as error:
            raise ArenaError(
                f"{role.value} state[{key!r}]={raw!r} is not a canonical string"
                " decimal; fail closed"
            ) from error


def report_field_names() -> tuple[str, ...]:
    """The report's full surface — used by tests to prove there is no
    promotion action anywhere in it."""
    return tuple(field.name for field in fields(ComparisonReport))


__all__ = [
    "ARENA_VERSION",
    "ActivePointerSnapshot",
    "ArenaError",
    "CandidateComparison",
    "ChallengerParticipant",
    "ChampionChallengerArena",
    "ComparisonClassification",
    "ComparisonMetrics",
    "ComparisonReport",
    "DecisionPolicy",
    "ParticipantRole",
    "ParticipantState",
    "VirtualParticipantDecision",
    "report_field_names",
]
