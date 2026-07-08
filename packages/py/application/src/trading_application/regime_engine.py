"""Market regime engine v1 (IMP-0805).

Authority: Portfolio_Capital_Allocation_and_Market_Regime_v0.1 section 5
(regime classes: TRENDING_RISK_ON — positive indices/breadth/sector momentum;
RANGE_BOUND — weak trend/reversion; HIGH_VOLATILITY — elevated ATR-like
measures/gaps; RISK_OFF — negative breadth, rising correlation; EVENT_STRESS
— systemic macro/news; UNKNOWN — missing data or low confidence), section 6
(the regime contract: regime, confidence_state HEURISTIC|CALIBRATED, score,
observed_at, valid_until, evidence, model_version) and section 10 (UNKNOWN is
never treated as NORMAL).

Deterministic, heuristic classification over FROZEN market-level indicator
fixtures (ADR-0014 — no provider, no network, no secrets):

- **No hardcoded thresholds**: every band/threshold/validity window is a
  required ``RegimePolicy`` field. A missing/invalid policy fails closed.
- **UNKNOWN is explicit and fail-closed**: missing, stale, unknown-freshness
  or insufficient indicators mean the affected rules cannot fire; if no rule
  fires conclusively the result is UNKNOWN — the engine never guesses and
  UNKNOWN never masquerades as a normal regime.
- **Point-in-time**: an indicator observed after ``as_of`` is a hard
  lookahead error; naive timestamps are rejected.
- **Severity precedence** (derived from section 5 semantics): EVENT_STRESS,
  then HIGH_VOLATILITY, then RISK_OFF, then TRENDING_RISK_ON, then
  RANGE_BOUND — a stressed/volatile market is never reported as a benign
  trend.
- ``confidence_state`` is HEURISTIC (v1; CALIBRATED requires the model
  governance PoI) and no self-reported confidence exists. Output is a regime
  assessment only — never a Candidate/Decision/Order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading_contracts.enums import CalibrationStatus, FreshnessClass, MarketRegime

ENGINE_MODEL_ID = "market-regime-engine"
ENGINE_MODEL_VERSION = "regime.heuristic.v1"  # section 6 example format

# Indicator names consumed by the rules (values come from fixtures).
INDICATOR_BREADTH = "breadth"  # advancers fraction / breadth measure
INDICATOR_TREND = "index_trend_change"  # relative index change over lookback
INDICATOR_ATR_RATIO = "atr_ratio"  # ATR-like measure relative to price
INDICATOR_CORRELATION = "correlation"  # cross-asset correlation measure

_UNUSABLE_FRESHNESS = (FreshnessClass.STALE, FreshnessClass.UNKNOWN)


class RegimeEngineError(Exception):
    """Fail-closed engine failure (bad policy/inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class RegimeIndicator:
    """One frozen market-level indicator with attribution."""

    name: str
    value: Decimal
    provider_id: str
    observed_at: datetime
    freshness: FreshnessClass

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if not self.provider_id:
            raise RegimeEngineError("indicator provider attribution is mandatory")


@dataclass(frozen=True, slots=True)
class RegimePolicy:
    """All classification thresholds — configuration input, no defaults."""

    policy_version: str
    high_vol_min_atr_ratio: Decimal
    risk_off_max_breadth: Decimal
    risk_off_min_correlation: Decimal
    trend_min_change: Decimal
    trend_min_breadth: Decimal
    range_max_abs_change: Decimal
    validity_minutes: int

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise RegimeEngineError("policy_version is mandatory")
        if self.validity_minutes <= 0:
            raise RegimeEngineError("validity_minutes must be positive")
        if self.range_max_abs_change >= self.trend_min_change:
            raise RegimeEngineError(
                "range band must be strictly inside the trend threshold"
            )


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    """Canonical regime contract (Portfolio/Regime section 6)."""

    regime: MarketRegime
    confidence_state: CalibrationStatus
    score: Decimal
    observed_at: datetime
    valid_until: datetime
    as_of: datetime
    model_version: str
    evidence: tuple[str, ...]
    reason_codes: tuple[str, ...]


class MarketRegimeEngine:
    """Deterministic regime classifier over frozen indicators."""

    def __init__(self, policy: RegimePolicy) -> None:
        self._policy = policy

    def classify(
        self,
        indicators: tuple[RegimeIndicator, ...],
        *,
        as_of: datetime,
        event_stress_active: bool = False,
    ) -> RegimeAssessment:
        """Classify the market regime at ``as_of``. UNKNOWN when unsure."""
        if as_of.tzinfo is None:
            raise RegimeEngineError("as_of must be timezone-aware")
        policy = self._policy
        reason_codes: list[str] = []
        evidence = [f"policy:{policy.policy_version}"]

        usable: dict[str, RegimeIndicator] = {}
        for indicator in indicators:
            if indicator.observed_at > as_of:
                raise RegimeEngineError(
                    f"lookahead: indicator {indicator.name!r} observed after as_of"
                )
            if indicator.freshness in _UNUSABLE_FRESHNESS:
                reason_codes.append(f"INDICATOR_FRESHNESS_{indicator.name.upper()}")
                continue
            if indicator.name in usable:
                raise RegimeEngineError(f"duplicate indicator {indicator.name!r}")
            usable[indicator.name] = indicator
        for indicator in sorted(usable.values(), key=lambda item: item.name):
            evidence.append(
                f"indicator:{indicator.name}={indicator.value}"
                f"@{indicator.provider_id}@{indicator.observed_at.isoformat()}"
            )

        def value(name: str) -> Decimal | None:
            indicator = usable.get(name)
            if indicator is None and f"INDICATOR_FRESHNESS_{name.upper()}" not in reason_codes:
                reason_codes.append(f"INDICATOR_MISSING_{name.upper()}")
            return indicator.value if indicator is not None else None

        breadth = value(INDICATOR_BREADTH)
        trend = value(INDICATOR_TREND)
        atr_ratio = value(INDICATOR_ATR_RATIO)
        correlation = value(INDICATOR_CORRELATION)

        regime: MarketRegime
        conditions_met = Decimal(0)
        conditions_total = Decimal(0)

        # Severity precedence (section 5): stress and volatility dominate.
        if event_stress_active:
            regime = MarketRegime.EVENT_STRESS
            reason_codes.append("EVENT_STRESS_INPUT_ACTIVE")
            conditions_met, conditions_total = Decimal(1), Decimal(1)
        elif atr_ratio is not None and atr_ratio >= policy.high_vol_min_atr_ratio:
            regime = MarketRegime.HIGH_VOLATILITY
            reason_codes.append("ATR_RATIO_ABOVE_BAND")
            conditions_met, conditions_total = Decimal(1), Decimal(1)
        elif (
            breadth is not None
            and correlation is not None
            and (
                breadth <= policy.risk_off_max_breadth
                or correlation >= policy.risk_off_min_correlation
            )
        ):
            regime = MarketRegime.RISK_OFF
            conditions_total = Decimal(2)
            if breadth <= policy.risk_off_max_breadth:
                reason_codes.append("BREADTH_NEGATIVE")
                conditions_met += 1
            if correlation >= policy.risk_off_min_correlation:
                reason_codes.append("CORRELATION_ELEVATED")
                conditions_met += 1
        elif (
            trend is not None
            and breadth is not None
            and trend >= policy.trend_min_change
            and breadth >= policy.trend_min_breadth
        ):
            regime = MarketRegime.TRENDING_RISK_ON
            reason_codes.extend(("TREND_POSITIVE", "BREADTH_POSITIVE"))
            conditions_met, conditions_total = Decimal(2), Decimal(2)
        elif trend is not None and abs(trend) <= policy.range_max_abs_change:
            regime = MarketRegime.RANGE_BOUND
            reason_codes.append("TREND_WEAK_WITHIN_RANGE_BAND")
            conditions_met, conditions_total = Decimal(1), Decimal(1)
        else:
            # Missing/contradictory/insufficient data: explicit UNKNOWN,
            # never a guess and never treated as normal (section 10).
            regime = MarketRegime.UNKNOWN
            reason_codes.append("REGIME_UNDETERMINED")
            conditions_met, conditions_total = Decimal(0), Decimal(1)

        score = conditions_met / conditions_total
        observed_candidates = [indicator.observed_at for indicator in usable.values()]
        observed_at = max(observed_candidates) if observed_candidates else as_of
        return RegimeAssessment(
            regime=regime,
            confidence_state=CalibrationStatus.HEURISTIC,  # v1; no real calibration
            score=score,
            observed_at=observed_at,
            valid_until=as_of + timedelta(minutes=policy.validity_minutes),
            as_of=as_of,
            model_version=ENGINE_MODEL_VERSION,
            evidence=tuple(evidence),
            reason_codes=tuple(dict.fromkeys(reason_codes)),
        )


__all__ = [
    "ENGINE_MODEL_ID",
    "ENGINE_MODEL_VERSION",
    "INDICATOR_ATR_RATIO",
    "INDICATOR_BREADTH",
    "INDICATOR_CORRELATION",
    "INDICATOR_TREND",
    "MarketRegimeEngine",
    "RegimeAssessment",
    "RegimeEngineError",
    "RegimeIndicator",
    "RegimePolicy",
]
