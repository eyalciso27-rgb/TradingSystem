"""Real-PostgreSQL tests for the single-instance ownership lock (A2).

Skipped unless DATABASE_URL is set (CI provides a postgres service).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import Engine

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL not set; advisory-lock tests run in CI only.",
        allow_module_level=True,
    )

from trading_persistence.advisory_lock import (  # noqa: E402
    AdvisoryLock,
    advisory_lock_held,
)
from trading_persistence.db import create_sync_engine  # noqa: E402


def _name() -> str:
    return f"test-runtime-lock:{uuid4()}"


def test_only_one_instance_can_hold_the_lock(engine: Engine) -> None:
    name = _name()
    other = create_sync_engine()
    lock_a = AdvisoryLock(engine, name=name)
    lock_b = AdvisoryLock(other, name=name)
    try:
        assert lock_a.try_acquire() is True
        assert lock_a.is_held() is True
        # A second instance (separate session) cannot acquire the same lock.
        assert lock_b.try_acquire() is False
        # Releasing the first lets the second acquire it.
        lock_a.release()
        assert lock_b.try_acquire() is True
        assert lock_b.is_held() is True
    finally:
        lock_a.release()
        lock_b.release()
        other.dispose()


def test_reacquire_is_idempotent_while_held(engine: Engine) -> None:
    lock = AdvisoryLock(engine, name=_name())
    try:
        assert lock.try_acquire() is True
        assert lock.try_acquire() is True  # idempotent, still held
    finally:
        lock.release()


def test_lost_connection_reports_not_held(engine: Engine) -> None:
    lock = AdvisoryLock(engine, name=_name())
    try:
        assert lock.try_acquire() is True
        assert lock.is_held() is True
        # Simulate a dropped session: close the owning connection out from under
        # the lock. is_held() must report the lock as lost, not still owned.
        lock._connection.close()  # type: ignore[union-attr]
        assert lock.is_held() is False
    finally:
        lock.release()


def test_advisory_lock_held_probe(engine: Engine) -> None:
    name = _name()
    lock = AdvisoryLock(engine, name=name)
    try:
        assert advisory_lock_held(engine, name=name) is False  # free
        assert lock.try_acquire() is True
        # A separate session's probe sees it held.
        other = create_sync_engine()
        try:
            assert advisory_lock_held(other, name=name) is True
        finally:
            other.dispose()
    finally:
        lock.release()
