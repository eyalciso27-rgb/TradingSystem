"""Static schema checks that need no database (IMP-0201).

These run in normal pytest (no PostgreSQL required) and enforce the
broker-neutrality and secret-hygiene done criteria at the metadata level.
"""

from pathlib import Path

import trading_persistence.models  # noqa: F401  (populates Base.metadata)
from sqlalchemy import Float, LargeBinary, Numeric
from trading_persistence.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT
    / "packages"
    / "py"
    / "persistence"
    / "alembic"
    / "versions"
    / "0001_core_schema.py"
)

EXPECTED_TABLES = {
    "workspaces",
    "instruments",
    "instrument_aliases",
    "raw_objects",
    "source_events",
    "source_attachments",
    "social_message_observations",
    "trade_candidates",
    "signal_assessments",
    "decision_runs",
    "system_trade_plans",
    "risk_evaluations",
    "risk_vetoes",
    "brokers",
    "broker_accounts",
    "order_intents",
    "broker_orders",
    "broker_order_events",
    "fills",
    "positions",
    "config_versions",
    "active_version_pointers",
    "outbox_messages",
    "consumer_inbox",
    "audit_events",
}

FORBIDDEN_BROKER_TOKENS = [
    "ibkr",
    "tws",
    "conid",
    "permid",
    "clientid",
    "alpaca",
    "binance",
    "native_order",
    "broker_status_raw",
]

FORBIDDEN_SECRET_TOKENS = [
    "password",
    "token",
    "cookie",
    "session",
    "private_key",
    "api_key",
    "bearer",
    "credential",
    "secret_value",
]

MONEY_COLUMNS = {
    "quantity",
    "quantity_open",
    "approved_quantity",
    "filled_quantity",
    "remaining_quantity",
    "last_fill_quantity",
    "limit_price",
    "stop_price",
    "price",
    "average_fill_price",
    "last_fill_price",
    "average_entry_price",
    "max_loss_amount",
    "approved_risk_amount",
    "commission",
    "realized_pnl",
    "unrealized_pnl",
    "score",
    "calibrated_probability",
}


def _all_identifiers() -> set[str]:
    names: set[str] = set(Base.metadata.tables)
    for table in Base.metadata.tables.values():
        for column in table.columns:
            names.add(column.name)
        for constraint in table.constraints:
            if constraint.name:
                names.add(str(constraint.name))
        for index in table.indexes:
            if index.name:
                names.add(index.name)
    return names


def test_core_tables_present() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_broker_specific_identifiers() -> None:
    identifiers = {name.lower() for name in _all_identifiers()}
    for token in FORBIDDEN_BROKER_TOKENS:
        assert all(token not in name for name in identifiers), token


def test_migration_source_is_broker_neutral() -> None:
    text = MIGRATION.read_text(encoding="utf-8").lower()
    for token in FORBIDDEN_BROKER_TOKENS:
        assert token not in text, token


def test_no_secret_columns() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            for token in FORBIDDEN_SECRET_TOKENS:
                assert token not in column.name.lower(), f"{table.name}.{column.name}"


def test_no_raw_binary_columns() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, LargeBinary), f"{table.name}.{column.name}"


def test_money_columns_use_numeric_not_float() -> None:
    for table in Base.metadata.tables.values():
        for column in table.columns:
            assert not isinstance(column.type, Float), f"{table.name}.{column.name} is Float"
            if column.name in MONEY_COLUMNS:
                assert isinstance(column.type, Numeric), f"{table.name}.{column.name} not Numeric"


def test_migration_is_initial_and_frozen() -> None:
    """0001 must be frozen DDL, never referencing the live metadata."""
    text = MIGRATION.read_text(encoding="utf-8")
    assert "down_revision: str | None = None" in text
    assert "create_all" not in text
    assert "trading_persistence.models" not in text
    assert text.count("CREATE TABLE") == 22  # the frozen 0001-era tables


def test_outbox_migration_exists_and_is_frozen() -> None:
    outbox = MIGRATION.parent / "0003_transactional_outbox.py"
    text = outbox.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0002_point_in_time_storage"' in text
    assert "outbox_messages" in text
    assert "trading_persistence.models" not in text  # frozen DDL, no live metadata


def test_inbox_migration_exists_and_is_frozen() -> None:
    inbox = MIGRATION.parent / "0004_consumer_inbox.py"
    text = inbox.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0003_transactional_outbox"' in text
    assert "uq_consumer_inbox_consumer_id_message_id" in text
    assert "trading_persistence.models" not in text  # frozen DDL, no live metadata


def test_audit_migration_exists_and_is_frozen() -> None:
    audit = MIGRATION.parent / "0005_tamper_evident_audit.py"
    text = audit.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0004_consumer_inbox"' in text
    assert "uq_audit_events_sequence_no" in text
    assert "trading_persistence.models" not in text  # frozen DDL, no live metadata


def test_point_in_time_migration_exists() -> None:
    pit = MIGRATION.parent / "0002_point_in_time_storage.py"
    text = pit.read_text(encoding="utf-8")
    assert 'down_revision: str | None = "0001_core_schema"' in text
    for column in ("effective_at", "available_at", "superseded_at", "ingested_at"):
        assert column in text


def test_primary_keys_are_uuid() -> None:
    from sqlalchemy.dialects.postgresql import UUID

    for table in Base.metadata.tables.values():
        pk_columns = list(table.primary_key.columns)
        assert len(pk_columns) == 1, table.name
        assert isinstance(pk_columns[0].type, UUID), table.name


def test_timestamps_are_timezone_aware() -> None:
    from sqlalchemy import DateTime

    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True, f"{table.name}.{column.name}"
