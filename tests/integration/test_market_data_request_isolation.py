"""One snapshot must never be built from another snapshot's data.

``IbkrMarketDataRawClient`` owns ONE ``MarketDataSink`` for the life of the
connection, and the buffers it fills -- the realtime and delayed tick families,
the served market-data type, the source tick ids, the entitlement errors -- are
REQUEST-local. Before this, a new snapshot cleared only ``mkt_data_ready``, so a
request that received nothing, or only some ticks, was answered from whatever the
previous security had left behind.

The failure is not theoretical and not cosmetic:

* B's bid beside A's ask, presented as B's quote;
* B inheriting A's REALTIME classification when B was never classified;
* B inheriting A's entitlement error, or clearing its own;
* and worst, ``received_at = self._now() if values else None`` -- A's leftover
  values made ``values`` non-empty, so A's stale prices were stamped with a
  fresh capture time. That is stale data wearing a current timestamp, which is
  exactly what the quote-freshness gate exists to catch.

``requested_for`` said B throughout, which is what made it dangerous: the
attribution was right and the data was wrong.

Every test here drives the REAL chain -- ``IbkrQuoteSource`` ->
``IbkrMarketDataSession`` -> ``IbkrMarketDataRawClient`` -- over ONE persistent
connection, because a fresh chain per quote is precisely what would hide this.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from trading_application.trading_readiness_runtime import MarketDataState, QuoteState
from trading_broker_core.port import BrokerInstrument
from trading_broker_ibkr.poi_market_data_client import (
    IbkrMarketDataRawClient,
    MarketDataSink,
)
from trading_broker_ibkr.poi_market_data_session import IbkrMarketDataSession
from trading_broker_ibkr.poi_readonly_session import PoiSessionError
from trading_execution_agent.ibkr_quote_source import IbkrQuoteSource

CAPTURED_AT = "2026-08-19T15:30:01+00:00"

BID, ASK, LAST = 1, 2, 4
BID_SIZE, ASK_SIZE, LAST_SIZE = 0, 3, 5
#: IBKR's "no market data permissions" family; the sink treats these as
#: request-scoped entitlement problems.
SUBSCRIPTION_ERROR = 354


def _instrument(reference: str) -> BrokerInstrument:
    return BrokerInstrument(
        broker_instrument_ref=reference,
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
    )


A = _instrument("conid:111111")
B = _instrument("conid:222222")


class _Script:
    """What the gateway does for one snapshot."""

    def __init__(
        self,
        *,
        market_data_type: int | None = 1,
        prices: dict[int, float] | None = None,
        sizes: dict[int, int] | None = None,
        entitlement_error: bool = False,
        snapshot_end: bool = True,
    ) -> None:
        self.market_data_type = market_data_type
        self.prices = prices or {}
        self.sizes = sizes or {}
        self.entitlement_error = entitlement_error
        self.snapshot_end = snapshot_end


class ScriptedMarketData:
    """The SDK seam, driven by a per-request script and honest about reqId.

    Every callback carries the reqId of the request it belongs to, exactly as
    the real wrapper now does, so a late callback can be modelled faithfully.
    """

    def __init__(
        self,
        *scripts: _Script,
        by_con_id: dict[int, _Script] | None = None,
    ) -> None:
        self._scripts = list(scripts)
        #: Keyed by SECURITY. Arrival order proves nothing about which quote a
        #: security received; keying the gateway's answers by con id is what
        #: makes "conid:A got A's price" an actual assertion.
        self._by_con_id = by_con_id or {}
        self.requests: list[tuple[int, int]] = []  # (req_id, con_id)
        self.cancelled: list[int] = []
        self.calls: list[str] = []
        self.cancel_error: Exception | None = None
        self.request_error: Exception | None = None
        self._sink: MarketDataSink | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, host: str, port: int, client_id: int, sink: MarketDataSink) -> None:
        self._sink = sink
        sink.on_ready()

    def stop(self) -> None:
        self._sink = None

    def request_current_time(self) -> None:
        assert self._sink is not None
        self._sink.on_current_time(0)

    def request_account_summary(self, req_id: int, tags: str) -> None:
        raise NotImplementedError

    def cancel_account_summary(self, req_id: int) -> None:
        raise NotImplementedError

    def request_contract_details(self, req_id: int, symbol: str) -> None:
        raise NotImplementedError

    def set_market_data_type(self, market_data_type: int) -> None:
        self.calls.append(f"set_market_data_type:{market_data_type}")

    def request_market_data(self, req_id: int, symbol: str) -> None:
        raise AssertionError("the execution path must never request by ticker")

    def request_market_data_for_con_id(self, req_id: int, con_id: int) -> None:
        self.requests.append((req_id, con_id))
        self.calls.append(f"reqMktData:{req_id}:{con_id}")
        if self.request_error is not None:
            raise self.request_error
        keyed = self._by_con_id.get(con_id)
        if keyed is not None:
            self.play(keyed, req_id)
            return
        index = len(self.requests) - 1
        if index < len(self._scripts):
            self.play(self._scripts[index], req_id)

    def cancel_market_data(self, req_id: int) -> None:
        self.cancelled.append(req_id)
        self.calls.append(f"cancel:{req_id}")
        if self.cancel_error is not None:
            raise self.cancel_error

    # -- the gateway's side ------------------------------------------------
    def play(self, script: _Script, req_id: int) -> None:
        """Deliver one script's callbacks under ``req_id``."""
        assert self._sink is not None
        sink = self._sink
        if script.market_data_type is not None:
            sink.on_market_data_type(script.market_data_type, req_id)
        for tick, price in script.prices.items():
            sink.on_tick_price(tick, price, req_id)
        for tick, size in script.sizes.items():
            sink.on_tick_size(tick, size, req_id)
        if script.entitlement_error:
            sink.on_error(SUBSCRIPTION_ERROR, "no market data permissions", req_id)
        if script.snapshot_end:
            sink.on_snapshot_end(req_id)


