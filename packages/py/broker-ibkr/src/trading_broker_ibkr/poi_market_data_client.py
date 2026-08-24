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

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from trading_broker_ibkr.ibkr_error_callback import (
    ErrorCallbackShapeError,
    normalize_error_callback,
    normalize_message,
)
from trading_broker_ibkr.instrument_ref import decode_instrument_ref
from trading_broker_ibkr.poi_readonly_session import (
    PoiSessionError,
    ReadOnlyAccountSummary,
    _validate_endpoint,
)

_LOGGER = logging.getLogger("trading_broker_ibkr.market_data")

MARKET_DATA_CLIENT_VERSION = "poi-ibkr-market-data-client-1.0.0"

DEFAULT_CLIENT_ID = 0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
_ACCOUNT_SUMMARY_REQ_ID = 9101
_CONTRACT_DETAILS_REQ_ID = 9201
#: The FIRST market-data request id. Each snapshot takes the next one, so a
#: late callback from a finished snapshot carries an id the sink no longer
#: accepts. A single fixed id could not distinguish them at all: an old
#: tickSnapshotEnd would have completed the next request, and old ticks would
#: have entered the next quote.
_MARKET_DATA_REQ_ID_BASE = 9301
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

#: IBKR 300: "Can't find EId with ticker Id ...". It means the market-data id
#: named is not currently subscribed. We only ever see it in response to the
#: cancel we issue during cleanup, and for a snapshot that already completed
#: that is the EXPECTED answer -- there was nothing left to cancel. It describes
#: a subscription that is already gone, so it can never be a connection fatal
#: and must never poison a later quote. (This was the third owner-host defect:
#: cancelling a completed snapshot produced 300, which was classified fatal and
#: failed the NEXT quote on the same session.)
_MARKET_DATA_NOT_SUBSCRIBED_CODE = 300

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

    def request_market_data_for_con_id(self, req_id: int, con_id: int) -> None:
        """Request market data for ONE broker-identified security.

        The ticker-shaped ``request_market_data`` above stays for the read-only
        PoI tooling that predates contract resolution. The execution path must
        not use it: after a security has been resolved, asking for a price by
        ticker lets IBKR resolve the string a second time, and nothing compares
        its answer to the security the order was authorized for.
        """
        ...

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
    #: EXACTLY what this snapshot was requested for -- ``conid:265598`` on the
    #: execution path, ``symbol:AAPL`` on the legacy read-only path. Taken from
    #: the request we made, never inferred from what came back, so a quote can
    #: always be attributed to the security it was asked about.
    requested_for: str
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


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    """ONE completed market-data request, frozen and detached from the sink.

    The sink is shared and mutable for the whole connection. Building a quote by
    reading it after the request lock had been released meant the next request
    could reset and refill those buffers mid-read -- so a quote for A could be
    assembled from B's ticks, with the request lock having done nothing to stop
    it because issuance was serialised and *materialisation* was not.

    Everything a quote needs is copied out under the sink lock, in one operation,
    at the moment the request completes. After that there is nothing left to
    race: this object cannot change, and the sink's buffers are irrelevant to it.
    """

    req_id: int
    market_data_type: int | None
    realtime: Mapping[str, str]
    delayed: Mapping[str, str]
    source_tick_ids: tuple[int, ...]
    entitlement_errors: tuple[str, ...]
    got_tick: bool
    #: True iff this request reached ``tickSnapshotEnd``. A ``snapshot=True``
    #: request that completed is already over at the gateway, so cancelling it
    #: afterwards asks the gateway to cancel an id it no longer has (error 300).
    #: The caller uses this to skip that pointless -- and, historically,
    #: poisoning -- cancel. False means the request did not complete normally
    #: (timeout, or unblocked by an entitlement/error), where a cancel is still
    #: the safe thing to do.
    completed: bool = False

    def values(self, family: str) -> Mapping[str, str]:
        return self.realtime if family == "realtime" else self.delayed


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
    #: Did the request being collected reach ``tickSnapshotEnd``? Reset per
    #: request; read at ``finish`` so the caller can skip cancelling a snapshot
    #: the gateway has already closed.
    snapshot_end_seen: bool = False
    #: The market-data request currently being collected, or None between
    #: snapshots. Every market-data callback carries IBKR's reqId, and a
    #: callback whose id is not this one belongs to a snapshot that is over.
    active_market_data_req_id: int | None = None

    # -- one snapshot at a time, starting from nothing ---------------------
    def begin_market_data_request(self, req_id: int) -> None:
        """Arm the sink for ONE snapshot, discarding every previous one.

        The buffers below are REQUEST-local, and the client used to clear only
        ``mkt_data_ready`` before the next request. So a snapshot that received
        no ticks -- or only some of them -- was built from whatever the previous
        security had left behind: B's bid beside A's ask, or A's prices entirely,
        stamped with a fresh ``received_at`` because ``values`` was non-empty.
        That made stale data look freshly observed and could defeat the quote
        freshness gate, all while ``requested_for`` correctly named B.

        Connection-level state is deliberately NOT reset. ``fatal_error`` is
        sticky until reconnect, which is the canonical fail-closed behaviour,
        and contract/account/time state belongs to other requests entirely.
        """
        with self._lock:
            self.active_market_data_req_id = req_id
            self.market_data_type = None
            self.families = {"realtime": {}, "delayed": {}}
            self.source_tick_ids = []
            self.entitlement_errors = []
            self.got_tick = False
            self.snapshot_end_seen = False
            # Cleared INSIDE the lock. A callback that had already passed its
            # identity check could otherwise set the event after this clear, and
            # the new request would start life already "complete".
            self.mkt_data_ready.clear()

    def finish_market_data_request(self, req_id: int) -> MarketDataSnapshot:
        """Freeze this request's evidence and disarm it, in ONE atomic step.

        Copying and disarming cannot be two operations: between them, the next
        request could arm itself and start refilling the buffers being copied.
        """
        with self._lock:
            snapshot = MarketDataSnapshot(
                req_id=req_id,
                market_data_type=self.market_data_type,
                realtime=dict(self.families["realtime"]),
                delayed=dict(self.families["delayed"]),
                source_tick_ids=tuple(self.source_tick_ids),
                entitlement_errors=tuple(self.entitlement_errors),
                got_tick=self.got_tick,
                completed=self.snapshot_end_seen,
            )
            if self.active_market_data_req_id == req_id:
                self.active_market_data_req_id = None
            return snapshot

    def end_market_data_request(self, req_id: int) -> None:
        """Disarm without freezing. Idempotent; safe on a finished request."""
        with self._lock:
            if self.active_market_data_req_id == req_id:
                self.active_market_data_req_id = None

    def _is_current(self, req_id: int | None) -> bool:
        """Does this callback belong to the snapshot being collected NOW?

        Must be called with ``self._lock`` held: the answer is only meaningful
        for as long as no begin/finish transition can run between the check and
        whatever the caller does with it.

        ``req_id`` is MANDATORY for every callback that can put data into a
        quote -- ticks, classification, completion. It stays optional only for
        ``on_error``, because IBKR genuinely reports some errors with no request
        id, and an unattributed error can only make a quote unavailable, never
        fabricate one.
        """
        if req_id is None:
            return True
        return self.active_market_data_req_id == req_id

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
    def on_market_data_type(self, market_data_type: int, req_id: int) -> None:
        with self._lock:
            if not self._is_current(req_id):
                return  # a late classification cannot classify this snapshot
            self.market_data_type = int(market_data_type)

    def on_tick_price(self, tick_type: int, price: object, req_id: int) -> None:
        mapping = _PRICE_FIELD_BY_TICK.get(tick_type)
        with self._lock:
            if not self._is_current(req_id):
                return  # a late tick belongs to a security we already answered
            self._note_tick(tick_type)
            if mapping is not None:
                family, key = mapping
                self.families[family][key] = str(price)

    def on_tick_size(self, tick_type: int, size: object, req_id: int) -> None:
        mapping = _SIZE_FIELD_BY_TICK.get(tick_type)
        with self._lock:
            if not self._is_current(req_id):
                return
            self._note_tick(tick_type)
            if mapping is not None:
                family, key = mapping
                self.families[family][key] = str(size)

    def on_tick_string(self, tick_type: int, value: str, req_id: int) -> None:
        family = _TIMESTAMP_FAMILY_BY_TICK.get(tick_type)
        if family is None:
            return
        raw = str(value).strip()
        if not raw or raw == "0":
            return  # nothing received => never invent a timestamp
        with self._lock:
            if not self._is_current(req_id):
                return
            self._note_tick(tick_type)
            self.families[family]["timestamp"] = (
                datetime.fromtimestamp(int(raw), tz=UTC).isoformat()
            )

    def on_snapshot_end(self, req_id: int) -> None:
        with self._lock:
            if not self._is_current(req_id):
                return  # an old snapshot's end must never complete a new one
            # Record that THIS request completed normally at the gateway, so the
            # caller knows not to cancel a subscription that is already gone.
            self.snapshot_end_seen = True
            # Set INSIDE the lock. Checking under the lock and signalling after
            # it is a TOCTOU: an old callback could pass its check, the request
            # could then end and the next one begin (clearing the event), and
            # the old callback would then complete a snapshot that has no data.
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

    def on_error(self, code: int, message: object, req_id: int | None = None) -> None:
        # ``message`` is normalized defensively: the wrapper already delivers a
        # str, but a non-str payload must never raise (that is the 10.48 bug).
        text = f"{code}: {normalize_message(message)}"
        if code in _SUBSCRIPTION_ERROR_CODES:
            with self._lock:
                if not self._is_current(req_id):
                    return  # an old request's entitlement problem is not ours
                self.entitlement_errors.append(text)
                # Same atomicity as on_snapshot_end: this unblocks the waiting
                # caller, so the identity check and the signal are one step.
                self.mkt_data_ready.set()  # no data is coming
            return
        if code in _DELAYED_NOTICE_CODES:
            with self._lock:
                if not self._is_current(req_id):
                    return
                self.entitlement_errors.append(text)
            return  # delayed ticks still arrive; wait for snapshot end
        if code == _NO_SECURITY_DEFINITION_CODE:
            with self._lock:
                self.contract_error = text
            self.contract_ready.set()
            return
        if code == _MARKET_DATA_NOT_SUBSCRIBED_CODE:
            # "Not currently subscribed": the id in question is already gone.
            # For the cleanup cancel of a completed snapshot this is the normal
            # answer and means cleanup is effectively complete. It is never a
            # connection fatal, and it must never contaminate a later request --
            # so it is recorded (for audit) only when it refers to the CURRENT
            # request, and dropped otherwise.
            with self._lock:
                if self._is_current(req_id):
                    self.entitlement_errors.append(text)
            return
        if code < _FATAL_ERROR_CEILING and code not in _CONNECTIVITY_NOTICES:
            with self._lock:
                # IBKR reports connection/system-level errors with reqId -1 (or
                # 0/None); those are genuinely sticky and every waiter must fail.
                # A positive reqId that is NOT the current request belongs to a
                # snapshot that is already over -- making that sticky is exactly
                # what let a finished request's error fail the NEXT quote. It
                # cannot fail a later request, so it is dropped.
                connection_level = req_id is None or req_id <= 0
                if not connection_level and not self._is_current(req_id):
                    return
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
        #: ONE market-data snapshot at a time per client. This API is
        #: synchronous, so serialising is both sufficient and honest: two
        #: different securities can genuinely be executed concurrently (the
        #: execution single-flight de-duplicates one identity, it does not
        #: serialise two), and they would otherwise share these buffers.
        self._market_data_lock = threading.Lock()
        self._next_market_data_req_id = _MARKET_DATA_REQ_ID_BASE
        #: Set when cleanup for a snapshot could not be completed. The gateway
        #: may still be streaming that request, so anything this client returns
        #: afterwards could carry data from it -- refuse instead.
        self._poisoned: str | None = None

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

    def market_data_for_instrument(
        self,
        broker_instrument_ref: str,
        requested_market_data_type: int = _REQUESTED_MARKET_DATA_TYPE,
    ) -> MarketDataQuote:
        """A quote for THE resolved security, by its opaque reference.

        This is the execution path's only market-data entry point. The
        reference is decoded here -- inside the package that minted it -- and an
        unusable, foreign or missing one raises before any request is made.
        There is deliberately no ticker to fall back to: a caller that has
        resolved a security and then asks for a price by symbol has thrown away
        the identity it just established.
        """
        con_id = decode_instrument_ref(broker_instrument_ref)
        if con_id is None:
            raise PoiSessionError(
                "market data was requested with no usable broker instrument"
                " reference; refusing to price a security we cannot name"
            )
        return self._market_data(
            requested_market_data_type,
            # The request lineage IS the attribution: we know which security
            # this snapshot was asked for because we asked for it by id. It is
            # never inferred from a ticker in the response.
            requested_for=broker_instrument_ref,
            # Deliberately blank: this request named no symbol, and inventing
            # one from the reference would fabricate exactly the identity the
            # opaque reference exists to keep honest.
            symbol="",
            request=lambda transport, req_id: (
                transport.request_market_data_for_con_id(req_id, con_id)
            ),
        )

    def market_data(
        self, symbol: str, requested_market_data_type: int = _REQUESTED_MARKET_DATA_TYPE
    ) -> MarketDataQuote:
        """Ticker-shaped read-only quote, for the PoI tooling that predates
        contract resolution. NOT for the execution path -- see
        ``market_data_for_instrument``.
        """
        return self._market_data(
            requested_market_data_type,
            requested_for=f"symbol:{symbol}",
            symbol=symbol,
            request=lambda transport, req_id: (
                transport.request_market_data(req_id, symbol)
            ),
        )

    def _market_data(
        self,
        requested_market_data_type: int,
        *,
        requested_for: str,
        symbol: str,
        request: Callable[[MarketDataTransport, int], None],
    ) -> MarketDataQuote:
        """ONE snapshot: its own id, its own empty buffers, its own cleanup.

        The lock is held until the quote has been built from an IMMUTABLE
        snapshot. Releasing it after cancellation but before materialisation was
        a real race: the next request could arm itself and refill the shared sink
        while this caller was still reading it, so a quote for A could be
        assembled out of B's ticks. Serialising the *request* is not enough --
        the result has to be captured inside the same boundary.
        """
        with self._market_data_lock:
            sink, transport = self._require_open()
            # A genuine, sticky, connection-level fatal must fail HERE, before a
            # second reqMktData is ever issued -- not after waiting a whole
            # snapshot interval only to read the same fatal in _await. Reconnect
            # is the only thing that clears it (begin_market_data_request
            # deliberately leaves fatal_error alone).
            if sink.fatal_error is not None:
                raise PoiSessionError(sink.fatal_error)
            req_id = self._next_market_data_req_id
            self._next_market_data_req_id += 1
            # Arm BEFORE requesting: every buffer starts empty and every
            # callback is matched against this id.
            sink.begin_market_data_request(req_id)
            snapshot: MarketDataSnapshot | None = None
            # Set BEFORE the transport call, not after: a request that partially
            # reached the gateway and then raised still needs cancelling, and we
            # cannot tell from here how far it got.
            requested = False
            try:
                # Request the caller's mode explicitly (1 realtime / 3 delayed);
                # the ACTUAL type served is read from the marketDataType
                # callback and never assumed from the request.
                transport.set_market_data_type(requested_market_data_type)
                requested = True
                request(transport, req_id)
                self._await(sink.mkt_data_ready, "market data snapshot")
                # Freeze and disarm in ONE atomic step, while the request lock
                # is still held. From here the quote depends on nothing mutable.
                snapshot = sink.finish_market_data_request(req_id)
            finally:
                # Cancel only a request that is NOT already finished at the
                # gateway. A snapshot=True that reached tickSnapshotEnd is over;
                # cancelling it asks the gateway to cancel an id it no longer has
                # (IBKR 300) -- which used to be classified fatal and fail the
                # next quote. An uncertain request -- a timeout (snapshot is
                # None), a synchronous failure inside request() (never issued,
                # cancel=requested handles that), or an entitlement-unblocked
                # request that did not reach snapshot end -- is still cancelled,
                # because it may still be live. _release_market_data poisons the
                # client on a genuine cleanup failure, so that can never read as
                # permission.
                completed = snapshot is not None and snapshot.completed
                self._release_market_data(
                    transport, sink, req_id, cancel=requested and not completed
                )

            assert snapshot is not None  # noqa: S101 - or _await would have raised
            return self._quote_from(snapshot, requested_for=requested_for, symbol=symbol)

    def _quote_from(
        self, snapshot: MarketDataSnapshot, *, requested_for: str, symbol: str
    ) -> MarketDataQuote:
        """Build the quote from ONE frozen request result, and nothing else."""
        # Draw the quote from the family matching the ACTUAL served type, so a
        # delayed quote uses only delayed ticks (66-71, 88) and a realtime -1
        # placeholder can never clobber it (and vice versa).
        classification = data_classification(snapshot.market_data_type)
        family = (
            "delayed"
            if snapshot.market_data_type in _DELAYED_MARKET_DATA_TYPES
            else "realtime"
        )
        values = snapshot.values(family)
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
            requested_for=requested_for,
            market_data_type=snapshot.market_data_type,
            bid=bid,
            ask=ask,
            last=last,
            bid_size=values.get("bid_size"),
            ask_size=values.get("ask_size"),
            last_size=values.get("last_size"),
            exchange_timestamp=values.get("timestamp"),
            received_at=received_at,
            entitlement_errors=snapshot.entitlement_errors,
            missing_fields=missing,
            freshness=classify_freshness(snapshot.market_data_type),
            data_classification=classification,
            source_tick_ids=snapshot.source_tick_ids,
        )

    def close(self) -> None:
        if self._transport is not None:
            self._transport.stop()
        self._transport = None
        self._sink = None
        # A reconnect is the one thing that clears a poisoned client: the
        # subscription we could not cancel dies with the connection.
        self._poisoned = None

    # -- internals ----------------------------------------------------------
    def _require_open(self) -> tuple[MarketDataSink, MarketDataTransport]:
        if self._poisoned is not None:
            raise PoiSessionError(self._poisoned)
        if self._sink is None or self._transport is None:
            raise PoiSessionError("client is not open")
        return self._sink, self._transport

    def _release_market_data(
        self,
        transport: MarketDataTransport,
        sink: MarketDataSink,
        req_id: int,
        *,
        cancel: bool,
    ) -> None:
        """Cancel this snapshot and stop accepting its callbacks.

        Disarming happens first and unconditionally: even if the cancel call
        fails, the sink must stop treating this request as current, or a late
        tick would land in the next quote.

        ``cancel`` is False only when no request was ever issued -- there is
        nothing at the gateway to cancel, and calling cancelMktData for an id it
        never saw would be noise, not safety.
        """
        sink.end_market_data_request(req_id)
        if not cancel:
            return
        try:
            transport.cancel_market_data(req_id)  # read-only cleanup only
        except Exception as error:  # noqa: BLE001 - unproven cleanup is unsafe
            # The gateway may still be streaming this request. Anything this
            # client returned afterwards could carry its data, so the client
            # refuses until a reconnect kills the subscription.
            self._poisoned = (
                "market-data cleanup failed"
                f" ({type(error).__name__}); this client is no longer safe to"
                " reuse because a cancelled request may still be streaming."
                " Reconnect before requesting another quote."
            )

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

    def _by_con_id(con_id: int) -> Any:
        """The contract for ONE resolved security. No symbol, by design.

        A symbol here would be a second identity for the same request, and the
        two could disagree; ``conId`` alone is unambiguous to IBKR.
        """
        contract = Contract()
        contract.conId = con_id
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        return contract

    class _MarketDataApp(EWrapper, EClient):  # type: ignore[misc]  # pragma: no cover
        """EClient+EWrapper limited to read-only requests and callbacks."""

        def __init__(self) -> None:
            EClient.__init__(self, self)
            self._sink: MarketDataSink | None = None

        def bind(self, sink: MarketDataSink | None) -> None:
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

        # Every market-data callback carries IBKR's reqId, and every one of
        # them passes it on. Dropping it made a late callback from a finished
        # snapshot indistinguishable from one belonging to the current request.
        def marketDataType(self, req_id: int, market_data_type: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_market_data_type(market_data_type, req_id)

        def tickPrice(  # noqa: N802
            self, req_id: int, tick_type: int, price: float, attrib: Any
        ) -> None:
            if self._sink is not None:
                self._sink.on_tick_price(tick_type, price, req_id)

        def tickSize(self, req_id: int, tick_type: int, size: object) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_tick_size(tick_type, size, req_id)

        def tickString(self, req_id: int, tick_type: int, value: str) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_tick_string(tick_type, value, req_id)

        def tickSnapshotEnd(self, req_id: int) -> None:  # noqa: N802
            if self._sink is not None:
                self._sink.on_snapshot_end(req_id)

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
            # The reqId travels too, so a request-scoped market-data error
            # from a finished snapshot cannot attach itself to the current one.
            self._sink.on_error(
                normalized.code, normalized.message, normalized.request_id
            )

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
            """Deterministic teardown, including from a PARTIAL connection.

            The first real owner-host run aborted startup between connecting and
            finishing, and the SDK reader thread then tore down through
            ``serverVersion() == None`` and printed a traceback from a
            background thread. A probe that aborts cleanly must not leave that
            behind: every step here tolerates a half-established connection and
            none of them may raise past this method.
            """
            try:
                connected = bool(self._app.isConnected())
            except Exception:  # noqa: BLE001 - a partial connect cannot answer
                connected = False
            if connected:
                try:
                    self._app.disconnect()
                except Exception as error:  # noqa: BLE001 - teardown is best effort
                    _LOGGER.debug(
                        "market-data disconnect raised %s", type(error).__name__
                    )
            else:
                # Never connected, or connected far enough to have a socket and
                # no server version. Release the reader loop either way.
                try:
                    self._app.done = True
                except Exception:  # noqa: BLE001 - attribute may not exist yet
                    pass
            if self._thread is not None:
                self._thread.join(timeout=5)
                if self._thread.is_alive():
                    _LOGGER.warning(
                        "the market-data SDK thread did not stop within 5s"
                    )
                self._thread = None
            self._app.bind(None)

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

        def request_market_data_for_con_id(self, req_id: int, con_id: int) -> None:
            """reqMktData for the EXACT resolved security.

            ``conId`` identifies the security; the ticker travels beside it for
            IBKR's own logging and is not what the request is keyed on.
            """
            self._app.reqMktData(
                req_id, _by_con_id(con_id), "", True, False, []
            )

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
