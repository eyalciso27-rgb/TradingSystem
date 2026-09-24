from __future__ import annotations

import pytest
from trading_observability.timeline import (
    BrokenTimelineError,
    TimelineEntry,
    build_timeline,
)


def entry(
    event_id: str,
    stage: str,
    causation_id: str | None,
    *,
    correlation_id: str = "correlation-1",
) -> TimelineEntry:
    return TimelineEntry(
        event_id=event_id,
        stage=stage,
        correlation_id=correlation_id,
        causation_id=causation_id,
        timestamp="2026-01-01T00:00:00+00:00",
    )


def test_timeline_entry_rejects_unknown_stage() -> None:
    with pytest.raises(ValueError, match="unknown timeline stage: 'publish'"):
        entry("event-1", "publish", None)


def test_build_timeline_follows_causation_and_ignores_other_correlations() -> None:
    root = entry("source-1", "source_event", None)
    candidate = entry("candidate-1", "candidate", "source-1")
    decision = entry("decision-1", "decision", "candidate-1")
    unrelated = entry("other-source", "source_event", None, correlation_id="correlation-2")

    result = build_timeline([decision, unrelated, root, candidate], correlation_id="correlation-1")

    assert result == [root, candidate, decision]


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([], "no source event"),
        (
            [
                entry("root", "source_event", None),
                entry("orphan", "candidate", "missing"),
            ],
            "broken causation link after event 'root'",
        ),
        (
            [
                entry("root", "source_event", None),
                entry("decision", "decision", "root"),
                entry("candidate", "candidate", "decision"),
            ],
            "stage order violation: 'decision' -> 'candidate'",
        ),
        (
            [
                entry("root-1", "source_event", None),
                entry("root-2", "source_event", None),
            ],
            "multiple entries share causation_id None",
        ),
    ],
)
def test_build_timeline_rejects_invalid_chains(entries: list[TimelineEntry], message: str) -> None:
    with pytest.raises(BrokenTimelineError, match=message):
        build_timeline(entries, correlation_id="correlation-1")