def _chain(*scripts: _Script) -> tuple[IbkrQuoteSource, ScriptedMarketData]:
    """ONE persistent connected chain. A fresh chain per quote hides the bug."""
    transport = ScriptedMarketData(*scripts)
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=0.2
    )
    session = IbkrMarketDataSession(client)
    session.connect("127.0.0.1", 4002)
    return IbkrQuoteSource(session), transport


FULL = _Script(prices={BID: 100.00, ASK: 100.05, LAST: 100.02},
               sizes={BID_SIZE: 500, ASK_SIZE: 400})


# == sequential contamination ================================================


def test_a_silent_second_snapshot_returns_nothing_not_the_first_one() -> None:
    """Test A. B gets no price ticks at all, and must answer with nothing.

    Before the fix this returned A's prices as B's, complete with a freshly
    stamped ``received_at``.
    """
    source, transport = _chain(FULL, _Script(market_data_type=None))

    first = source.current_quote(A)
    second = source.current_quote(B)

    assert first.bid is not None and first.ask is not None, "A really had a quote"
    assert second.bid is None, second.bid
    assert second.ask is None, second.ask
    assert second.observed_at is None, (
        "an empty snapshot must not be stamped with a capture time"
    )
    assert second.bid_size is None and second.ask_size is None
    assert second.market_data_state is MarketDataState.UNKNOWN, (
        "B was never classified, so it inherits nothing"
    )
    assert "requested_for=conid:222222" in second.detail
    assert len(transport.requests) == 2


def test_a_partial_second_snapshot_never_borrows_the_missing_half() -> None:
    """Test B. A had bid+ask; B has a bid only. B's ask stays absent."""
    source, _ = _chain(FULL, _Script(prices={BID: 250.00}))

    first = source.current_quote(A)
    second = source.current_quote(B)

    assert first.ask is not None
    assert second.bid == Decimal("250")
    assert second.ask is None, (
        f"B borrowed A's ask ({second.ask}); a half-quote is not a quote"
    )


def test_an_unclassified_snapshot_does_not_inherit_realtime() -> None:
    """Test C. A was REALTIME; B reported no marketDataType."""
    source, _ = _chain(
        _Script(market_data_type=1, prices={BID: 100.00, ASK: 100.05}),
        _Script(market_data_type=None, prices={BID: 250.00, ASK: 250.10}),
    )

    first = source.current_quote(A)
    second = source.current_quote(B)

    assert first.market_data_state is MarketDataState.REALTIME
    assert second.market_data_state is MarketDataState.UNKNOWN, (
        "an unclassified snapshot must never be reported as realtime"
    )


