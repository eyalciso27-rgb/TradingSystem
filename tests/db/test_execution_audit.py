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
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool
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
def sink(engine: Engine, objects: FilesystemObjectStorage) -> DurableExecutionAuditSink:
    """The sink owns its own transactions, so it takes the ENGINE.

    These tests therefore COMMIT. They do not clean up: the chain is append-only
    and hash-linked, so deleting rows afterwards would break the linkage for
    everything appended later — which is precisely the tampering the chain
    exists to detect. Extra rows in an ephemeral CI database are the correct
    cost of proving durability, and unique decision ids keep them separable.
    """
    return DurableExecutionAuditSink(engine, objects, environment=Environment.PAPER)


@pytest.fixture()
def decision_id(request: pytest.FixtureRequest) -> str:
    """A decision id unique to this test, so committed rows never collide."""
    return f"decision-{request.node.name}-{uuid4().hex[:8]}"


# == it really persists ========================================================


def test_the_sink_probes_its_real_dependencies(
    sink: DurableExecutionAuditSink,
) -> None:
    assert sink.can_persist() is True


def test_a_pre_submit_record_is_visible_from_a_brand_new_connection(
    sink: DurableExecutionAuditSink, engine: Engine,
    objects: FilesystemObjectStorage, decision_id: str,
) -> None:
    """THE durability test, and the one the previous design could not pass.

    No caller transaction, no ``with engine.begin()`` wrapper around the sink,
    no commit by this test. ``persist`` returns, a completely new connection
    opens, and the record is already there — because the sink committed it.

    Previously the sink appended inside a caller's transaction, so a reference
    could be handed back, an order could reach the broker, and a crash before
    the caller's commit would have lost the pre-submit record entirely.
    """
    reference = sink.persist(_record(decision_id=decision_id))

    assert reference.startswith("sha256:")
    with engine.connect() as fresh:
        rows = load_decision_records(fresh, decision_id)

    assert [row[0] for row in rows] == [ACTION_PRE_SUBMIT]
    payload_ref = rows[0][2]
    assert payload_ref is not None
    assert objects.exists(payload_ref), "the full record is retrievable"


def test_the_stored_payload_reconstructs_the_decision(
    sink: DurableExecutionAuditSink, engine: Engine,
    objects: FilesystemObjectStorage, decision_id: str,
) -> None:
    import json

    sink.persist(_record(decision_id=decision_id))
    with engine.connect() as fresh:
        _, _, payload_ref = load_decision_records(fresh, decision_id)[0]
    assert payload_ref is not None

    restored = json.loads(objects.get(payload_ref))

    assert restored["trading_readiness"] == "PAPER_READY"
    assert restored["quote"]["market_data_state"] == "REALTIME"
    assert restored["gates"][0]["name"] == "risk_limits"
    assert restored["security_id"] == "conid:265598"


def test_the_sink_leaves_no_transaction_or_connection_open(
    sink: DurableExecutionAuditSink, engine: Engine, decision_id: str
) -> None:
    """A leaked transaction holds locks for the life of the process."""
    pool = engine.pool
    assert isinstance(pool, QueuePool), "the checkout count is a QueuePool fact"
    before = pool.checkedout()

    sink.can_persist()
    sink.persist(_record(decision_id=decision_id))
    sink.persist_outcome(
        _record(decision_id=decision_id, checkpoint="POST_SUBMIT",
                submission_outcome="SUBMITTED")
    )

    assert pool.checkedout() == before, "every connection was returned"


def test_the_chain_still_verifies_after_committed_records(engine: Engine) -> None:
    """The committed rows above left the chain intact."""
    with engine.connect() as reader:
        assert verify_audit_chain(reader) == []


# == pre and post are two records, never one ===================================


def test_the_post_submit_record_is_appended_beside_the_pre_submit_one(
    sink: DurableExecutionAuditSink, engine: Engine, decision_id: str
) -> None:
    sink.persist(_record(decision_id=decision_id))
    sink.persist_outcome(
        _record(decision_id=decision_id, checkpoint="POST_SUBMIT",
                submission_outcome="SUBMITTED")
    )

    with engine.connect() as fresh:
        rows = load_decision_records(fresh, decision_id)
    assert [row[0] for row in rows] == [ACTION_PRE_SUBMIT, ACTION_POST_SUBMIT]
    assert rows[0][1] != rows[1][1], "two distinct chained records"


def test_execution_records_keep_the_chain_verifiable(
    sink: DurableExecutionAuditSink, engine: Engine, decision_id: str
) -> None:
    sink.persist(_record(decision_id=decision_id))
    sink.persist_outcome(
        _record(decision_id=decision_id, checkpoint="POST_SUBMIT",
                submission_outcome="SUBMITTED")
    )

    with engine.connect() as fresh:
        assert verify_audit_chain(fresh) == []


def test_a_refused_decision_is_still_queryable(
    sink: DurableExecutionAuditSink, engine: Engine, decision_id: str
) -> None:
    """A refusal is a decision. Finding it later is the whole point."""
    sink.persist(
        _record(
            decision_id=decision_id,
            trading_readiness="BLOCKED",
            blockers=["mandatory gate not satisfied: market_data_usable"],
        )
    )

    with engine.connect() as fresh:
        assert len(load_decision_records(fresh, decision_id)) == 1


def test_one_decision_id_does_not_imply_a_second_submission(
    sink: DurableExecutionAuditSink, engine: Engine, decision_id: str
) -> None:
    """Recording twice records twice; it never submits anything.

    Identical content hashes to the same object, so a retry costs one chain row
    and no duplicate payload — and nothing here can reach a broker.
    """
    first = sink.persist(_record(decision_id=decision_id))
    second = sink.persist(_record(decision_id=decision_id))

    with engine.connect() as fresh:
        rows = load_decision_records(fresh, decision_id)
    assert len(rows) == 2
    assert first != second, "each chain row is distinct"
    assert rows[0][2] == rows[1][2], "identical content is stored once"


# == failure modes =============================================================


def test_a_broken_object_store_refuses_rather_than_returning_a_reference(
    engine: Engine,
) -> None:
    class _Broken:
        def put(self, data: bytes) -> object:
            raise OSError("disk full")

        def get(self, ref: str) -> bytes:
            raise OSError("disk full")

        def exists(self, ref: str) -> bool:
            return False

    sink = DurableExecutionAuditSink(engine, _Broken(), environment=Environment.PAPER)

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
