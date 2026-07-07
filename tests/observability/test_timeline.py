"""Source-to-decision (and onward) linked timeline reconstruction."""

from __future__ import annotations

import pytest
from trading_observability import (
    TIMELINE_STAGES,
    BrokenTimelineError,
    TimelineEntry,
    build_timeline,
)

CID = "corr-1"


def make_chain() -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    previous: str | None = None
    for index, stage in enumerate(TIMELINE_STAGES):
        event_id = f"evt-{index}"
        entries.append(
            TimelineEntry(
                event_id=event_id,
                stage=stage,
                correlation_id=CID,
                causation_id=previous,
                timestamp=f"2026-07-07T10:00:0{index}+00:00",
            )
        )
        previous = event_id
    return entries


def test_source_to_decision_trace_resolves_in_order() -> None:
    shuffled = make_chain()[::-1]  # arrival order must not matter
    chain = build_timeline(shuffled, correlation_id=CID)
    assert [e.stage for e in chain] == list(TIMELINE_STAGES)
    stages = [e.stage for e in chain]
    assert stages.index("source_event") < stages.index("decision")  # DoD: source→decision
    assert chain[-1].stage == "position"
    for parent, child in zip(chain, chain[1:], strict=False):
        assert child.causation_id == parent.event_id


def test_other_correlations_ignored() -> None:
    noise = TimelineEntry(
        event_id="other", stage="decision", correlation_id="corr-2",
        causation_id=None, timestamp="2026-07-07T10:00:00+00:00",
    )
    chain = build_timeline([*make_chain(), noise], correlation_id=CID)
    assert len(chain) == len(TIMELINE_STAGES)


def test_broken_link_fails_closed() -> None:
    entries = [e for e in make_chain() if e.stage != "assessment"]
    with pytest.raises(BrokenTimelineError):
        build_timeline(entries, correlation_id=CID)


def test_missing_source_event_fails_closed() -> None:
    entries = [e for e in make_chain() if e.stage != "source_event"]
    with pytest.raises(BrokenTimelineError):
        build_timeline(entries, correlation_id=CID)


def test_unknown_stage_rejected() -> None:
    with pytest.raises(ValueError):
        TimelineEntry(
            event_id="e", stage="mystery", correlation_id=CID,
            causation_id=None, timestamp="2026-07-07T10:00:00+00:00",
        )
