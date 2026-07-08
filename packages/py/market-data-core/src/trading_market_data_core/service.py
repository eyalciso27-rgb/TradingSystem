"""Market data service — primary/fallback + freshness policy enforcement.

Authority: Integration_Specification_v0.6 sections 7.3-7.4 and 20.3:

- STALE is never allowed for a new decision; UNKNOWN fails closed.
- No fallback to DELAYED when the policy requires REALTIME (7.4 / 20.3).
- Fallback to the secondary provider happens only on a *technical* failure
  of the primary (error or no data), never to launder a freshness downgrade.
- Data from two providers is never mixed in one response (source
  attribution); a material divergence between providers raises
  MARKET_DATA_CONFLICT.
- Point-in-time safe: every request carries ``as_of`` and any datum observed
  after ``as_of`` is rejected (no lookahead in replay/backtest).

The service accepts an ``InstrumentRecord`` from the Instrument Master —
never a raw ticker — so an ambiguous symbol is structurally impossible here
(it already failed to review at resolution time, IMP-0701).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.enums import FreshnessClass
from trading_domain.instruments import InstrumentRecord

from trading_market_data_core.port import MarketDataProviderPort
from trading_market_data_core.types import BarSeries, FundamentalsMetadata, Quote

# Ordering of usability for a required class (7.3): a datum satisfies the
# requirement when its class is at least as fresh as required. STALE/UNKNOWN
# never satisfy anything.
_FRESHNESS_RANK: dict[FreshnessClass, int] = {
    FreshnessClass.REALTIME: 3,
    FreshnessClass.NEAR_REALTIME: 2,
    FreshnessClass.DELAYED: 1,
}


class MarketDataError(Exception):
    """Base class; always fail closed, never a silent default."""


class MarketDataUnavailableError(MarketDataError):
    """No provider could serve the datum: canonical unavailable state."""


class FreshnessViolationError(MarketDataError):
    """The served datum does not meet the required freshness class."""


class LookaheadError(MarketDataError):
    """A datum observed after ``as_of`` was offered to a replay/decision."""


class MarketDataConflictError(MarketDataError):
    """Material divergence between providers (MARKET_DATA_CONFLICT)."""


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """Required freshness per request — policy input, never a code constant."""

    required: FreshnessClass

    def satisfied_by(self, actual: FreshnessClass) -> bool:
        if actual not in _FRESHNESS_RANK or self.required not in _FRESHNESS_RANK:
            return False  # STALE/UNKNOWN never satisfy; UNKNOWN requirement invalid
        return _FRESHNESS_RANK[actual] >= _FRESHNESS_RANK[self.required]


class MarketDataService:
    """Policy-enforcing façade over a primary and optional secondary provider."""

    def __init__(
        self,
        primary: MarketDataProviderPort,
        secondary: MarketDataProviderPort | None = None,
    ) -> None:
        if secondary is not None and secondary.provider_id == primary.provider_id:
            raise ValueError("secondary provider must be a distinct provider")
        self._primary = primary
        self._secondary = secondary

    # -- quotes ---------------------------------------------------------------

    def get_quote(
        self,
        instrument: InstrumentRecord,
        *,
        as_of: datetime,
        policy: FreshnessPolicy,
    ) -> Quote:
        """Serve one quote satisfying ``policy`` at ``as_of``; fail closed."""
        quote, provider_failed = self._try_quote(self._primary, instrument, as_of)
        if quote is None and provider_failed and self._secondary is not None:
            # Technical failure of the primary -> secondary allowed (7.4).
            quote, _failed = self._try_quote(self._secondary, instrument, as_of)
        if quote is None:
            raise MarketDataUnavailableError(
                f"no quote for instrument {instrument.instrument_id!r} at "
                f"{as_of.isoformat()} (fail closed)"
            )
        self._check_point_in_time(quote.observed_at, as_of)
        if quote.freshness is FreshnessClass.UNKNOWN:
            raise MarketDataUnavailableError("freshness UNKNOWN: fail closed (7.3)")
        if quote.freshness is FreshnessClass.STALE:
            raise FreshnessViolationError("STALE datum is not allowed for a new decision")
        if not policy.satisfied_by(quote.freshness):
            # NO fallback to a less-fresh datum (e.g. DELAYED when REALTIME
            # is required) — a freshness shortfall is not a technical failure.
            raise FreshnessViolationError(
                f"quote freshness {quote.freshness.value} does not satisfy "
                f"required {policy.required.value}; no delayed fallback (7.4)"
            )
        return quote

    def _try_quote(
        self,
        provider: MarketDataProviderPort,
        instrument: InstrumentRecord,
        as_of: datetime,
    ) -> tuple[Quote | None, bool]:
        """Return (quote, technical_failure)."""
        try:
            quote = provider.get_quote(instrument.instrument_id, as_of=as_of)
        except Exception:  # noqa: BLE001 - provider failure must not propagate raw
            return None, True
        if quote is None:
            return None, True
        if quote.provider_id != provider.provider_id:
            raise MarketDataConflictError(
                "provider returned a quote attributed to a different provider"
            )
        return quote, False

    # -- bars -----------------------------------------------------------------

    def get_bars(
        self,
        instrument: InstrumentRecord,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> BarSeries:
        """Historical bars from the primary provider only (20.3: bars come
        from the approved primary/historical provider; no silent fallback)."""
        try:
            series = self._primary.get_bars(
                instrument.instrument_id,
                interval=interval,
                start=start,
                end=end,
                as_of=as_of,
            )
        except Exception as error:  # noqa: BLE001
            raise MarketDataUnavailableError(f"bars unavailable: {error}") from error
        if series is None or not series.bars:
            raise MarketDataUnavailableError(
                f"no bars for instrument {instrument.instrument_id!r} "
                f"[{start.isoformat()} .. {end.isoformat()}]"
            )
        for bar in series.bars:
            self._check_point_in_time(bar.observed_at, as_of)
        return series

    # -- fundamentals metadata -------------------------------------------------

    def get_fundamentals_metadata(
        self, instrument: InstrumentRecord, *, as_of: datetime
    ) -> FundamentalsMetadata:
        try:
            metadata = self._primary.get_fundamentals_metadata(
                instrument.instrument_id, as_of=as_of
            )
        except Exception as error:  # noqa: BLE001
            raise MarketDataUnavailableError(
                f"fundamentals metadata unavailable: {error}"
            ) from error
        if metadata is None:
            raise MarketDataUnavailableError(
                f"no fundamentals metadata for {instrument.instrument_id!r}"
            )
        self._check_point_in_time(metadata.as_of, as_of)
        return metadata

    # -- cross-provider conflict (7.4) ----------------------------------------

    def crosscheck_quote(
        self,
        instrument: InstrumentRecord,
        *,
        as_of: datetime,
        relative_tolerance: Decimal,
    ) -> None:
        """Compare primary and secondary; raise MARKET_DATA_CONFLICT on
        material divergence. ``relative_tolerance`` is a policy input."""
        if self._secondary is None:
            return
        primary, p_failed = self._try_quote(self._primary, instrument, as_of)
        secondary, s_failed = self._try_quote(self._secondary, instrument, as_of)
        if primary is None or secondary is None:
            return  # nothing to compare; availability is handled elsewhere
        mid_primary = (primary.bid + primary.ask) / 2
        mid_secondary = (secondary.bid + secondary.ask) / 2
        if mid_primary == 0:
            return
        divergence = abs(mid_primary - mid_secondary) / mid_primary
        if divergence > relative_tolerance:
            raise MarketDataConflictError(
                f"MARKET_DATA_CONFLICT: providers {primary.provider_id!r} and "
                f"{secondary.provider_id!r} diverge by {divergence} "
                f"(> {relative_tolerance})"
            )

    @staticmethod
    def _check_point_in_time(observed_at: datetime, as_of: datetime) -> None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if observed_at > as_of:
            raise LookaheadError(
                f"datum observed at {observed_at.isoformat()} is in the future "
                f"of as_of {as_of.isoformat()} (lookahead forbidden)"
            )
