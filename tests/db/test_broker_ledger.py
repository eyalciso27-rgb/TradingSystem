"""Real-PostgreSQL tests for the durable broker-observation ledger (A5).

Skipped unless DATABASE_URL is set (CI provides a postgres service). Every test
runs inside the rolled-back ``connection`` fixture, so nothing persists.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Connection

if not os.environ.get("DATABASE_URL"):
    pytest.skip(
        "DATABASE_URL not set; broker-ledger tests run in CI only.",
        allow_module_level=True,
    )

from trading_contracts.enums import Environment, OrderState  # noqa: E402
from trading_persistence.broker_ledger import BrokerObservationLedger  # noqa: E402

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


def _ledger() -> BrokerObservationLedger:
    return BrokerObservationLedger(Environment.PAPER)


def test_duplicate_fill_execution_id_is_idempotent(connection: Connection) -> None:
    ledger = _ledger()
    first = ledger.record_fill(
        connection,
        external_execution_ref="EXEC-1",
        external_order_ref="ORD-1",
        account_alias="paper",
        observed_at=NOW,
        quantity=Decimal("1"),
        price=Decimal("100.00"),
        currency="USD",
        side="BUY",
    )
    second = ledger.record_fill(
        connection,
        external_execution_ref="EXEC-1",  # same execId, redelivered
        external_order_ref="ORD-1",
        account_alias="paper",
        observed_at=NOW,
        quantity=Decimal("1"),
        price=Decimal("100.00"),
        currency="USD",
        side="BUY",
    )
    assert first is True  # a new economic event
    assert second is False  # the redelivery created nothing
    assert ledger.fill_recorded(connection, "EXEC-1") is True
    assert len(ledger.fills_for_order(connection, "ORD-1")) == 1


def test_order_status_dedups_exact_but_records_progression(connection: Connection) -> None:
    ledger = _ledger()
    ack = ledger.record_order_status(
        connection,
        external_order_ref="ORD-2",
        order_state=OrderState.ACKNOWLEDGED,
        account_alias="paper",
        observed_at=NOW,
        filled_quantity=Decimal("0"),
    )
    ack_again = ledger.record_order_status(
        connection,
        external_order_ref="ORD-2",
        order_state=OrderState.ACKNOWLEDGED,
        account_alias="paper",
        observed_at=NOW,
        filled_quantity=Decimal("0"),
    )
    partial = ledger.record_order_status(
        connection,
        external_order_ref="ORD-2",
        order_state=OrderState.PARTIALLY_FILLED,
        account_alias="paper",
        observed_at=NOW,
        filled_quantity=Decimal("40"),
    )
    assert ack is True
    assert ack_again is False  # exact duplicate callback
    assert partial is True  # a genuine progression
    history = ledger.order_status_history(connection, "ORD-2")
    assert len(history) == 2
    assert ledger.latest_order_state(connection, "ORD-2") is OrderState.PARTIALLY_FILLED


def test_position_snapshot_records_only_changes(connection: Connection) -> None:
    ledger = _ledger()
    opened = ledger.record_position(
        connection,
        instrument_ref="CON-1",
        position_quantity=Decimal("100"),
        account_alias="paper",
        observed_at=NOW,
        average_price=Decimal("25.00"),
        symbol="AAPL",
    )
    unchanged = ledger.record_position(
        connection,
        instrument_ref="CON-1",
        position_quantity=Decimal("100"),
        account_alias="paper",
        observed_at=NOW,
        average_price=Decimal("25.00"),
        symbol="AAPL",
    )
    reduced = ledger.record_position(
        connection,
        instrument_ref="CON-1",
        position_quantity=Decimal("50"),
        account_alias="paper",
        observed_at=NOW,
        average_price=Decimal("25.00"),
        symbol="AAPL",
    )
    assert opened is True
    assert unchanged is False  # unchanged snapshot lands nothing
    assert reduced is True
    positions = ledger.open_positions(connection)
    assert len(positions) == 1
    assert positions[0].position_quantity == Decimal("50")


def test_unknown_position_is_absent_never_zero(connection: Connection) -> None:
    ledger = _ledger()
    # An OBSERVED flat (quantity 0) is recorded but excluded from open exposure.
    ledger.record_position(
        connection,
        instrument_ref="CON-FLAT",
        position_quantity=Decimal("0"),
        account_alias="paper",
        observed_at=NOW,
    )
    open_refs = {p.instrument_ref for p in ledger.open_positions(connection)}
    # The observed-flat instrument is not 'open', and an instrument never
    # observed at all (CON-NEVER) is simply absent -- never a fabricated zero.
    assert "CON-FLAT" not in open_refs
    assert "CON-NEVER" not in open_refs


def test_environments_are_isolated(connection: Connection) -> None:
    paper = BrokerObservationLedger(Environment.PAPER)
    research = BrokerObservationLedger(Environment.RESEARCH)
    paper.record_fill(
        connection,
        external_execution_ref="EXEC-ISO",
        account_alias="paper",
        observed_at=NOW,
        quantity=Decimal("1"),
    )
    # The same execId in a different environment is a different, new event.
    assert research.fill_recorded(connection, "EXEC-ISO") is False
    assert (
        research.record_fill(
            connection,
            external_execution_ref="EXEC-ISO",
            account_alias="research",
            observed_at=NOW,
            quantity=Decimal("1"),
        )
        is True
    )
