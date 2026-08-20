"""The real IBKR quote source behind the readiness ``QuoteSource`` seam.

Until now the seam had no production implementation, so every readiness
evaluation priced its decision from a test double. This binds it to the IBKR
market-data session the repository already has — the same one POI-MD-001 used
to prove realtime quotes work on the owner host — rather than opening a second
Gateway connection stack beside it.

The whole job is translation, and the only interesting part is what happens
when a fact is missing. Every value here is either something IBKR actually
said or ``None``/``UNKNOWN``:

* a price that never arrived is ``None``, never a placeholder;
* a size that never arrived is ``None``, never zero — a configured minimum size
  must fail closed rather than silently pass;
* the feed state is whatever IBKR reported it was *serving*, which is not the
  same as what we asked for. An account without a realtime subscription is
  quietly served delayed ticks, and calling that REALTIME is how a stale price
  prices a live decision.

``received_at`` is the client's own capture time, so quote age is measured
against our clock rather than an exchange timestamp we may not have been sent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from trading_application.trading_readiness_runtime import MarketDataState, QuoteState
from trading_broker_core.port import BrokerInstrument
from trading_broker_ibkr.poi_market_data_client import MarketDataQuote

QUOTE_SOURCE_VERSION = "ibkr-quote-source-1.0.0"

#: IBKR ``marketDataType`` -> what the feed is actually serving. IBKR answers
#: with what it is entitled to serve, so this reads the SERVED type and never
#: the requested one.
MARKET_DATA_STATE_BY_IBKR_TYPE: dict[int, MarketDataState] = {
    1: MarketDataState.REALTIME,
    2: MarketDataState.FROZEN,
    3: MarketDataState.DELAYED,
    4: MarketDataState.DELAYED_FROZEN,
}

#: Request realtime. What we receive is a separate question, answered by the
#: served type — asking for realtime never implies getting it.
REQUEST_REALTIME = 1


class MarketDataSessionLike(Protocol):
    """The existing IBKR market-data session, structurally.

    Typed structurally so this module drives the real session without owning
    its lifecycle: connecting, disconnecting and reconnecting stay where they
    already are.

    Note what is NOT here: the ticker-shaped ``req_market_data``. The execution
    path may not reach it. A quote requested by symbol after a security has been
    resolved lets IBKR resolve the string a second time, and nothing compares
    its answer to the security the order is authorized for -- which is exactly
    the identity break the rest of this seal exists to close.
    """

    def req_market_data_for_instrument(
        self, broker_instrument_ref: str, requested_market_data_type: int = ...
    ) -> MarketDataQuote: ...


class QuoteSourceError(Exception):
    """The quote source could not be driven at all."""


def parse_price(raw: str | None) -> Decimal | None:
    """A price IBKR actually sent, or None. Never a substitute.

    IBKR sends ``-1`` for "no data on this tick". That is not a price, but it
    is also not this function's call to reject: it is parsed faithfully and the
    readiness gate refuses non-positive quotes, so the refusal happens once, at
    the boundary that owns it.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def parse_size(raw: str | None) -> int | None:
    """A size IBKR actually sent, or None.

    Never zero-by-default: a configured minimum-size rule that cannot be
    evaluated must block, and it can only do that if "unknown" stays unknown.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(value)


def parse_observed_at(raw: str | None) -> datetime | None:
    """The client's capture time, normalised to UTC. Unparseable is None."""
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        # A naive timestamp cannot be compared to an aware clock without
        # assuming a zone, and assuming one here would silently shift quote age.
        return None
    return stamp.astimezone(UTC)


def market_data_state(quote: MarketDataQuote) -> MarketDataState:
    """What the feed said it was serving.

    An entitlement error with no served type is the one case we can name
    precisely: the subscription is missing, so nothing is coming. Everything
    else that we cannot identify stays UNKNOWN, which blocks.
    """
    if quote.market_data_type is None:
        return (
            MarketDataState.UNAVAILABLE
            if quote.entitlement_errors
            else MarketDataState.UNKNOWN
        )
    return MARKET_DATA_STATE_BY_IBKR_TYPE.get(
        quote.market_data_type, MarketDataState.UNKNOWN
    )


def to_quote_state(quote: MarketDataQuote) -> QuoteState:
    """Map one IBKR snapshot onto the canonical readiness observation."""
    detail_parts = [f"ibkr_market_data_type={quote.market_data_type}"]
    if quote.missing_fields:
        detail_parts.append(f"missing={list(quote.missing_fields)}")
    if quote.entitlement_errors:
        # Entitlement problems are the single most likely real-world blocker on
        # the owner host, so they are carried into the readiness detail rather
        # than left in a log nobody reads during a refusal.
        detail_parts.append(f"entitlement={list(quote.entitlement_errors)}")
    requested_for = str(getattr(quote, "requested_for", "") or "")
    if requested_for:
        # Attribution from the REQUEST, not the response: we know which security
        # this prices because we asked for that security by id.
        detail_parts.append(f"requested_for={requested_for}")
    return QuoteState(
        bid=parse_price(quote.bid),
        ask=parse_price(quote.ask),
        observed_at=parse_observed_at(quote.received_at),
        bid_size=parse_size(quote.bid_size),
        ask_size=parse_size(quote.ask_size),
        market_data_state=market_data_state(quote),
        detail="; ".join(detail_parts),
    )


class IbkrQuoteSource:
    """``QuoteSource`` implemented against the real IBKR market-data session.

    A failure to reach the feed becomes an UNAVAILABLE observation rather than
    an exception: readiness must be able to refuse for a stated reason, and a
    caller that has to catch exceptions to learn "no quote" will eventually
    forget to.
    """

    def __init__(
        self,
        session: MarketDataSessionLike,
        *,
        requested_market_data_type: int = REQUEST_REALTIME,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._requested = requested_market_data_type
        self._now = now or (lambda: datetime.now(tz=UTC))

    def current_quote(self, instrument: BrokerInstrument) -> QuoteState:
        """A quote for THE resolved security.

        The opaque reference passes straight through to the IBKR package, the
        only component permitted to translate it into a contract.
        ``instrument.symbol`` is deliberately NOT used: falling back to it would
        quietly reintroduce the ticker-shaped request this signature replaces,
        and the fallback would be invisible because a ticker usually does
        resolve to the right thing.

        An unusable, foreign or missing reference raises inside the IBKR client
        BEFORE any request is made, and arrives here as UNAVAILABLE -- readiness
        then refuses for a stated reason rather than pricing a security we
        cannot name.
        """
        try:
            raw = self._session.req_market_data_for_instrument(
                instrument.broker_instrument_ref, self._requested
            )
        except Exception as error:  # noqa: BLE001 - any feed failure is UNAVAILABLE
            return QuoteState(
                bid=None,
                ask=None,
                observed_at=None,
                market_data_state=MarketDataState.UNAVAILABLE,
                detail=(
                    f"market data request raised {type(error).__name__}"
                    f" (requested for {instrument.broker_instrument_ref})"
                ),
            )
        return to_quote_state(raw)


__all__ = [
    "MARKET_DATA_STATE_BY_IBKR_TYPE",
    "QUOTE_SOURCE_VERSION",
    "REQUEST_REALTIME",
    "IbkrQuoteSource",
    "MarketDataSessionLike",
    "QuoteSourceError",
    "market_data_state",
    "parse_observed_at",
    "parse_price",
    "parse_size",
    "to_quote_state",
]