def test_a_clean_snapshot_does_not_inherit_an_entitlement_error() -> None:
    """Test D. A hit a subscription error; B is clean."""
    source, _ = _chain(
        _Script(market_data_type=None, entitlement_error=True, snapshot_end=False),
        FULL,
    )

    first = source.current_quote(A)
    second = source.current_quote(B)

    assert "354" in first.detail, first.detail
    assert "354" not in second.detail, (
        f"B inherited A's request-scoped entitlement error: {second.detail}"
    )
    assert second.market_data_state is MarketDataState.REALTIME


def test_every_snapshot_starts_from_empty_request_local_state() -> None:
    """The reset is one atomic operation on the sink, not scattered clears."""
    sink = MarketDataSink()
    sink.begin_market_data_request(1)
    sink.on_market_data_type(1, 1)
    sink.on_tick_price(BID, 100.0, 1)
    sink.on_error(SUBSCRIPTION_ERROR, "denied", 1)
    assert sink.families["realtime"], "state was collected"

    sink.begin_market_data_request(2)

    assert sink.market_data_type is None
    assert sink.families == {"realtime": {}, "delayed": {}}
    assert sink.source_tick_ids == []
    assert sink.entitlement_errors == []
    assert sink.got_tick is False
    assert not sink.mkt_data_ready.is_set()


def test_a_fatal_connection_error_is_not_cleared_by_a_new_request() -> None:
    """Connection health is sticky until reconnect; only request state resets."""
    sink = MarketDataSink()
    sink.on_callback_shape_error("unsupported error callback")
    assert sink.fatal_error is not None

    sink.begin_market_data_request(7)

    assert sink.fatal_error is not None, (
        "a per-request reset must not clear connection-level fail-closed state"
    )


# == late-callback correlation ===============================================


def test_a_late_callback_from_a_timed_out_request_cannot_reach_the_next_one(
) -> None:
    """The full sequence: A times out, B starts, A's callbacks arrive late."""
    # A's script never ends the snapshot, so A times out.
    source, transport = _chain(
        _Script(market_data_type=1, prices={BID: 100.00}, snapshot_end=False)
    )

    first = source.current_quote(A)
    assert first.market_data_state is MarketDataState.UNAVAILABLE, (
        "A timed out, and a timeout is not a quote"
    )
    (a_req_id, _con) = transport.requests[0]
    assert a_req_id in transport.cancelled, (
        "cancellation ran even though the wait failed"
    )

    # B starts. Its own ticks arrive interleaved with A's late ones.
    b_script = _Script(market_data_type=1, prices={BID: 250.00, ASK: 250.10})
    transport._scripts.append(b_script)  # noqa: SLF001 - scripting the gateway

    class _LateSender:
        """Delivers A's late callbacks while B is in flight."""

        def __init__(self) -> None:
            self.sent = False

        def __call__(self, market_data_type: int) -> None:
            if self.sent:
                return
            self.sent = True

    # Drive B, injecting A's late callbacks first.
    original_request = transport.request_market_data_for_con_id
    late: list[str] = []

    def racing(req_id: int, con_id: int) -> None:
        transport.requests.append((req_id, con_id))
        transport.calls.append(f"reqMktData:{req_id}:{con_id}")
        sink = transport._sink  # noqa: SLF001
        assert sink is not None
        # A's late traffic, under A's OWN id. A different classification too,
        # so an inherited one would be visible rather than coincidentally equal.
        sink.on_market_data_type(3, a_req_id)
        sink.on_tick_price(BID, 999.99, a_req_id)
        sink.on_tick_price(ASK, 999.99, a_req_id)
        sink.on_snapshot_end(a_req_id)
        late.append("a-late-delivered")
        # Then B's own.
        transport.play(b_script, req_id)

    transport.request_market_data_for_con_id = racing  # type: ignore[method-assign]
    second = source.current_quote(B)
    transport.request_market_data_for_con_id = original_request  # type: ignore[method-assign]

    assert late == ["a-late-delivered"], "the late callbacks really were sent"
    (b_req_id, _b_con) = transport.requests[-1]
    assert b_req_id != a_req_id, "each snapshot takes its own request id"
    assert second.bid == Decimal("250"), f"A's late tick entered B: {second.bid}"
    assert second.ask == Decimal("250.10")
    assert second.market_data_state is MarketDataState.REALTIME, (
        "A's late marketDataType (DELAYED) must not reclassify B"
    )


