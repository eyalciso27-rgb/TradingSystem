"""Market data port: freshness policy, primary/fallback, point-in-time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_contracts.enums import FreshnessClass, IntegrationHealthStatus
from trading_domain.instruments import InstrumentRecord
from trading_market_data_core import (
    Bar,
    FixtureMarketDataProvider,
    FreshnessPolicy,
    FreshnessViolationError,
    FundamentalsMetadata,
    LookaheadError,
    MarketDataConflictError,
    MarketDataService,
    MarketDataUnavailableError,
    Quote,
)

AS_OF = datetime(2026, 7, 1, 15, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=5)
INSTRUMENT = InstrumentRecord(
    instrument_id="inst-1", asset_class="EQUITY", currency="USD", status="ACTIVE"
)
REALTIME_POLICY = FreshnessPolicy(required=FreshnessClass.REALTIME)
DELAYED_OK_POLICY = FreshnessPolicy(required=FreshnessClass.DELAYED)


def quote(
    provider: str,
    *,
    observed: datetime = EARLIER,
    freshness: FreshnessClass = FreshnessClass.REALTIME,
    bid: str = "100.00",
    ask: str = "100.10",
) -> Quote:
    return Quote(
        instrument_id="inst-1",
        provider_id=provider,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("300"),
        ask_size=Decimal("200"),
        last=Decimal("100.05"),
        observed_at=observed,
        received_at=observed,
        freshness=freshness,
    )


def make_service(
    primary_quotes: tuple[Quote, ...] | list[Quote] = (),
    secondary_quotes: tuple[Quote, ...] | list[Quote] = (),
) -> tuple[MarketDataService, FixtureMarketDataProvider, FixtureMarketDataProvider]:
    primary = FixtureMarketDataProvider("prov-primary")
    secondary = FixtureMarketDataProvider("prov-secondary")
    for item in primary_quotes:
        primary.add_quote(item)
    for item in secondary_quotes:
        secondary.add_quote(item)
    return MarketDataService(primary, secondary), primary, secondary


def test_valid_quote_by_instrument_with_provider_identity() -> None:
    service, _p, _s = make_service([quote("prov-primary")])
    served = service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)
    assert served.provider_id == "prov-primary"  # explicit provider identity
    assert served.bid == Decimal("100.00") and served.ask == Decimal("100.10")


def test_missing_data_fails_closed() -> None:
    service, _p, _s = make_service()
    with pytest.raises(MarketDataUnavailableError):
        service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)


def test_technical_failure_falls_back_to_secondary_with_attribution() -> None:
    service, primary, _s = make_service([], [quote("prov-secondary")])
    primary.fail_next = True
    served = service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)
    assert served.provider_id == "prov-secondary"


def test_no_fallback_to_delayed_when_realtime_required() -> None:
    # primary serves DELAYED; secondary has REALTIME — but a freshness
    # shortfall is NOT a technical failure, so no fallback happens (7.4).
    service, _p, _s = make_service(
        [quote("prov-primary", freshness=FreshnessClass.DELAYED)],
        [quote("prov-secondary", freshness=FreshnessClass.REALTIME)],
    )
    with pytest.raises(FreshnessViolationError, match="no delayed fallback"):
        service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)
    # the same DELAYED datum is fine for a research policy
    served = service.get_quote(INSTRUMENT, as_of=AS_OF, policy=DELAYED_OK_POLICY)
    assert served.freshness is FreshnessClass.DELAYED


def test_stale_and_unknown_fail_closed() -> None:
    service, _p, _s = make_service([quote("prov-primary", freshness=FreshnessClass.STALE)])
    with pytest.raises(FreshnessViolationError, match="STALE"):
        service.get_quote(INSTRUMENT, as_of=AS_OF, policy=DELAYED_OK_POLICY)
    service2, _p2, _s2 = make_service(
        [quote("prov-primary", freshness=FreshnessClass.UNKNOWN)]
    )
    with pytest.raises(MarketDataUnavailableError, match="UNKNOWN"):
        service2.get_quote(INSTRUMENT, as_of=AS_OF, policy=DELAYED_OK_POLICY)


def test_future_datum_is_lookahead_rejected() -> None:
    future = AS_OF + timedelta(seconds=1)
    primary = FixtureMarketDataProvider("prov-primary")
    primary.add_quote(quote("prov-primary", observed=future))
    service = MarketDataService(primary)
    # fixture provider itself is point-in-time (returns nothing before observed)
    with pytest.raises(MarketDataUnavailableError):
        service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)
    # and the service-level guard rejects a future datum even if a provider
    # misbehaves and serves it anyway
    with pytest.raises(LookaheadError):
        MarketDataService._check_point_in_time(future, AS_OF)


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        quote("prov-primary", observed=datetime(2026, 7, 1, 15, 0))  # noqa: DTZ001


def test_raw_ticker_not_accepted_by_service() -> None:
    service, _p, _s = make_service([quote("prov-primary")])
    with pytest.raises(AttributeError):
        service.get_quote("AAPL", as_of=AS_OF, policy=REALTIME_POLICY)  # type: ignore[arg-type]


def test_conflict_between_providers_detected() -> None:
    service, _p, _s = make_service(
        [quote("prov-primary", bid="100.00", ask="100.10")],
        [quote("prov-secondary", bid="150.00", ask="150.10")],
    )
    with pytest.raises(MarketDataConflictError, match="MARKET_DATA_CONFLICT"):
        service.crosscheck_quote(
            INSTRUMENT, as_of=AS_OF, relative_tolerance=Decimal("0.01")
        )
    # within tolerance: no conflict
    service.crosscheck_quote(INSTRUMENT, as_of=AS_OF, relative_tolerance=Decimal("1.0"))


def test_bars_point_in_time_and_fail_closed() -> None:
    primary = FixtureMarketDataProvider("prov-primary")
    start = AS_OF - timedelta(hours=2)
    primary.add_bar(
        Bar(
            instrument_id="inst-1",
            provider_id="prov-primary",
            interval="1m",
            open=Decimal("99"),
            high=Decimal("101"),
            low=Decimal("98.5"),
            close=Decimal("100"),
            volume=Decimal("10000"),
            start_at=start,
            end_at=start + timedelta(minutes=1),
            observed_at=start + timedelta(minutes=1),
            freshness=FreshnessClass.DELAYED,
        )
    )
    service = MarketDataService(primary)
    series = service.get_bars(
        INSTRUMENT, interval="1m", start=start, end=AS_OF, as_of=AS_OF
    )
    assert series.provider_id == "prov-primary" and len(series.bars) == 1
    assert series.bars[0].close == Decimal("100")  # Decimal precision retained
    with pytest.raises(MarketDataUnavailableError):
        service.get_bars(
            INSTRUMENT, interval="5m", start=start, end=AS_OF, as_of=AS_OF
        )


def test_fundamentals_metadata_and_health() -> None:
    primary = FixtureMarketDataProvider("prov-primary")
    primary.set_fundamentals_metadata(
        FundamentalsMetadata(
            instrument_id="inst-1",
            provider_id="prov-primary",
            available_fields=("pe_ratio", "eps"),
            as_of=EARLIER,
        )
    )
    service = MarketDataService(primary)
    metadata = service.get_fundamentals_metadata(INSTRUMENT, as_of=AS_OF)
    assert metadata.available_fields == ("pe_ratio", "eps")
    health = primary.health()
    assert health.status is IntegrationHealthStatus.HEALTHY
    assert health.provider_id == "prov-primary"


def test_fixture_provider_is_deterministic() -> None:
    service, _p, _s = make_service([quote("prov-primary")])
    first = service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)
    second = service.get_quote(INSTRUMENT, as_of=AS_OF, policy=REALTIME_POLICY)
    assert first == second


def test_same_provider_twice_rejected() -> None:
    provider = FixtureMarketDataProvider("prov-x")
    with pytest.raises(ValueError, match="distinct provider"):
        MarketDataService(provider, provider)
