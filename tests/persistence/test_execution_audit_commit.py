"""A COMMIT that fails must not look like a durable record.

``test_a_failed_audit_commit_means_zero_broker_submissions`` proves what the
execution service does when the sink raises. This proves the other half — that
the sink actually raises when the COMMIT itself fails, rather than the append
body succeeding and the failure being lost on the way out of the transaction.

Deterministic and offline: the transaction manager is driven directly, with no
database involved. Nothing here claims a real PostgreSQL commit failure was
induced; it was not.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from trading_contracts.enums import Environment
from trading_persistence.execution_audit import (
    DurableExecutionAuditSink,
    ExecutionAuditError,
)
from trading_persistence.object_storage import FilesystemObjectStorage

NOW = datetime(2026, 8, 19, 15, 30, tzinfo=UTC)


def _record() -> dict[str, object]:
    return {
        "decision_id": "decision-commit-failure",
        "client_order_id": "exec-ord-1",
        "symbol": "AAPL",
        "mode": "PAPER",
        "trading_readiness": "PAPER_READY",
        "evaluated_at": NOW.isoformat(),
    }


class _CommitFailingEngine:
    """An Engine whose transaction fails on COMMIT, not on the work.

    The append body runs to completion; the failure happens where SQLAlchemy
    commits, on context exit. That is the case a sink can silently swallow, so
    it is the case worth writing down.
    """

    def __init__(self) -> None:
        self.body_ran = False
        self.committed = False

    def begin(self) -> _CommitFailingEngine:
        return self

    def connect(self) -> _CommitFailingEngine:
        return self

    def __enter__(self) -> _CommitFailingEngine:
        return self

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            # The work succeeded; the COMMIT is what fails.
            raise OSError("connection lost during COMMIT")

    def execute(self, *args: object, **kw: object) -> Any:
        self.body_ran = True
        return _Result()


class _Result:
    def one_or_none(self) -> None:
        return None

    def all(self) -> list[object]:
        return []


@pytest.fixture()
def objects(tmp_path: Path) -> FilesystemObjectStorage:
    return FilesystemObjectStorage(tmp_path / "objects")


def test_a_commit_failure_propagates_and_returns_no_reference(
    objects: FilesystemObjectStorage,
) -> None:
    """The append ran, the commit failed, and persist() returned nothing."""
    engine = _CommitFailingEngine()
    sink = DurableExecutionAuditSink(
        engine,  # type: ignore[arg-type]
        objects,
        environment=Environment.PAPER,
    )

    with pytest.raises(ExecutionAuditError, match="could not be committed"):
        sink.persist(_record())

    assert engine.body_ran, "the append body really did execute"
    assert engine.committed is False, "and the commit never succeeded"


def test_a_commit_failure_on_the_outcome_record_also_propagates(
    objects: FilesystemObjectStorage,
) -> None:
    sink = DurableExecutionAuditSink(
        _CommitFailingEngine(),  # type: ignore[arg-type]
        objects,
        environment=Environment.PAPER,
    )

    with pytest.raises(ExecutionAuditError):
        sink.persist_outcome({**_record(), "submission_outcome": "SUBMITTED"})


def test_a_commit_failure_on_the_prepared_marker_also_propagates(
    objects: FilesystemObjectStorage,
) -> None:
    """The point of no return must fail loudly, or it is not a marker."""
    sink = DurableExecutionAuditSink(
        _CommitFailingEngine(),  # type: ignore[arg-type]
        objects,
        environment=Environment.PAPER,
    )

    with pytest.raises(ExecutionAuditError):
        sink.persist_prepared(_record())


def test_the_payload_is_written_before_the_commit_is_attempted(
    objects: FilesystemObjectStorage,
) -> None:
    """A crash between them leaves an orphan object, which is harmless.

    The reverse order would leave a committed chain row pointing at nothing,
    which is indistinguishable from tampering.
    """
    from trading_persistence.execution_audit import canonical_payload

    sink = DurableExecutionAuditSink(
        _CommitFailingEngine(),  # type: ignore[arg-type]
        objects,
        environment=Environment.PAPER,
    )
    record: Mapping[str, object] = _record()

    with pytest.raises(ExecutionAuditError):
        sink.persist(record)

    from trading_persistence.object_storage import content_ref

    assert objects.exists(content_ref(canonical_payload(record))), (
        "the payload was stored before the commit was attempted"
    )
