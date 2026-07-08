"""Market data provider port and core types (IMP-0702).

Quotes, bars, fundamentals metadata and health behind a provider-neutral
port; primary/fallback and freshness policies enforced by the service. No
real provider is wired (ADR-0014) — only the deterministic fixture provider.
"""

from trading_market_data_core.fixtures import FixtureMarketDataProvider
from trading_market_data_core.port import MarketDataProviderPort
from trading_market_data_core.service import (
    FreshnessPolicy,
    FreshnessViolationError,
    LookaheadError,
    MarketDataConflictError,
    MarketDataError,
    MarketDataService,
    MarketDataUnavailableError,
)
from trading_market_data_core.types import (
    Bar,
    BarSeries,
    FundamentalsMetadata,
    ProviderHealth,
    Quote,
)

__all__ = [
    "Bar",
    "BarSeries",
    "FixtureMarketDataProvider",
    "FreshnessPolicy",
    "FreshnessViolationError",
    "FundamentalsMetadata",
    "LookaheadError",
    "MarketDataConflictError",
    "MarketDataError",
    "MarketDataProviderPort",
    "MarketDataService",
    "MarketDataUnavailableError",
    "ProviderHealth",
    "Quote",
]
