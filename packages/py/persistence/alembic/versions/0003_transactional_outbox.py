# ruff: noqa: E501
"""IMP-0203 transactional outbox.

Adds outbox_messages (Data_Model v0.6 section 24.3): domain state and the
outbox row are written in one transaction; a publisher later marks rows
PUBLISHED. Frozen explicit DDL per the binding migration policy.

Revision ID: 0003_transactional_outbox
Revises: 0002_point_in_time_storage
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_transactional_outbox"
down_revision: str | None = "0002_point_in_time_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE outbox_messages (
    id UUID NOT NULL,
    aggregate_id UUID NOT NULL,
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_outbox_messages PRIMARY KEY (id),
    CONSTRAINT ck_outbox_messages_outbox_messages_status CHECK (status IN ('PENDING', 'PUBLISHED')),
    CONSTRAINT ck_outbox_messages_outbox_messages_attempts_nonneg CHECK (attempts >= 0),
    CONSTRAINT ck_outbox_messages_outbox_messages_published_consistency CHECK ((status = 'PUBLISHED') = (published_at IS NOT NULL))
)"""
    )
    op.execute(
        "CREATE INDEX ix_outbox_messages_status_created_at "
        "ON outbox_messages (status, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE outbox_messages")