def test_a_late_snapshot_end_cannot_complete_the_next_request() -> None:
    """An old tickSnapshotEnd must not unblock a snapshot that has no data."""
    source, transport = _chain(_Script(market_data_type=1, snapshot_end=False))

    source.current_quote(A)  # times out
    (a_req_id, _con) = transport.requests[0]

    def only_a_late_end(req_id: int, con_id: int) -> None:
        transport.requests.append((req_id, con_id))
        sink = transport._sink  # noqa: SLF001
        assert sink is not None
        sink.on_snapshot_end(a_req_id)  # the OLD request's completion

    transport.request_market_data_for_con_id = only_a_late_end  # type: ignore[method-assign]

    second = source.current_quote(B)

    assert second.market_data_state is MarketDataState.UNAVAILABLE, (
        "B was completed by A's snapshot end instead of timing out"
    )


def test_each_snapshot_takes_a_distinct_request_id() -> None:
    source, transport = _chain(FULL, FULL, FULL)

    for instrument in (A, B, A):
        source.current_quote(instrument)

    ids = [req_id for req_id, _con in transport.requests]
    assert len(set(ids)) == len(ids) == 3, ids
    assert ids == sorted(ids), "deterministic, monotonic allocation"
    # A COMPLETED snapshot is not cancelled. snapshot=True auto-terminates at
    # tickSnapshotEnd, so cancelling it afterwards asks the gateway to drop an
    # id it no longer has (IBKR 300) -- which the third owner-host run showed
    # was being classified fatal and failing the NEXT quote on the session.
    assert transport.cancelled == [], "a completed snapshot must not be cancelled"


# == cleanup ==================================================================


def test_cancellation_runs_even_when_the_wait_fails() -> None:
    source, transport = _chain(_Script(snapshot_end=False))

    state = source.current_quote(A)

    assert state.market_data_state is MarketDataState.UNAVAILABLE
    assert transport.cancelled == [transport.requests[0][0]], (
        "a timed-out request must still be cancelled, by its own id"
    )


def test_a_failed_cleanup_poisons_the_client_rather_than_continuing() -> None:
    """If we cannot prove an UNCERTAIN request stopped, we stop instead.

    A completed snapshot is no longer cancelled at all, so the failed-cleanup
    poison is exercised where it actually matters: a request that did NOT
    complete (a timeout), where the cancel is genuinely needed to stop a
    possibly-live subscription and genuinely fails.
    """
    source, transport = _chain(_Script(snapshot_end=False), FULL)
    transport.cancel_error = RuntimeError("cancel rejected")

    first = source.current_quote(A)
    assert first.market_data_state is MarketDataState.UNAVAILABLE, (
        "the uncertain request could not complete"
    )

    second = source.current_quote(B)

    assert second.market_data_state is MarketDataState.UNAVAILABLE, (
        "a client whose cleanup failed must refuse, not answer"
    )
    assert "PoiSessionError" in second.detail, second.detail
    assert len(transport.requests) == 1, (
        "no further market-data request was issued after the failed cleanup"
    )


def test_the_poisoned_client_reports_why() -> None:
    # The first request does not complete (no snapshot end), so its cleanup
    # cancel runs and fails -- the path that still poisons.
    transport = ScriptedMarketData(_Script(snapshot_end=False), FULL)
    transport.cancel_error = RuntimeError("cancel rejected")
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=0.2
    )
    client.open("127.0.0.1", 4002)
    with pytest.raises(PoiSessionError):
        client.market_data_for_instrument("conid:111111")  # times out; cleanup fails

    with pytest.raises(PoiSessionError, match="no longer safe to reuse"):
        client.market_data_for_instrument("conid:222222")


# == concurrency ==============================================================




