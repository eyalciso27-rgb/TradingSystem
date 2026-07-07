"""Durable notification-event persistence (IMP-0405).

Authority: Data_Model_and_Retention_Specification_v0.6 section 18
(notification_events) and Technology spec section 32.3. ``dedup_key`` is
UNIQUE, so ``record_notification`` is idempotent: a duplicate alert claims no
new row and returns the existing event id instead of sending twice.

Outbound records only — nothing here parses or executes a command. The pure
severity/dedup/escalation service lives in ``trading_application.notifications``;
this is the infrastructure half and does not import the application layer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Connection
from trading_contracts.enums import Environment, NotificationChannel, NotificationSeverity

from trading_persistence.models import NotificationEvent


def record_notification(
    connection: Connection,
    *,
    severity: NotificationSeverity,
    channel: NotificationChannel,
    dedup_key: str,
    environment: Environment,
    title: str,
    body: str,
    created_at: datetime,
    correlation_id: UUID | None = None,
    delivery_status: str = "PENDING",
) -> tuple[UUID, bool]:
    """Insert a notification idempotently by ``dedup_key``.

    Returns ``(event_id, created)``. ``created`` is False for a duplicate
    (the existing row's id is returned; no second notification is emitted).
    Must run inside the caller's open transaction.
    """
    if created_at.tzinfo is None:
        raise ValueError("created_at must be timezone-aware")
    if severity is NotificationSeverity.CRITICAL and correlation_id is None:
        raise ValueError("critical notifications require a correlation_id")
    event_id = uuid4()
    # RETURNING decides the claim: PostgreSQL returns the id iff a row was
    # actually inserted (driver rowcount is unreliable for ON CONFLICT).
    claim = connection.execute(
        pg_insert(NotificationEvent)
        .values(
            id=event_id,
            severity=severity.value,
            channel=channel.value,
            dedup_key=dedup_key,
            delivery_status=delivery_status,
            environment=environment.value,
            title=title,
            body=body,
            correlation_id=correlation_id,
            acknowledged=False,
            escalated=False,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["dedup_key"])
        .returning(NotificationEvent.id)
    )
    if claim.scalar_one_or_none() is not None:
        return event_id, True
    existing = connection.execute(
        NotificationEvent.__table__.select()
        .with_only_columns(NotificationEvent.id)
        .where(NotificationEvent.dedup_key == dedup_key)
    ).scalar_one()
    return existing, False


def mark_delivered(
    connection: Connection, *, event_id: UUID, delivered_at: datetime
) -> None:
    connection.execute(
        update(NotificationEvent)
        .where(NotificationEvent.id == event_id)
        .values(delivery_status="DELIVERED", delivered_at=delivered_at)
    )


def mark_failed(connection: Connection, *, event_id: UUID) -> None:
    connection.execute(
        update(NotificationEvent)
        .where(NotificationEvent.id == event_id)
        .values(delivery_status="FAILED")
    )


def acknowledge(
    connection: Connection, *, event_id: UUID, actor_ref: str, acknowledged_at: datetime
) -> None:
    connection.execute(
        update(NotificationEvent)
        .where(NotificationEvent.id == event_id)
        .values(acknowledged=True, acknowledged_at=acknowledged_at, acknowledged_by=actor_ref)
    )


def mark_escalated(
    connection: Connection, *, event_id: UUID, escalated_at: datetime
) -> None:
    connection.execute(
        update(NotificationEvent)
        .where(NotificationEvent.id == event_id)
        .values(escalated=True, escalated_at=escalated_at)
    )
