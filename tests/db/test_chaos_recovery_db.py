"""IMP-1303 — DB-backed chaos and recovery drills (PostgreSQL-gated).

The whole module is skipped unless ``DATABASE_URL`` points at a PostgreSQL
server (set by CI's postgres service); no production database is contacted.
These drills prove the recovery invariants for the fault domains that need a
real transactional store:

- PostgreSQL outage / restart — atomicity: a transaction that fails before
  commit leaves NEITHER the domain row NOR its outbox row (no partial
  state); and a publisher restart republishes idempotently (exactly one
  logical event, never a double publish).
- NATS outage / at-least-once redelivery / replay — the consumer-inbox
  identity (a unique content hash) makes replay idempotent: a redelivered
  raw object yields exactly one logical row, not a duplicate.

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

    with engine.begin() as conn:
        _wipe(conn)
    yield engine
    with engine.begin() as conn:
        _wipe(conn)


# -- PostgreSQL outage / restart: atomicity ---------------------------------


def test_db_failure_before_commit_leaves_no_partial_state(clean_tables: Any) -> None:
    """A crash mid-transaction must not leave a domain row without its event,
    nor an event without its domain row — the outbox's whole point."""
    engine = clean_tables
    aggregate_id, message_id = uuid4(), uuid4()

    class SimulatedDbOutage(RuntimeError):
        pass

    with pytest.raises(SimulatedDbOutage):
        with engine.begin() as conn:  # rolls back on the exception below
            conn.execute(
                insert(m.Workspace).values(
                    id=aggregate_id, name="ws", created_at=NOW, updated_at=NOW
                )
            )
            enqueue(
                conn,
                message_id=message_id,
                aggregate_id=aggregate_id,
                type="WORKSPACE_CREATED",
                payload={"name": "ws"},
                created_at=NOW,
            )
            raise SimulatedDbOutage("connection dropped before COMMIT")

    with engine.connect() as conn:
        workspaces = conn.execute(select(func.count()).select_from(m.Workspace)).scalar_one()
        outbox = conn.execute(select(func.count()).select_from(m.OutboxMessage)).scalar_one()
    assert workspaces == 0  # domain row rolled back
    assert outbox == 0  # outbox row rolled back with it


def test_publisher_restart_republishes_idempotently_exactly_once(clean_tables: Any) -> None:
    """After a publisher crash between publish and mark, a restart marks the
    row once; a redundant mark is a no-op — never a double publish."""
    engine = clean_tables
    aggregate_id, message_id = uuid4(), uuid4()
    with engine.begin() as conn:
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

    # Publisher restart: exactly one pending row is visible.
    with engine.connect() as conn:
        assert [row.id for row in fetch_pending(conn)] == [message_id]

    with engine.begin() as conn:
        assert mark_published(conn, message_id, NOW) is True  # first publish
    with engine.begin() as conn:
        assert mark_published(conn, message_id, NOW) is False  # idempotent no-op

    with engine.connect() as conn:
        published = conn.execute(
            select(func.count())
            .select_from(m.OutboxMessage)
            .where(m.OutboxMessage.status == "PUBLISHED")
        ).scalar_one()
        pending = fetch_pending(conn)
    assert published == 1  # exactly one logical event
    assert pending == []  # nothing left to republish


# -- NATS at-least-once redelivery / replay: consumer-inbox dedup -----------


def _raw_object_values(content_hash: str) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "uri": "s3://raw/obj",
        "mime_type": "application/json",
        "size_bytes": 3,
        "content_hash": content_hash,
        "created_at": NOW,
    }


def test_redelivered_event_is_deduplicated_to_one_logical_row(clean_tables: Any) -> None:
    """At-least-once delivery (NATS redelivery, or a replay) must not create a
    duplicate: the consumer-inbox identity rejects the second copy."""
    engine = clean_tables
    content_hash = "sha256:" + "d" * 64

    with engine.begin() as conn:
        conn.execute(insert(m.RawObject).values(**_raw_object_values(content_hash)))

    # The exact same logical object is redelivered — the unique identity
    # constraint fails the duplicate insert; the consumer treats it as
    # already-processed rather than storing a second copy.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(insert(m.RawObject).values(**_raw_object_values(content_hash)))

    with engine.connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(m.RawObject)
            .where(m.RawObject.content_hash == content_hash)
        ).scalar_one()
    assert count == 1  # exactly one logical row after redelivery
