"""Core market-data types (IMP-0702).

Authority: Integration_Specification_v0.6 sections 7.1-7.3 and I-07 (every
datum carries observed_at, received_at and freshness) and 20.3 (field
ownership). Broker-neutral, provider-neutral value objects:

- Prices/sizes are ``Decimal`` — precision is never lost to floats.
- Every timestamp is timezone-aware (naive timestamps are rejected).
- Every datum names its provider explicitly (``provider_id``) — data from
  two providers is never mixed inside one object (section 7.4 source
  attribution).
- The core contract speaks ONLY internal ``instrument_id`` values from the
  Instrument Master; a raw ticker exists solely *before* resolution and has
  no field here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.enums import FreshnessClass, IntegrationHealthStatus


def _require_aware(*stamps: datetime | None) -> None:
    for stamp in stamps:
        if stamp is not None and stamp.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Quote:
    """One provider's bid/ask/last snapshot for one internal instrument."""

    instrument_id: str
    provider_id: str
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    last: Decimal | None
    observed_at: datetime
    received_at: datetime
    freshness: FreshnessClass

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, self.received_at)


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar; ``interval`` is the canonical interval token."""

    instrument_id: str
    provider_id: str
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    start_at: datetime
    end_at: datetime
    observed_at: datetime
    freshness: FreshnessClass

    def __post_init__(self) -> None:
        _require_aware(self.start_at, self.end_at, self.observed_at)


@dataclass(frozen=True, slots=True)
class BarSeries:
    """Bars from exactly ONE provider (no cross-provider mixing)."""

    instrument_id: str
    provider_id: str
    interval: str
    bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        for bar in self.bars:
            if bar.provider_id != self.provider_id:
                raise ValueError(
                    "a BarSeries must come wholly from one provider "
                    "(section 7.4 source attribution)"
                )


@dataclass(frozen=True, slots=True)
class FundamentalsMetadata:
    """Which fundamental fields a provider can serve for an instrument.

    Metadata only (IMP-0702 scope) — actual fundamental values arrive with
    the feature/fundamental tasks.
    """

    instrument_id: str
    provider_id: str
    available_fields: tuple[str, ...]
    as_of: datetime

    def __post_init__(self) -> None:
        _require_aware(self.as_of)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Health of one provider (canonical health states)."""

    provider_id: str
    status: IntegrationHealthStatus
    checked_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.checked_at)
