"""Fixtures for real-PostgreSQL migration tests (IMP-0201).

The whole module is skipped unless DATABASE_URL points at a PostgreSQL server
(set by CI's postgres service). No production database is ever contacted.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Connection, Engine

PERSISTENCE_DIR = (
    Path(__file__).resolve().parents[2] / "packages" / "py" / "persistence"
)


def _has_database() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def _alembic_config() -> Config:
    config = Config(str(PERSISTENCE_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PERSISTENCE_DIR / "alembic"))
    return config


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    from trading_persistence.db import create_sync_engine

    eng = create_sync_engine()
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def schema_at_head() -> None:
    """Ensure the schema is at head before each DB test (idempotent).

    No-op when no database is configured, so the static (no-DB) schema tests
    in this directory still run locally.
    """
    if not _has_database():
        return
    from alembic import command

    command.upgrade(_alembic_config(), "head")


@pytest.fixture()
def connection(engine: Engine) -> Iterator[Connection]:
    """A connection wrapped in a transaction that is rolled back after the test."""
    conn = engine.connect()
    transaction = conn.begin()
    try:
        yield conn
    finally:
        transaction.rollback()
        conn.close()


@pytest.fixture()
def alembic_config() -> Config:
    return _alembic_config()
