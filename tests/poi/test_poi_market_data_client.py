"""POI market-data raw-client drills (fake transport; no IBKR contact).

The fake transport replays IBKR callbacks synchronously through the sink —
no socket, no SDK, no network. These prove contract resolution (single /
none / ambiguous), the ACTUAL marketDataType being recorded, tick
collection, honest timestamps, entitlement surfacing, timeout fail-closed
and read-only cleanup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from trading_broker_ibkr.poi_market_data_client import (
    IbkrMarketDataRawClient,
    MarketDataSink,
    classify_freshness,
)
from trading_broker_ibkr.poi_readonly_session import PoiSessionError

HOST = "127.0.0.1"
PORT = 4002
PAPER_ACCOUNT = "DU" + "1234567"

# One fixed epoch used by the fake feed for both current time and tick 45,
# with the expected ISO string derived from the same conversion the client
# performs (never hardcoded, so it can't drift from the code).
FEED_EPOCH = 1752518400
FEED_EPOCH_ISO = datetime.fromtimestamp(FEED_EPOCH, tz=UTC).isoformat()

# IBKR tick-type ids used by the fake feed.
BID, ASK, LAST = 1, 2, 4
BID_SIZE, ASK_SIZE, LAST_SIZE = 0, 3, 5
LAST_TIMESTAMP = 45


class FakeTransport:
    """Replays a scripted callback sequence into the sink, synchronously."""

    def __init__(
        self,
        *,
        con_ids: tuple[int, ...] = (12345,),
        contract_error: tuple[int, str] | None = None,
        market_data_type: int | None = 1,
        ticks: bool = True,
        last_timestamp: str | None = str(FEED_EPOCH),
        mkt_errors: tuple[tuple[int, str], ...] = (),
        snapshot_end: bool = True,
        drop_time: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self._sink: MarketDataSink | None = None
        self._con_ids = con_ids
        self._contract_error = contract_error
        self._market_data_type = market_data_type
        self._ticks = ticks
        self._last_timestamp = last_timestamp
        self._mkt_errors = mkt_errors
        self._snapshot_end = snapshot_end
        self._drop_time = drop_time

    def start(self, host: str, port: int, client_id: int, sink: MarketDataSink) -> None:
        self.calls.append(f"start:{host}:{port}:{client_id}")
        self._sink = sink
        sink.on_ready()

    def stop(self) -> None:
        self.calls.append("stop")

    def request_current_time(self) -> None:
        self.calls.append("current_time")
        assert self._sink is not None
        if not self._drop_time:
            self._sink.on_current_time(FEED_EPOCH)

    def request_account_summary(self, req_id: int, tags: str) -> None:
        self.calls.append("account_summary")
        assert self._sink is not None
        self._sink.on_account_value(PAPER_ACCOUNT, "NetLiquidation", "100000")
        self._sink.on_account_end()

    def cancel_account_summary(self, req_id: int) -> None:
        self.calls.append("cancel_account_summary")

    def request_contract_details(self, req_id: int, symbol: str) -> None:
        self.calls.append(f"contract_details:{symbol}")
        assert self._sink is not None
        if self._contract_error is not None:
            self._sink.on_error(*self._contract_error)
            return
        for con_id in self._con_ids:
            self._sink.on_contract(con_id)
        self._sink.on_contract_end()

    def set_market_data_type(self, market_data_type: int) -> None:
        self.calls.append(f"set_market_data_type:{market_data_type}")

    def request_market_data(self, req_id: int, symbol: str) -> None:
        self.calls.append(f"market_data:{symbol}")
        assert self._sink is not None
        if self._market_data_type is not None:
            self._sink.on_market_data_type(self._market_data_type)
        if self._ticks:
            self._sink.on_tick_price(BID, 191.20)
            self._sink.on_tick_price(ASK, 191.24)
            self._sink.on_tick_price(LAST, 191.22)
            self._sink.on_tick_size(BID_SIZE, 300)
            self._sink.on_tick_size(ASK_SIZE, 500)
            self._sink.on_tick_size(LAST_SIZE, 100)
            if self._last_timestamp is not None:
                self._sink.on_tick_string(LAST_TIMESTAMP, self._last_timestamp)
        for error in self._mkt_errors:
            self._sink.on_error(*error)
        if self._snapshot_end:
            self._sink.on_snapshot_end()

    def cancel_market_data(self, req_id: int) -> None:
        self.calls.append("cancel_market_data")


def build(transport: FakeTransport, **kwargs: Any) -> IbkrMarketDataRawClient:
    kwargs.setdefault("now", lambda: "2026-07-14T18:30:00+00:00")
    kwargs.setdefault("timeout_seconds", 1.0)
    return IbkrMarketDataRawClient(lambda: transport, **kwargs)


def opened(transport: FakeTransport, **kwargs: Any) -> IbkrMarketDataRawClient:
    client = build(transport, **kwargs)
    client.open(HOST, PORT)
    return client


# -- endpoint / connect ------------------------------------------------------------


def test_open_validates_endpoint_and_rejects_live_port() -> None:
    with pytest.raises(PoiSessionError, match="LIVE API port"):
        build(FakeTransport()).open(HOST, 4001)
    with pytest.raises(PoiSessionError, match="only 127.0.0.1/localhost"):
        build(FakeTransport()).open("10.0.0.9", PORT)


def test_current_time_and_account_summary() -> None:
    transport = FakeTransport()
    client = opened(transport)
    assert client.current_time() == FEED_EPOCH_ISO
    summary = client.account_summary()
    assert summary.account_id == PAPER_ACCOUNT
    assert "cancel_account_summary" in transport.calls


# -- contract resolution -----------------------------------------------------------


def test_single_contract_resolves() -> None:
    resolution = opened(FakeTransport(con_ids=(12345,))).resolve_contract("AAPL")
    assert resolution.resolved is True
    assert resolution.con_id_present is True


def test_no_contract_is_unresolved() -> None:
    resolution = opened(FakeTransport(con_ids=())).resolve_contract("ZZZZ")
    assert resolution.resolved is False
    assert resolution.con_id_present is False
    assert "no contract" in resolution.detail


def test_ambiguous_contract_fails_closed() -> None:
    resolution = opened(FakeTransport(con_ids=(1, 2, 3))).resolve_contract("AAPL")
    assert resolution.resolved is False
    assert "ambiguous" in resolution.detail
    assert "fail closed" in resolution.detail


def test_no_security_definition_error_is_unresolved() -> None:
    resolution = opened(
        FakeTransport(contract_error=(200, "No security definition has been found"))
    ).resolve_contract("BADSYM")
    assert resolution.resolved is False
    assert "200" in resolution.detail


# -- market data type + ticks ------------------------------------------------------


def test_realtime_quote_collected_and_classified() -> None:
    transport = FakeTransport(market_data_type=1)
    quote = opened(transport).market_data("AAPL")
    assert quote.market_data_type == 1
    assert quote.freshness == "REALTIME"
    assert (quote.bid, quote.ask, quote.last) == ("191.2", "191.24", "191.22")
    assert (quote.bid_size, quote.ask_size, quote.last_size) == ("300", "500", "100")
    assert quote.missing_fields == ()
    assert quote.received_at == "2026-07-14T18:30:00+00:00"
    assert quote.exchange_timestamp == FEED_EPOCH_ISO
    assert "set_market_data_type:1" in transport.calls
    assert "cancel_market_data" in transport.calls  # read-only cleanup


def test_delayed_and_frozen_and_unknown_types_map_correctly() -> None:
    assert opened(FakeTransport(market_data_type=3)).market_data("AAPL").freshness == "DELAYED"
    assert opened(FakeTransport(market_data_type=2)).market_data("AAPL").freshness == "STALE"
    assert opened(FakeTransport(market_data_type=4)).market_data("AAPL").freshness == "STALE"
    unknown = opened(FakeTransport(market_data_type=None)).market_data("AAPL")
    assert unknown.market_data_type is None
    assert unknown.freshness == "UNKNOWN"
    assert classify_freshness(99) == "UNKNOWN"


def test_reqmarketdatatype_one_does_not_force_realtime() -> None:
    # We ask for 1 but IBKR serves delayed (3); the ACTUAL type is reported.
    quote = opened(FakeTransport(market_data_type=3)).market_data("AAPL")
    assert quote.market_data_type == 3
    assert quote.freshness == "DELAYED"


def test_requested_market_data_type_is_forwarded_to_reqmarketdatatype() -> None:
    realtime = FakeTransport()
    opened(realtime).market_data("AAPL")  # default realtime
    assert "set_market_data_type:1" in realtime.calls
    delayed = FakeTransport(market_data_type=3)
    opened(delayed).market_data("AAPL", 3)  # explicit delayed
    assert "set_market_data_type:3" in delayed.calls


# -- honest timestamp / missing fields ---------------------------------------------


def test_missing_last_timestamp_is_not_invented() -> None:
    quote = opened(FakeTransport(last_timestamp=None)).market_data("AAPL")
    assert quote.exchange_timestamp is None  # never fabricated
    assert quote.received_at == "2026-07-14T18:30:00+00:00"  # our capture is real


def test_zero_last_timestamp_is_treated_as_absent() -> None:
    quote = opened(FakeTransport(last_timestamp="0")).market_data("AAPL")
    assert quote.exchange_timestamp is None


def test_no_ticks_reports_missing_fields_and_no_received_at() -> None:
    quote = opened(
        FakeTransport(ticks=False, last_timestamp=None)
    ).market_data("AAPL")
    assert set(quote.missing_fields) == {"bid", "ask", "last"}
    assert quote.received_at is None  # nothing received => no capture time


# -- entitlement handling ----------------------------------------------------------


def test_subscription_error_surfaced_and_unblocks() -> None:
    quote = opened(
        FakeTransport(
            ticks=False,
            last_timestamp=None,
            snapshot_end=False,  # no snapshot end; the error must unblock
            mkt_errors=((354, "Requested market data is not subscribed"),),
        )
    ).market_data("AAPL")
    assert any("354" in e for e in quote.entitlement_errors)
    assert quote.missing_fields  # and the missing data is still reported honestly


def test_delayed_notice_recorded_without_silent_downgrade() -> None:
    quote = opened(
        FakeTransport(
            market_data_type=3,
            mkt_errors=((10167, "Market data farm: displaying delayed data"),),
        )
    ).market_data("AAPL")
    assert any("10167" in e for e in quote.entitlement_errors)
    assert quote.market_data_type == 3  # explicit, not hidden


# -- fail-closed -------------------------------------------------------------------


def test_timeout_fails_closed() -> None:
    # drop_time: the current-time callback never fires => wait times out.
    client = opened(FakeTransport(drop_time=True))
    with pytest.raises(PoiSessionError, match="timed out"):
        client.current_time()


def test_fatal_error_fails_closed() -> None:
    client = opened(
        FakeTransport(contract_error=(502, "Couldn't connect to TWS"))
    )
    with pytest.raises(PoiSessionError, match="IBKR error 502"):
        client.resolve_contract("AAPL")


def test_calls_before_open_fail_closed() -> None:
    client = build(FakeTransport())
    with pytest.raises(PoiSessionError, match="not open"):
        client.market_data("AAPL")


# -- read-only surface -------------------------------------------------------------


def test_no_order_methods_or_imports_in_client_source() -> None:
    import trading_broker_ibkr.poi_market_data_client as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for fragment in (
        "place" + "Order",
        "cancel" + "Order",
        "reqGlobal" + "Cancel",
        "exercise" + "Options",
        "OrderIntent",
        "SubmitAuthorization",
    ):
        assert fragment not in source, fragment
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord"):
        assert fragment not in source, fragment
    # ibapi is only ever imported lazily inside the build function, never at
    # module top level.
    top_level = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    for line in top_level:
        assert "ib" + "api" not in line, line
