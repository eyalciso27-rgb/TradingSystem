"""Feature snapshots: deterministic replay, point-in-time, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_contracts.enums import FreshnessClass
from trading_domain.instruments import InstrumentRecord
from trading_market_data_core import (
    FEATURE_AVAILABLE,
    FEATURE_UNAVAILABLE,
    Bar,
    FeatureSnapshotBuilder,
    FeatureValue,
    FixtureMarketDataProvider,
    FreshnessPolicy,
    MarketDataService,
    Quote,
)
from trading_market_data_core.features import (
    FeatureComputationError,
    adv_dollar,
    atr,
    relative_volume,
    spread,
)

AS_OF = datetime(2026, 7, 1, 21, 0, tzinfo=UTC)
INSTRUMENT = InstrumentRecord(
    instrument_id="inst-1", asset_class="EQUITY", currency="USD", status="ACTIVE"
)


def daily_bar(day: int, *, close: str, volume: str, provider: str = "prov-a") -> Bar:
    start = datetime(2026, 6, day, 13, 30, tzinfo=UTC)
    close_d = Decimal(close)
    return Bar(
        instrument_id="inst-1",
        provider_id=provider,
        interval="1d",
        open=close_d - Decimal("1"),
        high=close_d + Decimal("2"),
        low=close_d - Decimal("2"),
        close=close_d,
        volume=Decimal(volume),
        start_at=start,
        end_at=start + timedelta(hours=6, minutes=30),
        observed_at=start + timedelta(hours=6, minutes=30),
        freshness=FreshnessClass.DELAYED,
    )


def fixture_quote(provider: str = "prov-a") -> Quote:
    observed = AS_OF - timedelta(minutes=1)
    return Quote(
        instrument_id="inst-1",
        provider_id=provider,
        bid=Decimal("100.00"),
        ask=Decimal("100.06"),
        bid_size=Decimal("500"),
        ask_size=Decimal("400"),
        last=Decimal("100.02"),
        observed_at=observed,
        received_at=observed,
        freshness=FreshnessClass.REALTIME,
    )


def make_builder(
    *, with_quote: bool = True, bars: int = 6
) -> tuple[FeatureSnapshotBuilder, FixtureMarketDataProvider]:
    provider = FixtureMarketDataProvider("prov-a")
    for day in range(1, bars + 1):
        provider.add_bar(daily_bar(day, close=str(100 + day), volume=str(10_000 + 100 * day)))
    if with_quote:
        provider.add_quote(fixture_quote())
    service = MarketDataService(provider)
    builder = FeatureSnapshotBuilder(
        service,
        bar_interval="1d",
        atr_period=3,
        adv_period=3,
        relative_volume_period=3,
        quote_policy=FreshnessPolicy(required=FreshnessClass.REALTIME),
    )
    return builder, provider


HISTORY_START = datetime(2026, 5, 25, tzinfo=UTC)


def test_snapshot_built_from_fixture_data_with_attribution() -> None:
    builder, _provider = make_builder()
    snapshot = builder.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    assert snapshot.instrument_id == "inst-1"
    assert {feature.name for feature in snapshot.features} == {
        "atr",
        "adv_dollar",
        "relative_volume",
        "spread",
    }
    for feature in snapshot.features:
        assert feature.status == FEATURE_AVAILABLE
        assert feature.source_provider_id == "prov-a"  # attribution preserved
        assert feature.formula_version  # mandatory (20.3)
        assert feature.value is not None
    assert snapshot.get("spread").value == Decimal("0.06")
    assert snapshot.get("spread").freshness is FreshnessClass.REALTIME


def test_replay_is_deterministic_same_inputs_same_snapshot() -> None:
    builder_a, _pa = make_builder()
    builder_b, _pb = make_builder()
    first = builder_a.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    second = builder_b.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    assert first == second
    assert first.content_hash() == second.content_hash()  # DoD: replay deterministic


def test_no_lookahead_bars_after_as_of_are_invisible() -> None:
    builder, provider = make_builder()
    early_as_of = datetime(2026, 6, 4, 23, 0, tzinfo=UTC)  # bars 5-6 are the future
    snapshot = builder.build(INSTRUMENT, as_of=early_as_of, history_start=HISTORY_START)
    late = builder.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    # volumes differ per day, so the visible-window average must differ
    assert snapshot.get("adv_dollar").value != late.get("adv_dollar").value
    observed = snapshot.get("adv_dollar").source_observed_at
    assert observed is not None and observed <= early_as_of  # nothing from the future


def test_missing_bars_yield_canonical_unavailable() -> None:
    provider = FixtureMarketDataProvider("prov-a")
    provider.add_quote(fixture_quote())
    builder = FeatureSnapshotBuilder(
        MarketDataService(provider),
        bar_interval="1d",
        atr_period=3,
        adv_period=3,
        relative_volume_period=3,
        quote_policy=FreshnessPolicy(required=FreshnessClass.REALTIME),
    )
    snapshot = builder.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    for name in ("atr", "adv_dollar", "relative_volume"):
        feature = snapshot.get(name)
        assert feature.status == FEATURE_UNAVAILABLE
        assert feature.value is None and feature.reason  # no silent fallback
    assert snapshot.get("spread").status == FEATURE_AVAILABLE


def test_stale_quote_yields_unavailable_spread() -> None:
    builder, provider = make_builder(with_quote=False)
    stale = Quote(
        instrument_id="inst-1",
        provider_id="prov-a",
        bid=Decimal("100"),
        ask=Decimal("100.10"),
        bid_size=Decimal("1"),
        ask_size=Decimal("1"),
        last=None,
        observed_at=AS_OF - timedelta(hours=5),
        received_at=AS_OF - timedelta(hours=5),
        freshness=FreshnessClass.STALE,
    )
    provider.add_quote(stale)
    snapshot = builder.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    feature = snapshot.get("spread")
    assert feature.status == FEATURE_UNAVAILABLE
    assert "STALE" in feature.reason  # stale never feeds a new decision


def test_naive_as_of_rejected() -> None:
    builder, _provider = make_builder()
    with pytest.raises(ValueError, match="timezone-aware"):
        builder.build(
            INSTRUMENT,
            as_of=datetime(2026, 7, 1, 21, 0),  # noqa: DTZ001 (intentional)
            history_start=HISTORY_START,
        )


def test_snapshot_document_has_no_secret_material() -> None:
    builder, _provider = make_builder()
    snapshot = builder.build(INSTRUMENT, as_of=AS_OF, history_start=HISTORY_START)
    document = str(snapshot.to_document()).lower()
    for marker in ("secret", "token", "password", "cookie", "session", "credential"):
        assert marker not in document


def test_snapshot_creates_no_candidate_decision_or_order() -> None:
    """The snapshot layer must not touch candidate/decision/order machinery."""
    import trading_market_data_core.snapshot as snapshot_module

    source = open(snapshot_module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in ("trading_persistence", "trading_application", "broker"):
            assert forbidden not in line, line


def test_feature_value_invariant() -> None:
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        FeatureValue(name="x", status=FEATURE_AVAILABLE, formula_version="v1", value=None)


# -- formula sanity (pure functions) ------------------------------------------


def test_formula_math_sanity() -> None:
    bars = tuple(daily_bar(day, close="100", volume="10000") for day in range(1, 5))
    # constant closes: TR = high-low = 4 every bar -> ATR == 4
    assert atr(bars, period=3) == Decimal("4")
    assert adv_dollar(bars, period=3) == Decimal("100") * Decimal("10000")
    assert relative_volume(bars, period=3) == Decimal("1")
    assert spread(fixture_quote()) == Decimal("0.06")


def test_formula_fail_closed_on_insufficient_bars() -> None:
    bars = (daily_bar(1, close="100", volume="10000"),)
    with pytest.raises(FeatureComputationError):
        atr(bars, period=3)
    with pytest.raises(FeatureComputationError):
        relative_volume(bars, period=3)
