"""Positions and open-order snapshots must be repeatable on one session.

The round-10 owner run exposed a structural regression from round 9. The
callback router retires a stream key on ``release`` so a late callback for a
FINISHED unique request cannot recreate a live waiter. That is correct for
``request:<id>`` / ``order:<id>``. But ``POSITIONS_STREAM`` and
``OPEN_ORDERS_STREAM`` were FIXED, reusable channel names -- so the first read
retired them, and the SECOND read (independent portfolio evidence, then the
canonical evaluation) had every callback archived and timed out. The owner
summary read "timed out waiting for positions" and dropped the contract and
quote it had already observed.

The fix gives each reusable operation a UNIQUE lifecycle identity rather than
weakening retirement:

* positions -> ``reqPositionsMulti`` (request-scoped), routed to
  ``request_stream(req_id)`` with a fresh id per read;
* open-order snapshots -> a transport-owned generation
  (``open-orders:<generation>``), routed to the CURRENT active generation.

These tests drive the REAL ``_IbApiPaperTransport``, the real ``_PaperApp``
EWrapper callbacks and the real ``CallbackRouter`` over a stub SDK. Nothing
connects to IBKR.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest


class _Contract:
    def __init__(self, symbol: str = "AAPL", con_id: int = 265598) -> None:
        self.symbol = symbol
        self.conId = con_id  # noqa: N815 - mirrors the SDK name


class _Order:
    def __init__(self, account: str, *, client_id: int = 1) -> None:
        self.account = account
        self.clientId = client_id  # noqa: N815 - mirrors the SDK name
        self.permId = 900001  # noqa: N815
        self.action = "BUY"
        self.totalQuantity = Decimal("10")  # noqa: N815
        self.orderType = "LMT"  # noqa: N815
        self.orderRef = "poi-x"  # noqa: N815
        self.lmtPrice = 100.0  # noqa: N815
        self.auxPrice = None  # noqa: N815


class _OrderState:
    def __init__(self, status: str = "Submitted") -> None:
        self.status = status


class _StubEClient:
    """Records position/open-order requests and replays callbacks through the
    app's own EWrapper methods, so the production routing runs."""

    def __init__(self, wrapper: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.positions_multi_ids: list[int] = []
        self.cancel_positions_multi_ids: list[int] = []
        #: per reqPositionsMulti: list of (account, symbol, qty, avg) to deliver,
        #: or None to deliver NOTHING (models a timeout).
        self.positions_script: list[list[tuple[str, str, float, float]] | None] = []
        self._pos_index = 0
        #: per open-order request: list of (account, symbol, status) to deliver,
        #: or None to deliver no end marker (timeout).
        self.open_orders_script: list[list[tuple[str, str, str]] | None] = []
        self._oo_index = 0

    def reqPositionsMulti(self, req_id: int, account: str, model_code: str) -> None:  # noqa: N802
        self.positions_multi_ids.append(req_id)
        self.calls.append(("reqPositionsMulti", req_id))
        script = (
            self.positions_script[self._pos_index]
            if self._pos_index < len(self.positions_script)
            else []
        )
        self._pos_index += 1
        if script is None:
            return  # deliver nothing -> the collector times out
        for account_name, symbol, qty, avg in script:
            self.positionMulti(  # type: ignore[attr-defined]
                req_id, account_name, "", _Contract(symbol), Decimal(str(qty)), avg
            )
        self.positionMultiEnd(req_id)  # type: ignore[attr-defined]

    def cancelPositionsMulti(self, req_id: int) -> None:  # noqa: N802
        self.cancel_positions_multi_ids.append(req_id)
        self.calls.append(("cancelPositionsMulti", req_id))

    def _deliver_open_orders(self, label: str) -> None:
        script = (
            self.open_orders_script[self._oo_index]
            if self._oo_index < len(self.open_orders_script)
            else []
        )
        self._oo_index += 1
        self.calls.append((label, None))
        if script is None:
            return  # no end marker -> timeout
        for account_name, symbol, status in script:
            self.openOrder(  # type: ignore[attr-defined]
                900000, _Contract(symbol), _Order(account_name), _OrderState(status)
            )
        self.openOrderEnd()  # type: ignore[attr-defined]

    def reqAllOpenOrders(self) -> None:  # noqa: N802
        self._deliver_open_orders("reqAllOpenOrders")

    def reqOpenOrders(self) -> None:  # noqa: N802
        self._deliver_open_orders("reqOpenOrders")

    def isConnected(self) -> bool:  # noqa: N802
        return False

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))


