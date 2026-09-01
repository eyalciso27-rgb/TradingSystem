"""Restart-recovery of a protected bracket (ADR-0019, Increment 5).

After a restart a durable SUBMIT_PREPARED bracket must recover BOTH legs from
the broker's own facts. A matched parent whose protective stop is missing or
wrong — wrong side, type, quantity or security — is an UNPROTECTED position
wearing a recovered name, and must fail closed to NEEDS_RECONCILIATION rather
than be claimed as fine. These pin ``_protective_child_problem`` directly, which
is the check the recovery path calls before it ever reports SUBMITTED.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading_broker_core.port import OrderSide, RecoveredOrder
from trading_contracts.enums import OrderState, OrderType
from trading_execution_agent.execution_service import (
    ExecutionFingerprint,
    ExecutionService,
)

NOW = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)
CONID = "conid:265598"

_problem = ExecutionService._protective_child_problem


def _prepared() -> ExecutionFingerprint:
    """A prepared LONG bracket: BUY entry, protective SELL STOP of qty 1."""
    return ExecutionFingerprint(
        decision_id="d1",
        client_order_id="brk-1",
        strategy_id="s",
        source_id="src",
        symbol="AAPL",
        side=OrderSide.BUY.value,
        environment="PAPER",
        account_alias="paper-poi",
        broker_instrument_ref=CONID,
        bracket_mechanism="native-bracket",
        protective_order_refs="brk-1:stop",
        protective_quantity="1",
        protective_stop_price="9",
    )


def _child(
    *,
    side: OrderSide = OrderSide.SELL,
    order_type: OrderType = OrderType.STOP,
    quantity: Decimal = Decimal("1"),
    conid: str = CONID,
    stop_price: Decimal | None = Decimal("9"),
) -> RecoveredOrder:
    return RecoveredOrder(
        client_order_id="brk-1:stop",
        broker_order_ref="ibkr-paper:1002",
        state=OrderState.ACKNOWLEDGED,
        account_alias="paper-poi",
        symbol="AAPL",
        broker_instrument_ref=conid,
        side=side,
        quantity=quantity,
        order_type=order_type,
        limit_price=None,
        stop_price=stop_price,
        observed_at=NOW,
    )


def _parent() -> RecoveredOrder:
    return RecoveredOrder(
        client_order_id="brk-1",
        broker_order_ref="ibkr-paper:1001",
        state=OrderState.ACKNOWLEDGED,
        account_alias="paper-poi",
        symbol="AAPL",
        broker_instrument_ref=CONID,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10"),
        stop_price=None,
        observed_at=NOW,
    )


def test_both_legs_present_and_correct_is_protected() -> None:
    assert _problem(_prepared(), (_parent(), _child())) is None


def test_missing_stop_child_is_unprotected() -> None:
    problem = _problem(_prepared(), (_parent(),))
    assert problem is not None
    assert "not working at the broker" in problem


def test_wrong_side_stop_protects_nothing() -> None:
    problem = _problem(_prepared(), (_parent(), _child(side=OrderSide.BUY)))
    assert problem is not None
    assert "SELL" in problem


def test_wrong_type_child_is_not_a_stop() -> None:
    problem = _problem(_prepared(), (_parent(), _child(order_type=OrderType.LIMIT)))
    assert problem is not None
    assert "not a STOP" in problem


def test_wrong_quantity_child_undercovers_exposure() -> None:
    problem = _problem(_prepared(), (_parent(), _child(quantity=Decimal("2"))))
    assert problem is not None
    assert "not the protected" in problem


def test_wrong_security_child_does_not_protect_this_entry() -> None:
    problem = _problem(_prepared(), (_parent(), _child(conid="conid:999999")))
    assert problem is not None
    assert "different security" in problem


def test_exact_stop_price_is_accepted() -> None:
    # The control for the stop-price checks: the right level passes.
    assert _problem(_prepared(), (_parent(), _child(stop_price=Decimal("9")))) is None


def test_wrong_stop_price_protects_the_wrong_exposure() -> None:
    problem = _problem(_prepared(), (_parent(), _child(stop_price=Decimal("8"))))
    assert problem is not None
    assert "wrong price" in problem


def test_missing_broker_stop_price_cannot_prove_protection() -> None:
    problem = _problem(_prepared(), (_parent(), _child(stop_price=None)))
    assert problem is not None
    assert "no stop price" in problem
