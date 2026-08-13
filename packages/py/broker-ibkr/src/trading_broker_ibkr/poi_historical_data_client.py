"""Read-only HISTORICAL-DATA qualification client (empirical IBKR PIT check).

Extends the established read-only PoI pattern (``poi_market_data_client`` /
``poi_readonly_session``) with the historical requests needed to EMPIRICALLY
qualify the owner's IBKR access against the frozen strategy's dataset contract —
without any order, cancel, or account-mutating surface anywhere.

Read-only requests only:

- ``reqHeadTimeStamp``  -> earliest available date per whatToShow (coverage);
- ``reqHistoricalData`` -> 1-day bars for TRADES / BID / ASK / MIDPOINT /
  ADJUSTED_LAST, ``useRTH=1`` (RTH only), a SMALL duration to prove schema and
  adjustment semantics (never a bulk S&P download);
- ``reqContractDetails`` -> conId + longName + primaryExchange (identity/provenance);
- ``reqMatchingSymbols`` -> symbol-change / ticker-history discovery;
- ``reqAccountSummary`` -> the DU-paper gate only (masked; no values surfaced
  beyond the connectivity proof).

Safety (ADR-0014; PoI plan v2), identical to the merged read-only clients:

- ISOLATION: ``ibapi`` is imported lazily inside ``_build_ibapi_transport`` and
  only read-only request verbs are ever called; this module imports and is fully
  unit-tested WITHOUT ``ibapi`` and WITHOUT any socket via an injected transport.
- PAPER ONLY: endpoint re-validated (localhost + paper port 4002; live 4001
  rejected). The DU-account gate + masking live alongside.
- HONEST DATA: bar values are recorded as strings exactly as IBKR returns them
  (no float in evidence, no invented adjustment). The ACTUAL adjustment is
  revealed by comparing TRADES vs ADJUSTED_LAST — never assumed.
- FAIL CLOSED: every timeout or genuine IBKR error fails closed; pacing/error
  codes are recorded, never silently retried. No order verb is referenced.
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

HISTORICAL_CLIENT_VERSION = "poi-ibkr-historical-client-1.0.0"

DEFAULT_CLIENT_ID = 0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

_ACCOUNT_SUMMARY_REQ_ID = 8101
_HEAD_TS_REQ_ID = 8201
_HISTORICAL_REQ_ID = 8301
_CONTRACT_REQ_ID = 8401
_MATCHING_REQ_ID = 8501
_ACCOUNT_SUMMARY_TAGS = "NetLiquidation"

#: whatToShow values relevant to the frozen strategy's dataset contract.
WHAT_TO_SHOW = ("TRADES", "BID", "ASK", "MIDPOINT", "ADJUSTED_LAST")

#: IBKR error codes that indicate PACING / historical service limits (recorded,
#: never silently retried). 162 carries pacing violations + "no data"; 165 is a
#: service note; 320-322 server/pacing; 366 no query found; 100 max message rate.
_PACING_ERROR_CODES: frozenset[int] = frozenset({100, 162, 165, 320, 321, 322, 366, 420})
_NO_SECURITY_DEFINITION_CODE = 200
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


@dataclass(frozen=True, slots=True)
class HeadTimestamp:
    symbol: str
    what_to_show: str
    value: str | None
    error: str | None


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
    """Minimal read-only historical transport seam the client drives."""

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
class HistoricalSink:
    """Thread-safe buffers + events that read-only callbacks feed."""

    ready: threading.Event = field(default_factory=threading.Event)
    time_ready: threading.Event = field(default_factory=threading.Event)
    account_ready: threading.Event = field(default_factory=threading.Event)
    head_ready: threading.Event = field(default_factory=threading.Event)
    hist_ready: threading.Event = field(default_factory=threading.Event)
    contract_ready: threading.Event = field(default_factory=threading.Event)
    matching_ready: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    time_value: str = ""
    account_id: str = ""
    head_value: str | None = None
    head_error: str | None = None
    bars: list[HistoricalBar] = field(default_factory=list)
    hist_error: str | None = None
    pacing_notes: list[str] = field(default_factory=list)
    contract_con_ids: list[int] = field(default_factory=list)
    contract_long_name: str = ""
    contract_primary_exchange: str = ""
    contract_error: str | None = None
    matches: list[SymbolMatch] = field(default_factory=list)
    matching_error: str | None = None
    fatal_error: str | None = None

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

    def on_account_end(self) -> None:
        self.account_ready.set()

    def on_head_timestamp(self, value: str) -> None:
        with self._lock:
            self.head_value = str(value)
        self.head_ready.set()

    def on_historical_bar(
        self, date: str, o: object, h: object, low: object, c: object, volume: object
    ) -> None:
        with self._lock:
            self.bars.append(
                HistoricalBar(str(date), str(o), str(h), str(low), str(c), str(volume))
            )

    def on_historical_end(self) -> None:
        self.hist_ready.set()

    def on_contract(self, con_id: int, long_name: str, primary_exchange: str) -> None:
        with self._lock:
            self.contract_con_ids.append(int(con_id))
            self.contract_long_name = str(long_name)
            self.contract_primary_exchange = str(primary_exchange)

    def on_contract_end(self) -> None:
        self.contract_ready.set()

    def on_symbol_match(
        self, symbol: str, con_id: int, sec_type: str, primary_exchange: str, description: str
    ) -> None:
        with self._lock:
            self.matches.append(
                SymbolMatch(str(symbol), int(con_id), str(sec_type),
                            str(primary_exchange), str(description))
            )
        self.matching_ready.set()

    def on_callback_shape_error(self, detail: str) -> None:
        with self._lock:
            self.fatal_error = f"IBKR callback incompatibility: {detail}"
        self._release_all()

    def on_error(self, code: int, message: object) -> None:
        text = f"{code}: {normalize_message(message)}"
        if code in _PACING_ERROR_CODES:
            with self._lock:
                self.pacing_notes.append(text)
                # 162/366 terminate the pending historical/head request.
                if self.head_value is None:
                    self.head_error = self.head_error or text
                self.hist_error = self.hist_error or text
            self.head_ready.set()
            self.hist_ready.set()
            return
        if code == _NO_SECURITY_DEFINITION_CODE:
            with self._lock:
                self.contract_error = text
                self.matching_error = self.matching_error or text
            self.contract_ready.set()
            self.matching_ready.set()
            return
        if code < _FATAL_ERROR_CEILING and code not in _CONNECTIVITY_NOTICES:
            with self._lock:
                self.fatal_error = f"IBKR error {text}"
            self._release_all()

    def _release_all(self) -> None:
        for event in (
            self.ready, self.time_ready, self.account_ready, self.head_ready,
            self.hist_ready, self.contract_ready, self.matching_ready,
        ):
            event.set()


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class IbkrHistoricalRawClient:
    """Concrete read-only historical client driving a transport seam."""

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
        return ReadOnlyAccountSummary(
            account_id=sink.account_id, net_liquidation="", total_cash="", buying_power=""
        )

    def head_timestamp(self, symbol: str, what_to_show: str, use_rth: int = 1) -> HeadTimestamp:
        sink, transport = self._require_open()
        sink.head_ready.clear()
        sink.head_value = None
        sink.head_error = None
        transport.request_head_timestamp(_HEAD_TS_REQ_ID, symbol, what_to_show, use_rth)
        self._await(sink.head_ready, "head timestamp")
        transport.cancel_head_timestamp(_HEAD_TS_REQ_ID)
        return HeadTimestamp(symbol, what_to_show, sink.head_value, sink.head_error)

    def historical(
        self, symbol: str, what_to_show: str, *, bar_size: str = "1 day",
        duration: str = "10 D", use_rth: int = 1,
    ) -> HistoricalSeries:
        sink, transport = self._require_open()
        sink.hist_ready.clear()
        sink.bars = []
        sink.hist_error = None
        sink.pacing_notes = []
        transport.request_historical_data(
            _HISTORICAL_REQ_ID, symbol, what_to_show, bar_size, duration, use_rth
        )
        self._await(sink.hist_ready, "historical data")
        transport.cancel_historical_data(_HISTORICAL_REQ_ID)  # read-only cleanup
        bars = list(sink.bars)
        sample: tuple[HistoricalBar, ...] = ()
        if bars:
            sample = (bars[0], bars[-1]) if len(bars) > 1 else (bars[0],)
        return HistoricalSeries(
            symbol=symbol, what_to_show=what_to_show, bar_size=bar_size, use_rth=use_rth,
            duration=duration, row_count=len(bars),
            earliest_bar=bars[0].date if bars else None,
            latest_bar=bars[-1].date if bars else None,
            bars_sample=sample, pacing_notes=tuple(sink.pacing_notes), error=sink.hist_error,
        )

    def contract_identity(self, symbol: str) -> ContractIdentity:
        sink, transport = self._require_open()
        sink.contract_ready.clear()
        sink.contract_con_ids = []
        sink.contract_error = None
        transport.request_contract_details(_CONTRACT_REQ_ID, symbol)
        self._await(sink.contract_ready, "contract details")
        if sink.contract_error is not None:
            return ContractIdentity(symbol, False, None, "", "", sink.contract_error)
        count = len(sink.contract_con_ids)
        if count == 0:
            return ContractIdentity(symbol, False, None, "", "", "no contract found")
        if count > 1:
            return ContractIdentity(
                symbol, False, None, "", "", f"ambiguous: {count} contracts; fail closed"
            )
        return ContractIdentity(
            symbol, True, sink.contract_con_ids[0], sink.contract_long_name,
            sink.contract_primary_exchange, "single contract resolved",
        )

    def matching_symbols(self, pattern: str) -> MatchingSymbols:
        sink, transport = self._require_open()
        sink.matching_ready.clear()
        sink.matches = []
        sink.matching_error = None
        transport.request_matching_symbols(_MATCHING_REQ_ID, pattern)
        self._await(sink.matching_ready, "matching symbols")
        return MatchingSymbols(pattern, tuple(sink.matches), sink.matching_error)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.stop()
        self._transport = None
        self._sink = None

    def _require_open(self) -> tuple[HistoricalSink, HistoricalTransport]:
        if self._sink is None or self._transport is None:
            raise PoiSessionError("client is not open")
        return self._sink, self._transport

    def _await(self, event: threading.Event, what: str) -> None:
        if not event.wait(self._timeout):
            raise PoiSessionError(f"timed out waiting for {what}")
        if self._sink is not None and self._sink.fatal_error:
            raise PoiSessionError(self._sink.fatal_error)


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
                self._sink.on_account_value(account, tag, value)

        def accountSummaryEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_account_end()

        def headTimestamp(self, req_id: int, head_timestamp: str) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_head_timestamp(head_timestamp)

        def historicalData(self, req_id: int, bar: Any) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_historical_bar(
                    bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume
                )

        def historicalDataEnd(self, req_id: int, start: str, end: str) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_historical_end()

        def contractDetails(self, req_id: int, contract_details: Any) -> None:  # noqa: N802
            if self._sink is not None:
                contract = contract_details.contract
                self._sink.on_contract(
                    getattr(contract, "conId", 0),
                    getattr(contract_details, "longName", ""),
                    getattr(contract, "primaryExchange", ""),
                )

        def contractDetailsEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_contract_end()

        def symbolSamples(self, req_id: int, contract_descriptions: Any) -> None:  # noqa: N802
            if self._sink is None:
                return
            for description in contract_descriptions:
                contract = description.contract
                self._sink.on_symbol_match(
                    getattr(contract, "symbol", ""),
                    getattr(contract, "conId", 0),
                    getattr(contract, "secType", ""),
                    getattr(contract, "primaryExchange", ""),
                    ",".join(getattr(description, "derivativeSecTypes", []) or []),
                )
            self._sink.matching_ready.set()

        def error(self, *args: object, **kwargs: object) -> None:  # noqa: N802
            if self._sink is None:
                return
            try:
                normalized = normalize_error_callback(*args, **kwargs)
            except ErrorCallbackShapeError as shape_error:
                self._sink.on_callback_shape_error(str(shape_error))
                return
            self._sink.on_error(normalized.code, normalized.message)

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
