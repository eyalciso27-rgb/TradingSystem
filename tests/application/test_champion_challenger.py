"""Champion/challenger drills (IMP-1205). DoD: the challenger cannot
execute or change the active pointer — same events, isolated state."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from trading_application.champion_challenger import (
    ActivePointerSnapshot,
    ArenaError,
    ChallengerParticipant,
    ChampionChallengerArena,
    ComparisonClassification,
    ComparisonReport,
    ParticipantRole,
    VirtualParticipantDecision,
    report_field_names,
)
from trading_application.replay_runner import ReplayDataset, ReplayEvent, ReplayRunner

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trading_application.replay_runner import PayloadValue

T0 = datetime(2026, 6, 1, 14, 30, tzinfo=UTC)


# -- deterministic policies (never shared between the two sides) ---------------------


def approve_all(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    del payload, as_of, state
    return "APPROVE"


def approve_all_too(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    del payload, as_of, state
    return "APPROVE"


def reject_c2(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    del as_of, state
    return "REJECT" if payload.get("candidate_id") == "c2" else "APPROVE"


def record_seen_a(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    state["seen"] = state.get("seen", "") + f"{payload.get('candidate_id')}@{as_of.isoformat()};"
    return "WATCH"


def record_seen_b(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    state["seen"] = state.get("seen", "") + f"{payload.get('candidate_id')}@{as_of.isoformat()};"
    return "WATCH"


def fail_on_c2(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    del as_of, state
    if payload.get("candidate_id") == "c2":
        raise ValueError("engine blew up")
    return "APPROVE"


def mutate_payload(
    payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
) -> str:
    del as_of, state
    payload["hacked"] = True  # type: ignore[index]
    return "APPROVE"


def champion(
    policy: object = approve_all, versions: dict[str, str] | None = None
) -> ChallengerParticipant:
    return ChallengerParticipant(
        participant_id="champ-1",
        role=ParticipantRole.CHAMPION,
        versions=versions or {"policy": "1.0.0"},
        policy=policy,  # type: ignore[arg-type]
        initial_state={},
    )


def challenger(
    policy: object = approve_all_too, versions: dict[str, str] | None = None
) -> ChallengerParticipant:
    return ChallengerParticipant(
        participant_id="chal-1",
        role=ParticipantRole.CHALLENGER,
        versions=versions or {"policy": "2.0.0"},
        policy=policy,  # type: ignore[arg-type]
        initial_state={},
    )


POINTER = ActivePointerSnapshot(active_participant_id="champ-1", recorded_at=T0)


def candidate_event(event_id: str, minute: int, candidate_id: str, **extra: str) -> ReplayEvent:
    at = T0 + timedelta(minutes=minute)
    payload: dict[str, PayloadValue] = {
        "candidate_id": candidate_id,
        "run_id": "",
        "split_id": "",
        **extra,
    }
    return ReplayEvent(
        event_id=event_id,
        sequence=0,
        kind="CANDIDATE",
        payload=payload,
        observed_at=at,
        available_at=at,
    )


BASIC_EVENTS = [
    candidate_event("e1", 0, "c1"),
    candidate_event("e2", 1, "c2"),
    candidate_event("e3", 2, "c3"),
]


def run_arena(
    events: list[ReplayEvent],
    arena: ChampionChallengerArena | None = None,
    *,
    seed: str = "seed:1",
    versions: dict[str, str] | None = None,
) -> tuple[str, ComparisonReport, ChampionChallengerArena]:
    arena = arena or ChampionChallengerArena(champion(), challenger(), POINTER)
    result = ReplayRunner(arena.consume).run(
        ReplayDataset(
            dataset_id="arena-ds",
            events=tuple(events),
            versions=versions or {"arena": "1.0.0"},
            seed=seed,
        )
    )
    return result.result_hash, arena.report(), arena


# -- identical delivery + state isolation -------------------------------------------


def test_both_sides_receive_same_events_and_as_of() -> None:
    arena = ChampionChallengerArena(
        champion(policy=record_seen_a), challenger(policy=record_seen_b), POINTER
    )
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    states = {state.role: dict(state.values) for state in report.final_states}
    assert states[ParticipantRole.CHAMPION]["seen"] == states[ParticipantRole.CHALLENGER]["seen"]
    assert states[ParticipantRole.CHAMPION]["seen"].count(";") == 3
    for comparison in report.comparisons:
        assert comparison.champion is not None
        assert comparison.challenger is not None
        assert comparison.champion.event_id == comparison.challenger.event_id
        assert comparison.champion.as_of == comparison.challenger.as_of


def test_states_are_distinct_objects_built_from_frozen_initial_state() -> None:
    initial = {"budget": "100"}
    champ = ChallengerParticipant(
        participant_id="champ-1",
        role=ParticipantRole.CHAMPION,
        versions={"policy": "1.0.0"},
        policy=approve_all,
        initial_state=initial,
    )
    arena = ChampionChallengerArena(champ, challenger(), POINTER)
    champion_state = arena._states[ParticipantRole.CHAMPION]  # noqa: SLF001
    challenger_state = arena._states[ParticipantRole.CHALLENGER]  # noqa: SLF001
    assert champion_state is not challenger_state
    assert champion_state is not initial  # fresh copy, not the caller's dict
    initial["budget"] = "999"
    assert champion_state["budget"] == "100"


def test_mutation_on_one_side_does_not_leak_to_the_other() -> None:
    arena = ChampionChallengerArena(
        champion(policy=record_seen_a), challenger(), POINTER
    )
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    states = {state.role: dict(state.values) for state in report.final_states}
    assert "seen" in states[ParticipantRole.CHAMPION]
    assert "seen" not in states[ParticipantRole.CHALLENGER]


def test_shared_mutable_dependency_is_rejected() -> None:
    with pytest.raises(ArenaError, match="same policy callable"):
        ChampionChallengerArena(
            champion(policy=approve_all), challenger(policy=approve_all), POINTER
        )
    shared_state: dict[str, str] = {"budget": "100"}
    champ = ChallengerParticipant(
        participant_id="champ-1",
        role=ParticipantRole.CHAMPION,
        versions={"policy": "1.0.0"},
        policy=approve_all,
        initial_state=shared_state,
    )
    chal = ChallengerParticipant(
        participant_id="chal-1",
        role=ParticipantRole.CHALLENGER,
        versions={"policy": "2.0.0"},
        policy=approve_all_too,
        initial_state=shared_state,
    )
    with pytest.raises(ArenaError, match="mutable initial-state"):
        ChampionChallengerArena(champ, chal, POINTER)


def test_policy_cannot_mutate_the_event_payload() -> None:
    arena = ChampionChallengerArena(
        champion(policy=mutate_payload), challenger(), POINTER
    )
    events = list(BASIC_EVENTS)
    before = [event.canonical() for event in events]
    _, report, _ = run_arena(events, arena)
    assert [event.canonical() for event in events] == before
    # The mutation attempt is that side's recorded operational failure.
    assert report.metrics.champion_failures == 3
    assert all("TypeError" in failure for failure in report.operational_failures)
    # The other side's results are untouched.
    assert report.metrics.challenger_failures == 0


# -- determinism / order independence -----------------------------------------------


def test_champion_first_and_challenger_first_give_same_result() -> None:
    order_a = (ParticipantRole.CHAMPION, ParticipantRole.CHALLENGER)
    order_b = (ParticipantRole.CHALLENGER, ParticipantRole.CHAMPION)
    arena_a = ChampionChallengerArena(
        champion(policy=record_seen_a), challenger(policy=reject_c2), POINTER,
        evaluation_order=order_a,
    )
    arena_b = ChampionChallengerArena(
        champion(policy=record_seen_a), challenger(policy=reject_c2), POINTER,
        evaluation_order=order_b,
    )
    hash_a, report_a, _ = run_arena(list(BASIC_EVENTS), arena_a)
    hash_b, report_b, _ = run_arena(list(BASIC_EVENTS), arena_b)
    assert hash_a == hash_b
    assert report_a == report_b


def test_repeat_and_shuffle_yield_identical_report_and_hash() -> None:
    hash_a, report_a, _ = run_arena(list(BASIC_EVENTS))
    hash_b, report_b, _ = run_arena(list(BASIC_EVENTS))
    hash_c, report_c, _ = run_arena(list(reversed(BASIC_EVENTS)))
    assert hash_a == hash_b == hash_c
    assert report_a == report_b == report_c


def test_changing_either_side_version_changes_fingerprint_and_hash() -> None:
    base_hash, base_report, _ = run_arena(list(BASIC_EVENTS))
    champ_arena = ChampionChallengerArena(
        champion(versions={"policy": "1.1.0"}), challenger(), POINTER
    )
    champ_hash, champ_report, _ = run_arena(list(BASIC_EVENTS), champ_arena)
    assert champ_report.champion_fingerprint != base_report.champion_fingerprint
    assert champ_hash != base_hash
    chal_arena = ChampionChallengerArena(
        champion(), challenger(versions={"policy": "2.1.0"}), POINTER
    )
    chal_hash, chal_report, _ = run_arena(list(BASIC_EVENTS), chal_arena)
    assert chal_report.challenger_fingerprint != base_report.challenger_fingerprint
    assert chal_hash != base_hash


def test_missing_or_blank_exact_version_fails_closed() -> None:
    with pytest.raises(ArenaError, match="no exact versions"):
        ChallengerParticipant(
            participant_id="champ-1",
            role=ParticipantRole.CHAMPION,
            versions={},
            policy=approve_all,
            initial_state={},
        )
    with pytest.raises(ArenaError, match="fail closed"):
        champion(versions={"model": "  "})


# -- comparison / conservation --------------------------------------------------------


def test_agreement_and_divergence_reported_with_explicit_denominators() -> None:
    arena = ChampionChallengerArena(champion(), challenger(policy=reject_c2), POINTER)
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    assert report.metrics.agreement == "0.6667 (2/3)"
    assert report.metrics.divergence == "0.3333 (1/3)"
    divergent = [
        c for c in report.comparisons
        if c.classification is ComparisonClassification.DIVERGENCE
    ]
    assert [c.candidate_id for c in divergent] == ["c2"]
    assert divergent[0].champion is not None
    assert divergent[0].challenger is not None
    assert divergent[0].champion.decision == "APPROVE"
    assert divergent[0].challenger.decision == "REJECT"


def test_operational_failure_of_one_side_is_kept_and_hides_nothing() -> None:
    arena = ChampionChallengerArena(champion(), challenger(policy=fail_on_c2), POINTER)
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    failed = next(
        c for c in report.comparisons
        if c.classification is ComparisonClassification.CHALLENGER_FAILED
    )
    assert failed.candidate_id == "c2"
    assert failed.champion is not None
    assert failed.champion.decision == "APPROVE"  # the other side is preserved
    assert failed.challenger is None
    assert any("ValueError: engine blew up" in f for f in report.operational_failures)
    # A failed side is NOT agreement; the denominator excludes it.
    assert report.metrics.agreement == "1.0000 (2/2)"
    assert report.metrics.challenger_coverage == "0.6667 (2/3)"
    assert report.metrics.champion_coverage == "1.0000 (3/3)"


def test_every_candidate_appears_and_conservation_is_enforced() -> None:
    arena = ChampionChallengerArena(champion(), challenger(policy=fail_on_c2), POINTER)
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    assert {c.candidate_id for c in report.comparisons} == {"c1", "c2", "c3"}
    assert report.metrics.total_candidates == len(report.comparisons) == 3
    with pytest.raises(ArenaError, match="conservation"):
        replace(report, metrics=replace(report.metrics, total_candidates=99))


def test_reject_veto_watch_decisions_never_disappear() -> None:
    def coded(
        payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
    ) -> str:
        del as_of, state
        return {"c1": "REJECT", "c2": "VETO", "c3": "WATCH"}[str(payload["candidate_id"])]

    arena = ChampionChallengerArena(champion(policy=coded), challenger(), POINTER)
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    champion_codes = {
        c.candidate_id: c.champion.decision
        for c in report.comparisons
        if c.champion is not None
    }
    assert champion_codes == {"c1": "REJECT", "c2": "VETO", "c3": "WATCH"}
    assert report.metrics.total_candidates == 3


def test_virtual_pnl_metrics_come_only_from_the_policy_never_fabricated() -> None:
    def pnl_reporting(
        payload: Mapping[str, PayloadValue], as_of: datetime, state: dict[str, str]
    ) -> str:
        del payload, as_of
        state["virtual_net_pnl"] = "12.50"
        return "APPROVE"

    arena = ChampionChallengerArena(champion(policy=pnl_reporting), challenger(), POINTER)
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    assert report.metrics.champion_virtual_net_pnl == str(Decimal("12.50"))
    assert report.metrics.challenger_virtual_net_pnl is None
    assert any("virtual_net_pnl not reported" in finding for finding in report.findings)


# -- DoD: no execution, no promotion ---------------------------------------------------


def test_neither_side_can_execute_through_the_arena() -> None:
    arena = ChampionChallengerArena(champion(), challenger(), POINTER)
    for surface in ("execute", "submit", "cancel", "flatten", "place_order"):
        assert not hasattr(arena, surface)
        assert not hasattr(champion(), surface)
        assert not hasattr(challenger(), surface)


def test_challenger_and_arena_cannot_change_the_active_pointer() -> None:
    pointer = ActivePointerSnapshot(active_participant_id="champ-1", recorded_at=T0)
    with pytest.raises(FrozenInstanceError):
        pointer.active_participant_id = "chal-1"  # type: ignore[misc]
    arena = ChampionChallengerArena(champion(), challenger(), pointer)
    for surface in ("promote", "swap_active", "set_active", "update_pointer", "demote"):
        assert not hasattr(arena, surface)
        assert not hasattr(pointer, surface)
    _, report, _ = run_arena(list(BASIC_EVENTS), arena)
    assert report.active_pointer == pointer  # unchanged by the run
    assert not any("promot" in name for name in report_field_names())


def test_active_pointer_not_on_champion_is_rejected() -> None:
    foreign = ActivePointerSnapshot(active_participant_id="chal-1", recorded_at=T0)
    with pytest.raises(ArenaError, match="never moves the active pointer"):
        ChampionChallengerArena(champion(), challenger(), foreign)


def test_every_output_is_marked_virtual_and_non_executable() -> None:
    arena = ChampionChallengerArena(champion(), challenger(), POINTER)
    result = ReplayRunner(arena.consume).run(
        ReplayDataset(
            dataset_id="mark",
            events=tuple(BASIC_EVENTS),
            versions={"arena": "1.0.0"},
            seed="seed:1",
        )
    )
    for step in result.steps:
        assert step.output["virtual"] is True
        assert step.output["executable"] is False
        assert str(step.output["note"]).startswith("RESEARCH:")
    report = arena.report()
    assert report.virtual_only is True and report.executable is False
    for comparison in report.comparisons:
        for decision in (comparison.champion, comparison.challenger):
            assert decision is not None
            assert decision.virtual_only is True
            assert decision.executable is False
    with pytest.raises(ArenaError, match="virtual/non-executable"):
        VirtualParticipantDecision(
            participant_id="p",
            role=ParticipantRole.CHALLENGER,
            event_id="e",
            candidate_id="c",
            as_of=T0,
            decision="APPROVE",
            executable=True,
        )


# -- grouping / hygiene ------------------------------------------------------------------


def test_run_split_mixing_rejected() -> None:
    arena = ChampionChallengerArena(
        champion(), challenger(), POINTER, run_id="run-A", split_id="s1"
    )
    with pytest.raises(ArenaError, match="fail closed"):
        run_arena([candidate_event("e1", 0, "c1")], arena)  # group ("", "")


def test_conflicting_duplicate_candidate_rejected_identical_deduped() -> None:
    deduped = [
        candidate_event("e1", 0, "c1"),
        candidate_event("e1-dup", 0, "c1"),
        candidate_event("e2", 1, "c2"),
    ]
    _, report, _ = run_arena(deduped)
    assert report.metrics.total_candidates == 2
    conflicting = [
        candidate_event("e1", 0, "c1"),
        candidate_event("e2", 0, "c1", score="9"),
    ]
    with pytest.raises(ArenaError, match="different content"):
        run_arena(conflicting)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ArenaError, match="timezone-aware"):
        ActivePointerSnapshot(
            active_participant_id="champ-1",
            recorded_at=datetime(2026, 6, 1, 14, 30),  # noqa: DTZ001
        )


def test_input_events_not_mutated() -> None:
    events = list(BASIC_EVENTS)
    before = [event.canonical() for event in events]
    run_arena(events)
    assert [event.canonical() for event in events] == before


def test_source_has_no_forbidden_imports_or_order_surface() -> None:
    import trading_application.champion_challenger as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for token in (
        "OrderIntent", "SubmitAuthorization", ".submit(", ".cancel(", "FLATTEN",
        "model_registry", "datetime.now", "utcnow", "time.time", "random.", "uuid4",
        "def promote", "def swap", "def set_active",
    ):
        assert token not in source, token
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "execution_agent", "httpx", "requests",
            "socket", "trading_persistence", "trading_event_bus",
        ):
            assert forbidden not in line, line
