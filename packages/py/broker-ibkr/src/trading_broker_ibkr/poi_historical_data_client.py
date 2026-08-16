"""Read-only HISTORICAL-DATA qualification client (empirical IBKR PIT check).

Extends the established read-only PoI pattern (``poi_market_data_client`` /
``poi_readonly_session``) with the historical requests needed to EMPIRICALLY
qualify the owner's IBKR access for the frozen strategy's dataset contract —
without any order, cancel-order, modify or account-mutating surface anywhere.

Read-only requests only:

- ``reqHeadTimeStamp``  -> earliest available date per whatToShow (coverage);
- ``reqHistoricalData`` -> 1-day bars for TRADES / BID / ASK / MIDPOINT /
  ADJUSTED_LAST, ``useRTH=1`` (RTH only), a SMALL duration to prove schema and
  adjustment semantics (never a bulk S&P download);
- ``reqContractDetails`` -> conId + longName + primaryExchange (identity/provenance);
- ``reqMatchingSymbols`` -> symbol-change / ticker-history discovery;
- ``reqAccountSummary`` -> the DU-paper gate only (masked; no values surfaced).

Request lifecycle correctness (fixes the reqId-reuse bug found on the owner run,
which produced 322 "Duplicate ticker ID", 366 "No historical data query found"
and 162 "query cancelled"):

- **Unique request ids.** A per-client monotonic allocator gives EVERY request
  (historical, head-timestamp, contract, matching, account) its own reqId. No
  fixed constant is reused across sequential requests.
- **Request-correlated callbacks.** Every callback and error carries its reqId
  and is routed to that request's own slot. An error for a retired reqId is a
  stray note and can NEVER mutate a later request's evidence.
- **No cancel after normal completion.** A finite ``reqHistoricalData``
  (``keepUpToDate=False``) is complete on ``historicalDataEnd`` and is NOT
  cancelled afterwards (cancelling a completed one-shot is what generated 366).
  ``reqHeadTimeStamp`` completion is likewise not cancelled. Cancel happens ONLY
  on a timeout/abort while the request is still active, and only for that reqId.
  (``reqAccountSummary`` IS a subscription, so it is cancelled after its end —
  the documented pattern.)

Safety (ADR-0014; PoI plan v2): ``ibapi`` is imported lazily inside
``_build_ibapi_transport`` and only read-only request verbs are ever called;
this module imports and is fully unit-tested WITHOUT ``ibapi`` and WITHOUT any
socket. Endpoint re-validated (localhost + paper port 4002; live 4001 rejected).
Bar values are recorded as strings exactly as IBKR returns them (no float in
evidence). Every timeout or genuine IBKR error fails closed; no order verb is
referenced.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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
    ReadOnlyAccountSummary,
    _validate_endpoint,
)

HISTORICAL_CLIENT_VERSION = "poi-ibkr-historical-client-2.0.0"

DEFAULT_CLIENT_ID = 0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

#: Base for the monotonic per-client request-id allocator. IBKR reqIds must be
#: positive and unique per connection; a fresh id is allocated for EVERY request.
_REQ_ID_BASE = 9000

#: whatToShow values relevant to the frozen strategy's dataset contract.
WHAT_TO_SHOW = ("TRADES", "BID", "ASK", "MIDPOINT", "ADJUSTED_LAST")

#: IBKR error codes that indicate PACING / historical service limits (recorded,
#: never silently retried). 162 carries pacing/"no data"/"cancelled"; 165 a
#: service note; 320-322 server/duplicate-id; 366 no query found; 100 max rate.
_PACING_ERROR_CODES: frozenset[int] = frozenset({100, 162, 165, 320, 321, 322, 366, 420})
_NO_SECURITY_DEFINITION_CODE = 200
#: reqId-less system messages below this code are treated as connection-fatal
#: unless they are benign connectivity notices.
_FATAL_ERROR_CEILING = 1100
_CONNECTIVITY_NOTICES: frozenset[int] = frozenset({1100, 1101, 1102})


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    """One 1-day bar exactly as IBKR returned it (strings; no float)."""

    date: str
    open: str
    high: str
    low: str
    close: str
    volume: str


@dataclass(frozen=True, slots=True)
class HistoricalSeries:
    """Result of one reqHistoricalData request (read-only)."""

    symbol: str
    what_to_show: str
    bar_size: str
    use_rth: int
    duration: str
    row_count: int
    earliest_bar: str | None
    latest_bar: str | None
    bars_sample: tuple[HistoricalBar, ...]  # first/last only, for evidence
    pacing_notes: tuple[str, ...]
    error: str | None
    request_id: int


@dataclass(frozen=True, slots=True)
class HeadTimestamp:
    symbol: str
    what_to_show: str
    value: str | None
    error: str | None
    request_id: int


@dataclass(frozen=True, slots=True)
class ContractIdentity:
    """conId is a CONTRACT id (not an account id) — safe to record as provenance."""

    symbol: str
    resolved: bool
    con_id: int | None
    long_name: str
    primary_exchange: str
    detail: str


@dataclass(frozen=True, slots=True)
class SymbolMatch:
    symbol: str
    con_id: int | None
    sec_type: str
    primary_exchange: str
    description: str


@dataclass(frozen=True, slots=True)
class MatchingSymbols:
    pattern: str
    matches: tuple[SymbolMatch, ...]
    error: str | None


class HistoricalTransport(Protocol):
    """Minimal read-only historical transport seam the client drives.

    Every request carries its own ``req_id``; cancels target a specific
    ``req_id`` and are issued ONLY on timeout/abort (except account summary,
    a subscription cancelled after its end).
    """

    def start(self, host: str, port: int, client_id: int, sink: HistoricalSink) -> None: ...

    def stop(self) -> None: ...

    def request_current_time(self) -> None: ...

    def request_account_summary(self, req_id: int, tags: str) -> None: ...

    def cancel_account_summary(self, req_id: int) -> None: ...

    def request_head_timestamp(
        self, req_id: int, symbol: str, what_to_show: str, use_rth: int
    ) -> None: ...

    def cancel_head_timestamp(self, req_id: int) -> None: ...

    def request_historical_data(
        self, req_id: int, symbol: str, what_to_show: str, bar_size: str,
        duration: str, use_rth: int,
    ) -> None: ...

    def cancel_historical_data(self, req_id: int) -> None: ...

    def request_contract_details(self, req_id: int, symbol: str) -> None: ...

    def request_matching_symbols(self, req_id: int, pattern: str) -> None: ...


@dataclass
class _Slot:
    """Per-reqId request state; a request only ever touches its own slot."""

    kind: str  # hist | head | contract | match | account
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    pacing: list[str] = field(default_factory=list)
    # historical
    bars: list[HistoricalBar] = field(default_factory=list)
    complete: bool = False  # historicalDataEnd received (normal completion)
    # head
    head_value: str | None = None
    # contract
    con_ids: list[int] = field(default_factory=list)
    long_name: str = ""
    primary_exchange: str = ""
    # match
    matches: list[SymbolMatch] = field(default_factory=list)
    # account
    account_id: str = ""


@dataclass
class HistoricalSink:
    """Thread-safe, per-reqId routing of read-only callbacks and errors."""

    ready: threading.Event = field(default_factory=threading.Event)
    time_ready: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    time_value: str = ""
    slots: dict[int, _Slot] = field(default_factory=dict)
    stray_errors: list[str] = field(default_factory=list)
    fatal_error: str | None = None

    # -- slot lifecycle ----------------------------------------------------
    def open_slot(self, req_id: int, kind: str) -> _Slot:
        slot = _Slot(kind=kind)
        with self._lock:
            self.slots[req_id] = slot
        return slot

    def close_slot(self, req_id: int) -> None:
        with self._lock:
            self.slots.pop(req_id, None)

    def _slot(self, req_id: int) -> _Slot | None:
        return self.slots.get(req_id)

    # -- connection / time -------------------------------------------------
    def on_ready(self) -> None:
        self.ready.set()

    def on_current_time(self, epoch_seconds: int) -> None:
        with self._lock:
            self.time_value = datetime.fromtimestamp(int(epoch_seconds), tz=UTC).isoformat()
        self.time_ready.set()

    # -- account -----------------------------------------------------------
    def on_account_value(self, req_id: int, account: str, tag: str, value: str) -> None:
        with self._lock:
            slot = self.slots.get(req_id)
            if slot is not None and not slot.account_id:
                slot.account_id = account

    def on_account_end(self, req_id: int) -> None:
        self._complete(req_id)

    # -- head timestamp ----------------------------------------------------
    def on_head_timestamp(self, req_id: int, value: str) -> None:
        with self._lock:
            slot = self.slots.get(req_id)
            if slot is not None:
                slot.head_value = str(value)
        self._complete(req_id)

    # -- historical --------------------------------------------------------
    def on_historical_bar(
        self, req_id: int, date: str, o: object, h: object, low: object, c: object, volume: object
    ) -> None:
        with self._lock:
            slot = self.slots.get(req_id)
            if slot is not None:
                slot.bars.append(
                    HistoricalBar(str(date), str(o), str(h), str(low), str(c), str(volume))
                )

    def on_historical_end(self, req_id: int) -> None:
        with self._lock:
            slot = self.slots.get(req_id)
            if slot is not None:
                slot.complete = True
        self._complete(req_id)

    # -- contract details --------------------------------------------------
    def on_contract(self, req_id: int, con_id: int, long_name: str, primary_exchange: str) -> None:
        with self._lock:
            slot = self.slots.get(req_id)
            if slot is not None:
                slot.con_ids.append(int(con_id))
                slot.long_name = str(long_name)
                slot.primary_exchange = str(primary_exchange)

    def on_contract_end(self, req_id: int) -> None:
        self._complete(req_id)

    # -- matching symbols --------------------------------------------------
    def on_symbol_samples(self, req_id: int, matches: list[SymbolMatch]) -> None:
        with self._lock:
            slot = self.slots.get(req_id)
            if slot is not None:
                slot.matches.extend(matches)
        self._complete(req_id)

    # -- errors ------------------------------------------------------------
    def on_callback_shape_error(self, detail: str) -> None:
        with self._lock:
            self.fatal_error = f"IBKR callback incompatibility: {detail}"
        self._release_all()

    def on_error(self, req_id: int | None, code: int, message: object) -> None:
        text = f"{code}: {normalize_message(message)}"
        if req_id is not None and req_id >= 0:
            with self._lock:
                slot = self.slots.get(req_id)
                if slot is None:
                    # Late error for a retired request: recorded but NEVER
                    # allowed to touch any active request's evidence.
                    self.stray_errors.append(text)
                    return
                if code in _PACING_ERROR_CODES:
                    slot.pacing.append(text)
                if slot.error is None:
                    slot.error = text
                event = slot.done
            event.set()
            return
        # reqId-less system message: fatal only for a genuine connection failure.
        if code < _FATAL_ERROR_CEILING and code not in _CONNECTIVITY_NOTICES:
            with self._lock:
                self.fatal_error = f"IBKR error {text}"
            self._release_all()

    def _complete(self, req_id: int) -> None:
        slot = self.slots.get(req_id)
        if slot is not None:
            slot.done.set()

    def _release_all(self) -> None:
        self.ready.set()
        self.time_ready.set()
        with self._lock:
            events = [slot.done for slot in self.slots.values()]
        for event in events:
            event.set()


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class IbkrHistoricalRawClient:
    """Concrete read-only historical client with unique-reqId correlation."""

    def __init__(
        self,
        transport_factory: Callable[[], HistoricalTransport],
        *,
        client_id: int = DEFAULT_CLIENT_ID,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        now: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._factory = transport_factory
        self._client_id = client_id
        self._timeout = timeout_seconds
        self._now = now
        self._transport: HistoricalTransport | None = None
        self._sink: HistoricalSink | None = None
        self._next_req_id = _REQ_ID_BASE
        self._id_lock = threading.Lock()

    def _alloc(self) -> int:
        with self._id_lock:
            req_id = self._next_req_id
            self._next_req_id += 1
        return req_id

    def open(self, host: str, port: int) -> None:
        _validate_endpoint(host, port)
        sink = HistoricalSink()
        try:
            transport = self._factory()
            transport.start(host, port, self._client_id, sink)
        except PoiSessionError:
            raise
        except Exception as error:  # noqa: BLE001 - unknown => fail closed
            raise PoiSessionError(f"connect failed: {type(error).__name__}") from error
        self._transport = transport
        self._sink = sink
        if not sink.ready.wait(self._timeout):
            raise PoiSessionError("timed out waiting for connection ready")
        if sink.fatal_error:
            raise PoiSessionError(sink.fatal_error)

    def current_time(self) -> str:
        sink, transport = self._require_open()
        sink.time_ready.clear()
        transport.request_current_time()
        if not sink.time_ready.wait(self._timeout):
            raise PoiSessionError("timed out waiting for current time")
        self._check_fatal(sink)
        return sink.time_value

    def account_summary(self) -> ReadOnlyAccountSummary:
        sink, transport = self._require_open()
        req_id = self._alloc()
        slot = sink.open_slot(req_id, "account")
        transport.request_account_summary(req_id, "NetLiquidation")
        try:
            self._wait_slot(sink, slot, req_id, "account summary", transport.cancel_account_summary)
        finally:
            pass
        # Account summary is a SUBSCRIPTION: cancel after its end (documented).
        transport.cancel_account_summary(req_id)
        account_id = slot.account_id
        sink.close_slot(req_id)
        return ReadOnlyAccountSummary(
            account_id=account_id, net_liquidation="", total_cash="", buying_power=""
        )

    def head_timestamp(self, symbol: str, what_to_show: str, use_rth: int = 1) -> HeadTimestamp:
        sink, transport = self._require_open()
        req_id = self._alloc()
        slot = sink.open_slot(req_id, "head")
        transport.request_head_timestamp(req_id, symbol, what_to_show, use_rth)
        # A one-shot head timestamp; on normal completion do NOT cancel (that is
        # what generated cleanup errors). Cancel ONLY if the wait times out.
        self._wait_slot(sink, slot, req_id, "head timestamp", transport.cancel_head_timestamp)
        result = HeadTimestamp(symbol, what_to_show, slot.head_value, slot.error, req_id)
        sink.close_slot(req_id)
        return result

    def historical(
        self, symbol: str, what_to_show: str, *, bar_size: str = "1 day",
        duration: str = "10 D", use_rth: int = 1,
    ) -> HistoricalSeries:
        sink, transport = self._require_open()
        req_id = self._alloc()
        slot = sink.open_slot(req_id, "hist")
        transport.request_historical_data(
            req_id, symbol, what_to_show, bar_size, duration, use_rth
        )
        # A finite (keepUpToDate=False) request is complete on historicalDataEnd;
        # do NOT cancel it afterwards. Cancel ONLY on a timeout while still active.
        self._wait_slot(sink, slot, req_id, "historical data", transport.cancel_historical_data)
        bars = list(slot.bars)
        sample: tuple[HistoricalBar, ...] = ()
        if bars:
            sample = (bars[0], bars[-1]) if len(bars) > 1 else (bars[0],)
        result = HistoricalSeries(
            symbol=symbol, what_to_show=what_to_show, bar_size=bar_size, use_rth=use_rth,
            duration=duration, row_count=len(bars),
            earliest_bar=bars[0].date if bars else None,
            latest_bar=bars[-1].date if bars else None,
            bars_sample=sample, pacing_notes=tuple(slot.pacing), error=slot.error,
            request_id=req_id,
        )
        sink.close_slot(req_id)
        return result

    def contract_identity(self, symbol: str) -> ContractIdentity:
        sink, transport = self._require_open()
        req_id = self._alloc()
        slot = sink.open_slot(req_id, "contract")
        transport.request_contract_details(req_id, symbol)
        self._wait_slot(sink, slot, req_id, "contract details", None)
        error, con_ids = slot.error, list(slot.con_ids)
        long_name, primary = slot.long_name, slot.primary_exchange
        sink.close_slot(req_id)
        if error is not None:
            return ContractIdentity(symbol, False, None, "", "", error)
        if len(con_ids) == 0:
            return ContractIdentity(symbol, False, None, "", "", "no contract found")
        if len(con_ids) > 1:
            return ContractIdentity(
                symbol, False, None, "", "", f"ambiguous: {len(con_ids)} contracts; fail closed"
            )
        return ContractIdentity(
            symbol, True, con_ids[0], long_name, primary, "single contract resolved"
        )

    def matching_symbols(self, pattern: str) -> MatchingSymbols:
        sink, transport = self._require_open()
        req_id = self._alloc()
        slot = sink.open_slot(req_id, "match")
        transport.request_matching_symbols(req_id, pattern)
        self._wait_slot(sink, slot, req_id, "matching symbols", None)
        result = MatchingSymbols(pattern, tuple(slot.matches), slot.error)
        sink.close_slot(req_id)
        return result

    def close(self) -> None:
        if self._transport is not None:
            self._transport.stop()
        self._transport = None
        self._sink = None

    # -- internals ---------------------------------------------------------
    def _require_open(self) -> tuple[HistoricalSink, HistoricalTransport]:
        if self._sink is None or self._transport is None:
            raise PoiSessionError("client is not open")
        return self._sink, self._transport

    def _wait_slot(
        self,
        sink: HistoricalSink,
        slot: _Slot,
        req_id: int,
        what: str,
        cancel: Callable[[int], None] | None,
    ) -> None:
        """Wait for THIS request's completion; cancel only on timeout/abort."""
        if not slot.done.wait(self._timeout):
            if cancel is not None:
                cancel(req_id)  # cancel ONLY this request, and ONLY on timeout
            sink.close_slot(req_id)
            raise PoiSessionError(f"timed out waiting for {what} (reqId={req_id})")
        if sink.fatal_error:
            sink.close_slot(req_id)
            raise PoiSessionError(sink.fatal_error)

    @staticmethod
    def _check_fatal(sink: HistoricalSink) -> None:
        if sink.fatal_error:
            raise PoiSessionError(sink.fatal_error)


