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
from trading_application.trading_readiness_runtime import MarketDataState
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

    def __init__(self, *scripts: _Script) -> None:
        self._scripts = list(scripts)
        self.requests: list[tuple[int, int]] = []  # (req_id, con_id)
        self.cancelled: list[int] = []
        self.calls: list[str] = []
        self.cancel_error: Exception | None = None
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
    assert transport.cancelled == ids, "every request was cancelled by its own id"


# == cleanup ==================================================================


def test_cancellation_runs_even_when_the_wait_fails() -> None:
    source, transport = _chain(_Script(snapshot_end=False))

    state = source.current_quote(A)

    assert state.market_data_state is MarketDataState.UNAVAILABLE
    assert transport.cancelled == [transport.requests[0][0]], (
        "a timed-out request must still be cancelled, by its own id"
    )


def test_a_failed_cleanup_poisons_the_client_rather_than_continuing() -> None:
    """If we cannot prove the old request stopped, we stop instead."""
    source, transport = _chain(FULL, FULL)
    transport.cancel_error = RuntimeError("cancel rejected")

    first = source.current_quote(A)
    assert first.bid is not None, "the first quote itself still completed"

    second = source.current_quote(B)

    assert second.market_data_state is MarketDataState.UNAVAILABLE, (
        "a client whose cleanup failed must refuse, not answer"
    )
    assert "PoiSessionError" in second.detail, second.detail
    assert len(transport.requests) == 1, (
        "no further market-data request was issued after the failed cleanup"
    )


def test_the_poisoned_client_reports_why() -> None:
    transport = ScriptedMarketData(FULL, FULL)
    transport.cancel_error = RuntimeError("cancel rejected")
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=0.2
    )
    client.open("127.0.0.1", 4002)
    client.market_data_for_instrument("conid:111111")

    with pytest.raises(PoiSessionError, match="no longer safe to reuse"):
        client.market_data_for_instrument("conid:222222")


# == concurrency ==============================================================


def test_two_securities_requested_concurrently_never_share_a_buffer() -> None:
    """Two DIFFERENT securities, one session, two threads.

    Execution single-flight de-duplicates one identity; it does not serialise
    two different decisions, so the market-data client has to hold this line
    itself. The API is synchronous, so it serialises -- and the proof is that
    each quote contains only its own ticks and each request is cancelled before
    the next is issued.
    """
    scripts = [
        _Script(market_data_type=1, prices={BID: 100.00, ASK: 100.05}),
        _Script(market_data_type=1, prices={BID: 250.00, ASK: 250.10}),
    ]
    source, transport = _chain(*scripts)
    start = threading.Barrier(2)
    results: dict[str, object] = {}
    guard = threading.Lock()

    def run(instrument: BrokerInstrument) -> None:
        start.wait(timeout=5)
        state = source.current_quote(instrument)
        with guard:
            results[instrument.broker_instrument_ref] = state

    threads = [
        threading.Thread(target=run, args=(instrument,)) for instrument in (A, B)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    quotes = {ref: state for ref, state in results.items()}
    bids = sorted(Decimal(str(getattr(state, "bid", 0))) for state in quotes.values())
    assert bids == [Decimal("100"), Decimal("250")], bids
    for ref, state in quotes.items():
        assert f"requested_for={ref}" in getattr(state, "detail", ""), ref

    # Serialised: request, cancel, request, cancel -- never two open at once.
    market_data_calls = [
        call for call in transport.calls
        if call.startswith(("reqMktData:", "cancel:"))
    ]
    assert len(market_data_calls) == 4, market_data_calls
    assert market_data_calls[0].startswith("reqMktData:")
    assert market_data_calls[1].startswith("cancel:")
    assert market_data_calls[2].startswith("reqMktData:")
    assert market_data_calls[3].startswith("cancel:")
    first_id = market_data_calls[0].split(":")[1]
    assert market_data_calls[1] == f"cancel:{first_id}", (
        "the first request was cancelled before the second was issued"
    )
