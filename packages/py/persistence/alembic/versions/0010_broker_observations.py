# ruff: noqa: E501
"""Autonomous-paper-runtime Phase A: durable broker-observation ledger.

Adds broker_observations (Integration_Specification_v0.6 sections 9.4/9.5:
broker events are append-only and processed idempotently; the durable record
of what the broker reported feeds reconciliation). It is the runtime consumer's
durable landing zone for raw broker order-status/fill/position callbacks, kept
deliberately decoupled from the FK-heavy normalized broker_orders/fills/
positions graph so the runtime can land observations without first fabricating
the domain graph (the projector that folds these into the normalized tables is
the runtime's Phase B). Idempotency is structural: (environment, dedup_key) is
UNIQUE and the repository appends with ON CONFLICT DO NOTHING, so a redelivered
callback or a duplicate execution id can never create two economic events.

Broker-neutral by construction: identities are external_order_ref /
external_execution_ref / instrument_ref (the adapter maps its own order id,
execution id and contract id onto these neutral refs). Frozen explicit DDL per
the binding migration policy.

Revision ID: 0010_broker_observations
Revises: 0009_corporate_actions
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_broker_observations"
down_revision: str | None = "0009_corporate_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE broker_observations (
    id UUID NOT NULL,
    environment TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    observation_kind TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    external_order_ref TEXT,
    external_execution_ref TEXT,
    instrument_ref TEXT,
    correlation_ref TEXT,
    symbol TEXT,
    side TEXT,
    order_state TEXT,
    filled_quantity NUMERIC(28, 10),
    remaining_quantity NUMERIC(28, 10),
    last_fill_quantity NUMERIC(28, 10),
    price NUMERIC(20, 8),
    commission NUMERIC(20, 8),
    currency CHAR(3),
    position_quantity NUMERIC(28, 10),
    average_price NUMERIC(20, 8),
    raw_payload_ref TEXT,
    observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_broker_observations PRIMARY KEY (id),
    CONSTRAINT ck_broker_observations_broker_observations_environment CHECK (environment IN ('RESEARCH', 'PAPER', 'LIVE')),
    CONSTRAINT ck_broker_observations_broker_observations_kind CHECK (observation_kind IN ('ORDER_STATUS', 'FILL', 'POSITION')),
    CONSTRAINT ck_broker_observations_broker_observations_side CHECK (side IS NULL OR side IN ('BUY', 'SELL')),
    CONSTRAINT ck_broker_observations_broker_observations_order_state CHECK (order_state IS NULL OR order_state IN ('CREATED', 'VALIDATED', 'QUEUED', 'SUBMITTING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED', 'CANCEL_PENDING', 'CANCELLED', 'CANCELLED_BY_POLICY', 'REJECTED', 'EXPIRED', 'UNKNOWN')),
    CONSTRAINT uq_broker_observations_environment_dedup_key UNIQUE (environment, dedup_key)
)"""
    )
    op.execute(
        "CREATE INDEX ix_broker_observations_external_order_ref ON broker_observations (external_order_ref)"
    )
    op.execute(
        "CREATE INDEX ix_broker_observations_observation_kind_observed_at ON broker_observations (observation_kind, observed_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE broker_observations")
