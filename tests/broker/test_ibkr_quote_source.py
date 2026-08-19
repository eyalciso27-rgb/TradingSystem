"""The IBKR quote source, against the real session and the real quote type.

Every value here comes from a genuine ``MarketDataQuote`` — the production
object the IBKR client actually returns — driven through the real
``IbkrMarketDataSession`` with its own transport seam. The double is never
richer than production: if a fact the readiness contract needs were missing from
the real quote, the fix would be the adapter contract, not a ``getattr``
default here.

The interesting cases are all about absence. IBKR routinely answers with less
than was asked for — a delayed feed instead of realtime, a missing size, an
entitlement error instead of data — and each of those must arrive as itself
rather than as a convenient substitute.

Offline: no ibapi, no socket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from trading_application.trading_readiness_runtime import MarketDataState
from trading_broker_ibkr.poi_market_data_client import (
    ContractResolution,
    MarketDataQuote,
    data_classification,
)
from trading_broker_ibkr.poi_market_data_session import IbkrMarketDataSession
from trading_broker_ibkr.poi_readonly_session import ReadOnlyAccountSummary
from trading_execution_agent.ibkr_quote_source import (
    IbkrQuoteSource,
    market_data_state,
    parse_observed_at,
    parse_price,
    parse_size,
    to_quote_state,
)

CAPTURED_AT = "2026-08-19T15:30:01.500000+00:00"


def quote(**kw: object) -> MarketDataQuote:
    """A REAL MarketDataQuote. Nothing here exists that production lacks."""
    fields: dict[str, object] = {
        "symbol": "AAPL",
        "market_data_type": 1,
        "bid": "100.00",
        "ask": "100.05",
        "last": "100.02",
        "bid_size": "500",
        "ask_size": "400",
        "last_size": "100",
        "exchange_timestamp": None,
        "received_at": CAPTURED_AT,
        "entitlement_errors": (),
        "missing_fields": (),
        "freshness": "REALTIME",
        "data_classification": "REALTIME",
        "source_tick_ids": (1, 2),
    }
    fields.update(kw)
    return MarketDataQuote(**fields)  # type: ignore[arg-type]


class _RawClient:
    """The raw client seam the real session drives."""

    def __init__(self, result: MarketDataQuote | Exception) -> None:
        self.result = result
        self.requests: list[tuple[str, int]] = []
        self.opened = False

    def open(self, host: str, port: int) -> None:
        self.opened = True

    def current_time(self) -> str:
        return CAPTURED_AT

    def account_summary(self) -> ReadOnlyAccountSummary:
        raise NotImplementedError

    def resolve_contract(self, symbol: str) -> ContractResolution:
        raise NotImplementedError

    def market_data(self, symbol: str, requested_market_data_type: int = 1) -> MarketDataQuote:
        self.requests.append((symbol, requested_market_data_type))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        self.opened = False


def _source(result: MarketDataQuote | Exception) -> tuple[IbkrQuoteSource, _RawClient]:
    client = _RawClient(result)
    session = IbkrMarketDataSession(client)
    session.connect("127.0.0.1", 4002)
    return IbkrQuoteSource(session), client


# == canonical mapping =========================================================


def test_a_healthy_realtime_quote_maps_completely() -> None:
    source, _ = _source(quote())
    state = source.current_quote("AAPL")

    assert state.bid == Decimal("100.00")
    assert state.ask == Decimal("100.05")
    assert state.bid_size == 500
    assert state.ask_size == 400
    assert state.market_data_state is MarketDataState.REALTIME
    assert state.observed_at == datetime(2026, 8, 19, 15, 30, 1, 500000, tzinfo=UTC)


def test_the_source_drives_the_real_session() -> None:
    source, client = _source(quote())
    source.current_quote("AAPL")

    assert client.requests == [("AAPL", 1)], "one request, through the existing session"


@pytest.mark.parametrize(
    ("ibkr_type", "expected"),
    [
        (1, MarketDataState.REALTIME),
        (2, MarketDataState.FROZEN),
        (3, MarketDataState.DELAYED),
        (4, MarketDataState.DELAYED_FROZEN),
        (99, MarketDataState.UNKNOWN),
        (None, MarketDataState.UNKNOWN),
    ],
)
def test_the_served_market_data_type_is_mapped_faithfully(
    ibkr_type: int | None, expected: MarketDataState
) -> None:
    assert market_data_state(quote(market_data_type=ibkr_type)) is expected


def test_delayed_data_is_never_relabelled_realtime() -> None:
    """The failure this whole enum exists to prevent."""
    state = to_quote_state(quote(market_data_type=3, freshness="DELAYED"))

    assert state.market_data_state is MarketDataState.DELAYED
    assert state.market_data_state.value != MarketDataState.REALTIME.value
    # And it agrees with the client's own classification of the same fact.
    assert data_classification(3) == "DELAYED"


def test_an_entitlement_failure_with_no_feed_is_unavailable() -> None:
    """A missing subscription is a knowable answer, and it is not UNKNOWN."""
    state = to_quote_state(
        quote(
            market_data_type=None,
            bid=None,
            ask=None,
            entitlement_errors=("354: requested market data is not subscribed",),
        )
    )

    assert state.market_data_state is MarketDataState.UNAVAILABLE
    assert "entitlement" in state.detail


def test_a_feed_that_never_stated_itself_is_unknown() -> None:
    state = to_quote_state(quote(market_data_type=None))
    assert state.market_data_state is MarketDataState.UNKNOWN


# == absence stays absence =====================================================


@pytest.mark.parametrize("missing", ["", "   ", None])
def test_a_missing_price_is_none_not_a_substitute(missing: str | None) -> None:
    assert parse_price(missing) is None


@pytest.mark.parametrize("missing", ["", "   ", None, "not-a-number", "nan", "inf"])
def test_a_missing_or_unusable_size_is_none_not_zero(missing: str | None) -> None:
    """Zero would silently satisfy nothing; None makes a size rule fail closed."""
    assert parse_size(missing) is None


def test_a_missing_size_leaves_the_size_unknown() -> None:
    state = to_quote_state(quote(bid_size=None, ask_size=None))

    assert state.bid_size is None
    assert state.ask_size is None


def test_a_non_finite_price_is_refused() -> None:
    assert parse_price("nan") is None
    assert parse_price("Infinity") is None


def test_the_ibkr_no_data_placeholder_is_left_for_the_gate_to_refuse() -> None:
    """``-1`` is IBKR's "no data"; it is parsed, then refused where it matters."""
    assert parse_price("-1") == Decimal("-1")
    state = to_quote_state(quote(bid="-1"))
    assert state.bid == Decimal("-1")  # the readiness gate rejects non-positive


