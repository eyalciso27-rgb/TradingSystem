"""Deterministic feature computations on frozen market data (IMP-0703).

Authority: Integration_Specification_v0.6 section 20.3 — ATR and ADV are
computed by the Feature Engine on frozen bars (never scraped) and every
formula carries a mandatory ``formula_version``; spread comes from a quote
snapshot with mandatory timestamp and provider.

Pure functions over the IMP-0702 value objects: Decimal arithmetic (no float
precision loss), no I/O, no randomness — the same inputs always produce the
same value, which is what makes feature replay deterministic.
"""

from __future__ import annotations

from decimal import Decimal

from trading_market_data_core.types import Bar, Quote

# Registered formula versions (section 20.3: formula_version is mandatory).
# A new formula behavior requires a NEW version token — never a silent change.
ATR_WILDER_V1 = "atr-wilder-v1"
ADV_DOLLAR_V1 = "adv-dollar-v1"
SPREAD_QUOTE_V1 = "spread-quote-v1"
RELATIVE_VOLUME_V1 = "relative-volume-v1"
SMA_CLOSE_V1 = "sma-close-v1"


class FeatureComputationError(Exception):
    """Inputs cannot produce the feature (insufficient/inconsistent bars)."""


def _require_bars(bars: tuple[Bar, ...], minimum: int, feature: str) -> None:
    if len(bars) < minimum:
        raise FeatureComputationError(
            f"{feature}: requires at least {minimum} bars, got {len(bars)}"
        )
    for earlier, later in zip(bars, bars[1:], strict=False):
        if later.start_at <= earlier.start_at:
            raise FeatureComputationError(f"{feature}: bars must be strictly ordered")


def atr(bars: tuple[Bar, ...], *, period: int) -> Decimal:
    """Average True Range (Wilder smoothing) over frozen bars.

    Formula version: ``ATR_WILDER_V1``. Requires ``period + 1`` bars (the
    first true range needs a previous close).
    """
    if period < 1:
        raise FeatureComputationError("atr: period must be >= 1")
    _require_bars(bars, period + 1, "atr")
    true_ranges: list[Decimal] = []
    for previous, current in zip(bars, bars[1:], strict=False):
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    window = true_ranges[-period:]
    value = sum(window[:1], Decimal(0))  # seed with the first TR of the window
    for true_range in window[1:]:
        value = (value * (period - 1) + true_range) / period
    return value


def sma_close(bars: tuple[Bar, ...], *, period: int) -> Decimal:
    """Simple moving average of the CLOSE over the last ``period`` bars.

    Formula version: ``SMA_CLOSE_V1`` (the baseline trend filter uses
    period=150). Pure Decimal mean of the final ``period`` closes — the same
    frozen bars always produce the same value.
    """
    if period < 1:
        raise FeatureComputationError("sma_close: period must be >= 1")
    _require_bars(bars, period, "sma_close")
    window = bars[-period:]
    total = sum((bar.close for bar in window), Decimal(0))
    return total / period


def adv_dollar(bars: tuple[Bar, ...], *, period: int) -> Decimal:
    """Average daily dollar volume (close * volume) over the last ``period`` bars.

    Formula version: ``ADV_DOLLAR_V1`` (ADV20 uses period=20).
    """
    if period < 1:
        raise FeatureComputationError("adv_dollar: period must be >= 1")
    _require_bars(bars, period, "adv_dollar")
    window = bars[-period:]
    total = sum((bar.close * bar.volume for bar in window), Decimal(0))
    return total / period


def spread(quote: Quote) -> Decimal:
    """Absolute bid/ask spread from one quote snapshot.

    Formula version: ``SPREAD_QUOTE_V1``. The quote already carries the
    mandatory timestamp and provider attribution (section 20.3).
    """
    if quote.ask < quote.bid:
        raise FeatureComputationError("spread: crossed quote (ask < bid)")
    return quote.ask - quote.bid


def relative_volume(bars: tuple[Bar, ...], *, period: int) -> Decimal:
    """Latest bar volume relative to the average of the prior ``period`` bars.

    Formula version: ``RELATIVE_VOLUME_V1``.
    """
    if period < 1:
        raise FeatureComputationError("relative_volume: period must be >= 1")
    _require_bars(bars, period + 1, "relative_volume")
    baseline_bars = bars[-(period + 1) : -1]
    baseline = sum((bar.volume for bar in baseline_bars), Decimal(0)) / period
    if baseline == 0:
        raise FeatureComputationError("relative_volume: zero baseline volume")
    return bars[-1].volume / baseline
