"""Connection-management helpers for the runtime (persistence layer).

The runtime app must not import a database driver directly -- ``sqlalchemy`` is
allowed only under ``packages/py/persistence`` (the SDK-boundary guardrail). So
the few engine/connection operations the runtime needs live here, behind small
functions the app calls with an opaque engine. This keeps every ``sqlalchemy``
symbol on the persistence side of the boundary while the app stays free of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection
from trading_contracts.enums import Environment

from trading_persistence.db import create_sync_engine
from trading_persistence.emergency import ActiveHalt, load_active_halts


def runtime_engine() -> Engine:
    """A fresh sync engine from ``DATABASE_URL`` (the app never builds one)."""
    return create_sync_engine()


def dispose(engine: Engine) -> None:
    """Release an engine's pooled connections."""
    engine.dispose()


def ping(engine: Engine) -> None:
    """Prove the database is reachable. Raises on failure (fail closed)."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def read_active_halts(
    engine: Engine, environment: Environment
) -> list[ActiveHalt]:
    """Replay the append-only emergency log on a short-lived connection."""
    with engine.connect() as connection:
        return load_active_halts(connection, environment)


@contextmanager
def emergency_unit_of_work(engine: Engine) -> Iterator[Connection]:
    """A single committed transaction for an emergency command + its audit.

    Yields one connection so the durable command row and its tamper-evident
    audit record commit together; a failure rolls both back.
    """
    with engine.begin() as connection:
        yield connection


__all__ = [
    "dispose",
    "emergency_unit_of_work",
    "ping",
    "read_active_halts",
    "runtime_engine",
]
