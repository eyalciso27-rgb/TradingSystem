"""D-CANCEL-01/02/03: the IBKR cancellation transport, against a stub SDK.

The owner-run reached the real broker order path and the CANCELLATION FAILED:
the adapter called ``cancelOrder(order_id, "")`` while the installed
``EClient.cancelOrder(orderId, orderCancel: ibapi.order_cancel.OrderCancel)``
reads ``orderCancel.manualOrderCancelTime`` / ``.extOperator`` /
``.manualOrderIndicator``. Passing a string therefore raised before a valid
cancellation request was ever encoded, the order stayed ACKNOWLEDGED, and the
owner had to cancel by hand.

These tests drive the REAL host transport with a stubbed ``ibapi`` package
installed into ``sys.modules``. Nothing here connects to IBKR, opens a socket
or sends a Paper/Live order: the stub records what the adapter asked for and
replays broker callbacks through the adapter's own EWrapper methods, so the
production routing, correlation and classification code is what runs.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from trading_broker_core.port import BrokerErrorCode, BrokerPortError

# -- stub ibapi -------------------------------------------------------------


class _StubOrderCancel:
    """Mirrors ibapi.order_cancel.OrderCancel's cancellation fields."""

    def __init__(self) -> None:
        self.manualOrderCancelTime = ""  # noqa: N815 - mirrors the SDK name
        self.extOperator = ""  # noqa: N815 - mirrors the SDK name
        self.manualOrderIndicator = 0  # noqa: N815 - mirrors the SDK name


class _StubContract:
    def __init__(self) -> None:
        self.symbol = ""
        self.secType = ""  # noqa: N815 - mirrors the SDK name
        self.exchange = ""
        self.currency = ""


class _StubOrder:
    def __init__(self) -> None:
        self.account = ""
        self.action = ""
        self.orderType = ""  # noqa: N815 - mirrors the SDK name
        self.tif = ""
        self.totalQuantity = Decimal(0)  # noqa: N815 - mirrors the SDK name
        self.orderRef = ""  # noqa: N815 - mirrors the SDK name
        self.transmit = False
        self.outsideRth = False  # noqa: N815 - mirrors the SDK name
        self.eTradeOnly = False  # noqa: N815 - mirrors the SDK name
        self.firmQuoteOnly = False  # noqa: N815 - mirrors the SDK name


class _StubEWrapper:
    pass


