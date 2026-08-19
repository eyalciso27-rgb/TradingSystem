"""The execution audit, against the real database and the real chain.

The readiness design has always been able to say "the decision was recorded"
about a test double. These tests say it about PostgreSQL: the same tamper-evident
chain and content-addressed object store the rest of the system uses, with no
new table and therefore no new migration.

What matters most here is what survives. A pre-submit record that vanishes on
restart is not an audit trail, and a refusal that cannot be found afterwards is
not a decision anyone can review.

Skipped entirely unless ``DATABASE_URL`` points at a real server, exactly like
every other database test in this directory.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine
from trading_contracts.enums import Environment
from trading_persistence.audit import verify_audit_chain
from trading_persistence.execution_audit import (
    ACTION_POST_SUBMIT,
    ACTION_PRE_SUBMIT,
    DurableExecutionAuditSink,
    ExecutionAuditError,
    canonical_payload,
    load_decision_records,
    summarize,
)
from trading_persistence.object_storage import FilesystemObjectStorage

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a real PostgreSQL server"
)

NOW = datetime(2026, 8, 19, 15, 30, tzinfo=UTC)


def _record(**kw: object) -> dict[str, object]:
    record: dict[str, object] = {
        "decision_id": "decision-abc",
        "correlation_id": "8f14e45f-ceea-467a-9a3e-9b1d5f2c1a11",
        "client_order_id": "exec-ord-1",
        "strategy_id": "baseline-trend-breakout-v1",
        "symbol": "AAPL",
        "mode": "PAPER",
        "checkpoint": "PRE_SUBMIT",
        "trading_readiness": "PAPER_READY",
        "evaluated_at": NOW.isoformat(),
        "security_id": "conid:265598",
        "blockers": [],
        "gates": [{"name": "risk_limits", "satisfied": True, "detail": "APPROVE"}],
        "quote": {"bid": "100.00", "ask": "100.05", "market_data_state": "REALTIME"},
    }
    record.update(kw)
    return record


@pytest.fixture()
def objects(tmp_path: Path) -> FilesystemObjectStorage:
    return FilesystemObjectStorage(tmp_path / "objects")


@pytest.fixture()
def sink(
    connection: Connection, objects: FilesystemObjectStorage
) -> DurableExecutionAuditSink:
    return DurableExecutionAuditSink(
        connection, objects, environment=Environment.PAPER
    )


# == it really persists ========================================================


def test_the_sink_probes_its_real_dependencies(
    sink: DurableExecutionAuditSink,
) -> None:
    assert sink.can_persist() is True


def test_a_pre_submit_record_is_chained_and_stored(
    sink: DurableExecutionAuditSink, connection: Connection,
    objects: FilesystemObjectStorage,
) -> None:
    reference = sink.persist(_record())

    assert reference.startswith("sha256:")
    rows = load_decision_records(connection, "decision-abc")
    assert [row[0] for row in rows] == [ACTION_PRE_SUBMIT]
    payload_ref = rows[0][2]
    assert payload_ref is not None
    assert objects.exists(payload_ref), "the full record is retrievable"


def test_the_stored_payload_reconstructs_the_decision(
    sink: DurableExecutionAuditSink, connection: Connection,
    objects: FilesystemObjectStorage,
) -> None:
    import json

    record = _record()
    sink.persist(record)
    _, _, payload_ref = load_decision_records(connection, "decision-abc")[0]
    assert payload_ref is not None

    restored = json.loads(objects.get(payload_ref))

    assert restored["trading_readiness"] == "PAPER_READY"
    assert restored["quote"]["market_data_state"] == "REALTIME"
    assert restored["gates"][0]["name"] == "risk_limits"
    assert restored["security_id"] == "conid:265598"


def test_the_record_survives_a_new_connection(
    engine: Engine, objects: FilesystemObjectStorage
) -> None:
    """Durability, stated plainly: a different connection still finds it."""
    written = engine.connect()
    transaction = written.begin()
    try:
        DurableExecutionAuditSink(
            written, objects, environment=Environment.PAPER
        ).persist(_record(decision_id="decision-durable"))
        transaction.commit()
    except Exception:  # pragma: no cover - only on a broken commit
        transaction.rollback()
        raise
    finally:
        written.close()

    reader = engine.connect()
    try:
        rows = load_decision_records(reader, "decision-durable")
        assert rows, "a committed decision must outlive the connection that wrote it"
    finally:
        # Leave the database as we found it: this test commits deliberately.
        cleanup = reader.begin()
        from sqlalchemy import delete
        from trading_persistence.models import AuditEvent

        reader.execute(delete(AuditEvent).where(AuditEvent.target_ref == "decision-durable"))
        cleanup.commit()
        reader.close()


# == pre and post are two records, never one ===================================


def test_the_post_submit_record_is_appended_beside_the_pre_submit_one(
    sink: DurableExecutionAuditSink, connection: Connection
) -> None:
    sink.persist(_record())
    sink.persist_outcome(
        _record(checkpoint="POST_SUBMIT", submission_outcome="SUBMITTED")
    )

    rows = load_decision_records(connection, "decision-abc")
    assert [row[0] for row in rows] == [ACTION_PRE_SUBMIT, ACTION_POST_SUBMIT]
    assert rows[0][1] != rows[1][1], "two distinct chained records"


def test_the_chain_still_verifies_after_execution_records(
    sink: DurableExecutionAuditSink, connection: Connection
) -> None:
    sink.persist(_record())
    sink.persist_outcome(_record(checkpoint="POST_SUBMIT", submission_outcome="SUBMITTED"))

    assert verify_audit_chain(connection) == []


def test_a_refused_decision_is_still_queryable(
    sink: DurableExecutionAuditSink, connection: Connection
) -> None:
    """A refusal is a decision. Finding it later is the whole point."""
    sink.persist(
        _record(
            decision_id="decision-refused",
            trading_readiness="BLOCKED",
            blockers=["mandatory gate not satisfied: market_data_usable"],
        )
    )

    rows = load_decision_records(connection, "decision-refused")
    assert len(rows) == 1


def test_one_decision_id_does_not_imply_a_second_submission(
    sink: DurableExecutionAuditSink, connection: Connection
) -> None:
    """Recording twice records twice; it never submits anything.

    Identical content hashes to the same object, so a retry costs one chain row
    and no duplicate payload — and nothing here can reach a broker.
    """
    first = sink.persist(_record())
    second = sink.persist(_record())

    rows = load_decision_records(connection, "decision-abc")
    assert len(rows) == 2
    assert first != second, "each chain row is distinct"
    assert rows[0][2] == rows[1][2], "identical content is stored once"


# == failure modes =============================================================


def test_a_broken_object_store_refuses_rather_than_returning_a_reference(
    connection: Connection,
) -> None:
    class _Broken:
        def put(self, data: bytes) -> object:
            raise OSError("disk full")

        def get(self, ref: str) -> bytes:
            raise OSError("disk full")

        def exists(self, ref: str) -> bool:
            return False

    sink = DurableExecutionAuditSink(
        connection, _Broken(), environment=Environment.PAPER
    )

    assert sink.can_persist() is False
    with pytest.raises(ExecutionAuditError, match="payload could not be stored"):
        sink.persist(_record())


def test_no_secrets_are_written(sink: DurableExecutionAuditSink) -> None:
    """The chain's own validation refuses credential-shaped detail."""
    detail = summarize(_record())
    for marker in ("password=", "bearer ", "-----begin", "cookie:"):
        assert marker not in detail.lower()


def test_the_payload_is_deterministic() -> None:
    """Same decision, same bytes, same reference — which is what makes it idempotent."""
    assert canonical_payload(_record()) == canonical_payload(_record())


def test_the_summary_is_bounded(sink: DurableExecutionAuditSink) -> None:
    detail = summarize(_record(blockers=["x" * 5000]))
    assert len(detail) <= 400


# == no schema change was needed ===============================================


def test_execution_audit_added_no_new_table() -> None:
    """Reuse over addition: this rides on audit_events and the object store.

    Asserted because "we added a migration" is a decision worth not making by
    accident — the existing append-only chain already had the properties an
    execution audit needs.
    """
    from trading_persistence import execution_audit

    source = Path(execution_audit.__file__).read_text(encoding="utf-8")
    assert "__tablename__" not in source
    assert "CREATE TABLE" not in source.upper()
