"""Concrete read-only MARKET-DATA raw client for the POI-MD-001 pre-check.

Supplies the ``RawMarketDataClient`` that ``poi_market_data_session`` needs:
a real IBKR-backed transport performing ONLY read-only requests — current
time, account summary (for the DU gate), contract details, and a market-data
SNAPSHOT — plus their read-only teardown. It is a market-data pre-check for
POI-MD-001, NOT the IMP-1002 adapter and NOT POI-IBKR-001 execution: there
is no order-placing, cancellation, modification, global-cancel or exercise
surface anywhere, and no order verb is referenced.

Design (ADR-0014; PoI plan v2), mirroring the merged read-only client:

- ISOLATION + TESTABILITY: all IBKR SDK use is confined to
  ``_build_ibapi_transport`` and imported lazily, so this module imports and
  is fully unit-tested without ``ibapi`` and without any network. A small
  injectable ``MarketDataTransport`` seam drives the request/callback flow;
  tests inject a fake transport that replays callbacks synchronously — no
  socket is ever opened.
- READ-ONLY: the transport exposes only current-time / account-summary /
  contract-details / market-data-snapshot requests plus their read-only
  cancels. ``EClient`` order methods are never referenced.
- HONEST DATA: the ACTUAL ``marketDataType`` reported by IBKR is recorded
  (``reqMarketDataType(1)`` is a request, never a guarantee). Realtime and
  delayed ticks are captured separately (realtime 0-5/45, delayed 66-71/88)
  and never mixed; the quote is drawn from the family matching the served
  type, so a realtime -1 placeholder can never clobber a delayed price. An
  exchange timestamp is recorded only if IBKR actually sent the family's
  last-timestamp tick (45/88) — never invented. Entitlement/permission
  notices are surfaced explicitly, never silently downgraded. Missing
  bid/ask/last is reported as a fact.
- SAFETY: the endpoint is re-validated here (localhost + paper port 4002;
  live port 4001 rejected) as defence in depth on top of the session gate.
  The DU-account gate and masking live in the session. Every timeout or
  genuine IBKR error fails closed with ``PoiSessionError``. No automatic
  retry: each request is issued exactly once.
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

MARKET_DATA_CLIENT_VERSION = "poi-ibkr-market-data-client-1.0.0"

DEFAULT_CLIENT_ID = 0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
_ACCOUNT_SUMMARY_REQ_ID = 9101
_CONTRACT_DETAILS_REQ_ID = 9201
_MARKET_DATA_REQ_ID = 9301
_ACCOUNT_SUMMARY_TAGS = "NetLiquidation,TotalCashValue,BuyingPower"

#: We ASK for live data; IBKR reports what it actually served via the
#: ``marketDataType`` callback (1 live, 2 frozen, 3 delayed, 4 delayed-frozen).
_REQUESTED_MARKET_DATA_TYPE = 1

#: IBKR tick-type ids are split by FAMILY so realtime and delayed ticks are
#: never silently mixed. Realtime (0-5, 45) and delayed (66-71, 88) values are
#: captured into separate buffers; the quote is drawn from the family that
#: matches the ACTUAL ``marketDataType`` served (3/4 => delayed). A realtime -1
#: placeholder can therefore never clobber a good delayed price.
#: (tick_type -> (family, field))
_PRICE_FIELD_BY_TICK: dict[int, tuple[str, str]] = {
    1: ("realtime", "bid"),
    2: ("realtime", "ask"),
    4: ("realtime", "last"),
    66: ("delayed", "bid"),
    67: ("delayed", "ask"),
    68: ("delayed", "last"),
}
_SIZE_FIELD_BY_TICK: dict[int, tuple[str, str]] = {
    0: ("realtime", "bid_size"),
    3: ("realtime", "ask_size"),
    5: ("realtime", "last_size"),
    69: ("delayed", "bid_size"),
    70: ("delayed", "ask_size"),
    71: ("delayed", "last_size"),
}
#: Last-timestamp ticks: 45 realtime, 88 delayed.
_TIMESTAMP_FAMILY_BY_TICK: dict[int, str] = {45: "realtime", 88: "delayed"}

#: IBKR marketDataType values that mean delayed data (3 delayed, 4 delayed-frozen).
_DELAYED_MARKET_DATA_TYPES: frozenset[int] = frozenset({3, 4})

#: Canonical freshness (Integration spec 7.3). A frozen/delayed-frozen
#: snapshot is a non-updating value => STALE; unknown/missing => UNKNOWN
#: (fail closed). The harness re-derives this from ``market_data_type`` for
#: its PASS/FAIL gate; this copy is for the client's own evidence result.
FRESHNESS_BY_IBKR_TYPE: dict[int, str] = {1: "REALTIME", 2: "STALE", 3: "DELAYED", 4: "STALE"}

#: Market-data subscription/permission errors: no data is coming, so record
#: the entitlement error and unblock the waiter (never a silent downgrade).
_SUBSCRIPTION_ERROR_CODES: frozenset[int] = frozenset({354, 10089, 10090, 10091, 10187})

#: Delayed-data notices: IBKR fell back to delayed and WILL still send delayed
#: ticks — record the entitlement note but wait for the snapshot to complete.
_DELAYED_NOTICE_CODES: frozenset[int] = frozenset({10167, 10168, 10169, 10197})

#: "No security definition" for a contract request.
_NO_SECURITY_DEFINITION_CODE = 200

#: Below this codes are genuine client/connectivity errors; 1100-1102 are
#: connectivity notices and >= 2000 are benign system/info messages.
_FATAL_ERROR_CEILING = 1100
_CONNECTIVITY_NOTICES: frozenset[int] = frozenset({1100, 1101, 1102})


class MarketDataTransport(Protocol):
    """Minimal read-only transport seam the client drives."""

    def start(self, host: str, port: int, client_id: int, sink: MarketDataSink) -> None: ...

    def stop(self) -> None: ...

    def request_current_time(self) -> None: ...

    def request_account_summary(self, req_id: int, tags: str) -> None: ...

    def cancel_account_summary(self, req_id: int) -> None: ...

    def request_contract_details(self, req_id: int, symbol: str) -> None: ...

    def set_market_data_type(self, market_data_type: int) -> None: ...

    def request_market_data(self, req_id: int, symbol: str) -> None: ...

    def cancel_market_data(self, req_id: int) -> None: ...


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """Result of resolving one symbol; the broker id is never surfaced."""

    symbol: str
    resolved: bool
    con_id_present: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class MarketDataQuote:
    """One read-only market-data snapshot (duck-typed to the harness).

    ``received_at`` is the client's own capture time; ``exchange_timestamp``
    is present only if IBKR actually sent the family's last-timestamp tick (45
    realtime / 88 delayed). ``freshness`` mirrors the canonical mapping.
    ``data_classification`` (REALTIME/DELAYED/UNKNOWN) and ``source_tick_ids``
    record, for audit, which family and exact IBKR tick ids produced this quote.
    """

    symbol: str
    market_data_type: int | None
    bid: str | None
    ask: str | None
    last: str | None
    bid_size: str | None
    ask_size: str | None
    last_size: str | None
    exchange_timestamp: str | None
    received_at: str | None
    entitlement_errors: tuple[str, ...]
    missing_fields: tuple[str, ...]
    freshness: str
    data_classification: str = "UNKNOWN"
    source_tick_ids: tuple[int, ...] = ()


def data_classification(market_data_type: int | None) -> str:
    """REALTIME / DELAYED / UNKNOWN for the ACTUAL served ``marketDataType``."""
    if market_data_type in _DELAYED_MARKET_DATA_TYPES:
        return "DELAYED"
    if market_data_type in (1, 2):
        return "REALTIME"
    return "UNKNOWN"


def classify_freshness(market_data_type: int | None) -> str:
    """Map an IBKR ``marketDataType`` to a canonical freshness class."""
    if market_data_type is None:
        return "UNKNOWN"
    return FRESHNESS_BY_IBKR_TYPE.get(market_data_type, "UNKNOWN")


@dataclass
class MarketDataSink:
    """Thread-safe buffers + events that transport callbacks feed."""

    ready: threading.Event = field(default_factory=threading.Event)
    time_ready: threading.Event = field(default_factory=threading.Event)
    account_ready: threading.Event = field(default_factory=threading.Event)
    contract_ready: threading.Event = field(default_factory=threading.Event)
    mkt_data_ready: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    time_value: str = ""
    account_id: str = ""
    account_values: dict[str, str] = field(default_factory=dict)
    contract_con_ids: list[int] = field(default_factory=list)
    contract_error: str | None = None
    market_data_type: int | None = None
    # Realtime and delayed ticks are captured into SEPARATE buffers and never
    # merged; the client picks the family matching the served marketDataType.
    families: dict[str, dict[str, str]] = field(
        default_factory=lambda: {"realtime": {}, "delayed": {}}
    )
    source_tick_ids: list[int] = field(default_factory=list)
    entitlement_errors: list[str] = field(default_factory=list)
    got_tick: bool = False
    fatal_error: str | None = None

    def _note_tick(self, tick_type: int) -> None:
        self.got_tick = True
        if tick_type not in self.source_tick_ids:
            self.source_tick_ids.append(tick_type)

    # -- connection / time / account ---------------------------------------
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

    # -- contract details --------------------------------------------------
    def on_contract(self, con_id: int) -> None:
        with self._lock:
            self.contract_con_ids.append(int(con_id))

    def on_contract_end(self) -> None:
        self.contract_ready.set()

    # -- market data -------------------------------------------------------
    def on_market_data_type(self, market_data_type: int) -> None:
        with self._lock:
            self.market_data_type = int(market_data_type)

    def on_tick_price(self, tick_type: int, price: object) -> None:
        mapping = _PRICE_FIELD_BY_TICK.get(tick_type)
        with self._lock:
            self._note_tick(tick_type)
            if mapping is not None:
                family, key = mapping
                self.families[family][key] = str(price)

    def on_tick_size(self, tick_type: int, size: object) -> None:
        mapping = _SIZE_FIELD_BY_TICK.get(tick_type)
        with self._lock:
            self._note_tick(tick_type)
            if mapping is not None:
                family, key = mapping
                self.families[family][key] = str(size)

    def on_tick_string(self, tick_type: int, value: str) -> None:
        family = _TIMESTAMP_FAMILY_BY_TICK.get(tick_type)
        if family is None:
            return
        raw = str(value).strip()
        if not raw or raw == "0":
            return  # nothing received => never invent a timestamp
        with self._lock:
            self._note_tick(tick_type)
            self.families[family]["timestamp"] = (
                datetime.fromtimestamp(int(raw), tz=UTC).isoformat()
            )

    def on_snapshot_end(self) -> None:
        self.mkt_data_ready.set()

    # -- errors ------------------------------------------------------------
    def on_callback_shape_error(self, detail: str) -> None:
        """An unsupported/malformed error callback: fail closed, sanitized."""
        with self._lock:
            self.fatal_error = f"IBKR callback incompatibility: {detail}"
        for event in (
            self.ready,
            self.time_ready,
            self.account_ready,
            self.contract_ready,
            self.mkt_data_ready,
        ):
            event.set()

    def on_error(self, code: int, message: object) -> None:
        # ``message`` is normalized defensively: the wrapper already delivers a
        # str, but a non-str payload must never raise (that is the 10.48 bug).
        text = f"{code}: {normalize_message(message)}"
        if code in _SUBSCRIPTION_ERROR_CODES:
            with self._lock:
                self.entitlement_errors.append(text)
            self.mkt_data_ready.set()  # no data is coming
            return
        if code in _DELAYED_NOTICE_CODES:
            with self._lock:
                self.entitlement_errors.append(text)
            return  # delayed ticks still arrive; wait for snapshot end
        if code == _NO_SECURITY_DEFINITION_CODE:
            with self._lock:
                self.contract_error = text
            self.contract_ready.set()
            return
        if code < _FATAL_ERROR_CEILING and code not in _CONNECTIVITY_NOTICES:
            with self._lock:
                self.fatal_error = f"IBKR error {text}"
            for event in (
                self.ready,
                self.time_ready,
                self.account_ready,
                self.contract_ready,
                self.mkt_data_ready,
            ):
                event.set()


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class IbkrMarketDataRawClient:
    """Concrete read-only market-data client driving a transport seam."""

    def __init__(
        self,
        transport_factory: Callable[[], MarketDataTransport],
        *,
        client_id: int = DEFAULT_CLIENT_ID,
        timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        now: Callable[[], str] = _utc_now_iso,
    ) -> None:
        self._factory = transport_factory
        self._client_id = client_id
        self._timeout = timeout_seconds
        self._now = now
        self._transport: MarketDataTransport | None = None
        self._sink: MarketDataSink | None = None

    def open(self, host: str, port: int) -> None:
        _validate_endpoint(host, port)
        sink = MarketDataSink()
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

    def resolve_contract(self, symbol: str) -> ContractResolution:
        sink, transport = self._require_open()
        sink.contract_ready.clear()
        transport.request_contract_details(_CONTRACT_DETAILS_REQ_ID, symbol)
        self._await(sink.contract_ready, "contract details")
        if sink.contract_error is not None:
            return ContractResolution(symbol, False, False, sink.contract_error)
        count = len(sink.contract_con_ids)
        if count == 0:
            return ContractResolution(symbol, False, False, "no contract found")
        if count > 1:
            # More than one match and no explicit canonical choice: fail closed.
            return ContractResolution(
                symbol, False, False, f"ambiguous: {count} contracts; fail closed"
            )
        return ContractResolution(symbol, True, True, "single contract resolved")

    def market_data(
        self, symbol: str, requested_market_data_type: int = _REQUESTED_MARKET_DATA_TYPE
    ) -> MarketDataQuote:
        sink, transport = self._require_open()
        sink.mkt_data_ready.clear()
        # Request the caller's mode explicitly (1 realtime / 3 delayed); the
        # ACTUAL type served is read from the marketDataType callback below and
        # never assumed from the request.
        transport.set_market_data_type(requested_market_data_type)
        transport.request_market_data(_MARKET_DATA_REQ_ID, symbol)
        self._await(sink.mkt_data_ready, "market data snapshot")
        transport.cancel_market_data(_MARKET_DATA_REQ_ID)  # read-only cleanup only

        # Draw the quote from the family matching the ACTUAL served type, so a
        # delayed quote uses only delayed ticks (66-71, 88) and a realtime -1
        # placeholder can never clobber it (and vice versa).
        classification = data_classification(sink.market_data_type)
        family = (
            "delayed" if sink.market_data_type in _DELAYED_MARKET_DATA_TYPES else "realtime"
        )
        values = sink.families[family]
        bid, ask, last = values.get("bid"), values.get("ask"), values.get("last")
        # received_at reflects capture of the SELECTED family's data (not any
        # stray off-family placeholder tick).
        received_at = self._now() if values else None
        missing = tuple(
            name
            for name, value in (("bid", bid), ("ask", ask), ("last", last))
            if value is None or str(value).strip() == ""
        )
        return MarketDataQuote(
            symbol=symbol,
            market_data_type=sink.market_data_type,
            bid=bid,
            ask=ask,
            last=last,
            bid_size=values.get("bid_size"),
            ask_size=values.get("ask_size"),
            last_size=values.get("last_size"),
            exchange_timestamp=values.get("timestamp"),
            received_at=received_at,
            entitlement_errors=tuple(sink.entitlement_errors),
            missing_fields=missing,
            freshness=classify_freshness(sink.market_data_type),
            data_classification=classification,
            source_tick_ids=tuple(sink.source_tick_ids),
        )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.stop()
        self._transport = None
        self._sink = None

    # -- internals ----------------------------------------------------------
    def _require_open(self) -> tuple[MarketDataSink, MarketDataTransport]:
        if self._sink is None or self._transport is None:
            raise PoiSessionError("client is not open")
        return self._sink, self._transport

    def _await(self, event: threading.Event, what: str) -> None:
        if not event.wait(self._timeout):
            raise PoiSessionError(f"timed out waiting for {what}")
        if self._sink is not None and self._sink.fatal_error:
            raise PoiSessionError(self._sink.fatal_error)


def _build_ibapi_transport() -> MarketDataTransport:
    """Build the real IBKR-backed read-only market-data transport (host only).

    The IBKR Python SDK is imported here, lazily, and only read-only request
    methods are ever called. It performs no login (the owner logs in via the
    Gateway UI) and never references any order method.
    """
    try:
        from ibapi.client import EClient  # type: ignore[import-not-found]
        from ibapi.contract import Contract  # type: ignore[import-not-found]
        from ibapi.wrapper import EWrapper  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - depends on host install
        raise PoiSessionError(
            "the IBKR Python API (ibapi) is not installed on this host; install it"
            " inside the broker-ibkr environment before running the PoI"
        ) from error

    def _stock(symbol: str) -> Any:
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    class _MarketDataApp(EWrapper, EClient):  # type: ignore[misc]  # pragma: no cover
        """EClient+EWrapper limited to read-only requests and callbacks."""

        def __init__(self) -> None:
            EClient.__init__(self, self)
            self._sink: MarketDataSink | None = None

        def bind(self, sink: MarketDataSink) -> None:
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

        def contractDetails(self, req_id: int, contract_details: Any) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_contract(getattr(contract_details.contract, "conId", 0))

        def contractDetailsEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_contract_end()

        def marketDataType(self, req_id: int, market_data_type: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_market_data_type(market_data_type)

        def tickPrice(  # noqa: N802
            self, req_id: int, tick_type: int, price: float, attrib: Any
        ) -> None:
            if self._sink is not None:
                self._sink.on_tick_price(tick_type, price)

        def tickSize(self, req_id: int, tick_type: int, size: object) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_tick_size(tick_type, size)

        def tickString(self, req_id: int, tick_type: int, value: str) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_tick_string(tick_type, value)

        def tickSnapshotEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_snapshot_end()

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
        """Adapts ``_MarketDataApp`` to the ``MarketDataTransport`` seam."""

        def __init__(self) -> None:
            self._app = _MarketDataApp()
            self._thread: threading.Thread | None = None

        def start(self, host: str, port: int, client_id: int, sink: MarketDataSink) -> None:
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

        def request_contract_details(self, req_id: int, symbol: str) -> None:
            self._app.reqContractDetails(req_id, _stock(symbol))

        def set_market_data_type(self, market_data_type: int) -> None:
            self._app.reqMarketDataType(market_data_type)

        def request_market_data(self, req_id: int, symbol: str) -> None:
            # snapshot=True auto-completes via tickSnapshotEnd;
            # regulatorySnapshot=False avoids any paid snapshot fee.
            self._app.reqMktData(req_id, _stock(symbol), "", True, False, [])

        def cancel_market_data(self, req_id: int) -> None:
            self._app.cancelMktData(req_id)

    return _IbApiTransport()


def build_market_data_client(
    *,
    client_id: int = DEFAULT_CLIENT_ID,
    timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> IbkrMarketDataRawClient:
    """Read-only market-data client backed by the lazy IBKR transport."""
    return IbkrMarketDataRawClient(
        _build_ibapi_transport, client_id=client_id, timeout_seconds=timeout_seconds
    )


__all__ = [
    "DEFAULT_CLIENT_ID",
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "FRESHNESS_BY_IBKR_TYPE",
    "MARKET_DATA_CLIENT_VERSION",
    "ContractResolution",
    "IbkrMarketDataRawClient",
    "MarketDataQuote",
    "MarketDataSink",
    "MarketDataTransport",
    "build_market_data_client",
    "classify_freshness",
    "data_classification",
]