#: The gateway's answers keyed by SECURITY. Sorting two prices and checking the
#: set appear proves only that both arrived somewhere; it says nothing about
#: which security got which. Neither does ``requested_for``, which comes from
#: the request argument and stays correct even when the underlying data is
#: another request's.
BY_SECURITY = {
    111111: _Script(market_data_type=1, prices={BID: 100.00, ASK: 100.05}),
    222222: _Script(market_data_type=1, prices={BID: 250.00, ASK: 250.10}),
}


def _keyed_chain() -> tuple[IbkrQuoteSource, ScriptedMarketData]:
    transport = ScriptedMarketData(by_con_id=BY_SECURITY)
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=1.0
    )
    session = IbkrMarketDataSession(client)
    session.connect("127.0.0.1", 4002)
    return IbkrQuoteSource(session), transport


def test_each_security_receives_exactly_its_own_prices_concurrently() -> None:
    """Two threads, one session, two securities -- bound by identity.

    Every assertion names a security and the price that security's own request
    returned. Nothing is sorted, so a swapped pair fails.
    """
    source, transport = _keyed_chain()
    start = threading.Barrier(2)
    quotes: dict[str, QuoteState] = {}
    guard = threading.Lock()

    def run(instrument: BrokerInstrument) -> None:
        start.wait(timeout=5)
        state = source.current_quote(instrument)
        with guard:
            quotes[instrument.broker_instrument_ref] = state

    threads = [
        threading.Thread(target=run, args=(instrument,)) for instrument in (A, B)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    first = quotes["conid:111111"]
    second = quotes["conid:222222"]
    assert first.bid == Decimal("100"), f"conid:111111 got {first.bid}"
    assert first.ask == Decimal("100.05"), f"conid:111111 got {first.ask}"
    assert second.bid == Decimal("250"), f"conid:222222 got {second.bid}"
    assert second.ask == Decimal("250.10"), f"conid:222222 got {second.ask}"
    assert "requested_for=conid:111111" in first.detail
    assert "requested_for=conid:222222" in second.detail

    # Serialised by the request lock, not by a cancel between requests. Both
    # snapshots complete, so neither is cancelled; the per-security price
    # binding above is what proves the two requests did not interleave.
    request_calls = [c for c in transport.calls if c.startswith("reqMktData:")]
    cancels = [c for c in transport.calls if c.startswith("cancel:")]
    assert len(request_calls) == 2, request_calls
    assert cancels == [], "completed snapshots are not cancelled"
    assert len({c.split(":")[1] for c in request_calls}) == 2, "distinct req ids"


#: Delayed ticks live in their own family (66 bid / 68 last / 67 ask).
DELAYED_BID, DELAYED_ASK = 66, 67

#: A is served REALTIME, B is served DELAYED. The classification is the field
#: that actually leaked: ``market_data_type`` was read from the mutable sink
#: AFTER ``received_at = self._now()``, so a request completing inside that
#: window relabelled the finished quote. It gates Paper (REALTIME|DELAYED) and
#: Live (REALTIME only), so a wrong label is a wrong safety decision.
RACING_SECURITIES = {
    111111: _Script(market_data_type=1, prices={BID: 100.00, ASK: 100.05}),
    222222: _Script(
        market_data_type=3, prices={DELAYED_BID: 250.00, DELAYED_ASK: 250.10}
    ),
}


def test_a_quote_is_materialised_before_the_next_request_can_touch_the_sink(
) -> None:
    """THE materialisation race, forced rather than hoped for.

    A completes its callbacks and begins building its quote. B is released at
    exactly that moment and resets the shared sink with its own data. Because
    the result was still being read from the mutable sink after the request lock
    had been released, A's finished quote was relabelled with B's served type:
    A, actually REALTIME, was reported DELAYED.

    The interleaving is forced from inside A's own materialisation path, so this
    does not depend on scheduling luck.
    """
    transport = ScriptedMarketData(by_con_id=RACING_SECURITIES)
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=1.0
    )
    session = IbkrMarketDataSession(client)
    session.connect("127.0.0.1", 4002)
    source = IbkrQuoteSource(session)

    b_started = threading.Event()
    b_done = threading.Event()

    def start_b() -> None:
        b_started.set()
        source.current_quote(B)
        b_done.set()

    def racing_now() -> str:
        # Called while A materialises. If materialisation sat outside the
        # request lock, B would get in here and reset the sink underneath it.
        if not b_started.is_set():
            threading.Thread(target=start_b, daemon=True).start()
            b_started.wait(timeout=2)
            b_done.wait(timeout=0.5)
        return CAPTURED_AT

    client._now = racing_now  # noqa: SLF001

    a_quote = source.current_quote(A)
    b_done.wait(timeout=5)

    assert b_started.is_set(), "the racing request really was started"
    assert a_quote.market_data_state is MarketDataState.REALTIME, (
        "A was served REALTIME and came back labelled"
        f" {a_quote.market_data_state.value}; the next request relabelled a"
        " finished quote"
    )
    assert a_quote.bid == Decimal("100"), a_quote.bid
    assert a_quote.ask == Decimal("100.05"), a_quote.ask
    assert "requested_for=conid:111111" in a_quote.detail
    assert "ibkr_market_data_type=1" in a_quote.detail, a_quote.detail


