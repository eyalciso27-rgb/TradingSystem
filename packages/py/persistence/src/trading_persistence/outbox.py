"""Transactional outbox operations (IMP-0203).

Flow (Technology_and_Deployment_Decisions_v0.6 section 6.1):

1. persist domain state; 2. persist outbox row (status=PENDING) — in the SAME
transaction; 3. commit. A separate publisher later publishes to the event bus
with the row id as the message id and marks the row PUBLISHED.

``enqueue`` never commits: it participates in the caller's transaction, which
is what makes domain state + event atomic. A crash after commit leaves exactly
one PENDING row (exactly one logical event); a rollback leaves neither the
domain state nor the event. ``mark_published`` is idempotent: only a PENDING
row transitions, so a publisher retry can never create a second outbox row.

Delivery semantics are **at-least-once**, never exactly-once: a crash after
publishing to the bus but before ``mark_published`` re-publishes the same
message on restart. The message id stays constant across retries, so
consumers deduplicate; end-to-end duplicate suppression is completed by the
Consumer Inbox (IMP-0204). The event bus itself (NATS) is out of scope here.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, Row, insert, select, update

from trading_persistence.models import OutboxMessage

TYPE_PATTERN_NOTE = "type follows the canonical event type format (IMP-0102)"


def enqueue(
    connection: Connection,
    *,
    message_id: UUID,
    aggregate_id: UUID,
    type: str,
    payload: Mapping[str, object],
    created_at: datetime,
) -> UUID:
    """Insert a PENDING outbox row inside the caller's open transaction."""
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    connection.execute(
        insert(OutboxMessage).values(
            id=message_id,
            aggregate_id=aggregate_id,
            type=type,
            payload=dict(payload),
            status="PENDING",
            attempts=0,
            created_at=created_at,
        )
    )
    return message_id


def fetch_pending(connection: Connection, limit: int = 100) -> Sequence[Row[tuple[UUID, str]]]:
    """Oldest-first PENDING rows for the publisher (id, type)."""
    stmt = (
        select(OutboxMessage.id, OutboxMessage.type)
        .where(OutboxMessage.status == "PENDING")
        .order_by(OutboxMessage.created_at)
        .limit(limit)
    )
    return connection.execute(stmt).all()


def mark_published(
    connection: Connection, message_id: UUID, published_at: datetime | None = None
) -> bool:
    """Mark one row PUBLISHED; idempotent (False if already published)."""
    moment = published_at or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("published_at must be timezone-aware")
    result = connection.execute(
        update(OutboxMessage)
        .where(OutboxMessage.id == message_id, OutboxMessage.status == "PENDING")
        .values(status="PUBLISHED", published_at=moment, attempts=OutboxMessage.attempts + 1)
    )
    return result.rowcount == 1
