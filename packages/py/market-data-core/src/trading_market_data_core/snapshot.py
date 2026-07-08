"""Point-in-time feature snapshots (IMP-0703).

Authority: Data_Model_and_Retention_Specification_v0.6 (feature_snapshots:
a FROZEN group of features for a candidate/decision, append-only; every
assessment/decision references a feature_snapshot and an as_of) and
Integration_Specification_v0.6 section 20.3 (formula_version mandatory;
spread carries timestamp+provider; ATR/ADV computed on frozen bars).

A snapshot is keyed by (instrument_id, as_of) and is **deterministic**: the
same inputs and the same ``as_of`` always produce an identical snapshot —
provable via ``content_hash()``. It is point-in-time safe by construction:
all inputs come through the IMP-0702 MarketDataService, which rejects any
datum observed after ``as_of`` (no lookahead).

Missing/stale/unknown inputs never fall back silently: the affected feature
is recorded as the canonical ``UNAVAILABLE`` status with a reason, and
callers that require it must treat UNAVAILABLE as fail-closed. Snapshots
carry market values and attribution only — never secrets, sessions or raw
payloads. Suitable for replay, backtesting, paper simulation, dashboard
explanation and audit/debug tracing (stable content hash + full attribution).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.enums import FreshnessClass
from trading_domain.instruments import InstrumentRecord

from trading_market_data_core import features as f
from trading_market_data_core.service import (
    FreshnessPolicy,
    MarketDataError,
    MarketDataService,
)

FEATURE_AVAILABLE = "OK"
FEATURE_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class FeatureValue:
    """One computed feature with full attribution.

    ``value is None`` iff ``status == UNAVAILABLE`` (canonical missing-data
    state; no silent default). ``formula_version`` is mandatory (20.3).
    """

    name: str
    status: str
    formula_version: str
    value: Decimal | None = None
    source_provider_id: str | None = None
    source_observed_at: datetime | None = None
    freshness: FreshnessClass | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if (self.value is None) != (self.status == FEATURE_UNAVAILABLE):
            raise ValueError("value must be None exactly when UNAVAILABLE")
        if self.source_observed_at is not None and self.source_observed_at.tzinfo is None:
            raise ValueError("source_observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """A frozen, deterministic feature group for one instrument at as_of."""

    instrument_id: str
    as_of: datetime
    features: tuple[FeatureValue, ...]

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        names = [feature.name for feature in self.features]
        if len(names) != len(set(names)):
            raise ValueError("duplicate feature names in one snapshot")

    def get(self, name: str) -> FeatureValue:
        for feature in self.features:
            if feature.name == name:
                return feature
        raise KeyError(name)

    def to_document(self) -> dict[str, object]:
        """Canonical serialization (Decimals as strings — no precision loss)."""
        return {
            "instrument_id": self.instrument_id,
            "as_of": self.as_of.isoformat(),
            "features": [
                {
                    "name": feature.name,
                    "status": feature.status,
                    "formula_version": feature.formula_version,
                    "value": str(feature.value) if feature.value is not None else None,
                    "source_provider_id": feature.source_provider_id,
                    "source_observed_at": (
                        feature.source_observed_at.isoformat()
                        if feature.source_observed_at
                        else None
                    ),
                    "freshness": feature.freshness.value if feature.freshness else None,
                    "reason": feature.reason,
                }
                for feature in sorted(self.features, key=lambda item: item.name)
            ],
        }

    def content_hash(self) -> str:
        """Stable hash proving replay determinism and enabling audit pinning."""
        payload = json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FeatureSnapshotBuilder:
    """Builds deterministic snapshots from the MarketDataService (IMP-0702)."""

    def __init__(
        self,
        service: MarketDataService,
        *,
        bar_interval: str,
        atr_period: int,
        adv_period: int,
        relative_volume_period: int,
        quote_policy: FreshnessPolicy,
        history: int | None = None,
    ) -> None:
        # Periods and freshness requirements are policy/config inputs.
        self._service = service
        self._interval = bar_interval
        self._atr_period = atr_period
        self._adv_period = adv_period
        self._rvol_period = relative_volume_period
        self._quote_policy = quote_policy
        self._history = history or (max(atr_period, adv_period, relative_volume_period) + 1)

    def build(
        self,
        instrument: InstrumentRecord,
        *,
        as_of: datetime,
        history_start: datetime,
    ) -> FeatureSnapshot:
        """Compute the canonical feature group for ``instrument`` at ``as_of``."""
        values: list[FeatureValue] = []
        values.extend(self._bar_features(instrument, as_of, history_start))
        values.append(self._spread_feature(instrument, as_of))
        return FeatureSnapshot(
            instrument_id=instrument.instrument_id,
            as_of=as_of,
            features=tuple(values),
        )

    def _bar_features(
        self, instrument: InstrumentRecord, as_of: datetime, history_start: datetime
    ) -> list[FeatureValue]:
        try:
            series = self._service.get_bars(
                instrument,
                interval=self._interval,
                start=history_start,
                end=as_of,
                as_of=as_of,
            )
        except MarketDataError as error:
            return [
                self._unavailable(name, version, str(error))
                for name, version in (
                    ("atr", f.ATR_WILDER_V1),
                    ("adv_dollar", f.ADV_DOLLAR_V1),
                    ("relative_volume", f.RELATIVE_VOLUME_V1),
                )
            ]
        bars = series.bars
        latest_observed = max(bar.observed_at for bar in bars)
        worst_freshness = min(
            (bar.freshness for bar in bars),
            key=lambda item: {"REALTIME": 3, "NEAR_REALTIME": 2, "DELAYED": 1}.get(
                item.value, 0
            ),
        )
        computations: tuple[tuple[str, str, Callable[[], Decimal]], ...] = (
            ("atr", f.ATR_WILDER_V1, lambda: f.atr(bars, period=self._atr_period)),
            (
                "adv_dollar",
                f.ADV_DOLLAR_V1,
                lambda: f.adv_dollar(bars, period=self._adv_period),
            ),
            (
                "relative_volume",
                f.RELATIVE_VOLUME_V1,
                lambda: f.relative_volume(bars, period=self._rvol_period),
            ),
        )
        results: list[FeatureValue] = []
        for name, version, compute in computations:
            try:
                results.append(
                    FeatureValue(
                        name=name,
                        status=FEATURE_AVAILABLE,
                        formula_version=version,
                        value=compute(),
                        source_provider_id=series.provider_id,
                        source_observed_at=latest_observed,
                        freshness=worst_freshness,
                    )
                )
            except f.FeatureComputationError as error:
                results.append(self._unavailable(name, version, str(error)))
        return results

    def _spread_feature(self, instrument: InstrumentRecord, as_of: datetime) -> FeatureValue:
        try:
            quote = self._service.get_quote(
                instrument, as_of=as_of, policy=self._quote_policy
            )
        except MarketDataError as error:
            # STALE/UNKNOWN/missing quotes land here: canonical UNAVAILABLE,
            # never a silent fallback (7.3/7.4).
            return self._unavailable("spread", f.SPREAD_QUOTE_V1, str(error))
        try:
            value = f.spread(quote)
        except f.FeatureComputationError as error:
            return self._unavailable("spread", f.SPREAD_QUOTE_V1, str(error))
        return FeatureValue(
            name="spread",
            status=FEATURE_AVAILABLE,
            formula_version=f.SPREAD_QUOTE_V1,
            value=value,
            source_provider_id=quote.provider_id,
            source_observed_at=quote.observed_at,
            freshness=quote.freshness,
        )

    @staticmethod
    def _unavailable(name: str, version: str, reason: str) -> FeatureValue:
        return FeatureValue(
            name=name,
            status=FEATURE_UNAVAILABLE,
            formula_version=version,
            reason=reason,
        )
