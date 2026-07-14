"""Replay runner (IMP-1201) — deterministic point-in-time event replay.

Authority: Master Architecture / CLAUDE.md invariants (preserve
point-in-time semantics and prevent look-ahead in replay/backtesting;
deterministic engines; tz-aware timestamps; schema-versioned inputs).
Task scope: as-of snapshots, exact versions and deterministic event
ordering. DoD: repeated replay yields identical results.

The runner is PURE and offline: frozen inputs (a ``ReplayDataset`` of
events + exact policy/engine/model/schema versions + a declared seed) in,
a ``ReplayResult`` out. It never reads a wall clock, never touches a
network, broker, connector or database, and never mutates its input — the
same dataset always produces byte-identical output and the same
``result_hash``, regardless of the arrival order of the input list.

Hard rules:

- CANONICAL ORDERING: events are replayed strictly by
  ``(available_at, sequence, event_id)``; the original input order carries
  no meaning.
- POINT-IN-TIME / AS-OF: each step's ``as_of`` is the consumed event's
  ``available_at``; the step snapshot contains ONLY events whose
  ``available_at <= as_of``. ``observed_at > available_at`` (an event
  visible before it happened) is rejected fail-closed, as are naive
  datetimes.
- EXACT VERSIONS: the run manifest freezes every declared component
  version and the seed; a consumer requiring a version that is missing or
  different fails closed — no silent fallback.
- DEDUP / CONFLICT: the same ``event_id`` with an identical payload is
  deduplicated deterministically (first canonical occurrence wins); the
  same ``event_id`` with a DIFFERENT payload is a corruption signal and is
  rejected fail-closed.
- IDENTITY: ``run_id`` and ``result_hash`` are sha256 over a canonical,
  stable JSON serialization (sorted keys, ISO timestamps, string decimals)
  of dataset fingerprint + ordering + versions + seed (+ outputs for the
  result hash).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

RUNNER_VERSION = "replay-runner-1.0.0"

#: Payload values are frozen, canonical-serializable primitives. Decimals
#: must be provided as strings by the dataset author (no uncontrolled
#: floats in hashed material).
PayloadValue = str | int | bool | None
Payload = Mapping[str, PayloadValue]


class ReplayError(Exception):
    """Fail-closed replay failure (ordering, lookahead, versions, dedup)."""


def _require_aware(stamp: datetime, name: str) -> None:
    if stamp.tzinfo is None:
        raise ReplayError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """One immutable historical event."""

    event_id: str
    sequence: int
    kind: str
    payload: Payload
    observed_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        if not self.event_id.strip():
            raise ReplayError("event_id is mandatory")
        if not self.kind.strip():
            raise ReplayError("kind is mandatory")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.available_at, "available_at")
        for key, value in self.payload.items():
            if isinstance(value, float):
                raise ReplayError(
                    f"payload[{key!r}] is a float; use string decimals for"
                    " canonical, stable hashing"
                )
        if self.observed_at > self.available_at:
            # An event cannot become visible before it was observed being
            # constructed the other way round; available_at >= observed_at.
            raise ReplayError(
                f"event {self.event_id!r}: available_at precedes observed_at"
                " (inconsistent point-in-time metadata)"
            )

    def canonical(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "payload": dict(sorted(self.payload.items())),
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ReplayDataset:
    """Frozen input: events + exact component versions + declared seed."""

    dataset_id: str
    events: tuple[ReplayEvent, ...]
    versions: Mapping[str, str]  # component -> exact version
    seed: str

    def __post_init__(self) -> None:
        if not self.dataset_id.strip():
            raise ReplayError("dataset_id is mandatory")
        if not self.seed.strip():
            raise ReplayError("seed is mandatory (no undeclared randomness)")
        for component, version in self.versions.items():
            if not version.strip():
                raise ReplayError(f"version for {component!r} is empty; fail closed")

    def fingerprint(self) -> str:
        """Stable content fingerprint — independent of input event order."""
        material = {
            "dataset_id": self.dataset_id,
            "events": sorted(
                (event.canonical() for event in self.events),
                key=lambda entry: (
                    str(entry["available_at"]),
                    int(str(entry["sequence"])),
                    str(entry["event_id"]),
                ),
            ),
            "versions": dict(sorted(self.versions.items())),
            "seed": self.seed,
        }
        return "sha256:" + hashlib.sha256(_canonical_json(material)).hexdigest()


@dataclass(frozen=True, slots=True)
class ReplayRunManifest:
    """Everything needed to reproduce the run exactly."""

    run_id: str
    dataset_fingerprint: str
    versions: Mapping[str, str]
    seed: str
    runner_version: str
    event_count: int


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    """The as-of view at one step: only what was knowable at ``as_of``."""

    as_of: datetime
    visible_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplayStep:
    """One consumed event + the consumer's output at that as_of."""

    step_index: int
    event_id: str
    kind: str
    as_of: datetime
    snapshot: ReplaySnapshot
    output: Payload


@dataclass(frozen=True, slots=True)
class ReplayResult:
    manifest: ReplayRunManifest
    steps: tuple[ReplayStep, ...]
    result_hash: str


