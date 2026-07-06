"""IMP-0201 core PostgreSQL schema.

The initial schema is created directly from the SQLAlchemy metadata, so the
migration and the models can never drift: upgrade issues CREATE TABLE for every
core table (with its constraints and indexes) in dependency order, and
downgrade drops them in reverse. `alembic check` after upgrade therefore
reports no differences.

Revision ID: 0001_core_schema
Revises:
Create Date: 2026-07-06
"""

from collections.abc import Sequence

from alembic import op
from trading_persistence.models import Base

revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