class _StubEClient:
    """Records API calls and replays scripted callbacks through the wrapper.

    ``cancelOrder`` deliberately mirrors the installed SDK: it READS the
    ``OrderCancel`` attributes, so passing a string (the defect) raises exactly
    as the real client does.
    """

    def __init__(self, wrapper: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        #: broker_order_id -> callbacks replayed when cancelOrder is called
        self.cancel_script: dict[int, list[tuple[str, tuple[Any, ...]]]] = {}
        #: broker_order_id -> callbacks replayed when placeOrder is called
        self.submit_script: dict[int, list[tuple[str, tuple[Any, ...]]]] = {}

    # -- calls the transport makes ------------------------------------------
    def cancelOrder(self, orderId: int, orderCancel: object) -> None:  # noqa: N802, N803
        # Exactly what the installed EClient does — a str has none of these.
        _ = (
            orderCancel.manualOrderCancelTime,  # type: ignore[attr-defined]
            orderCancel.extOperator,  # type: ignore[attr-defined]
            orderCancel.manualOrderIndicator,  # type: ignore[attr-defined]
        )
        self.calls.append(("cancelOrder", (orderId, orderCancel)))
        self._replay(self.cancel_script.get(orderId, []))

    def placeOrder(self, orderId: int, contract: object, order: object) -> None:  # noqa: N802, N803
        self.calls.append(("placeOrder", (orderId, contract, order)))
        self._replay(self.submit_script.get(orderId, []))

    def isConnected(self) -> bool:  # noqa: N802
        return False

    def disconnect(self) -> None:
        self.calls.append(("disconnect", ()))

    def _replay(self, script: list[tuple[str, tuple[Any, ...]]]) -> None:
        # Deliver through the adapter's own EWrapper methods so the production
        # routing/normalization code is what actually runs.
        for method, args in script:
            getattr(self, method)(*args)


def _install_stub_ibapi(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("ibapi")
    package.__path__ = []  # marks it as a package so submodules resolve
    modules = {
        "ibapi": package,
        "ibapi.client": types.ModuleType("ibapi.client"),
        "ibapi.contract": types.ModuleType("ibapi.contract"),
        "ibapi.order": types.ModuleType("ibapi.order"),
        "ibapi.order_cancel": types.ModuleType("ibapi.order_cancel"),
        "ibapi.wrapper": types.ModuleType("ibapi.wrapper"),
    }
    for module_name, attribute, stub in (
        ("ibapi.client", "EClient", _StubEClient),
        ("ibapi.contract", "Contract", _StubContract),
        ("ibapi.order", "Order", _StubOrder),
        ("ibapi.order_cancel", "OrderCancel", _StubOrderCancel),
        ("ibapi.wrapper", "EWrapper", _StubEWrapper),
    ):
        setattr(modules[module_name], attribute, stub)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The REAL host transport, wired to the stub SDK. Never touches IBKR."""
    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    # A short timeout keeps the "no confirmation" cases fast and deterministic.
    yield _build_ibapi_paper_transport(timeout_seconds=0.05)


def _app(transport: Any) -> Any:
    return transport._app


# -- A. the modern cancellation call ----------------------------------------


def test_cancel_passes_an_ordercancel_instance_not_a_string(transport: Any) -> None:
    """The exact defect from the owner-run: a str raised before encoding."""
    app = _app(transport)
    app.cancel_script[7] = [("orderStatus", (7, "Cancelled"))]

    assert transport.cancel(7) == "Cancelled"

    (name, (order_id, order_cancel)), = [c for c in app.calls if c[0] == "cancelOrder"]
    assert name == "cancelOrder"
    # The exact broker order id, and a real OrderCancel — not "" and not None.
    assert order_id == 7
    assert isinstance(order_cancel, _StubOrderCancel)
    assert not isinstance(order_cancel, str)


def test_a_string_argument_would_still_raise_in_the_stub() -> None:
    """Guards the guard: the stub reproduces the real SDK's failure mode."""
    client = _StubEClient(wrapper=None)
    with pytest.raises(AttributeError):
        client.cancelOrder(7, "")


def test_missing_order_cancel_surface_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SDK without ibapi.order_cancel must fail closed, never guess a shim."""
    _install_stub_ibapi(monkeypatch)
    monkeypatch.delitem(sys.modules, "ibapi.order_cancel")
    monkeypatch.setitem(sys.modules, "ibapi", types.ModuleType("ibapi"))
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    with pytest.raises(BrokerPortError) as error:
        _build_ibapi_paper_transport(timeout_seconds=0.05)
    assert error.value.code is BrokerErrorCode.BROKER_UNHEALTHY


# -- B. same-process cancellation -------------------------------------------


def test_stale_acknowledgements_are_waited_past(transport: Any) -> None:
    """A leftover Submitted/PreSubmitted is NEVER cancellation confirmation."""
    app = _app(transport)
    app.cancel_script[11] = [
        ("orderStatus", (11, "Submitted")),  # stale
        ("orderStatus", (11, "PreSubmitted")),  # stale
        ("orderStatus", (11, "PendingCancel")),  # accepted, not final
        ("orderStatus", (11, "Cancelled")),  # confirmed
    ]
    assert transport.cancel(11) == "Cancelled"


def test_api_cancelled_confirms(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[12] = [
        ("orderStatus", (12, "Submitted")),
        ("orderStatus", (12, "ApiCancelled")),
    ]
    assert transport.cancel(12) == "ApiCancelled"


def test_a_stale_ack_alone_never_confirms_a_cancellation(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[13] = [("orderStatus", (13, "Submitted"))]
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(13)
    # Unresolved, not "cancelled": reconciliation must settle it.
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE


def test_cancel_sends_exactly_one_request(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[14] = [
        ("orderStatus", (14, "PendingCancel")),
        ("orderStatus", (14, "Cancelled")),
    ]
    transport.cancel(14)
    assert len([c for c in app.calls if c[0] == "cancelOrder"]) == 1


def test_callbacks_for_another_order_never_confirm_this_cancellation(
    transport: Any,
) -> None:
    """Correlation is by broker order id, not by arrival order."""
    app = _app(transport)
    app.cancel_script[15] = [
        ("orderStatus", (99, "Cancelled")),  # a DIFFERENT order
        ("orderStatus", (15, "Cancelled")),
    ]
    assert transport.cancel(15) == "Cancelled"
    # The other order's status stayed on its own stream, unconsumed.
    from trading_broker_ibkr.callback_router import order_stream

    assert app.router.pending(order_stream(99)) == 1


# -- C. cancellation failures -----------------------------------------------


def test_order_error_surfaces_code_and_sanitized_text(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[21] = [
        ("orderStatus", (21, "Submitted")),
        ("error", (21, 0, 201, "Order rejected - reason: insufficient margin")),
    ]
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(21)
    message = str(error.value)
    assert "201" in message
    assert "insufficient margin" in message
    # Deterministic pre-acceptance reject => terminal, never a retry loop.
    assert error.value.code is BrokerErrorCode.BROKER_REJECTED


def test_advanced_rejection_is_preserved(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[22] = [("error", (22, 0, 321, "Error validating request", "risk-block"))]
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(22)
    assert "advanced-rejection" in str(error.value)
    assert "risk-block" in str(error.value)


def test_error_202_is_a_cancellation_confirmation(transport: Any) -> None:
    """IBKR reports "Order Cancelled" as error 202 — success, not failure."""
    app = _app(transport)
    app.cancel_script[23] = [("error", (23, 0, 202, "Order Cancelled - reason:"))]
    assert transport.cancel(23) == "Cancelled"


@pytest.mark.parametrize("code", [2104, 2106, 2107, 2158])
def test_informational_farm_messages_do_not_fail_a_cancellation(
    transport: Any, code: int
) -> None:
    app = _app(transport)
    app.cancel_script[24] = [
        ("error", (24, 0, code, "Market data farm connection is OK")),
        ("orderStatus", (24, "Cancelled")),
    ]
    assert transport.cancel(24) == "Cancelled"


def test_ambiguous_order_error_stays_unknown(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[25] = [("error", (25, 0, 10147, "OrderId not found"))]
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(25)
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE


def test_timeout_maps_to_unknown_and_requires_reconciliation(transport: Any) -> None:
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(26)  # nothing scripted: the broker says nothing
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE
    assert "reconcile" in str(error.value)


def test_a_fill_during_cancellation_is_exposure_not_success(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[27] = [("orderStatus", (27, "Filled"))]
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(27)
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE
    assert "exposure" in str(error.value)


def test_a_partial_fill_during_cancellation_is_exposure_not_success(
    transport: Any,
) -> None:
    app = _app(transport)
    app.cancel_script[28] = [("orderStatus", (28, "PartiallyFilled"))]
    with pytest.raises(BrokerPortError):
        transport.cancel(28)


def test_inactive_is_a_deterministic_terminal_failure(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[29] = [("orderStatus", (29, "Inactive"))]
    with pytest.raises(BrokerPortError) as error:
        transport.cancel(29)
    assert error.value.code is BrokerErrorCode.BROKER_REJECTED


def test_failed_cancellation_never_retries(transport: Any) -> None:
    app = _app(transport)
    app.cancel_script[30] = [("error", (30, 0, 201, "Order rejected"))]
    with pytest.raises(BrokerPortError):
        transport.cancel(30)
    assert len([c for c in app.calls if c[0] == "cancelOrder"]) == 1


# -- submit-side error correlation ------------------------------------------


def test_submit_errors_are_correlated_and_informational_ones_ignored(
    transport: Any,
) -> None:
    from trading_broker_ibkr.callback_router import order_stream

    app = _app(transport)
    app.router.publish(order_stream(41), "error", (41, 2104, "farm OK", ""))
    app.router.publish(order_stream(41), "status", (41, "Submitted"))
    assert transport._await_status(41) == "Submitted"

    app.router.publish(order_stream(42), "error", (42, 201, "Order rejected", ""))
    with pytest.raises(BrokerPortError) as error:
        transport._await_status(42)
    assert error.value.code is BrokerErrorCode.BROKER_REJECTED
    assert "201" in str(error.value)


def test_connectivity_notices_never_reach_an_order_waiter(transport: Any) -> None:
    """reqId -1 farm notices go to diagnostics, never onto an order stream."""
    from trading_broker_ibkr.callback_router import DIAGNOSTICS_STREAM, order_stream

    app = _app(transport)
    app.error(-1, 0, 2104, "Market data farm connection is OK")
    assert app.router.pending(order_stream(-1)) == 0
    assert app.router.pending(DIAGNOSTICS_STREAM) == 1


def test_an_unparseable_error_callback_is_dropped_not_raised(transport: Any) -> None:
    """A stray callback shape must never crash the reader thread."""
    app = _app(transport)
    app.error(object(), object(), object())  # unrecognised shape
    # No exception, and nothing bogus was routed onto an order stream.
    assert app.router.archived() == ()
