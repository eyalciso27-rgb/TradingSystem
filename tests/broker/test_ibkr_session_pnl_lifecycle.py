"""``reqPnL`` is a PERSISTENT subscription, cached with a freshness timestamp.

The real owner runs showed reqPnL is intermittent: one run returned a finite
value (`0.0`, `PNL_OK`), a later run timed out. The previous one-shot design
subscribed and cancelled on EVERY ``get_account_snapshot``, so each execution
decision blocked for the full window and a momentary gap read as "no P&L".

The connection now owns ONE subscription: started once, kept active, its
``pnl`` callbacks (~1/s) caching the latest daily value and the time it
arrived. ``get_account_snapshot`` consumes the LATEST cached callback, subject
to a bounded freshness window -- a value older than the window is ``PNL_STALE``
and fails closed, never used as if current. UNKNOWN in every form stays None.

These tests drive the REAL ``_IbApiPaperTransport``, the real ``_PaperApp``
callbacks and the real cache over a stub SDK, with an injectable monotonic
clock so freshness is deterministic. Nothing connects to IBKR.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from trading_broker_core.port import SessionPnlStatus

ACCOUNT = "DU" + "1234567"
DBL_MAX = sys.float_info.max


class _PnlStubEClient:
    """Records reqPnL/cancelPnL. ``reqPnL`` optionally delivers ONE initial
    scripted value synchronously (or nothing, to model no callback yet). Later
    callbacks are delivered by the test via ``transport._app.pnl(...)``."""

    def __init__(self, wrapper: object) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.req_pnl_ids: list[int] = []
        self.cancel_pnl_ids: list[int] = []
        #: value delivered on the FIRST reqPnL, or None for no initial callback.
        self.initial: float | None = None

    def reqPnL(self, req_id: int, account: str, model_code: str) -> None:  # noqa: N802
        self.req_pnl_ids.append(req_id)
        self.calls.append(("reqPnL", req_id))
        if self.initial is not None:
            self.pnl(req_id, self.initial, 0.0, 0.0)  # type: ignore[attr-defined]

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
    modules["ibapi.order_cancel"].OrderCancel = type("OrderCancel", (), {})  # type: ignore[attr-defined]
    modules["ibapi.wrapper"].EWrapper = type("EWrapper", (), {})  # type: ignore[attr-defined]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


class _Clock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> Any:
    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    def _factory(**kw: Any) -> Any:
        return _build_ibapi_paper_transport(
            timeout_seconds=0.2,
            session_pnl_timeout_seconds=0.2,
            session_pnl_freshness_seconds=30.0,
            monotonic_clock=clock,
            **kw,
        )

    return _factory


@pytest.fixture
def transport(build: Any) -> Iterator[Any]:
    yield build()


# == one persistent subscription =============================================


def test_first_observation_starts_exactly_one_reqpnl(transport: Any) -> None:
    transport._app.initial = 12.5

    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.OK
    assert observation.value == Decimal("12.5")
    assert observation.observed_at is not None
    assert transport._app.req_pnl_ids == [transport._pnl_req_id]


def test_a_second_snapshot_reuses_the_subscription_without_resubscribing(
    transport: Any,
) -> None:
    transport._app.initial = 12.5

    first = transport.session_pnl(ACCOUNT)
    second = transport.session_pnl(ACCOUNT)

    assert first.value == Decimal("12.5")
    assert second.value == Decimal("12.5")
    # Exactly ONE reqPnL, and NO cancelPnL between snapshots.
    assert len(transport._app.req_pnl_ids) == 1, transport._app.req_pnl_ids
    assert transport._app.cancel_pnl_ids == []


def test_a_later_callback_updates_the_cached_value_and_timestamp(
    transport: Any, clock: _Clock
) -> None:
    transport._app.initial = 1.0
    first = transport.session_pnl(ACCOUNT)
    first_ts = first.observed_at

    clock.advance(1.0)
    transport._app.pnl(transport._pnl_req_id, 2.0, 0.0, 0.0)  # a fresh callback
    second = transport.session_pnl(ACCOUNT)

    assert first.value == Decimal("1")
    assert second.value == Decimal("2")
    assert second.observed_at is not None and second.observed_at != first_ts


def test_the_timestamp_updates_only_on_a_real_callback(transport: Any) -> None:
    transport._app.initial = 5.0
    first = transport.session_pnl(ACCOUNT)
    second = transport.session_pnl(ACCOUNT)  # no new callback in between

    assert second.observed_at == first.observed_at, "timestamp moved without a callback"


# == freshness ===============================================================


def test_a_fresh_value_is_accepted(transport: Any, clock: _Clock) -> None:
    transport._app.initial = 7.0
    transport.session_pnl(ACCOUNT)

    clock.advance(29.0)  # still inside the 30s window
    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.OK
    assert observation.value == Decimal("7")


def test_a_stale_value_fails_closed(transport: Any, clock: _Clock) -> None:
    transport._app.initial = 7.0
    transport.session_pnl(ACCOUNT)

    clock.advance(31.0)  # past the 30s window
    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.STALE
    assert observation.value is None
    assert "ago" in observation.detail


def test_a_fresh_callback_recovers_from_stale(transport: Any, clock: _Clock) -> None:
    transport._app.initial = 7.0
    transport.session_pnl(ACCOUNT)
    clock.advance(31.0)
    assert transport.session_pnl(ACCOUNT).status is SessionPnlStatus.STALE

    transport._app.pnl(transport._pnl_req_id, 8.0, 0.0, 0.0)  # fresh update
    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.OK
    assert observation.value == Decimal("8")


# == initial timeout leaves the subscription alive ===========================


def test_initial_no_callback_times_out_but_keeps_the_subscription(
    transport: Any,
) -> None:
    transport._app.initial = None  # no initial callback -> initial wait expires

    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.TIMEOUT
    assert observation.value is None
    # The subscription is NOT cancelled and NOT resubscribed.
    assert len(transport._app.req_pnl_ids) == 1
    assert transport._app.cancel_pnl_ids == []


def test_a_callback_after_the_initial_timeout_makes_a_later_snapshot_ok(
    transport: Any,
) -> None:
    transport._app.initial = None
    assert transport.session_pnl(ACCOUNT).status is SessionPnlStatus.TIMEOUT

    # A pnl callback arrives late, on the SAME still-active subscription.
    transport._app.pnl(transport._pnl_req_id, 3.5, 0.0, 0.0)
    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.OK
    assert observation.value == Decimal("3.5")
    assert len(transport._app.req_pnl_ids) == 1, "must not resubscribe"


# == UNSET / errors ==========================================================


def test_a_nan_callback_is_unset(transport: Any) -> None:
    transport._app.initial = float("nan")
    observation = transport.session_pnl(ACCOUNT)
    assert observation.status is SessionPnlStatus.UNSET
    assert observation.value is None


def test_a_dbl_max_callback_is_unset(transport: Any) -> None:
    transport._app.initial = DBL_MAX
    observation = transport.session_pnl(ACCOUNT)
    assert observation.status is SessionPnlStatus.UNSET
    assert observation.value is None
    assert "unset" in observation.detail.lower()


def test_a_request_scoped_error_is_reported(transport: Any) -> None:
    def _req_pnl_error(req_id: int, account: str, model_code: str) -> None:
        transport._app.req_pnl_ids.append(req_id)
        transport._app.error(req_id, 321, "Server error validating the request")

    transport._app.reqPnL = _req_pnl_error

    observation = transport.session_pnl(ACCOUNT)

    assert observation.status is SessionPnlStatus.REQUEST_ERROR
    assert observation.value is None
    assert "321" in observation.detail
    assert "Server error" not in observation.detail
    assert ACCOUNT not in observation.detail


# == disconnect / reconnect ==================================================


def test_disconnect_cancels_the_active_subscription_exactly_once(
    transport: Any,
) -> None:
    transport._app.initial = 1.0
    transport.session_pnl(ACCOUNT)
    req_id = transport._pnl_req_id

    transport.disconnect()

    assert transport._app.cancel_pnl_ids == [req_id]
    assert transport._pnl_req_id is None


def test_reconnect_starts_a_new_subscription_with_a_different_reqid(
    build: Any,
) -> None:
    first = build()
    first._app.initial = 1.0
    first.session_pnl(ACCOUNT)
    first_id = first._pnl_req_id
    first.disconnect()

    second = build()  # a reconnect is a NEW transport
    second._app.initial = 2.0
    second.session_pnl(ACCOUNT)

    assert second._pnl_req_id != first_id


def test_a_callback_for_an_old_reqid_is_ignored(transport: Any) -> None:
    transport._app.initial = 1.0
    transport.session_pnl(ACCOUNT)
    active = transport._pnl_req_id

    # A callback for a DIFFERENT (old / foreign) reqId must not update the cache.
    transport._app.pnl(active + 999, 99.0, 0.0, 0.0)
    observation = transport.session_pnl(ACCOUNT)

    assert observation.value == Decimal("1"), "an old-reqId callback leaked in"


def test_a_different_account_is_refused_not_served(transport: Any) -> None:
    transport._app.initial = 1.0
    transport.session_pnl(ACCOUNT)

    other = transport.session_pnl("DU" + "9999999")

    assert other.status is SessionPnlStatus.REQUEST_ERROR
    assert "another account" in other.detail


def test_repeated_snapshots_do_not_create_a_subscription_storm(
    transport: Any,
) -> None:
    transport._app.initial = 1.0
    for _ in range(20):
        transport.session_pnl(ACCOUNT)

    assert len(transport._app.req_pnl_ids) == 1
    assert transport._app.cancel_pnl_ids == []


# == concurrency: exactly one subscription under a thundering herd ============


def test_concurrent_first_calls_start_exactly_one_subscription(
    transport: Any,
) -> None:
    """N threads calling session_pnl at once must create ONE reqPnL, share it,
    and leave a single subscription for disconnect to cancel."""
    import threading

    transport._app.initial = 5.0
    n = 16
    start = threading.Barrier(n)
    results: list[Any] = []
    guard = threading.Lock()

    def worker() -> None:
        start.wait(timeout=5)
        observation = transport.session_pnl(ACCOUNT)
        with guard:
            results.append(observation)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(transport._app.req_pnl_ids) == 1, (
        f"a subscription storm: {transport._app.req_pnl_ids}"
    )
    assert all(o.value == Decimal("5") for o in results), "callers saw different values"
    assert transport._pnl_req_id == transport._app.req_pnl_ids[0]

    transport.disconnect()
    assert transport._app.cancel_pnl_ids == [transport._pnl_req_id] or (
        transport._app.cancel_pnl_ids == [transport._app.req_pnl_ids[0]]
    ), transport._app.cancel_pnl_ids


# == reqId / orderId collision is attribution-safe ===========================


def test_an_order_error_that_aliases_the_pnl_reqid_never_poisons_pnl(
    transport: Any,
) -> None:
    """The collision the reviewer forced: an ORDER error whose orderId equals
    the active P&L reqId must route to the order stream (order attribution
    wins) and never become PNL_REQUEST_ERROR."""
    transport._app.initial = 3.0
    transport.session_pnl(ACCOUNT)
    pnl_req_id = transport._pnl_req_id

    # Make that exact id ALSO an issued ORDER id, then deliver an order error.
    transport._app.issue_order_id(pnl_req_id)
    transport._app.error(pnl_req_id, 201, "Order rejected")

    observation = transport.session_pnl(ACCOUNT)
    assert observation.status is SessionPnlStatus.OK, "an order error poisoned P&L"
    assert observation.value == Decimal("3")


def test_a_genuine_pnl_request_error_still_reports_request_error(
    transport: Any,
) -> None:
    """A request-scoped error for the P&L reqId (NOT an order id) is captured."""
    transport._app.initial = 3.0
    transport.session_pnl(ACCOUNT)
    pnl_req_id = transport._pnl_req_id

    transport._app.error(pnl_req_id, 321, "Server error validating the request")

    observation = transport.session_pnl(ACCOUNT)
    assert observation.status is SessionPnlStatus.REQUEST_ERROR
    assert "321" in observation.detail