def _canonical_json(material: object) -> bytes:
    return json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=_reject
    ).encode("ascii")


def _reject(value: object) -> object:
    raise ReplayError(f"non-canonical value in hashed material: {type(value).__name__}")


#: A consumer sees (event, snapshot, as_of, manifest) and returns a frozen
#: output payload. Consumers are injected explicitly — the runner has no
#: default consumer and creates no external connection.
Consumer = Callable[[ReplayEvent, ReplaySnapshot, datetime, ReplayRunManifest], Payload]


@dataclass(frozen=True, slots=True)
class VersionRequirement:
    """A consumer's exact-version demand, checked fail-closed before the run."""

    component: str
    exact_version: str


class ReplayRunner:
    """Pure deterministic replay over a frozen dataset."""

    def __init__(
        self,
        consumer: Consumer,
        *,
        required_versions: Sequence[VersionRequirement] = (),
    ) -> None:
        self._consumer = consumer
        self._required = tuple(required_versions)

    def run(self, dataset: ReplayDataset) -> ReplayResult:
        # -- exact versions, fail-closed --------------------------------------
        for requirement in self._required:
            declared = dataset.versions.get(requirement.component)
            if declared is None:
                raise ReplayError(
                    f"required component {requirement.component!r} has no declared"
                    " version in the dataset; fail closed (no silent fallback)"
                )
            if declared != requirement.exact_version:
                raise ReplayError(
                    f"component {requirement.component!r} version mismatch:"
                    f" dataset declares {declared!r}, consumer requires"
                    f" {requirement.exact_version!r}; fail closed"
                )

        # -- canonical ordering + dedup/conflict ------------------------------
        ordered = sorted(
            dataset.events,
            key=lambda event: (event.available_at, event.sequence, event.event_id),
        )
        seen: dict[str, ReplayEvent] = {}
        unique: list[ReplayEvent] = []
        for event in ordered:
            existing = seen.get(event.event_id)
            if existing is None:
                seen[event.event_id] = event
                unique.append(event)
                continue
            if existing.canonical() == event.canonical():
                continue  # deterministic dedup: first canonical occurrence wins
            raise ReplayError(
                f"event_id {event.event_id!r} appears with two different payloads;"
                " dataset is corrupt — fail closed"
            )

        fingerprint = dataset.fingerprint()
        manifest = ReplayRunManifest(
            run_id="sha256:"
            + hashlib.sha256(
                _canonical_json(
                    {
                        "fingerprint": fingerprint,
                        "versions": dict(sorted(dataset.versions.items())),
                        "seed": dataset.seed,
                        "runner": RUNNER_VERSION,
                    }
                )
            ).hexdigest(),
            dataset_fingerprint=fingerprint,
            versions=dict(sorted(dataset.versions.items())),
            seed=dataset.seed,
            runner_version=RUNNER_VERSION,
            event_count=len(unique),
        )

        # -- point-in-time replay ---------------------------------------------
        steps: list[ReplayStep] = []
        visible: list[str] = []
        previous_as_of: datetime | None = None
        for index, event in enumerate(unique):
            as_of = event.available_at
            if previous_as_of is not None and as_of < previous_as_of:
                raise ReplayError("ordering violated: as_of moved backwards")
            previous_as_of = as_of
            visible.append(event.event_id)
            snapshot = ReplaySnapshot(as_of=as_of, visible_event_ids=tuple(visible))
            # Guard: nothing in the snapshot may be later than as_of.
            for visible_id in snapshot.visible_event_ids:
                if seen[visible_id].available_at > as_of:
                    raise ReplayError(
                        f"lookahead: event {visible_id!r} is in the snapshot"
                        f" before its available_at"
                    )
            output = self._consumer(event, snapshot, as_of, manifest)
            for key, value in output.items():
                if isinstance(value, float):
                    raise ReplayError(
                        f"consumer output[{key!r}] is a float; use string decimals"
                    )
            steps.append(
                ReplayStep(
                    step_index=index,
                    event_id=event.event_id,
                    kind=event.kind,
                    as_of=as_of,
                    snapshot=snapshot,
                    output=dict(sorted(output.items())),
                )
            )

        result_hash = "sha256:" + hashlib.sha256(
            _canonical_json(
                {
                    "run_id": manifest.run_id,
                    "steps": [
                        {
                            "step_index": step.step_index,
                            "event_id": step.event_id,
                            "kind": step.kind,
                            "as_of": step.as_of.isoformat(),
                            "visible": list(step.snapshot.visible_event_ids),
                            "output": dict(step.output),
                        }
                        for step in steps
                    ],
                }
            )
        ).hexdigest()
        return ReplayResult(manifest=manifest, steps=tuple(steps), result_hash=result_hash)


__all__ = [
    "RUNNER_VERSION",
    "Consumer",
    "Payload",
    "ReplayDataset",
    "ReplayError",
    "ReplayEvent",
    "ReplayResult",
    "ReplayRunManifest",
    "ReplayRunner",
    "ReplaySnapshot",
    "ReplayStep",
    "VersionRequirement",
]
