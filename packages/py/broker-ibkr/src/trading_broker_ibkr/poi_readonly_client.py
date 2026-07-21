"""Concrete read-only raw client for the POI-IBKR-READONLY session.

This supplies the ``RawReadOnlyClient`` that ``poi_readonly_session`` needs:
a real IBKR-backed transport that performs ONLY the five read-only calls
(open / current time / account summary / positions / close). It is still
part of the read-only PoI, NOT the IMP-1002 adapter — there is no
order-placing, cancellation, modification, global-cancel or exercise
surface anywhere, and no order-sending verb is referenced.

Design (ADR-0014; PoI plan v2):

- ISOLATION + TESTABILITY: all IBKR SDK use is confined to
  ``_build_ibapi_transport`` and imported lazily, so this module imports and
  is fully unit-tested without ``ibapi`` and without any network. The
  request/response orchestration is driven through a small injectable
  ``ReadOnlyTransport`` seam; tests inject a fake transport that replays the
  callback sequence synchronously — no socket is ever opened.
- READ-ONLY: the transport exposes only current-time / account-summary /
  positions requests (plus their read-only subscription teardown). The
  IBKR ``EClient`` order methods are never referenced.
- SAFETY: endpoint is re-validated here (localhost + paper port 4002; live
  port 4001 rejected) as defence in depth on top of the session gate. The
  DU-account gate and masking live in the session. Every timeout or IBKR
  error fails closed with ``PoiSessionError``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from trading_broker_ibkr.ibkr_error_callback import (
    ErrorCallbackShapeError,
    normalize_error_callback,
    normalize_message,
)
from trading_broker_ibkr.poi_readonly_session import (
    PoiSessionError,
    RawReadOnlyClient,
    ReadOnlyAccountSummary,
    _validate_endpoint,
)

READONLY_CLIENT_VERSION = "poi-ibkr-readonly-client-1.0.0"

# Infrastructure parameters for the PoI (not trading policy): a fixed API
# client id and per-request timeout. Overridable via create args.
DEFAULT_CLIENT_ID = 0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
_ACCOUNT_SUMMARY_REQ_ID = 9001
_ACCOUNT_SUMMARY_TAGS = "NetLiquidation,TotalCashValue,BuyingPower"

# IBKR convention: codes below 1100 are genuine client/connectivity errors;
# 1100-1102 are connectivity notices and >= 2000 are benign system/info
# messages (e.g. market-data farm status). Only genuine errors are fatal, so
# routine info messages do not trip the fail-closed path.
_FATAL_ERROR_CEILING = 1100


class ReadOnlyTransport(Protocol):
    """Minimal transport seam the client drives; real impl wraps the SDK."""

    def start(self, host: str, port: int, client_id: int, sink: ReadOnlySink) -> None: ...

    def stop(self) -> None: ...

    def request_current_time(self) -> None: ...

    def request_account_summary(self, req_id: int, tags: str) -> None: ...

    def cancel_account_summary(self, req_id: int) -> None: ...

    def request_positions(self) -> None: ...

    def cancel_positions(self) -> None: ...


@dataclass
class ReadOnlySink:
    """Thread-safe buffers + events that transport callbacks feed."""

    ready: threading.Event = field(default_factory=threading.Event)
    time_ready: threading.Event = field(default_factory=threading.Event)
    account_ready: threading.Event = field(default_factory=threading.Event)
    positions_ready: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    time_value: str = ""
    account_id: str = ""
    account_values: dict[str, str] = field(default_factory=dict)
    positions: list[dict[str, str]] = field(default_factory=list)
    fatal_error: str | None = None

    # -- callbacks invoked by the transport --------------------------------
    def on_ready(self) -> None:
        self.ready.set()

    def on_current_time(self, epoch_seconds: int) -> None:
        with self._lock:
            self.time_value = datetime.fromtimestamp(int(epoch_seconds), tz=UTC).isoformat()
        self.time_ready.set()

    def on_account_value(self, account: str, tag: str, value: str) -> None:
        with self._lock:
            if not self.account_id:
                self.account_id = account
            self.account_values[tag] = value

    def on_account_end(self) -> None:
        self.account_ready.set()

    def on_position(self, symbol: str, quantity: object) -> None:
        with self._lock:
            self.positions.append({"symbol": symbol, "quantity": str(quantity)})

    def on_positions_end(self) -> None:
        self.positions_ready.set()

    def on_callback_shape_error(self, detail: str) -> None:
        """An unsupported/malformed error callback: fail closed, sanitized."""
        with self._lock:
            self.fatal_error = f"IBKR callback incompatibility: {detail}"
        for event in (self.ready, self.time_ready, self.account_ready, self.positions_ready):
            event.set()

    def on_error(self, code: int, message: object) -> None:
        # ``message`` is normalized defensively: the wrapper already delivers a
        # str, but a non-str payload must never raise (that is the 10.48 bug).
        if code < _FATAL_ERROR_CEILING:
            with self._lock:
                self.fatal_error = f"IBKR error {code}: {normalize_message(message)}"
            # Unblock any waiter so it re-checks fatal_error and fails closed.
            for event in (self.ready, self.time_ready, self.account_ready, self.positions_ready):
                event.set()


class IbkrReadOnlyRawClient:
    """Concrete ``RawReadOnlyClient`` driving a read-only transport."""

    def __init__(
        self,
        transport_factory: Callable[[], ReadOnlyTransport],
        *,
        client_id: int = DEFAULT_CLIENT_ID,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._factory = transport_factory
        self._client_id = client_id
        self._timeout = timeout_seconds
        self._transport: ReadOnlyTransport | None = None
        self._sink: ReadOnlySink | None = None

    def open(self, host: str, port: int) -> None:
        # Defence in depth: refuse a non-paper endpoint even if called
        # directly (the session validates too).
        _validate_endpoint(host, port)
        sink = ReadOnlySink()
        try:
            transport = self._factory()
            transport.start(host, port, self._client_id, sink)
        except PoiSessionError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise PoiSessionError(f"connect failed: {type(error).__name__}") from error
        self._transport = transport
        self._sink = sink
        self._await(sink.ready, "connection ready")

    def current_time(self) -> str:
        sink, transport = self._require_open()
        sink.time_ready.clear()
        transport.request_current_time()
        self._await(sink.time_ready, "current time")
        return sink.time_value

    def account_summary(self) -> ReadOnlyAccountSummary:
        sink, transport = self._require_open()
        sink.account_ready.clear()
        transport.request_account_summary(_ACCOUNT_SUMMARY_REQ_ID, _ACCOUNT_SUMMARY_TAGS)
        self._await(sink.account_ready, "account summary")
        transport.cancel_account_summary(_ACCOUNT_SUMMARY_REQ_ID)
        values = dict(sink.account_values)
        return ReadOnlyAccountSummary(
            account_id=sink.account_id,
            net_liquidation=values.get("NetLiquidation", ""),
            total_cash=values.get("TotalCashValue", ""),
            buying_power=values.get("BuyingPower", ""),
        )

    def positions(self) -> Sequence[object]:
        sink, transport = self._require_open()
        sink.positions_ready.clear()
        transport.request_positions()
        self._await(sink.positions_ready, "positions")
        transport.cancel_positions()
        return tuple(sink.positions)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.stop()
        self._transport = None
        self._sink = None

    # -- internals ----------------------------------------------------------
    def _require_open(self) -> tuple[ReadOnlySink, ReadOnlyTransport]:
        if self._sink is None or self._transport is None:
            raise PoiSessionError("client is not open")
        return self._sink, self._transport

    def _await(self, event: threading.Event, what: str) -> None:
        if not event.wait(self._timeout):
            raise PoiSessionError(f"timed out waiting for {what}")
        if self._sink is not None and self._sink.fatal_error:
            raise PoiSessionError(self._sink.fatal_error)


def _build_ibapi_transport() -> ReadOnlyTransport:
    """Build the real IBKR-backed read-only transport (PoI run time only).

    The IBKR Python SDK is imported here, lazily, and only read-only request
    methods are ever called. This runs only during the manual PoI on the
    host where IB Gateway is up; it performs no login (the owner logs in via
    the Gateway UI) and never references any order method.
    """
    try:
        from ibapi.client import EClient  # type: ignore[import-not-found]
        from ibapi.wrapper import EWrapper  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - depends on host install
        raise PoiSessionError(
            "the IBKR Python API (ibapi) is not installed on this host; install it"
            " inside the broker-ibkr environment before running the PoI"
        ) from error

    class _ReadOnlyApp(EWrapper, EClient):  # type: ignore[misc]  # pragma: no cover
        """EClient+EWrapper limited to read-only requests and callbacks."""

        def __init__(self) -> None:
            EClient.__init__(self, self)
            self._sink: ReadOnlySink | None = None

        def bind(self, sink: ReadOnlySink) -> None:
            self._sink = sink

        # ready signal (standard IBKR connection-ready callback; no order placed)
        def nextValidId(self, order_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_ready()

        def currentTime(self, time: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_current_time(time)

        def accountSummary(  # noqa: N802
            self, req_id: int, account: str, tag: str, value: str, currency: str
        ) -> None:
            if self._sink is not None:
                self._sink.on_account_value(account, tag, value)

        def accountSummaryEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_account_end()

        def position(  # noqa: N802
            self, account: str, contract: Any, position: object, avg_cost: float
        ) -> None:
            if self._sink is not None:
                self._sink.on_position(getattr(contract, "symbol", ""), position)

        def positionEnd(self) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_positions_end()

        def error(self, *args: object, **kwargs: object) -> None:  # noqa: N802
            # Accepts the IBKR 10.48 shape (reqId, errorTime, errorCode,
            # errorString, advancedOrderRejectJson="") and the legacy shape;
            # an unsupported shape fails closed with a sanitized diagnostic
            # instead of raising an unrelated TypeError.
            if self._sink is None:
                return
            try:
                normalized = normalize_error_callback(*args, **kwargs)
            except ErrorCallbackShapeError as shape_error:
                self._sink.on_callback_shape_error(str(shape_error))
                return
            self._sink.on_error(normalized.code, normalized.message)

    class _IbApiTransport:  # pragma: no cover - exercised only on the host
        """Adapts ``_ReadOnlyApp`` to the ``ReadOnlyTransport`` seam."""

        def __init__(self) -> None:
            self._app = _ReadOnlyApp()
            self._thread: threading.Thread | None = None

        def start(self, host: str, port: int, client_id: int, sink: ReadOnlySink) -> None:
            self._app.bind(sink)
            self._app.connect(host, port, client_id)
            self._thread = threading.Thread(target=self._app.run, daemon=True)
            self._thread.start()

        def stop(self) -> None:
            try:
                if self._app.isConnected():
                    self._app.disconnect()
            finally:
                if self._thread is not None:
                    self._thread.join(timeout=5)

        def request_current_time(self) -> None:
            self._app.reqCurrentTime()

        def request_account_summary(self, req_id: int, tags: str) -> None:
            self._app.reqAccountSummary(req_id, "All", tags)

        def cancel_account_summary(self, req_id: int) -> None:
            self._app.cancelAccountSummary(req_id)

        def request_positions(self) -> None:
            self._app.reqPositions()

        def cancel_positions(self) -> None:
            self._app.cancelPositions()

    return _IbApiTransport()


def build_readonly_client(
    *,
    client_id: int = DEFAULT_CLIENT_ID,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> IbkrReadOnlyRawClient:
    """Read-only client backed by the lazy IBKR transport (host-only)."""
    return IbkrReadOnlyRawClient(
        _build_ibapi_transport, client_id=client_id, timeout_seconds=timeout_seconds
    )


# Runtime check: the concrete client satisfies the session's protocol.
_PROTOCOL_CHECK: type[RawReadOnlyClient] = IbkrReadOnlyRawClient


__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "READONLY_CLIENT_VERSION",
    "IbkrReadOnlyRawClient",
    "ReadOnlySink",
    "ReadOnlyTransport",
    "build_readonly_client",
]
