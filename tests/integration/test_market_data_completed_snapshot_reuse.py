"""The third owner-host defect: quote 1 succeeds, quote 2 fails on one session.

Both owner-host runs showed the same contradiction. The independent evidence
quote for AAPL (`conid:265598`) returned REALTIME bid/ask/sizes. The canonical
ExecutionService evaluation, moments later on the SAME persistent market-data
session, requested the SAME conId and got ``market_data_state = UNAVAILABLE``
with ``market data request raised PoiSessionError``.

The mechanism, reproduced deterministically here rather than asserted from a
transcript (the safe output intentionally hid the raw SDK text):

* a ``snapshot=True`` request completes at the gateway with ``tickSnapshotEnd``;
* the client's cleanup then cancels it anyway;
* the gateway, whose snapshot is already gone, answers that cancel
  asynchronously with error 300 ("Can't find EId with ticker Id ...");
* ``on_error`` classified 300 (``< 1100``, not a connectivity notice) as a
  sticky connection ``fatal_error``, without regard to whether that reqId was
  still current;
* ``fatal_error`` is deliberately sticky until reconnect, so the NEXT request's
  ``_await`` read it and raised ``PoiSessionError``.

Two independent corrections each break the chain, and both are asserted here:
a completed snapshot is not cancelled at all, and a 300 (and any error for a
request that is already over) can never poison a later quote.

The whole chain is driven -- ``IbkrQuoteSource`` -> ``IbkrMarketDataSession`` ->
``IbkrMarketDataRawClient`` -> transport seam -- over ONE persistent connection,
because a fresh client between quotes is exactly what would hide this.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from trading_application.trading_readiness_runtime import MarketDataState, QuoteState
from trading_broker_core.port import BrokerInstrument
from trading_broker_ibkr.poi_market_data_client import IbkrMarketDataRawClient
from trading_broker_ibkr.poi_market_data_session import IbkrMarketDataSession
from trading_broker_ibkr.poi_readonly_session import PoiSessionError
from trading_execution_agent.ibkr_quote_source import IbkrQuoteSource

from tests.integration.test_market_data_request_isolation import (
    FULL,
    ScriptedMarketData,
    _Script,
)

CAPTURED_AT = "2026-08-19T15:30:01+00:00"
#: The real owner-host security.
AAPL = BrokerInstrument(
    broker_instrument_ref="conid:265598",
    symbol="AAPL",
    exchange="SMART",
    currency="USD",
)
#: IBKR 300, verbatim shape.
NOT_SUBSCRIBED = (300, "Can't find EId with ticker Id:9301")


def _connected_chain(
    *scripts: _Script,
) -> tuple[IbkrQuoteSource, ScriptedMarketData, IbkrMarketDataRawClient]:
    transport = ScriptedMarketData(*scripts)
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=0.3
    )
    session = IbkrMarketDataSession(client)
    session.connect("127.0.0.1", 4002)
    return IbkrQuoteSource(session), transport, client


def _realtime(quote: QuoteState) -> None:
    assert quote.market_data_state is MarketDataState.REALTIME, quote.detail
    assert quote.bid == Decimal("100"), quote.detail
    assert quote.ask == Decimal("100.05"), quote.detail


def test_a_completed_snapshot_is_not_cancelled() -> None:
    """The primary fix: nothing is cancelled after tickSnapshotEnd, so the
    gateway never emits the 300 that used to poison the session."""
    source, transport, _client = _connected_chain(FULL)

    _realtime(source.current_quote(AAPL))

    assert transport.cancelled == [], "a completed snapshot must not be cancelled"


def test_a_late_300_after_a_completed_snapshot_does_not_poison_the_next_quote() -> None:
    """THE owner-host reproduction.

    The first snapshot completes REALTIME. Then the gateway's asynchronous
    answer to cancelling an already-complete subscription -- error 300 for that
    finished reqId -- arrives. The second quote for the same conId must still be
    REALTIME and usable, not PoiSessionError.
    """
    source, transport, client = _connected_chain(FULL, FULL)

    first = source.current_quote(AAPL)
    _realtime(first)

    # The async 300 for the FIRST (now finished) request, exactly as the gateway
    # would send it in response to a cancel of a completed snapshot.
    finished_req_id = transport.requests[0][0]
    assert client._sink is not None  # noqa: SLF001 - white-box: inject the callback
    client._sink.on_error(*NOT_SUBSCRIBED, finished_req_id)  # noqa: SLF001

    second = source.current_quote(AAPL)
    _realtime(second)
    assert "PoiSessionError" not in second.detail, second.detail


def test_a_stale_fatal_class_error_cannot_poison_a_later_quote() -> None:
    """Generalised: ANY fatal-class error for a request that is already over
    must not fail a later one -- not only code 300."""
    source, transport, client = _connected_chain(FULL, FULL)

    _realtime(source.current_quote(AAPL))

    finished_req_id = transport.requests[0][0]
    assert client._sink is not None  # noqa: SLF001
    # 322 (< 1100, not a notice) attributed to the finished request.
    client._sink.on_error(322, "Error processing request", finished_req_id)  # noqa: SLF001

    _realtime(source.current_quote(AAPL))


def test_a_genuine_connection_fatal_fails_before_issuing_a_new_request() -> None:
    """Item 5: a real connection-level fatal (reqId -1) is sticky, and the next
    quote must fail BEFORE another reqMktData is issued -- not after waiting."""
    source, transport, client = _connected_chain(FULL)

    _realtime(source.current_quote(AAPL))
    requests_before = len(transport.requests)

    assert client._sink is not None  # noqa: SLF001
    # A genuine connection error: reqId -1, code 504 (not connected).
    client._sink.on_error(504, "Not connected", -1)  # noqa: SLF001

    second = source.current_quote(AAPL)
    assert second.market_data_state is MarketDataState.UNAVAILABLE
    assert "PoiSessionError" in second.detail
    assert len(transport.requests) == requests_before, (
        "no new reqMktData may be issued once a connection fatal is known"
    )


def test_the_completed_snapshot_reuse_holds_for_many_quotes() -> None:
    """Not just twice: a long-lived session answers repeatedly."""
    source, _transport, _client = _connected_chain(FULL, FULL, FULL, FULL, FULL)

    for _ in range(5):
        _realtime(source.current_quote(AAPL))


def test_market_data_for_instrument_raises_on_a_genuine_fatal_but_not_a_stale_300() -> None:
    """At the client layer directly: a stale 300 does not raise; a live
    connection fatal does."""
    transport = ScriptedMarketData(FULL, FULL)
    client = IbkrMarketDataRawClient(
        lambda: transport, now=lambda: CAPTURED_AT, timeout_seconds=0.3
    )
    client.open("127.0.0.1", 4002)

    client.market_data_for_instrument("conid:265598")
    finished = transport.requests[0][0]
    assert client._sink is not None  # noqa: SLF001
    client._sink.on_error(*NOT_SUBSCRIBED, finished)  # noqa: SLF001

    # Must not raise: the stale 300 is cleanup noise.
    quote = client.market_data_for_instrument("conid:265598")
    assert quote.market_data_type == 1

    # A genuine connection fatal, however, does stop the session.
    client._sink.on_error(504, "Not connected", -1)  # noqa: SLF001
    with pytest.raises(PoiSessionError):
        client.market_data_for_instrument("conid:265598")
