"""IMP-0202 point-in-time storage.

Adds the point-in-time fields (Data_Model v0.6 section 24.2) to the evidence
tables:

- source_events: effective_at (nullable), available_at (backfilled from
  observed_at, then NOT NULL), superseded_at (nullable) + index on
  available_at (as-of replay access path).
- signal_assessments: effective_at (nullable), ingested_at (backfilled from
  created_at, then NOT NULL), superseded_at (nullable).

Downgrade removes exactly these additions, returning to the 0001 schema.

Revision ID: 0002_point_in_time_storage
Revises: 0001_core_schema
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_point_in_time_storage"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("source_events", sa.Column("effective_at", _TZ, nullable=True))
    op.add_column("source_events", sa.Column("available_at", _TZ, nullable=True))
    op.add_column("source_events", sa.Column("superseded_at", _TZ, nullable=True))
    op.execute("UPDATE source_events SET available_at = observed_at WHERE available_at IS NULL")
    op.alter_column("source_events", "available_at", nullable=False)
    op.create_index("ix_source_events_available_at", "source_events", ["available_at"])

    op.add_column("signal_assessments", sa.Column("effective_at", _TZ, nullable=True))
    op.add_column("signal_assessments", sa.Column("ingested_at", _TZ, nullable=True))
    op.add_column("signal_assessments", sa.Column("superseded_at", _TZ, nullable=True))
    op.execute(
        "UPDATE signal_assessments SET ingested_at = created_at WHERE ingested_at IS NULL"
    )
    op.alter_column("signal_assessments", "ingested_at", nullable=False)


def downgrade() -> None:
    op.drop_column("signal_assessments", "superseded_at")
    op.drop_column("signal_assessments", "ingested_at")
    op.drop_column("signal_assessments", "effective_at")

    op.drop_index("ix_source_events_available_at", table_name="source_events")
    op.drop_column("source_events", "superseded_at")
    op.drop_column("source_events", "available_at")
    op.drop_column("source_events", "effective_at")
