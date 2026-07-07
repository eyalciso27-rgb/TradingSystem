# ruff: noqa: E501
"""IMP-0404 emergency control commands.

Adds emergency_commands (Data_Model v0.6 section 18: append-only command log
carrying command, scope, actor, auth level, ack and result; Security spec
section 12). Active-halt state is derived by replaying this append-only log.
Frozen explicit DDL per the binding migration policy.

Revision ID: 0006_emergency_commands
Revises: 0005_tamper_evident_audit
Create Date: 2026-07-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_emergency_commands"
down_revision: str | None = "0005_tamper_evident_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE emergency_commands (
    id UUID NOT NULL,
    command TEXT NOT NULL,
    halt_scope TEXT,
    target_ref TEXT,
    environment TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    auth_level TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    keep_protection BOOLEAN NOT NULL,
    acknowledged BOOLEAN NOT NULL,
    acknowledged_at TIMESTAMP WITH TIME ZONE,
    acknowledged_by TEXT,
    result TEXT NOT NULL,
    correlation_id UUID,
    issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_emergency_commands PRIMARY KEY (id),
    CONSTRAINT ck_emergency_commands_emergency_commands_command CHECK (command IN ('ACTIVATE_HALT', 'RESUME', 'CANCEL_UNFILLED_ENTRIES', 'CANCEL_AND_REBUILD_PROTECTION', 'REDUCE_RISK', 'FLATTEN_ACCOUNT')),
    CONSTRAINT ck_emergency_commands_emergency_commands_halt_scope CHECK (halt_scope IS NULL OR halt_scope IN ('GLOBAL_ENTRY_HALT', 'ENVIRONMENT_HALT', 'BROKER_HALT', 'ACCOUNT_HALT', 'STRATEGY_HALT', 'SYMBOL_HALT', 'SOURCE_HALT', 'MODEL_HALT')),
    CONSTRAINT ck_emergency_commands_emergency_commands_environment CHECK (environment IN ('RESEARCH', 'PAPER', 'LIVE'))
)"""
    )
    op.execute(
        "CREATE INDEX ix_emergency_commands_halt_scope_target_ref ON emergency_commands (halt_scope, target_ref)"
    )
    op.execute("CREATE INDEX ix_emergency_commands_issued_at ON emergency_commands (issued_at)")


def downgrade() -> None:
    op.execute("DROP TABLE emergency_commands")