def _build_ibapi_transport() -> HistoricalTransport:
    """Build the real IBKR-backed read-only historical transport (host only).

    ``ibapi`` is imported here lazily and only read-only request methods are
    ever called: no order/cancel-order/modify/global-cancel verb is referenced.
    """
    try:
        from ibapi.client import EClient  # type: ignore[import-not-found]
        from ibapi.contract import Contract  # type: ignore[import-not-found]
        from ibapi.wrapper import EWrapper  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - depends on host install
        raise PoiSessionError(
            "the IBKR Python API (ibapi) is not installed on this host; install it"
            " inside the broker-ibkr environment before running the qualification"
        ) from error

    def _stock(symbol: str) -> Any:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    class _HistoricalApp(EWrapper, EClient):  # type: ignore[misc]  # pragma: no cover
        def __init__(self) -> None:
            EClient.__init__(self, self)
            self._sink: HistoricalSink | None = None

        def bind(self, sink: HistoricalSink) -> None:
            self._sink = sink

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
                self._sink.on_account_value(req_id, account, tag, value)

        def accountSummaryEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_account_end(req_id)

        def headTimestamp(self, req_id: int, head_timestamp: str) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_head_timestamp(req_id, head_timestamp)

        def historicalData(self, req_id: int, bar: Any) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_historical_bar(
                    req_id, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume
                )

        def historicalDataEnd(self, req_id: int, start: str, end: str) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_historical_end(req_id)

        def contractDetails(self, req_id: int, contract_details: Any) -> None:  # noqa: N802
            if self._sink is not None:
                contract = contract_details.contract
                self._sink.on_contract(
                    req_id,
                    getattr(contract, "conId", 0),
                    getattr(contract_details, "longName", ""),
                    getattr(contract, "primaryExchange", ""),
                )

        def contractDetailsEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_contract_end(req_id)

        def symbolSamples(self, req_id: int, contract_descriptions: Any) -> None:  # noqa: N802
            if self._sink is None:
                return
            matches = [
                SymbolMatch(
                    getattr(description.contract, "symbol", ""),
                    getattr(description.contract, "conId", 0),
                    getattr(description.contract, "secType", ""),
                    getattr(description.contract, "primaryExchange", ""),
                    ",".join(getattr(description, "derivativeSecTypes", []) or []),
                )
                for description in contract_descriptions
            ]
            self._sink.on_symbol_samples(req_id, matches)

        def error(self, *args: object, **kwargs: object) -> None:  # noqa: N802
            if self._sink is None:
                return
            try:
                normalized = normalize_error_callback(*args, **kwargs)
            except ErrorCallbackShapeError as shape_error:
                self._sink.on_callback_shape_error(str(shape_error))
                return
            self._sink.on_error(normalized.request_id, normalized.code, normalized.message)

    class _IbApiTransport:  # pragma: no cover - exercised only on the host
        def __init__(self) -> None:
            self._app = _HistoricalApp()
            self._thread: threading.Thread | None = None

        def start(self, host: str, port: int, client_id: int, sink: HistoricalSink) -> None:
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

        def request_head_timestamp(
            self, req_id: int, symbol: str, what_to_show: str, use_rth: int
        ) -> None:
            self._app.reqHeadTimeStamp(req_id, _stock(symbol), what_to_show, use_rth, 2)

        def cancel_head_timestamp(self, req_id: int) -> None:
            self._app.cancelHeadTimeStamp(req_id)

        def request_historical_data(
            self, req_id: int, symbol: str, what_to_show: str, bar_size: str,
            duration: str, use_rth: int,
        ) -> None:
            self._app.reqHistoricalData(
                req_id, _stock(symbol), "", duration, bar_size, what_to_show,
                use_rth, 2, False, [],
            )

        def cancel_historical_data(self, req_id: int) -> None:
            self._app.cancelHistoricalData(req_id)

        def request_contract_details(self, req_id: int, symbol: str) -> None:
            self._app.reqContractDetails(req_id, _stock(symbol))

        def request_matching_symbols(self, req_id: int, pattern: str) -> None:
            self._app.reqMatchingSymbols(req_id, pattern)

    return _IbApiTransport()


def build_historical_client(
    *,
    client_id: int = DEFAULT_CLIENT_ID,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> IbkrHistoricalRawClient:
    """Read-only historical client backed by the lazy IBKR transport."""
    return IbkrHistoricalRawClient(
        _build_ibapi_transport, client_id=client_id, timeout_seconds=timeout_seconds
    )


__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "HISTORICAL_CLIENT_VERSION",
    "WHAT_TO_SHOW",
    "ContractIdentity",
    "HeadTimestamp",
    "HistoricalBar",
    "HistoricalSeries",
    "HistoricalSink",
    "HistoricalTransport",
    "IbkrHistoricalRawClient",
    "MatchingSymbols",
    "SymbolMatch",
    "build_historical_client",
]
