# ruff: noqa: E501
"""IMP-0304 tamper-evident audit.

Adds audit_events (Data_Model v0.6 section 6.8: append-only) with a hash
chain (previous_hash/current_hash). Frozen explicit DDL per the binding
migration policy.

Revision ID: 0005_tamper_evident_audit
Revises: 0004_consumer_inbox
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_tamper_evident_audit"
down_revision: str | None = "0004_consumer_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE audit_events (
    id UUID NOT NULL,
    sequence_no BIGINT NOT NULL,
    actor_ref TEXT NOT NULL,
    action TEXT NOT NULL,
    target_ref TEXT,
    environment TEXT NOT NULL,
    detail TEXT,
    payload_ref TEXT,
    correlation_id UUID,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    previous_hash TEXT NOT NULL,
    current_hash TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_audit_events PRIMARY KEY (id),
    CONSTRAINT ck_audit_events_audit_events_environment CHECK (environment IN ('RESEARCH', 'PAPER', 'LIVE')),
    CONSTRAINT uq_audit_events_sequence_no UNIQUE (sequence_no),
    CONSTRAINT uq_audit_events_current_hash UNIQUE (current_hash)
)"""
    )
    op.execute("CREATE INDEX ix_audit_events_occurred_at ON audit_events (occurred_at)")


def downgrade() -> None:
    op.execute("DROP TABLE audit_events")
