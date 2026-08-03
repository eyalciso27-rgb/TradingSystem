"""Defects 2 and 3: ONE immutable snapshot, and exactly ONE disconnect.

**Defect 2.** The probe read open orders and positions, then handed the LIVE
broker view to ``ReconciliationEngine`` — which calls ``reconcile()`` and
``order_events()`` itself. So the counts came from one round trip and the
findings from another. A `CLEAN` verdict built that way describes a moment that
never existed: an order can appear between the two reads and be counted by
neither. Mixed-time data must never be classified.

**Defect 3.** ``_probe_connected`` disconnected, and the outer ``finally``
disconnected again — two disconnects per successful probe, with the second
failure swallowed.

These tests run the REAL reconciliation path (no stub), because the point is
what the engine does to the broker.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from test_account_safety_probe import (  # type: ignore[import-not-found]  # sibling fakes
    ALIAS,
    NOW,
    FakeView,
    _receipt,
    _snapshot,
)
from trading_broker_core.port import BrokerErrorCode, BrokerPortError, BrokerPosition
from trading_contracts.enums import Environment, OrderState
from trading_execution_agent.account_safety import (
    FORBIDDEN_BROKER_VERBS,
    AccountSafety,
    BrokerSnapshot,
    ProbeConfig,
    SafetyReason,
    _SnapshotBrokerView,
    probe_account_safety,
)

#: The live broker reads that make up one snapshot. Each must happen once.
SNAPSHOT_READS = (
    "get_account_snapshot",
    "get_open_orders",
    "get_positions",
    "order_events",
    "reconcile",
)


def run(view: FakeView, **over: Any) -> Any:
    """Probe with the REAL reconciliation engine — nothing stubbed."""
    return probe_account_safety(
        view, ProbeConfig(account_alias=ALIAS, **over), now=NOW
    )


# -- one snapshot -------------------------------------------------------------


def test_an_empty_account_is_clean_through_the_real_engine() -> None:
    """Baseline: the real path still reaches CLEAN, so the counts below mean something."""
    report = run(FakeView())
    assert report.safety is AccountSafety.CLEAN
    assert report.working_orders == 0
    assert report.non_zero_positions == 0


@pytest.mark.parametrize("method", SNAPSHOT_READS)
def test_each_broker_read_happens_exactly_once(method: str) -> None:
    """The whole snapshot is captured once; nothing re-reads the broker."""
    view = FakeView()
    report = run(view)
    assert report.safety is AccountSafety.CLEAN
    assert view.count(method) == 1, f"{method} called {view.count(method)}x: {view.calls}"


def test_reconciliation_triggers_no_second_broker_read() -> None:
    """The engine calls reconcile()/order_events() — on the SNAPSHOT, not the broker.

    Before the fix this test would see 2 order_events calls (one for the
    internal state, one from the engine) and 1 reconcile issued after the
    counts were already taken.
    """
    view = FakeView(orders=(_receipt(OrderState.FILLED),))
    run(view)
    assert view.count("order_events") == 1
    assert view.count("reconcile") == 1
    # And every read happened before classification, in one contiguous burst.
    reads = [c for c in view.calls if c in SNAPSHOT_READS]
    assert reads == list(SNAPSHOT_READS), view.calls


def test_the_snapshot_view_serves_the_captured_data_without_a_broker() -> None:
    snapshot = BrokerSnapshot(
        account=_snapshot(),
        open_orders=(),
        positions=(),
        order_events=(),
        reconciliation=FakeView().reconcile(ALIAS),
    )
    view = _SnapshotBrokerView(snapshot, ALIAS)
    assert view.reconcile(ALIAS) is snapshot.reconciliation
    assert tuple(view.order_events()) == ()
    assert view.get_open_orders(ALIAS) == ()
    assert view.get_positions(ALIAS) == ()
    assert view.get_account_snapshot(ALIAS) is snapshot.account


def test_the_snapshot_view_exposes_no_mutating_verb() -> None:
    """Read-only structurally, exactly like the outward facade."""
    for verb in FORBIDDEN_BROKER_VERBS:
        assert not hasattr(_SnapshotBrokerView, verb), verb


# -- mixed-time data ----------------------------------------------------------


def test_a_broker_that_gains_an_order_between_reads_is_never_clean() -> None:
    """The exact mixed-time hazard: empty on read 1, an order on read 2."""
    view = FakeView(
        orders=(),
        orders_on_second_read=(_receipt(OrderState.ACKNOWLEDGED),),
    )
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.SNAPSHOT_INCONSISTENT in report.reasons


def test_a_broker_that_loses_an_order_between_reads_is_never_clean() -> None:
    """The dangerous direction: read 2 looks empty, so a naive probe says CLEAN."""
    view = FakeView(
        orders=(_receipt(OrderState.ACKNOWLEDGED),),
        orders_on_second_read=(),
    )
    report = run(view)
    assert report.safety is not AccountSafety.CLEAN
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.SNAPSHOT_INCONSISTENT in report.reasons


def test_a_position_that_changes_between_reads_is_never_clean() -> None:
    view = FakeView(
        positions=(),
        positions_on_second_read=(
            BrokerPosition(symbol="DVLT", quantity=Decimal(5), average_price=Decimal(2)),
        ),
    )
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.SNAPSHOT_INCONSISTENT in report.reasons


def test_an_order_state_that_changes_between_reads_is_never_clean() -> None:
    """Same id, different state, is still two different instants."""
    view = FakeView(
        orders=(_receipt(OrderState.FILLED),),
        orders_on_second_read=(_receipt(OrderState.ACKNOWLEDGED),),
    )
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN


def test_a_zero_quantity_row_appearing_on_the_second_read_is_still_coherent() -> None:
    """Coherence compares real exposure, not bookkeeping rows (D-CLOCK-02)."""
    view = FakeView(
        positions=(),
        positions_on_second_read=(
            BrokerPosition(symbol="DVLT", quantity=Decimal(0), average_price=Decimal(2)),
        ),
    )
    report = run(view)
    assert report.safety is AccountSafety.CLEAN


def test_duplicated_orders_in_one_snapshot_are_unknown() -> None:
    duplicate = (_receipt(OrderState.FILLED), _receipt(OrderState.FILLED))
    report = run(FakeView(orders=duplicate))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.SNAPSHOT_INCONSISTENT in report.reasons


def test_duplicated_positions_in_one_snapshot_are_unknown() -> None:
    row = BrokerPosition(symbol="DVLT", quantity=Decimal(1), average_price=Decimal(2))
    report = run(FakeView(positions=(row, row)))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.SNAPSHOT_INCONSISTENT in report.reasons


def test_a_reconcile_answering_for_another_account_is_unknown() -> None:
    view = FakeView()
    view._reconcile_result = view.reconcile("someone-else")
    view.calls.clear()
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ACCOUNT_MISMATCH in report.reasons


def test_a_reconcile_returning_the_wrong_type_is_unknown() -> None:
    """The port declares ReconciliationResult; anything else is malformed."""
    report = run(FakeView(reconcile_result=object()))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.MALFORMED_DATA in report.reasons


def test_unreadable_order_events_are_unknown() -> None:
    report = run(FakeView(fail_on="order_events"))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ORDER_EVENTS_UNREADABLE in report.reasons


def test_no_snapshot_is_carried_between_probes() -> None:
    """Each run captures fresh; nothing is cached or reused."""
    first = FakeView()
    assert run(first).safety is AccountSafety.CLEAN
    second = FakeView(positions=(
        BrokerPosition(symbol="DVLT", quantity=Decimal(9), average_price=Decimal(2)),
    ))
    report = run(second)
    assert report.safety is AccountSafety.NOT_CLEAN
    assert all(second.count(read) == 1 for read in SNAPSHOT_READS)


# -- exactly one disconnect ---------------------------------------------------


def test_clean_connects_once_and_disconnects_once() -> None:
    view = FakeView()
    report = run(view)
    assert report.safety is AccountSafety.CLEAN
    assert view.count("connect") == 1
    assert view.count("disconnect") == 1
    assert report.disconnect_ok is True


def test_not_clean_connects_once_and_disconnects_once() -> None:
    view = FakeView(positions=(
        BrokerPosition(symbol="DVLT", quantity=Decimal(3), average_price=Decimal(2)),
    ))
    report = run(view)
    assert report.safety is AccountSafety.NOT_CLEAN
    assert view.count("connect") == 1
    assert view.count("disconnect") == 1
    assert report.disconnect_ok is True


@pytest.mark.parametrize(
    "failing_read",
    ["health_check", "get_account_snapshot", "get_open_orders", "get_positions", "reconcile"],
)
def test_a_read_failure_still_disconnects_exactly_once(failing_read: str) -> None:
    """Early failure must not leak the session, and must not double-close it."""
    view = FakeView(fail_on=failing_read)
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert view.count("connect") == 1
    assert view.count("disconnect") == 1


def test_a_failed_connect_never_calls_disconnect() -> None:
    view = FakeView(fail_on="connect")
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.CONNECT_FAILED in report.reasons
    assert view.count("disconnect") == 0, "nothing was open; nothing to close"
    assert report.disconnect_ok is False


def test_a_refused_endpoint_never_connects_or_disconnects() -> None:
    view = FakeView()
    report = run(view, port=4001)
    assert report.safety is AccountSafety.UNKNOWN
    assert view.calls == []


def test_a_failed_disconnect_is_unknown_and_attempted_once() -> None:
    view = FakeView(fail_on="disconnect")
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert report.reasons == (SafetyReason.DISCONNECT_FAILED,)
    assert report.disconnect_ok is False
    assert view.count("disconnect") == 1


def test_a_failed_disconnect_can_never_produce_clean() -> None:
    """The whole point: a session we could not close proves nothing."""
    view = FakeView(fail_on="disconnect")
    report = run(view)
    assert report.safety is not AccountSafety.CLEAN
    assert report.permits_unattended_action is False
    # The gathered counts survive for the alert, but the verdict does not.
    assert report.working_orders == 0


def test_a_failed_disconnect_after_a_dirty_account_stays_refused() -> None:
    view = FakeView(
        positions=(
            BrokerPosition(symbol="DVLT", quantity=Decimal(3), average_price=Decimal(2)),
        ),
        fail_on="disconnect",
    )
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert report.permits_unattended_action is False
    assert report.non_zero_positions == 1, "the evidence is kept for the alert"


def test_an_unexpected_exception_still_disconnects_once() -> None:
    view = FakeView(fail_on="get_positions", failure=RuntimeError("kaboom"))
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert view.count("disconnect") == 1


def test_a_disconnect_failure_is_never_silently_swallowed() -> None:
    """Regression: the old outer `finally` discarded this exception entirely."""
    view = FakeView(fail_on="disconnect", failure=RuntimeError("socket stuck"))
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.DISCONNECT_FAILED in report.reasons
    assert "RuntimeError" in report.detail


def test_an_unknown_report_never_carries_a_not_clean_reason() -> None:
    """UNKNOWN and NOT_CLEAN vocabularies stay disjoint even on the disconnect path."""
    from trading_execution_agent.account_safety import NOT_CLEAN_REASONS

    for view in (
        FakeView(fail_on="disconnect"),
        FakeView(fail_on="connect"),
        FakeView(orders=(), orders_on_second_read=(_receipt(OrderState.ACKNOWLEDGED),)),
    ):
        report = run(view)
        assert report.safety is AccountSafety.UNKNOWN
        assert not (set(report.reasons) & NOT_CLEAN_REASONS)


def test_a_broker_error_on_disconnect_is_also_unknown() -> None:
    view = FakeView(
        fail_on="disconnect",
        failure=BrokerPortError(BrokerErrorCode.UNKNOWN_BROKER_RESPONSE, "nope"),
    )
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.DISCONNECT_FAILED in report.reasons


def test_the_paper_gate_still_runs_before_any_other_read() -> None:
    """A live/unknown environment must abort before orders are even fetched."""
    view = FakeView(snapshot=_snapshot(env=Environment.LIVE))
    report = run(view)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ACCOUNT_NOT_PAPER in report.reasons
    assert view.count("get_open_orders") == 0
    assert view.count("disconnect") == 1
