"""Transactional outbox tests (IMP-0203).

Done criterion: a crash after commit produces exactly one logical event.
PostgreSQL-only (runs in CI where DATABASE_URL is set).
"""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select
from trading_persistence import models as m
from trading_persistence.outbox import enqueue, fetch_pending, mark_published

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; outbox tests run in CI only.", allow_module_level=True)

NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)


@pytest.fixture()
def clean_outbox(engine: Any) -> Any:
    with engine.begin() as conn:
        conn.execute(delete(m.OutboxMessage))
        conn.execute(delete(m.Workspace))
    yield engine
    with engine.begin() as conn:
        conn.execute(delete(m.OutboxMessage))
        conn.execute(delete(m.Workspace))


def _write_domain_and_event(conn: Any, message_id: Any, aggregate_id: Any) -> None:
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


def test_crash_after_commit_yields_exactly_one_event(clean_outbox: Any) -> None:
    engine = clean_outbox
    message_id, aggregate_id = uuid4(), uuid4()
    with engine.begin() as conn:  # commit, then "crash" (process gone)
        _write_domain_and_event(conn, message_id, aggregate_id)
    # Publisher restart after the crash: exactly one pending logical event.
    with engine.connect() as conn:
        pending = fetch_pending(conn)
        assert [row.id for row in pending] == [message_id]
        count = conn.execute(select(func.count()).select_from(m.OutboxMessage)).scalar_one()
        assert count == 1


def test_rollback_writes_neither_domain_state_nor_event(clean_outbox: Any) -> None:
    engine = clean_outbox
    aggregate_id = uuid4()
    with pytest.raises(RuntimeError), engine.begin() as conn:
        _write_domain_and_event(conn, uuid4(), aggregate_id)
        raise RuntimeError("crash before commit")
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(m.OutboxMessage)).scalar_one() == 0
        workspaces = conn.execute(
            select(func.count()).select_from(m.Workspace).where(m.Workspace.id == aggregate_id)
        ).scalar_one()
        assert workspaces == 0


def test_mark_published_is_idempotent(clean_outbox: Any) -> None:
    engine = clean_outbox
    message_id = uuid4()
    with engine.begin() as conn:
        _write_domain_and_event(conn, message_id, uuid4())
    with engine.begin() as conn:
        assert mark_published(conn, message_id, NOW) is True
    with engine.begin() as conn:
        assert mark_published(conn, message_id, NOW) is False  # publisher retry: no-op
    with engine.connect() as conn:
        assert fetch_pending(conn) == []
        row = conn.execute(
            select(m.OutboxMessage.status, m.OutboxMessage.attempts, m.OutboxMessage.published_at)
        ).one()
        assert row.status == "PUBLISHED"
        assert row.attempts == 1
        assert row.published_at is not None and row.published_at.tzinfo is not None


def test_duplicate_message_id_rejected(clean_outbox: Any) -> None:
    from sqlalchemy.exc import IntegrityError

    engine = clean_outbox
    message_id = uuid4()
    with engine.begin() as conn:
        _write_domain_and_event(conn, message_id, uuid4())
    with pytest.raises(IntegrityError), engine.begin() as conn:
        enqueue(
            conn,
            message_id=message_id,  # same message id twice
            aggregate_id=uuid4(),
            type="WORKSPACE_CREATED",
            payload={},
            created_at=NOW,
        )
