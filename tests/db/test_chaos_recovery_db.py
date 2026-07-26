"""IMP-1303 — DB-backed chaos and recovery drills (PostgreSQL-gated).

The whole module is skipped unless ``DATABASE_URL`` points at a PostgreSQL
server (set by CI's postgres service); no production database is contacted.
These drills prove the recovery invariants for the fault domains that need a
real transactional store. The guarantees proven here are stated precisely:

- PostgreSQL outage / restart — the transactional **outbox** makes domain
  state and its event atomic: a failure before commit leaves NEITHER; a
  crash after commit leaves exactly one PENDING row. External delivery is
  **at-least-once, not exactly-once**: a publisher that crashes after
  publishing to the bus but before ``mark_published`` re-publishes the same
  message on restart. The **message identity is stable** across that
  redelivery, and end-to-end duplicate suppression is the consumer's job.

- Consumer-side idempotency — the canonical Consumer Inbox
  (``trading_persistence.inbox.process_once``, IMP-0204) runs a handler
  exactly once per (consumer_id, message_id): a redelivered message is a
  no-op, and a crash mid-handler rolls back the claim so a retry processes
  cleanly. This is what actually suppresses at-least-once duplicates.

- Content-addressed object dedup — ``RawObject.content_hash`` is a unique
  content address: storing the same bytes twice collapses to one object.
  This is object-store dedup, **not** a consumer inbox and **not** NATS
  redelivery handling.

**Remaining gap (out of IMP-1303 scope):** there is no NATS JetStream
adapter yet (``trading_event_bus`` is a skeleton), so live broker/bus
redelivery is not exercised end to end here. The persistence-layer
idempotency contract it will rely on (the Consumer Inbox) is proven; wiring
a real bus to it belongs to the task that implements the adapter.

No broker execution surface is touched. POI-IBKR-001 stays BLOCKED / NOT
RUN; nothing here changes any PoI status.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from trading_persistence import models as m
from trading_persistence.inbox import process_once
from trading_persistence.outbox import enqueue, fetch_pending, mark_published

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; DB chaos drills run in CI only.", allow_module_level=True)

NOW = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)


@pytest.fixture()
def clean_tables(engine: Any) -> Any:
    def _wipe(conn: Any) -> None:
        conn.execute(delete(m.OutboxMessage))
        conn.execute(delete(m.Workspace))
        conn.execute(delete(m.RawObject))
        conn.execute(delete(m.ConsumerInboxRecord))

    with engine.begin() as conn:
        _wipe(conn)
    yield engine
    with engine.begin() as conn:
        _wipe(conn)


def _write_domain_and_outbox(conn: Any, aggregate_id: Any, message_id: Any) -> None:
    conn.execute(
        insert(m.Workspace).values(id=aggregate_id, name="ws", created_at=NOW, updated_at=NOW)
    )
    enqueue(
        conn,
        message_id=message_id,
        aggregate_id=aggregate_id,
        type="WORKSPACE_CREATED",
        payload={"name": "ws"},
        created_at=NOW,
    )


# -- PostgreSQL outage / restart: outbox atomicity --------------------------


def test_db_failure_before_commit_leaves_no_partial_state(clean_tables: Any) -> None:
    """A crash mid-transaction must not leave a domain row without its event,
    nor an event without its domain row — the outbox's atomicity guarantee."""
    engine = clean_tables
    aggregate_id, message_id = uuid4(), uuid4()

    class SimulatedDbOutage(RuntimeError):
        pass

    with pytest.raises(SimulatedDbOutage):
        with engine.begin() as conn:  # rolls back on the exception below
            _write_domain_and_outbox(conn, aggregate_id, message_id)
            raise SimulatedDbOutage("connection dropped before COMMIT")

    with engine.connect() as conn:
        workspaces = conn.execute(select(func.count()).select_from(m.Workspace)).scalar_one()
        outbox = conn.execute(select(func.count()).select_from(m.OutboxMessage)).scalar_one()
    assert workspaces == 0  # domain row rolled back
    assert outbox == 0  # outbox row rolled back with it


# -- PostgreSQL outage / restart: at-least-once delivery, stable identity ----


def test_publish_before_mark_crash_redelivers_with_stable_identity(clean_tables: Any) -> None:
    """Delivery is AT-LEAST-ONCE, not exactly-once.

    A publisher that crashes after publishing to the bus but before
    ``mark_published`` will publish the SAME message again on restart. This
    drill makes that concrete: the in-memory bus receives the message twice,
    and its identity (the outbox row id) is IDENTICAL across the redelivery.
    The outbox does not, and cannot, promise exactly-once external delivery;
    duplicate suppression is the consumer's responsibility (see the inbox
    drill below).
    """
    engine = clean_tables
    aggregate_id, message_id = uuid4(), uuid4()
    with engine.begin() as conn:
        _write_domain_and_outbox(conn, aggregate_id, message_id)

    bus: list[Any] = []  # the "event bus" the publisher writes to

    def publish_cycle(*, crash_before_mark: bool) -> None:
        # A publisher iteration: fetch PENDING, publish to the bus, then mark.
        with engine.begin() as conn:
            pending = fetch_pending(conn)
            for row in pending:
                bus.append(row.id)  # published to the bus
                if crash_before_mark:
                    # Process dies here: the mark never happens, but the
                    # publish already reached the bus. begin() commits with no
                    # status change, so the row stays PENDING.
                    continue
                mark_published(conn, row.id, NOW)

    publish_cycle(crash_before_mark=True)  # first attempt: crash after publish
    # The row is still PENDING because it was never marked.
    with engine.connect() as conn:
        assert [row.id for row in fetch_pending(conn)] == [message_id]

    publish_cycle(crash_before_mark=False)  # restart: publishes AGAIN, then marks

    # The bus saw the SAME message twice — redelivery — with a stable id.
    assert bus == [message_id, message_id]
    assert len(set(bus)) == 1  # identity is stable across redelivery
    # After a successful mark there is nothing left to publish.
    with engine.connect() as conn:
        assert fetch_pending(conn) == []
        published = conn.execute(
            select(func.count())
            .select_from(m.OutboxMessage)
            .where(m.OutboxMessage.status == "PUBLISHED")
        ).scalar_one()
    assert published == 1  # exactly one outbox ROW transitioned to PUBLISHED


