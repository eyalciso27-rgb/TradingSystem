"""Deterministic in-memory fixture provider (IMP-0702, tests/replay only).

Per ADR-0014 this is the ONLY provider implementation until the market data
PoI passes: no network, no secrets, fully deterministic. Fixture data is
keyed by internal instrument_id; the provider serves the latest datum whose
``observed_at`` is <= the requested ``as_of`` (point-in-time behavior), so
replay never sees the future.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading_contracts.enums import IntegrationHealthStatus

from trading_market_data_core.types import (
    Bar,
    BarSeries,
    FundamentalsMetadata,
    ProviderHealth,
    Quote,
)


class FixtureMarketDataProvider:
    """MarketDataProviderPort implementation backed by in-memory fixtures."""

    def __init__(self, provider_id: str, *, version: str = "fixture-1") -> None:
        self._provider_id = provider_id
        self._version = version
        self._quotes: dict[str, list[Quote]] = {}
        self._bars: dict[tuple[str, str], list[Bar]] = {}
        self._fundamentals: dict[str, FundamentalsMetadata] = {}
        self.fail_next = False  # simulate a technical outage deterministically

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def version(self) -> str:
        return self._version

    # -- fixture loading -------------------------------------------------------

    def add_quote(self, quote: Quote) -> None:
        if quote.provider_id != self._provider_id:
            raise ValueError("fixture quote must be attributed to this provider")
        self._quotes.setdefault(quote.instrument_id, []).append(quote)
        self._quotes[quote.instrument_id].sort(key=lambda q: q.observed_at)

    def add_bar(self, bar: Bar) -> None:
        if bar.provider_id != self._provider_id:
            raise ValueError("fixture bar must be attributed to this provider")
        key = (bar.instrument_id, bar.interval)
        self._bars.setdefault(key, []).append(bar)
        self._bars[key].sort(key=lambda b: b.start_at)

    def set_fundamentals_metadata(self, metadata: FundamentalsMetadata) -> None:
        if metadata.provider_id != self._provider_id:
            raise ValueError("fixture metadata must be attributed to this provider")
        self._fundamentals[metadata.instrument_id] = metadata

    # -- port implementation ---------------------------------------------------

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("simulated provider outage")

    def get_quote(self, instrument_id: str, *, as_of: datetime) -> Quote | None:
        self._maybe_fail()
        candidates = [
            quote
            for quote in self._quotes.get(instrument_id, [])
            if quote.observed_at <= as_of
        ]
        return candidates[-1] if candidates else None

    def get_bars(
        self,
        instrument_id: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> BarSeries | None:
        self._maybe_fail()
        bars = tuple(
            bar
            for bar in self._bars.get((instrument_id, interval), [])
            if start <= bar.start_at and bar.end_at <= end and bar.observed_at <= as_of
        )
        if not bars:
            return None
        return BarSeries(
            instrument_id=instrument_id,
            provider_id=self._provider_id,
            interval=interval,
            bars=bars,
        )

    def get_fundamentals_metadata(
        self, instrument_id: str, *, as_of: datetime
    ) -> FundamentalsMetadata | None:
        self._maybe_fail()
        metadata = self._fundamentals.get(instrument_id)
        if metadata is None or metadata.as_of > as_of:
            return None
        return metadata

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider_id=self._provider_id,
            status=IntegrationHealthStatus.HEALTHY,
            checked_at=datetime.now(UTC),
            detail="fixture provider (deterministic, no network)",
        )
