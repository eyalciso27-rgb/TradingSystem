"""``session_pnl`` must be repeatable, and must not leak router streams.

The third owner-host run returned session P&L 0.0 on the first run and
unavailable on the second. "No P&L permission" cannot explain a real value
followed by no value, so repeatability was never actually established -- and the
code had a concrete lifecycle gap (below) that leaked a router stream on every
call, which is enough on its own to justify closing it.

``session_pnl`` waits on ``router.take(request_stream(req_id))`` and cancels
``reqPnL`` in its ``finally`` -- but, unlike ``_collect``, it did not
``router.release(...)`` the stream afterwards. A ``pnl`` update arriving after
``cancelPnL`` was then neither archived nor cleared: the waiter's deque lingered
in the router for the life of the process, one leaked entry per call.

These tests drive the REAL ``_IbApiPaperTransport`` and the REAL
``CallbackRouter`` over a stub SDK that supports ``reqPnL``/``cancelPnL``.
Nothing connects to IBKR.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from trading_broker_core.port import SessionPnlStatus


class _PnlStubEClient:
    """A stub SDK client that records reqPnL/cancelPnL and delivers pnl updates.

    ``reqPnL`` delivers the next scripted daily value synchronously through the
    app's own ``pnl`` EWrapper method, so the production routing runs. A ``None``
    scripted value delivers nothing (the gateway serving no update -> timeout).
    """

    def __init__(self, wrapper: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.req_pnl_ids: list[int] = []
        self.cancel_pnl_ids: list[int] = []
        self.pnl_script: list[float | None] = []
        self._index = 0

    def reqPnL(self, req_id: int, account: str, model_code: str) -> None:  # noqa: N802
        self.req_pnl_ids.append(req_id)
        self.calls.append(("reqPnL", req_id))
        if self._index < len(self.pnl_script):
            value = self.pnl_script[self._index]
            self._index += 1
            if value is not None:
                # ``self`` is the _PaperApp (EWrapper+EClient); pnl publishes to
                # the real router under this request's stream.
                self.pnl(req_id, value, 0.0, 0.0)  # type: ignore[attr-defined]

    def cancelPnL(self, req_id: int) -> None:  # noqa: N802
        self.cancel_pnl_ids.append(req_id)
        self.calls.append(("cancelPnL", req_id))

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
    modules["ibapi.client"].EClient = _PnlStubEClient  # type: ignore[attr-defined]
    modules["ibapi.contract"].Contract = type("Contract", (), {})  # type: ignore[attr-defined]
    modules["ibapi.order"].Order = type("Order", (), {})  # type: ignore[attr-defined]
    modules["ibapi.order_cancel"].OrderCancel = type(  # type: ignore[attr-defined]
        "OrderCancel", (), {}
    )
    modules["ibapi.wrapper"].EWrapper = type("EWrapper", (), {})  # type: ignore[attr-defined]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    yield _build_ibapi_paper_transport(
        timeout_seconds=0.2, session_pnl_timeout_seconds=0.2
    )


def _stream(req_id: int) -> str:
    from trading_broker_ibkr.callback_router import request_stream

    return request_stream(req_id)


def test_two_session_pnl_calls_each_return_their_own_value(transport: Any) -> None:
    """Repeatable: a finite value, then another, on distinct request ids."""
    transport._app.pnl_script = [12.50, 34.25]

    first = transport.session_pnl("DU1234567")
    second = transport.session_pnl("DU1234567")

    assert first.status is SessionPnlStatus.OK and first.value == Decimal("12.50")
    assert second.status is SessionPnlStatus.OK and second.value == Decimal("34.25")
    ids = transport._app.req_pnl_ids
    assert len(set(ids)) == 2, f"distinct reqPnL ids, got {ids}"


def test_each_session_pnl_retires_its_router_stream(transport: Any) -> None:
    """The leak this round closes: the stream KEY is removed, not just emptied.

    ``pending()`` reads 0 either way once ``take`` consumes the value, so it
    cannot see the leak. ``release`` pops the key entirely; without it the empty
    deque lingers in ``_streams`` for the life of the process, one per call.
    """
    transport._app.pnl_script = [12.50, 34.25]

    transport.session_pnl("DU1234567")
    first_id = transport._app.req_pnl_ids[0]
    # White-box: the stream key must be gone from the router entirely.
    assert _stream(first_id) not in transport._app.router._streams, (
        "the first request's stream lingers in the router"
    )

    transport.session_pnl("DU1234567")
    second_id = transport._app.req_pnl_ids[1]
    assert _stream(second_id) not in transport._app.router._streams


def test_a_late_update_from_the_first_call_cannot_reach_the_second(
    transport: Any,
) -> None:
    """reqId isolation: a late #1 update lands on #1's stream, never #2's, and
    #2 returns its own value."""
    transport._app.pnl_script = [12.50, 34.25]

    transport.session_pnl("DU1234567")
    first_id = transport._app.req_pnl_ids[0]
    # A pnl update for the FIRST request arriving after it finished. Streams are
    # keyed by reqId, so this cannot even address the second request.
    transport._app.pnl(first_id, 999.99, 0.0, 0.0)

    second = transport.session_pnl("DU1234567")
    second_id = transport._app.req_pnl_ids[1]

    assert second.value == Decimal("34.25"), "the second call served its own value"
    assert first_id != second_id
    assert transport._app.router.pending(_stream(second_id)) == 0, (
        "the second request's stream is clean; the late #1 update never reached it"
    )