# == atomicity of the identity check and the completion signal ==============


def test_an_old_snapshot_end_that_passed_its_check_cannot_complete_the_next(
) -> None:
    """TOCTOU: the check and the signal must be ONE critical section.

    If ``on_snapshot_end`` validated under the lock and signalled outside it,
    an old callback could pass its check, the request could end and the next
    begin (clearing the event), and the old callback would then complete a
    snapshot that has no data at all.
    """
    sink = MarketDataSink()
    sink.begin_market_data_request(1)
    sink.finish_market_data_request(1)
    sink.begin_market_data_request(2)

    sink.on_snapshot_end(1)  # request 1 is long over

    assert not sink.mkt_data_ready.is_set(), (
        "an old snapshot end completed the new request"
    )


class _LockWatchingEvent(threading.Event):
    """Records whether the sink lock was held at the moment ``set()`` ran.

    This is what actually distinguishes atomic from not. Both shapes -- check
    under the lock and signal inside it, or check under the lock and signal
    after releasing it -- look identical from outside, because both block on the
    lock for the check. The difference is only visible at the instant of the
    signal, so that is where this looks.
    """

    def __init__(self, lock: threading.Lock) -> None:
        super().__init__()
        self._watched_lock = lock
        self.locked_when_set: bool | None = None

    def set(self) -> None:
        self.locked_when_set = self._watched_lock.locked()
        super().set()


def _watched_sink() -> MarketDataSink:
    sink = MarketDataSink()
    sink.mkt_data_ready = _LockWatchingEvent(sink._lock)  # noqa: SLF001
    return sink


def test_the_completion_signal_is_set_inside_the_identity_check() -> None:
    """No window exists between "is this request current?" and completing it.

    A separate critical section is a TOCTOU: an old callback passes its check,
    the request then ends and the next begins (clearing the event), and the old
    callback completes a snapshot that never had any data.
    """
    sink = _watched_sink()
    sink.begin_market_data_request(1)

    sink.on_snapshot_end(1)

    event = sink.mkt_data_ready
    assert isinstance(event, _LockWatchingEvent)
    assert event.is_set()
    assert event.locked_when_set is True, (
        "the completion signal ran outside the sink lock, so the identity check"
        " and the signal are two critical sections and not one"
    )


def test_the_entitlement_error_signal_is_also_set_inside_the_check() -> None:
    """The error path unblocks the caller too, so it needs the same atomicity."""
    sink = _watched_sink()
    sink.begin_market_data_request(1)

    sink.on_error(SUBSCRIPTION_ERROR, "no market data permissions", 1)

    event = sink.mkt_data_ready
    assert isinstance(event, _LockWatchingEvent)
    assert event.is_set()
    assert event.locked_when_set is True, (
        "a subscription error completed the request from outside the lock"
    )


