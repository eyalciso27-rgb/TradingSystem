# ruff: noqa: E501
"""IMP-0204 consumer inbox.

Adds consumer_inbox (Data_Model v0.6 section 24.3): idempotent processing at
every service boundary — UNIQUE(consumer_id, message_id) makes duplicate
deliveries no-ops. Frozen explicit DDL per the binding migration policy.

Revision ID: 0004_consumer_inbox
Revises: 0003_transactional_outbox
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_consumer_inbox"
down_revision: str | None = "0003_transactional_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE consumer_inbox (
    id UUID NOT NULL,
    consumer_id TEXT NOT NULL,
    message_id UUID NOT NULL,
    status TEXT NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    result_hash TEXT,
    CONSTRAINT pk_consumer_inbox PRIMARY KEY (id),
    CONSTRAINT uq_consumer_inbox_consumer_id_message_id UNIQUE (consumer_id, message_id)
)"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE consumer_inbox")
