"""Durable emergency-command persistence tests (IMP-0404).

Done criterion support: the emergency log is append-only and active-halt
state is reconstructed by replay. PostgreSQL-only (runs in CI where
DATABASE_URL is set).
"""

import os
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from trading_contracts.enums import EmergencyCommandType, Environment, HaltScope
from trading_persistence import models as m
from trading_persistence.emergency import (
    load_active_halts,
    record_emergency_command,
)

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL not set; emergency tests run in CI only.", allow_module_level=True
    )

NOW = datetime(2026, 7, 7, 10, 0, tzinfo=UTC)


@pytest.fixture()
def clean(engine: Any) -> Any:
    with engine.begin() as conn:
        conn.execute(delete(m.EmergencyCommand))
    yield engine
    with engine.begin() as conn:
        conn.execute(delete(m.EmergencyCommand))


def _activate(conn: Any, scope: HaltScope, target: str | None, at: datetime) -> None:
    record_emergency_command(
        conn,
        command=EmergencyCommandType.ACTIVATE_HALT,
        environment=Environment.PAPER,
        actor_ref="op-1",
        auth_level="OWNER",
        reason_code="R",
        reason_text="t",
        result="HALT_ACTIVE",
        issued_at=at,
        halt_scope=scope,
        target_ref=target,
    )


def test_activate_then_replay_shows_active_halt(clean: Any) -> None:
    with clean.begin() as conn:
        _activate(conn, HaltScope.STRATEGY_HALT, "strat-1", NOW)
    with clean.connect() as conn:
        halts = load_active_halts(conn, Environment.PAPER)
    assert len(halts) == 1
    assert halts[0].scope is HaltScope.STRATEGY_HALT
    assert halts[0].target_ref == "strat-1"


def test_resume_after_activate_clears_active_halt(clean: Any) -> None:
    with clean.begin() as conn:
        _activate(conn, HaltScope.ACCOUNT_HALT, "acct-1", NOW)
        record_emergency_command(
            conn,
            command=EmergencyCommandType.RESUME,
            environment=Environment.PAPER,
            actor_ref="op-1",
            auth_level="OWNER",
            reason_code="R",
            reason_text="resumed",
            result="RESUMED",
            issued_at=NOW.replace(hour=11),
            halt_scope=HaltScope.ACCOUNT_HALT,
            target_ref="acct-1",
        )
    with clean.connect() as conn:
        assert load_active_halts(conn, Environment.PAPER) == []
        # append-only: both rows persist
        count = conn.execute(select(func.count()).select_from(m.EmergencyCommand)).scalar_one()
    assert count == 2


def test_global_halt_visible_across_environments(clean: Any) -> None:
    with clean.begin() as conn:
        _activate(conn, HaltScope.GLOBAL_ENTRY_HALT, None, NOW)
    with clean.connect() as conn:
        research = load_active_halts(conn, Environment.RESEARCH)
    assert len(research) == 1
    assert research[0].scope is HaltScope.GLOBAL_ENTRY_HALT


def test_reactivate_after_resume_is_active_again(clean: Any) -> None:
    with clean.begin() as conn:
        _activate(conn, HaltScope.SYMBOL_HALT, "AAPL", NOW)
        record_emergency_command(
            conn,
            command=EmergencyCommandType.RESUME,
            environment=Environment.PAPER,
            actor_ref="op-1",
            auth_level="OWNER",
            reason_code="R",
            reason_text="r",
            result="RESUMED",
            issued_at=NOW.replace(hour=11),
            halt_scope=HaltScope.SYMBOL_HALT,
            target_ref="AAPL",
        )
        _activate(conn, HaltScope.SYMBOL_HALT, "AAPL", NOW.replace(hour=12))
    with clean.connect() as conn:
        halts = load_active_halts(conn, Environment.PAPER)
    assert len(halts) == 1 and halts[0].target_ref == "AAPL"


def test_naive_issued_at_is_rejected(clean: Any) -> None:
    with clean.begin() as conn:
        with pytest.raises(ValueError):
            record_emergency_command(
                conn,
                command=EmergencyCommandType.ACTIVATE_HALT,
                environment=Environment.PAPER,
                actor_ref="op-1",
                auth_level="OWNER",
                reason_code="R",
                reason_text="t",
                result="HALT_ACTIVE",
                issued_at=datetime(2026, 7, 7, 10, 0),  # noqa: DTZ001 (intentional)
                halt_scope=HaltScope.SYMBOL_HALT,
                target_ref="AAPL",
            )