def test_the_request_reset_clears_the_completion_event_inside_the_lock() -> None:
    """The other half of the same window: arming must be atomic too."""
    sink = MarketDataSink()
    sink.begin_market_data_request(1)
    sink.on_snapshot_end(1)
    assert sink.mkt_data_ready.is_set()

    cleared_under_lock: list[bool] = []
    original_clear = sink.mkt_data_ready.clear

    def watching_clear() -> None:
        cleared_under_lock.append(sink._lock.locked())  # noqa: SLF001
        original_clear()

    sink.mkt_data_ready.clear = watching_clear  # type: ignore[method-assign]

    sink.begin_market_data_request(2)

    assert cleared_under_lock == [True], (
        "the completion event was cleared outside the lock, so a callback that"
        " had already passed its check could set it again afterwards"
    )


def test_an_old_subscription_error_cannot_complete_the_next_request() -> None:
    """The same TOCTOU on the error path, which also unblocks the caller."""
    sink = MarketDataSink()
    sink.begin_market_data_request(1)
    sink.finish_market_data_request(1)
    sink.begin_market_data_request(2)

    sink.on_error(SUBSCRIPTION_ERROR, "no market data permissions", 1)

    assert not sink.mkt_data_ready.is_set(), (
        "an old request's entitlement error completed the new request"
    )
    assert sink.entitlement_errors == [], (
        "and it did not attach itself to the new request either"
    )


def test_a_completed_snapshot_is_frozen_and_detached_from_the_sink() -> None:
    """The capture is immutable: later callbacks cannot alter a taken result."""
    sink = MarketDataSink()
    sink.begin_market_data_request(1)
    sink.on_market_data_type(1, 1)
    sink.on_tick_price(BID, 100.00, 1)

    snapshot = sink.finish_market_data_request(1)

    # A whole new request refills the sink completely.
    sink.begin_market_data_request(2)
    sink.on_market_data_type(3, 2)
    sink.on_tick_price(BID, 250.00, 2)

    assert snapshot.req_id == 1
    assert snapshot.market_data_type == 1
    assert snapshot.realtime["bid"] == "100.0"
    assert snapshot.values("realtime")["bid"] == "100.0"


# == the request call itself failing =========================================


def test_a_request_that_raises_still_disarms_and_cancels() -> None:
    """Cleanup is guaranteed from the moment the request is attempted.

    A synchronous failure inside ``request(...)`` used to happen BEFORE the
    try/finally, so nothing was cancelled -- and that is exactly the boundary
    where we cannot know how much of the request reached the gateway.
    """
    source, transport = _keyed_chain()
    transport.request_error = RuntimeError("socket died mid-request")

    state = source.current_quote(A)

    assert state.market_data_state is MarketDataState.UNAVAILABLE
    assert state.bid is None and state.ask is None
    assert len(transport.requests) == 1
    req_id = transport.requests[0][0]
    assert transport.cancelled == [req_id], (
        "the attempted request was cancelled under its own id"
    )

    # Cleanup succeeded, so the client is still usable and the next request is
    # a clean one.
    transport.request_error = None
    recovered = source.current_quote(B)
    assert recovered.bid == Decimal("250"), recovered.bid


def test_a_request_that_raises_with_a_failing_cancel_poisons_the_client(
) -> None:
    """Two failures at once: we cannot prove the request stopped, so we stop."""
    source, transport = _keyed_chain()
    transport.request_error = RuntimeError("socket died mid-request")
    transport.cancel_error = RuntimeError("cancel rejected")

    first = source.current_quote(A)
    assert first.market_data_state is MarketDataState.UNAVAILABLE

    transport.request_error = None
    second = source.current_quote(B)

    assert second.market_data_state is MarketDataState.UNAVAILABLE, (
        "a poisoned client must refuse rather than answer"
    )
    assert "PoiSessionError" in second.detail, second.detail
    assert len(transport.requests) == 1, (
        "no further market-data request was issued after the failed cleanup"
    )


def test_no_request_is_cancelled_when_none_was_ever_issued() -> None:
    """Failing before the request means there is nothing at the gateway."""
    source, transport = _keyed_chain()

    def exploding(market_data_type: int) -> None:
        raise RuntimeError("reqMarketDataType failed")

    transport.set_market_data_type = exploding  # type: ignore[method-assign]

    state = source.current_quote(A)

    assert state.market_data_state is MarketDataState.UNAVAILABLE
    assert transport.requests == [], "no request was issued"
    assert transport.cancelled == [], (
        "cancelling an id the gateway never saw is noise, not safety"
    )
