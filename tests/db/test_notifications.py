"""Durable notification-event persistence tests (IMP-0405).

Done criterion support: critical alert dedup + ack. PostgreSQL-only (runs in
CI where DATABASE_URL is set).
"""

import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from trading_contracts.enums import Environment, NotificationChannel, NotificationSeverity
from trading_persistence import models as m
from trading_persistence.notifications import (
    acknowledge,
    mark_delivered,
    mark_escalated,
    record_notification,
)

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL not set; notification tests run in CI only.", allow_module_level=True
    )

NOW = datetime(2026, 7, 7, 10, 0, tzinfo=UTC)


@pytest.fixture()
def clean(engine: Any) -> Any:
    with engine.begin() as conn:
        conn.execute(delete(m.NotificationEvent))
    yield engine
    with engine.begin() as conn:
        conn.execute(delete(m.NotificationEvent))


def test_record_is_idempotent_by_dedup_key(clean: Any) -> None:
    corr = uuid4()
    with clean.begin() as conn:
        first_id, created1 = record_notification(
            conn,
            severity=NotificationSeverity.CRITICAL,
            channel=NotificationChannel.UI,
            dedup_key="UI:evt-1",
            environment=Environment.PAPER,
            title="t",
            body="b",
            created_at=NOW,
            correlation_id=corr,
        )
        second_id, created2 = record_notification(
            conn,
            severity=NotificationSeverity.CRITICAL,
            channel=NotificationChannel.UI,
            dedup_key="UI:evt-1",
            environment=Environment.PAPER,
            title="t",
            body="b",
            created_at=NOW,
            correlation_id=corr,
        )
    assert created1 is True and created2 is False
    assert first_id == second_id
    with clean.connect() as conn:
        count = conn.execute(select(func.count()).select_from(m.NotificationEvent)).scalar_one()
    assert count == 1


def test_critical_without_correlation_is_rejected(clean: Any) -> None:
    with clean.begin() as conn:
        with pytest.raises(ValueError):
            record_notification(
                conn,
                severity=NotificationSeverity.CRITICAL,
                channel=NotificationChannel.UI,
                dedup_key="UI:evt-2",
                environment=Environment.PAPER,
                title="t",
                body="b",
                created_at=NOW,
            )


def test_delivered_and_ack_lifecycle(clean: Any) -> None:
    with clean.begin() as conn:
        event_id, _ = record_notification(
            conn,
            severity=NotificationSeverity.HIGH,
            channel=NotificationChannel.TELEGRAM,
            dedup_key="TELEGRAM:evt-3",
            environment=Environment.PAPER,
            title="t",
            body="b",
            created_at=NOW,
        )
        mark_delivered(conn, event_id=event_id, delivered_at=NOW)
        mark_escalated(conn, event_id=event_id, escalated_at=NOW)
        acknowledge(conn, event_id=event_id, actor_ref="op-1", acknowledged_at=NOW)
    with clean.connect() as conn:
        row = conn.execute(
            select(
                m.NotificationEvent.delivery_status,
                m.NotificationEvent.acknowledged,
                m.NotificationEvent.acknowledged_by,
                m.NotificationEvent.escalated,
            ).where(m.NotificationEvent.id == event_id)
        ).one()
    assert row.delivery_status == "DELIVERED"
    assert row.acknowledged is True and row.acknowledged_by == "op-1"
    assert row.escalated is True


def test_naive_created_at_is_rejected(clean: Any) -> None:
    with clean.begin() as conn:
        with pytest.raises(ValueError):
            record_notification(
                conn,
                severity=NotificationSeverity.LOW,
                channel=NotificationChannel.UI,
                dedup_key="UI:evt-4",
                environment=Environment.PAPER,
                title="t",
                body="b",
                created_at=datetime(2026, 7, 7, 10, 0),  # noqa: DTZ001 (intentional)
            )
