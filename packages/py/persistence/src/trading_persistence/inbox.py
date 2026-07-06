"""Consumer inbox — idempotent processing (IMP-0204).

Flow (Technology_and_Deployment_Decisions_v0.6 section 6.1): the consumer
checks processed_event(event_id, consumer), applies the business effect
idempotently, and persists the processed record — all in ONE transaction.

``process_once`` claims (consumer_id, message_id) with an
INSERT .. ON CONFLICT DO NOTHING inside the caller's open transaction:

- duplicate delivery: the claim fails -> the handler never runs -> no-op;
- crash mid-handler: the whole transaction rolls back, including the claim,
  so a retry (same message_id, at-least-once redelivery) processes cleanly;
- success: business effect + COMPLETED inbox row commit together, so an inbox
  record never exists without its business effect or vice versa.
"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from trading_persistence.models import ConsumerInboxRecord


def process_once(
    connection: Connection,
    *,
    consumer_id: str,
    message_id: UUID,
    handler: Callable[[Connection], str | None],
    now: datetime,
) -> bool:
    """Run ``handler`` exactly once per (consumer_id, message_id).

    Returns True if the handler ran now, False for a duplicate delivery
    (no-op). Must be called inside an open transaction; never commits.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    # RETURNING decides the claim: PostgreSQL returns the id iff a row was
    # actually inserted. Driver rowcount is NOT reliable for
    # INSERT .. ON CONFLICT DO NOTHING and must not be used here.
    claim = connection.execute(
        pg_insert(ConsumerInboxRecord)
        .values(
            id=uuid4(),
            consumer_id=consumer_id,
            message_id=message_id,
            status="PROCESSING",
            first_seen_at=now,
        )
        .on_conflict_do_nothing(index_elements=["consumer_id", "message_id"])
        .returning(ConsumerInboxRecord.id)
    )
    if claim.scalar_one_or_none() is None:
        return False  # already processed (or being processed): duplicate no-op

    result_hash = handler(connection)
    connection.execute(
        update(ConsumerInboxRecord)
        .where(
            ConsumerInboxRecord.consumer_id == consumer_id,
            ConsumerInboxRecord.message_id == message_id,
        )
        .values(status="COMPLETED", completed_at=now, result_hash=result_hash)
    )
    return True