def test_mark_published_transitions_the_outbox_row_only_once(clean_tables: Any) -> None:
    """DB-row idempotency (not external-delivery idempotency): only a PENDING
    row transitions, so a re-mark is a no-op and no second row appears."""
    engine = clean_tables
    aggregate_id, message_id = uuid4(), uuid4()
    with engine.begin() as conn:
        _write_domain_and_outbox(conn, aggregate_id, message_id)

    with engine.begin() as conn:
        assert mark_published(conn, message_id, NOW) is True  # PENDING -> PUBLISHED
    with engine.begin() as conn:
        assert mark_published(conn, message_id, NOW) is False  # already PUBLISHED: no-op

    with engine.connect() as conn:
        rows = conn.execute(select(func.count()).select_from(m.OutboxMessage)).scalar_one()
    assert rows == 1  # the row is never duplicated by a re-mark


# -- Consumer-side idempotency: the canonical Consumer Inbox (IMP-0204) ------


def test_consumer_inbox_runs_a_handler_once_under_redelivery(clean_tables: Any) -> None:
    """At-least-once redelivery is suppressed consumer-side: the same
    (consumer_id, message_id) runs the handler exactly once; the redelivery
    is a no-op. This — not the outbox — is what makes processing idempotent."""
    engine = clean_tables
    message_id = uuid4()
    runs = 0

    def handler(_conn: Any) -> str | None:
        nonlocal runs
        runs += 1
        return "result-hash"

    with engine.begin() as conn:
        first = process_once(
            conn, consumer_id="c1", message_id=message_id, handler=handler, now=NOW
        )
    with engine.begin() as conn:
        second = process_once(  # at-least-once redelivery of the SAME message
            conn, consumer_id="c1", message_id=message_id, handler=handler, now=NOW
        )

    assert first is True  # ran now
    assert second is False  # duplicate delivery: no-op
    assert runs == 1  # the business effect happened exactly once

    with engine.connect() as conn:
        completed = conn.execute(
            select(m.ConsumerInboxRecord.status).where(
                m.ConsumerInboxRecord.consumer_id == "c1",
                m.ConsumerInboxRecord.message_id == message_id,
            )
        ).scalar_one()
    assert completed == "COMPLETED"


def test_consumer_inbox_crash_mid_handler_allows_a_clean_retry(clean_tables: Any) -> None:
    """A crash mid-handler rolls back the claim with the business effect, so a
    retry (same message_id) processes cleanly rather than being a lost no-op."""
    engine = clean_tables
    message_id = uuid4()

    class HandlerCrash(RuntimeError):
        pass

    def crashing_handler(_conn: Any) -> str | None:
        raise HandlerCrash("mid-handler")

    with pytest.raises(HandlerCrash):
        with engine.begin() as conn:
            process_once(
                conn,
                consumer_id="c1",
                message_id=message_id,
                handler=crashing_handler,
                now=NOW,
            )

    # The claim rolled back with the handler: no record survives the crash.
    with engine.connect() as conn:
        rows = conn.execute(
            select(func.count())
            .select_from(m.ConsumerInboxRecord)
            .where(m.ConsumerInboxRecord.message_id == message_id)
        ).scalar_one()
    assert rows == 0

    # The retry now processes cleanly (the redelivery was not silently lost).
    runs = 0

    def handler(_conn: Any) -> str | None:
        nonlocal runs
        runs += 1
        return None

    with engine.begin() as conn:
        ran = process_once(conn, consumer_id="c1", message_id=message_id, handler=handler, now=NOW)
    assert ran is True
    assert runs == 1


# -- Content-addressed object dedup (NOT a consumer inbox) -------------------


def _raw_object_values(content_hash: str) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "uri": "s3://raw/obj",
        "mime_type": "application/json",
        "size_bytes": 3,
        "content_hash": content_hash,
        "created_at": NOW,
    }


def test_raw_object_content_hash_dedupes_identical_bytes(clean_tables: Any) -> None:
    """``RawObject.content_hash`` is a unique content address: the same bytes
    stored twice collapse to one object. This proves content-addressed object
    deduplication only — it is NOT a consumer inbox and does NOT prove NATS
    redelivery handling (that is the inbox drill above)."""
    engine = clean_tables
    content_hash = "sha256:" + "d" * 64

    with engine.begin() as conn:
        conn.execute(insert(m.RawObject).values(**_raw_object_values(content_hash)))

    # Re-storing identical content violates the unique content address.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(m.RawObject).values(**_raw_object_values(content_hash)))

    with engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(m.RawObject)
            .where(m.RawObject.content_hash == content_hash)
        ).scalar_one()
    assert count == 1  # one content-addressed object, regardless of re-store attempts
