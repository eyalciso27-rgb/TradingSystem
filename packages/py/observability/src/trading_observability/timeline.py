"""Linked processing timeline reconstruction.

Every stage of the pipeline logs/emits its event_id, the causation_id of the
event that triggered it, and the correlation_id of the originating source
event. This module reconstructs the canonical end-to-end chain

    source_event -> candidate -> assessment -> decision -> risk -> order
    -> fill -> position

from those identifiers, enabling the source-to-decision (and onward) trace
required by the Definition of Done and later browser-side timeline views.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

TIMELINE_STAGES: Final[tuple[str, ...]] = (
    "source_event",
    "candidate",
    "assessment",
    "decision",
    "risk",
    "order",
    "fill",
    "position",
)


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One pipeline stage occurrence, as recorded in structured logs/events."""

    event_id: str
    stage: str
    correlation_id: str
    causation_id: str | None  # None only for the originating source event
    timestamp: str

    def __post_init__(self) -> None:
        if self.stage not in TIMELINE_STAGES:
            raise ValueError(f"unknown timeline stage: {self.stage!r}")


class BrokenTimelineError(Exception):
    """The causation chain cannot be resolved into a linked timeline."""


def build_timeline(
    entries: Iterable[TimelineEntry], *, correlation_id: str
) -> Sequence[TimelineEntry]:
    """Return the causation-linked chain for one correlation_id, in order.

    The chain starts at the entry without a causation_id (the source event)
    and follows event_id -> causation_id links. Entries from other
    correlations are ignored; a missing root, a broken link, or a stage that
    goes backwards raises :class:`BrokenTimelineError` (fail closed — a
    partial chain is reported, never silently patched).
    """
    scoped = [e for e in entries if e.correlation_id == correlation_id]
    by_cause: dict[str | None, TimelineEntry] = {}
    for entry in scoped:
        if entry.causation_id in by_cause:
            raise BrokenTimelineError(
                f"multiple entries share causation_id {entry.causation_id!r}"
            )
        by_cause[entry.causation_id] = entry

    root = by_cause.get(None)
    if root is None:
        raise BrokenTimelineError(f"no source event for correlation_id {correlation_id!r}")

    chain: list[TimelineEntry] = [root]
    while len(chain) < len(scoped):
        successor = by_cause.get(chain[-1].event_id)
        if successor is None:
            raise BrokenTimelineError(
                f"broken causation link after event {chain[-1].event_id!r}"
            )
        if TIMELINE_STAGES.index(successor.stage) < TIMELINE_STAGES.index(chain[-1].stage):
            raise BrokenTimelineError(
                f"stage order violation: {chain[-1].stage!r} -> {successor.stage!r}"
            )
        chain.append(successor)
    return chain
