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


def test_migration_is_initial_revision() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision: str | None = None' in text
    assert "create_all" in text
    assert "drop_all" in text


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