def test_a_buffered_extra_update_is_archived_on_release(transport: Any) -> None:
    """An update that arrives BEFORE release (a second tick on the same
    subscription) is archived by release(), never silently dropped."""
    transport._app.pnl_script = [12.50]

    # Deliver a SECOND update for the same request before session_pnl releases:
    # reqPnL delivers the first; a stub that pushes an extra one models a
    # subscription that ticked twice before we cancelled.
    original = transport._app.reqPnL

    def _req_pnl_twice(req_id: int, account: str, model_code: str) -> None:
        original(req_id, account, model_code)
        transport._app.pnl(req_id, 12.51, 0.0, 0.0)  # a second tick, still buffered

    transport._app.reqPnL = _req_pnl_twice

    value = transport.session_pnl("DU1234567")

    assert value.value == Decimal("12.50"), "the first update is the daily value taken"
    archived = transport._app.router.archived()
    assert any(env.payload == 12.51 for env in archived), archived


def test_a_first_timeout_does_not_stop_the_second_call(transport: Any) -> None:
    """First request gets no update (None -> timeout); the second still works.

    A momentary no-update must not be sticky."""
    transport._app.pnl_script = [None, 55.75]

    first = transport.session_pnl("DU1234567")
    second = transport.session_pnl("DU1234567")

    assert first.status is SessionPnlStatus.TIMEOUT, "no update is TIMEOUT, not a value"
    assert first.value is None
    assert second.status is SessionPnlStatus.OK and second.value == Decimal("55.75")


def test_an_unavailable_pnl_is_never_zero(transport: Any) -> None:
    """UNKNOWN stays None and must fail closed -- never a fabricated 0."""
    transport._app.pnl_script = [None]

    observation = transport.session_pnl("DU1234567")
    assert observation.status is SessionPnlStatus.TIMEOUT
    assert observation.value is None


def test_a_nan_daily_value_is_none_not_zero(transport: Any) -> None:
    """IBKR's nan 'not available' for a float field is None, never 0."""
    transport._app.pnl_script = [float("nan")]

    observation = transport.session_pnl("DU1234567")
    assert observation.status is SessionPnlStatus.UNSET
    assert observation.value is None


def test_every_request_is_cancelled_and_released_even_on_timeout(
    transport: Any,
) -> None:
    transport._app.pnl_script = [None]

    transport.session_pnl("DU1234567")

    req_id = transport._app.req_pnl_ids[0]
    assert req_id in transport._app.cancel_pnl_ids, "reqPnL was not cancelled"
    assert transport._app.router.pending(_stream(req_id)) == 0, "stream not released"


# == request-scoped errors are reported, not hidden as "no P&L" ===============


def test_a_request_scoped_error_is_reported_not_hidden_as_timeout(
    transport: Any,
) -> None:
    """The item-1 defect: a reqPnL rejection used to be ignored on the stream
    and collapsed into the timeout as 'no P&L'. It is now REQUEST_ERROR."""

    def _req_pnl_error(req_id: int, account: str, model_code: str) -> None:
        transport._app.req_pnl_ids.append(req_id)
        transport._app.error(req_id, 321, "Server error validating the request")

    transport._app.reqPnL = _req_pnl_error

    observation = transport.session_pnl("DU1234567")

    from trading_broker_core.port import SessionPnlStatus as _S

    assert observation.status is _S.REQUEST_ERROR
    assert observation.value is None
    assert "321" in observation.detail
    # The classified detail must not carry the raw broker text or an account id.
    assert "Server error" not in observation.detail
    assert "DU1234567" not in observation.detail


def test_a_request_error_does_not_wait_for_the_generic_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It must SHORT-CIRCUIT: with a long window, a rejection still returns
    quickly. This is what makes one bounded timeout honest for both uses."""
    import time as _time

    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    # A deliberately LONG window, so a slow return would be visible.
    transport: Any = _build_ibapi_paper_transport(
        timeout_seconds=0.2, session_pnl_timeout_seconds=30.0
    )

    def _req_pnl_error(req_id: int, account: str, model_code: str) -> None:
        transport._app.error(req_id, 321, "rejected")

    transport._app.reqPnL = _req_pnl_error

    started = _time.monotonic()
    observation = transport.session_pnl("DU1234567")
    elapsed = _time.monotonic() - started

    assert observation.status is SessionPnlStatus.REQUEST_ERROR
    assert elapsed < 5.0, f"a rejection waited {elapsed:.1f}s of the 30s window"


def test_an_informational_notice_is_ignored_then_pnl_is_returned(
    transport: Any,
) -> None:
    """A farm/connectivity notice on the stream is not a P&L and not an error:
    keep waiting, then serve the real value."""

    def _req_pnl_notice_then_pnl(req_id: int, account: str, model_code: str) -> None:
        transport._app.req_pnl_ids.append(req_id)
        transport._app.error(req_id, 2104, "Market data farm connection is OK")
        transport._app.pnl(req_id, 42.0, 0.0, 0.0)

    transport._app.reqPnL = _req_pnl_notice_then_pnl

    observation = transport.session_pnl("DU1234567")

    from trading_broker_core.port import SessionPnlStatus as _S

    assert observation.status is _S.OK
    assert observation.value == Decimal("42")


def test_a_first_request_error_does_not_stop_a_second_finite_pnl(
    transport: Any,
) -> None:
    calls = {"n": 0}

    def _req(req_id: int, account: str, model_code: str) -> None:
        transport._app.req_pnl_ids.append(req_id)
        calls["n"] += 1
        if calls["n"] == 1:
            transport._app.error(req_id, 321, "rejected")
        else:
            transport._app.pnl(req_id, 7.0, 0.0, 0.0)

    transport._app.reqPnL = _req

    first = transport.session_pnl("DU1234567")
    second = transport.session_pnl("DU1234567")

    from trading_broker_core.port import SessionPnlStatus as _S

    assert first.status is _S.REQUEST_ERROR
    assert second.status is _S.OK and second.value == Decimal("7")
