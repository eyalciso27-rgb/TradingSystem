# ruff: noqa: E501
"""IMP-0701 instrument master mappings.

Adds the time-aware instrument-master tables (Data_Model v0.6 section 24.1):
instrument_symbols, provider_instrument_mappings, broker_instrument_mappings.
The identity table ``instruments`` and ``instrument_aliases`` already exist
(0001). Frozen explicit DDL per the binding migration policy.

Revision ID: 0008_instrument_master
Revises: 0007_notification_events
Create Date: 2026-07-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_instrument_master"
down_revision: str | None = "0007_notification_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE instrument_symbols (
    id UUID NOT NULL,
    instrument_id UUID NOT NULL,
    symbol TEXT NOT NULL,
    exchange TEXT,
    valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
    valid_to TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_instrument_symbols PRIMARY KEY (id),
    CONSTRAINT fk_instrument_symbols_instrument_id_instruments FOREIGN KEY (instrument_id) REFERENCES instruments (id)
)"""
    )
    op.execute("CREATE INDEX ix_instrument_symbols_symbol ON instrument_symbols (symbol)")
    op.execute(
        "CREATE INDEX ix_instrument_symbols_instrument_id ON instrument_symbols (instrument_id)"
    )

    op.execute(
        """CREATE TABLE provider_instrument_mappings (
    id UUID NOT NULL,
    instrument_id UUID NOT NULL,
    provider_id TEXT NOT NULL,
    external_instrument_id TEXT NOT NULL,
    valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
    valid_to TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_provider_instrument_mappings PRIMARY KEY (id),
    CONSTRAINT fk_provider_instrument_mappings_instrument_id_instruments FOREIGN KEY (instrument_id) REFERENCES instruments (id)
)"""
    )
    op.execute(
        "CREATE INDEX ix_provider_instrument_mappings_provider_id_instrument_id ON provider_instrument_mappings (provider_id, instrument_id)"
    )

    op.execute(
        """CREATE TABLE broker_instrument_mappings (
    id UUID NOT NULL,
    instrument_id UUID NOT NULL,
    broker_id TEXT NOT NULL,
    external_instrument_id TEXT NOT NULL,
    capabilities TEXT[] NOT NULL,
    valid_from TIMESTAMP WITH TIME ZONE NOT NULL,
    valid_to TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_broker_instrument_mappings PRIMARY KEY (id),
    CONSTRAINT fk_broker_instrument_mappings_instrument_id_instruments FOREIGN KEY (instrument_id) REFERENCES instruments (id)
)"""
    )
    op.execute(
        "CREATE INDEX ix_broker_instrument_mappings_broker_id_instrument_id ON broker_instrument_mappings (broker_id, instrument_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE broker_instrument_mappings")
    op.execute("DROP TABLE provider_instrument_mappings")
    op.execute("DROP TABLE instrument_symbols")