def _install_stub_ibapi(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("ibapi")
    package.__path__ = []
    modules = {
        "ibapi": package,
        "ibapi.client": types.ModuleType("ibapi.client"),
        "ibapi.contract": types.ModuleType("ibapi.contract"),
        "ibapi.order": types.ModuleType("ibapi.order"),
        "ibapi.order_cancel": types.ModuleType("ibapi.order_cancel"),
        "ibapi.wrapper": types.ModuleType("ibapi.wrapper"),
    }
    modules["ibapi.client"].EClient = _StubEClient  # type: ignore[attr-defined]
    modules["ibapi.contract"].Contract = _Contract  # type: ignore[attr-defined]
    modules["ibapi.order"].Order = type("Order", (), {})  # type: ignore[attr-defined]
    modules["ibapi.order_cancel"].OrderCancel = type("OrderCancel", (), {})  # type: ignore[attr-defined]
    modules["ibapi.wrapper"].EWrapper = type("EWrapper", (), {})  # type: ignore[attr-defined]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


ACCOUNT = "DU" + "1234567"


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    t: Any = _build_ibapi_paper_transport(timeout_seconds=0.2)
    t._app.api_client_id = 1
    yield t


# == positions: repeatable on one session ====================================


def test_two_empty_positions_reads_both_succeed(transport: Any) -> None:
    """THE round-10 regression: the SECOND read must not time out."""
    transport._app.positions_script = [[], []]

    first = transport.positions(ACCOUNT)
    second = transport.positions(ACCOUNT)

    assert first == ()
    assert second == ()
    ids = transport._app.positions_multi_ids
    assert len(set(ids)) == 2, f"distinct reqPositionsMulti ids, got {ids}"


def test_three_reads_all_succeed_with_distinct_ids(transport: Any) -> None:
    transport._app.positions_script = [[], [], []]

    for _ in range(3):
        assert transport.positions(ACCOUNT) == ()

    ids = transport._app.positions_multi_ids
    assert len(set(ids)) == 3


def test_a_finite_read_then_an_empty_read(transport: Any) -> None:
    transport._app.positions_script = [
        [(ACCOUNT, "MSFT", 10.0, 400.0)],
        [],
    ]

    first = transport.positions(ACCOUNT)
    second = transport.positions(ACCOUNT)

    assert [p.symbol for p in first] == ["MSFT"]
    assert second == ()


def test_a_late_positions_callback_cannot_enter_the_next_read(transport: Any) -> None:
    transport._app.positions_script = [[], []]

    transport.positions(ACCOUNT)
    first_id = transport._app.positions_multi_ids[0]
    # A late callback for the FIRST (finished, retired) request.
    transport._app.positionMulti(
        first_id, ACCOUNT, "", _Contract("LATE"), Decimal("99"), 1.0
    )

    second = transport.positions(ACCOUNT)

    assert second == (), "the late #1 callback must not appear in read #2"
    assert all(p.symbol != "LATE" for p in second)


def test_every_positions_read_is_cancelled(transport: Any) -> None:
    transport._app.positions_script = [[], []]

    transport.positions(ACCOUNT)
    transport.positions(ACCOUNT)

    assert transport._app.cancel_positions_multi_ids == transport._app.positions_multi_ids


def test_a_positions_timeout_still_cancels_and_the_next_read_works(
    transport: Any,
) -> None:
    """Item 5: a timeout must still cancel, and must not poison the next read."""
    transport._app.positions_script = [None, []]  # first delivers nothing

    with pytest.raises(Exception):  # noqa: B017,PT011 - BrokerPortError timeout
        transport.positions(ACCOUNT)
    first_id = transport._app.positions_multi_ids[0]
    assert first_id in transport._app.cancel_positions_multi_ids

    second = transport.positions(ACCOUNT)
    assert second == (), "a prior timeout must not break the next read"


def test_a_synchronous_reqpositionsmulti_raise_still_cancels(transport: Any) -> None:
    """A sync failure does not prove nothing reached the gateway."""
    issued: list[int] = []

    def _raising(req_id: int, account: str, model_code: str) -> None:
        issued.append(req_id)
        transport._app.positions_multi_ids.append(req_id)
        raise RuntimeError("reqPositionsMulti blew up")

    transport._app.reqPositionsMulti = _raising

    with pytest.raises(Exception):  # noqa: B017,PT011
        transport.positions(ACCOUNT)

    assert issued[0] in transport._app.cancel_positions_multi_ids


# == open-order snapshots: repeatable on one session =========================


def test_two_global_open_order_snapshots_both_succeed(transport: Any) -> None:
    transport._app.open_orders_script = [[], []]

    first = transport.open_order_snapshot(ACCOUNT)
    second = transport.open_order_snapshot(ACCOUNT)

    assert first == ()
    assert second == ()


def test_two_same_client_snapshots_both_succeed(transport: Any) -> None:
    transport._app.open_orders_script = [[], []]

    assert transport.same_client_open_orders(ACCOUNT) == ()
    assert transport.same_client_open_orders(ACCOUNT) == ()


def test_global_then_same_client(transport: Any) -> None:
    transport._app.open_orders_script = [[], []]

    assert transport.open_order_snapshot(ACCOUNT) == ()
    assert transport.same_client_open_orders(ACCOUNT) == ()


def test_same_client_then_global(transport: Any) -> None:
    transport._app.open_orders_script = [[], []]

    assert transport.same_client_open_orders(ACCOUNT) == ()
    assert transport.open_order_snapshot(ACCOUNT) == ()


def test_first_snapshot_empty_second_has_an_order(transport: Any) -> None:
    transport._app.open_orders_script = [[], [(ACCOUNT, "AAPL", "Submitted")]]

    first = transport.open_order_snapshot(ACCOUNT)
    second = transport.open_order_snapshot(ACCOUNT)

    assert first == ()
    assert [o.symbol for o in second] == ["AAPL"]


def test_a_stale_open_order_end_cannot_terminate_a_later_snapshot(
    transport: Any,
) -> None:
    """Each snapshot has its own generation, so an end marker from a finished
    generation lands on a retired stream, not the current one."""
    transport._app.open_orders_script = [[], []]

    transport.open_order_snapshot(ACCOUNT)
    # A stale end marker arrives after the first generation retired. It must not
    # satisfy the next snapshot.
    from trading_broker_ibkr.callback_router import OPEN_ORDERS_STREAM

    stale_stream = f"{OPEN_ORDERS_STREAM}:1"
    transport._app.router.publish(stale_stream, "open_order_end", None)

    # The second snapshot still completes on its own (generation 2) callbacks.
    assert transport.open_order_snapshot(ACCOUNT) == ()
    assert transport._app._open_orders_generation >= 2
