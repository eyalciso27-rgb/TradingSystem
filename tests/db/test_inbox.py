"""Consumer inbox tests (IMP-0204).

Done criterion: duplicate deliveries do not duplicate state/orders.
PostgreSQL-only (runs in CI where DATABASE_URL is set).
"""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Connection, delete, func, insert, select
from trading_persistence import models as m
from trading_persistence.inbox import process_once

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set; inbox tests run in CI only.", allow_module_level=True)

NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)


@pytest.fixture()
def clean_db(engine: Any) -> Any:
    def wipe() -> None:
        with engine.begin() as conn:
            conn.execute(delete(m.ConsumerInboxRecord))
            conn.execute(delete(m.Workspace))

    wipe()
    yield engine
    wipe()


def _handler(conn: Connection) -> str:
    """Business effect: create one workspace row."""
    conn.execute(
        insert(m.Workspace).values(id=uuid4(), name="effect", created_at=NOW, updated_at=NOW)
    )
    return "sha256:" + "0" * 64


def _workspace_count(engine: Any) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(m.Workspace)).scalar_one())


def test_duplicate_delivery_is_noop(clean_db: Any) -> None:
    engine = clean_db
    message_id = uuid4()
    with engine.begin() as conn:
        assert process_once(
            conn, consumer_id="core-worker", message_id=message_id, handler=_handler, now=NOW
        )
    # At-least-once redelivery of the SAME message id: handler must not re-run.
    with engine.begin() as conn:
        assert not process_once(
            conn, consumer_id="core-worker", message_id=message_id, handler=_handler, now=NOW
        )
    assert _workspace_count(engine) == 1  # state was not duplicated
    with engine.connect() as conn:
        row = conn.execute(
            select(m.ConsumerInboxRecord.status, m.ConsumerInboxRecord.result_hash)
        ).one()
        assert row.status == "COMPLETED"
        assert row.result_hash is not None


def test_crash_mid_handler_allows_clean_retry(clean_db: Any) -> None:
    engine = clean_db
    message_id = uuid4()

    def crashing(conn: Connection) -> str:
        _handler(conn)
        raise RuntimeError("crash before commit")

    with pytest.raises(RuntimeError), engine.begin() as conn:
        process_once(
            conn, consumer_id="core-worker", message_id=message_id, handler=crashing, now=NOW
        )
    # Rollback removed both the inbox claim and the business effect.
    assert _workspace_count(engine) == 0
    with engine.connect() as conn:
        assert (
            conn.execute(select(func.count()).select_from(m.ConsumerInboxRecord)).scalar_one()
            == 0
        )
    # Retry after the crash processes cleanly, exactly once.
    with engine.begin() as conn:
        assert process_once(
            conn, consumer_id="core-worker", message_id=message_id, handler=_handler, now=NOW
        )
    assert _workspace_count(engine) == 1


def test_distinct_consumers_process_same_message_independently(clean_db: Any) -> None:
    engine = clean_db
    message_id = uuid4()
    for consumer in ("core-worker", "execution-agent"):
        with engine.begin() as conn:
            assert process_once(
                conn, consumer_id=consumer, message_id=message_id, handler=_handler, now=NOW
            )
    assert _workspace_count(engine) == 2  # one effect per consumer boundary
