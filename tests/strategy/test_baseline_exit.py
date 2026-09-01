"""Tests for the baseline strategy-exit DECISION semantics (mission section 8).

Exit decisions only — no broker mechanics. Each owner-frozen exit rule is
proven load-bearing, and the emitted trigger is a canonical Position-Coordinator
``ExitTrigger`` (never an order).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from trading_application.baseline_exit import (
    BaselineExitAction,
    BaselineExitDecision,
    BaselineExitEvaluator,
    BaselineExitPolicy,
    BaselineOpenPosition,
)
from trading_application.position_coordinator import ExitTrigger
from trading_contracts.enums import ExitTriggerType

AS_OF = datetime(2025, 6, 1, 20, 0, tzinfo=UTC)


def _policy() -> BaselineExitPolicy:
    return BaselineExitPolicy(
        policy_version="1.0.0",
        failed_breakout_sessions=3,
        time_exit_sessions=20,
        trail_atr_multiple=Decimal("2.5"),
    )


def _position(**kw: object) -> BaselineOpenPosition:
    base = dict(
        position_ref="pos-1",
        symbol="MVPX",
        entry_price=Decimal("100"),
        initial_stop=Decimal("95"),
        breakout_resistance=Decimal("99"),
        current_stop=Decimal("95"),
        sessions_since_entry=1,
        highest_close_since_entry=Decimal("100"),
        reached_1r=False,
    )
    base.update(kw)
    return BaselineOpenPosition(**base)  # type: ignore[arg-type]


def _evaluate(
    position: BaselineOpenPosition, *, close: Decimal, atr: Decimal = Decimal("2")
) -> BaselineExitDecision:
    return BaselineExitEvaluator(_policy()).evaluate(
        position, latest_completed_close=close, atr_long=atr, as_of=AS_OF
    )


def test_hold_when_no_condition_fires() -> None:
    decision = _evaluate(_position(sessions_since_entry=5), close=Decimal("101"))
    assert decision.action == BaselineExitAction.HOLD
    assert decision.exit_trigger_type is None
    assert decision.to_exit_trigger(trigger_id="t", observed_at=AS_OF) is None


def test_failed_breakout_exits_within_window() -> None:
    # Within the first 3 sessions, a close below breakout resistance => EXIT.
    decision = _evaluate(
        _position(sessions_since_entry=2, breakout_resistance=Decimal("99")),
        close=Decimal("98"),
    )
    assert decision.action == BaselineExitAction.EXIT_FAILED_BREAKOUT
    assert decision.exit_trigger_type is ExitTriggerType.THESIS_INVALIDATED
    trigger = decision.to_exit_trigger(trigger_id="t1", observed_at=AS_OF)
    assert isinstance(trigger, ExitTrigger)
    assert trigger.reduce_quantity is None  # full exit


def test_failed_breakout_does_not_fire_after_window() -> None:
    decision = _evaluate(
        _position(sessions_since_entry=4, breakout_resistance=Decimal("99")),
        close=Decimal("98"),
    )
    assert decision.action != BaselineExitAction.EXIT_FAILED_BREAKOUT


def test_time_exit_when_no_1r_by_deadline() -> None:
    decision = _evaluate(
        _position(sessions_since_entry=20, reached_1r=False), close=Decimal("101")
    )
    assert decision.action == BaselineExitAction.EXIT_TIME
    assert decision.exit_trigger_type is ExitTriggerType.TIME_EXIT


def test_time_exit_disabled_once_1r_reached() -> None:
    # +1R reached (close >= entry + initial risk = 100 + 5 = 105) => no time exit,
    # even at/after the deadline.
    decision = _evaluate(
        _position(sessions_since_entry=25, highest_close_since_entry=Decimal("106")),
        close=Decimal("106"),
    )
    assert decision.reached_1r
    assert decision.action != BaselineExitAction.EXIT_TIME


def test_trailing_stop_tightens_only_after_1r() -> None:
    # After +1R, new stop = highest_close - 2.5*ATR = 110 - 5 = 105 > current 95.
    decision = _evaluate(
        _position(
            sessions_since_entry=8, reached_1r=True,
            highest_close_since_entry=Decimal("110"), current_stop=Decimal("95"),
        ),
        close=Decimal("108"),
        atr=Decimal("2"),
    )
    assert decision.action == BaselineExitAction.ADJUST_TRAILING_STOP
    assert decision.new_stop_price == Decimal("105")
    assert decision.exit_trigger_type is ExitTriggerType.TRAILING_POLICY


def test_trailing_stop_never_widens() -> None:
    # A computed trailing stop below the current stop is ignored (tighten-only).
    decision = _evaluate(
        _position(
            sessions_since_entry=8, reached_1r=True,
            highest_close_since_entry=Decimal("101"), current_stop=Decimal("99"),
        ),
        close=Decimal("101"),
        atr=Decimal("5"),  # trailing = 101 - 12.5 = 88.5 < current 99
    )
    assert decision.action == BaselineExitAction.HOLD
    assert decision.new_stop_price is None


def test_failed_breakout_outranks_trailing() -> None:
    # Both a failed breakout and a +1R state present: the exit wins over adjust.
    decision = _evaluate(
        _position(
            sessions_since_entry=2, reached_1r=True, breakout_resistance=Decimal("99"),
            highest_close_since_entry=Decimal("110"),
        ),
        close=Decimal("98"),
    )
    assert decision.action == BaselineExitAction.EXIT_FAILED_BREAKOUT


def test_lookahead_naive_as_of_rejected() -> None:
    from trading_application.baseline_exit import BaselineExitError

    with pytest.raises(BaselineExitError):
        BaselineExitEvaluator(_policy()).evaluate(
            _position(), latest_completed_close=Decimal("100"), atr_long=Decimal("2"),
            as_of=datetime(2025, 6, 1, 20, 0),  # naive
        )
