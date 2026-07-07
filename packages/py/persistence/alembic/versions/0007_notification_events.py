# ruff: noqa: E501
"""IMP-0405 notification events.

Adds notification_events (Data_Model v0.6 section 18: severity, channel,
delivery status, dedup key and ack; Technology spec section 32.3: critical
alerts require dedup, correlation_id and acknowledgement). dedup_key is UNIQUE
for idempotent delivery. Frozen explicit DDL per the binding migration policy.

Revision ID: 0007_notification_events
Revises: 0006_emergency_commands
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_notification_events"
down_revision: str | None = "0006_emergency_commands"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE notification_events (
    id UUID NOT NULL,
    severity TEXT NOT NULL,
    channel TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    delivery_status TEXT NOT NULL,
    environment TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    correlation_id UUID,
    acknowledged BOOLEAN NOT NULL,
    acknowledged_at TIMESTAMP WITH TIME ZONE,
    acknowledged_by TEXT,
    escalated BOOLEAN NOT NULL,
    escalated_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    delivered_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_notification_events PRIMARY KEY (id),
    CONSTRAINT ck_notification_events_notification_events_severity CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
    CONSTRAINT ck_notification_events_notification_events_channel CHECK (channel IN ('UI', 'TELEGRAM')),
    CONSTRAINT ck_notification_events_notification_events_delivery_status CHECK (delivery_status IN ('PENDING', 'DELIVERED', 'FAILED')),
    CONSTRAINT ck_notification_events_notification_events_environment CHECK (environment IN ('RESEARCH', 'PAPER', 'LIVE')),
    CONSTRAINT uq_notification_events_dedup_key UNIQUE (dedup_key)
)"""
    )
    op.execute(
        "CREATE INDEX ix_notification_events_severity_created_at ON notification_events (severity, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE notification_events")
