"""Replay runner drills (IMP-1201). DoD: repeated replay yields identical
results — including byte-identical result hashes and shuffle invariance.
Pure/offline: no clock, no network, no broker, no persistence."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from trading_application.replay_runner import (
    Payload,
    ReplayDataset,
    ReplayError,
    ReplayEvent,
    ReplayRunManifest,
    ReplayRunner,
    ReplaySnapshot,
    VersionRequirement,
)

T0 = datetime(2026, 7, 1, 14, 30, tzinfo=UTC)


def event(
    event_id: str,
    *,
    minute: int,
    sequence: int = 0,
    kind: str = "OBSERVATION",
    payload: Payload | None = None,
    observed_minute: int | None = None,
) -> ReplayEvent:
    available = T0 + timedelta(minutes=minute)
    observed = T0 + timedelta(minutes=observed_minute if observed_minute is not None else minute)
    return ReplayEvent(
        event_id=event_id,
        sequence=sequence,
        kind=kind,
        payload=payload if payload is not None else {"symbol": "AAPL", "price": "232.10"},
        observed_at=observed,
        available_at=available,
    )


VERSIONS = {
    "decision-policy": "1.4.0",
    "risk-policy": "1.4.0",
    "technical-model": "1.3.0",
    "schema": "v1",
}


def dataset(events: list[ReplayEvent], *, seed: str = "seed:1") -> ReplayDataset:
    return ReplayDataset(dataset_id="ds-1", events=tuple(events), versions=VERSIONS, seed=seed)


def echo_consumer(
    ev: ReplayEvent, snapshot: ReplaySnapshot, as_of: datetime, manifest: ReplayRunManifest
) -> Payload:
    # Deterministic consumer: derives output only from its frozen inputs.
    return {
        "event": ev.event_id,
        "visible_count": len(snapshot.visible_event_ids),
        "as_of": as_of.isoformat(),
        "policy": manifest.versions["decision-policy"],
    }


def make_runner() -> ReplayRunner:
    return ReplayRunner(
        echo_consumer,
        required_versions=(VersionRequirement("decision-policy", "1.4.0"),),
    )


BASE_EVENTS = [
    event("e-1", minute=0),
    event("e-2", minute=1),
    event("e-3", minute=1, sequence=1),
    event("e-4", minute=5),
]


# -- DoD: identical repeated replay ------------------------------------------------


def test_repeated_replay_yields_identical_results() -> None:
    runner = make_runner()
    first = runner.run(dataset(list(BASE_EVENTS)))
    second = runner.run(dataset(list(BASE_EVENTS)))
    assert first == second
    assert first.result_hash == second.result_hash
    assert first.manifest.run_id == second.manifest.run_id


def test_shuffled_input_yields_same_result_and_hash() -> None:
    runner = make_runner()
    ordered = runner.run(dataset(list(BASE_EVENTS)))
    shuffled = runner.run(dataset(list(reversed(BASE_EVENTS))))
    assert shuffled == ordered
    assert shuffled.result_hash == ordered.result_hash


# -- canonical ordering --------------------------------------------------------------


def test_same_available_at_ordered_by_sequence_then_event_id() -> None:
    runner = make_runner()
    result = runner.run(dataset(list(BASE_EVENTS)))
    assert [step.event_id for step in result.steps] == ["e-1", "e-2", "e-3", "e-4"]
    tied = [
        event("b-later", minute=2, sequence=1),
        event("a-first", minute=2, sequence=1),
        event("z-seq0", minute=2, sequence=0),
    ]
    tied_result = runner.run(dataset(tied))
    assert [step.event_id for step in tied_result.steps] == ["z-seq0", "a-first", "b-later"]


def test_sequence_change_that_reorders_changes_result_hash() -> None:
    runner = make_runner()
    baseline = runner.run(dataset(list(BASE_EVENTS)))
    reordered = [
        event("e-1", minute=0),
        event("e-2", minute=1, sequence=2),  # now sorts after e-3
        event("e-3", minute=1, sequence=1),
        event("e-4", minute=5),
    ]
    changed = runner.run(dataset(reordered))
    assert [step.event_id for step in changed.steps] == ["e-1", "e-3", "e-2", "e-4"]
    assert changed.result_hash != baseline.result_hash


# -- point-in-time / as_of ------------------------------------------------------------


def test_snapshot_never_contains_future_events() -> None:
    result = make_runner().run(dataset(list(BASE_EVENTS)))
    for step in result.steps:
        for visible_id in step.snapshot.visible_event_ids:
            source = next(ev for ev in BASE_EVENTS if ev.event_id == visible_id)
            assert source.available_at <= step.as_of
    # the first step sees exactly one event; the last sees all four
    assert len(result.steps[0].snapshot.visible_event_ids) == 1
    assert len(result.steps[-1].snapshot.visible_event_ids) == 4


def test_as_of_is_the_consumed_events_available_at() -> None:
    result = make_runner().run(dataset(list(BASE_EVENTS)))
    for step in result.steps:
        source = next(ev for ev in BASE_EVENTS if ev.event_id == step.event_id)
        assert step.as_of == source.available_at


def test_observed_after_available_rejected_fail_closed() -> None:
    with pytest.raises(ReplayError, match="precedes observed_at"):
        event("bad", minute=1, observed_minute=2)  # visible before observed


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ReplayError, match="timezone-aware"):
        ReplayEvent(
            event_id="naive",
            sequence=0,
            kind="OBSERVATION",
            payload={},
            observed_at=datetime(2026, 7, 1, 14, 30),  # noqa: DTZ001
            available_at=T0,
        )


# -- exact versions / seed --------------------------------------------------------------


def test_manifest_records_exact_versions_and_seed() -> None:
    result = make_runner().run(dataset(list(BASE_EVENTS), seed="seed:42"))
    assert result.manifest.versions == dict(sorted(VERSIONS.items()))
    assert result.manifest.seed == "seed:42"
    assert result.manifest.dataset_fingerprint.startswith("sha256:")
    assert result.manifest.event_count == 4


def test_version_change_changes_run_id_and_hash() -> None:
    runner = ReplayRunner(echo_consumer)  # no requirement so both versions run
    baseline = runner.run(dataset(list(BASE_EVENTS)))
    bumped = ReplayDataset(
        dataset_id="ds-1",
        events=tuple(BASE_EVENTS),
        versions={**VERSIONS, "risk-policy": "1.5.0"},
        seed="seed:1",
    )
    changed = runner.run(bumped)
    assert changed.manifest.run_id != baseline.manifest.run_id
    assert changed.result_hash != baseline.result_hash


def test_missing_or_mismatched_required_version_fails_closed() -> None:
    missing = ReplayRunner(
        echo_consumer, required_versions=(VersionRequirement("news-model", "9.9.9"),)
    )
    with pytest.raises(ReplayError, match="no declared version"):
        missing.run(dataset(list(BASE_EVENTS)))
    mismatched = ReplayRunner(
        echo_consumer, required_versions=(VersionRequirement("decision-policy", "2.0.0"),)
    )
    with pytest.raises(ReplayError, match="version mismatch"):
        mismatched.run(dataset(list(BASE_EVENTS)))


def test_seed_identity_and_divergence() -> None:
    runner = make_runner()
    same_a = runner.run(dataset(list(BASE_EVENTS), seed="seed:7"))
    same_b = runner.run(dataset(list(BASE_EVENTS), seed="seed:7"))
    assert same_a.result_hash == same_b.result_hash
    other = runner.run(dataset(list(BASE_EVENTS), seed="seed:8"))
    assert other.manifest.dataset_fingerprint != same_a.manifest.dataset_fingerprint
    assert other.manifest.run_id != same_a.manifest.run_id


# -- duplicates ---------------------------------------------------------------------------


def test_identical_duplicate_event_deduplicated() -> None:
    runner = make_runner()
    with_dup = list(BASE_EVENTS) + [event("e-2", minute=1)]
    result = runner.run(dataset(with_dup))
    baseline = runner.run(dataset(list(BASE_EVENTS)))
    assert [step.event_id for step in result.steps] == [s.event_id for s in baseline.steps]


def test_conflicting_duplicate_event_rejected() -> None:
    conflicting = list(BASE_EVENTS) + [
        event("e-2", minute=1, payload={"symbol": "AAPL", "price": "999.99"})
    ]
    with pytest.raises(ReplayError, match="two different payloads"):
        make_runner().run(dataset(conflicting))


# -- purity / no mutation ------------------------------------------------------------------


def test_input_dataset_is_not_mutated() -> None:
    events = list(BASE_EVENTS)
    ds = dataset(events)
    before = copy.deepcopy([ev.canonical() for ev in ds.events])
    make_runner().run(ds)
    after = [ev.canonical() for ev in ds.events]
    assert after == before


def test_float_payload_and_float_output_rejected() -> None:
    with pytest.raises(ReplayError, match="float"):
        event("f-1", minute=0, payload={"price": 1.23})  # type: ignore[dict-item]

    def bad_consumer(
        ev: ReplayEvent, snapshot: ReplaySnapshot, as_of: datetime, manifest: ReplayRunManifest
    ) -> Any:
        return {"value": 0.5}

    with pytest.raises(ReplayError, match="float"):
        ReplayRunner(bad_consumer).run(dataset([event("e-1", minute=0)]))


def test_no_clock_network_broker_or_persistence_imports() -> None:
    import trading_application.replay_runner as module

    assert module.__file__ is not None
    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    for fragment in ("datetime.now", "time.time", "utcnow", "random.", "uuid4"):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "httpx", "requests", "socket",
            "trading_persistence", "trading_event_bus",
        ):
            assert forbidden not in line, line
