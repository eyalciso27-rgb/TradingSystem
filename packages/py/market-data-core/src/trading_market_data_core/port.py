"""Market data provider port (IMP-0702).

Authority: Integration_Specification_v0.6 section 7.1 (MarketDataProvider
contract) restricted to this task's scope: quotes, bars, fundamentals
metadata and health. Calendar and corporate actions arrive with IMP-0704/0705.

Adapters implement this Protocol; the core never imports an adapter and no
real provider is wired here (ADR-0014: mocks/fixtures only until the market
data PoI passes). A provider returns ``None`` when it has no datum — the
policy layer (``service``) turns that into a canonical fail-closed error.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from trading_market_data_core.types import (
    BarSeries,
    FundamentalsMetadata,
    ProviderHealth,
    Quote,
)


class MarketDataProviderPort(Protocol):
    """One provider's implementation surface (explicit identity + version)."""

    @property
    def provider_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def get_quote(self, instrument_id: str, *, as_of: datetime) -> Quote | None: ...

    def get_bars(
        self,
        instrument_id: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> BarSeries | None: ...

    def get_fundamentals_metadata(
        self, instrument_id: str, *, as_of: datetime
    ) -> FundamentalsMetadata | None: ...

    def health(self) -> ProviderHealth: ...
