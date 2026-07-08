# ruff: noqa: E501
"""IMP-0705 corporate actions.

Adds corporate_actions (Data_Model v0.6 section 24.1: type, effective_at,
source, status + point-in-time available_at and an exact integer-fraction
ratio). Frozen explicit DDL per the binding migration policy.

Revision ID: 0009_corporate_actions
Revises: 0008_instrument_master
Create Date: 2026-07-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_corporate_actions"
down_revision: str | None = "0008_instrument_master"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE corporate_actions (
    id UUID NOT NULL,
    instrument_id UUID NOT NULL,
    action_type TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    announced_at TIMESTAMP WITH TIME ZONE,
    effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE NOT NULL,
    ratio_numerator BIGINT,
    ratio_denominator BIGINT,
    new_symbol TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_corporate_actions PRIMARY KEY (id),
    CONSTRAINT fk_corporate_actions_instrument_id_instruments FOREIGN KEY (instrument_id) REFERENCES instruments (id),
    CONSTRAINT ck_corporate_actions_corporate_actions_action_type CHECK (action_type IN ('SPLIT', 'REVERSE_SPLIT', 'SYMBOL_CHANGE', 'DELISTING', 'MERGER')),
    CONSTRAINT ck_corporate_actions_corporate_actions_status CHECK (status IN ('OBSERVED', 'VALIDATED', 'CONFLICT', 'RESOLVED')),
    CONSTRAINT ck_corporate_actions_corporate_actions_ratio_pair CHECK ((ratio_numerator IS NULL) = (ratio_denominator IS NULL))
)"""
    )
    op.execute(
        "CREATE INDEX ix_corporate_actions_instrument_id_available_at ON corporate_actions (instrument_id, available_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE corporate_actions")