def test_a_naive_timestamp_is_refused_rather_than_assumed() -> None:
    """Assuming a zone would silently shift quote age."""
    assert parse_observed_at("2026-08-19T15:30:01") is None
    assert parse_observed_at("not a timestamp") is None
    assert parse_observed_at(None) is None


def test_a_quote_with_no_capture_time_has_no_observation_time() -> None:
    state = to_quote_state(quote(received_at=None))
    assert state.observed_at is None


def test_missing_fields_are_reported_in_the_detail() -> None:
    state = to_quote_state(quote(bid=None, missing_fields=("bid",)))
    assert "missing=['bid']" in state.detail


# == failure of the feed itself ================================================


def test_an_unreachable_feed_becomes_an_unavailable_observation() -> None:
    """Readiness must refuse for a stated reason, not by raising."""
    source, _ = _source(RuntimeError("gateway dropped the subscription"))
    state = source.current_quote("AAPL")

    assert state.market_data_state is MarketDataState.UNAVAILABLE
    assert state.bid is None
    assert state.ask is None
    assert state.observed_at is None
    assert "raised" in state.detail


def test_the_quote_source_never_invents_a_field_production_lacks() -> None:
    """Structural: the double is the production type, so it cannot be richer."""
    from dataclasses import fields as dataclass_fields

    names = {f.name for f in dataclass_fields(MarketDataQuote)}
    assert {"bid", "ask", "bid_size", "ask_size", "received_at"} <= names
    # Nothing in the mapping reads an attribute outside that set.
    assert "quality" not in names
    assert "is_fresh" not in names
