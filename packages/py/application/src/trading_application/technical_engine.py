"""Technical engine v1 (IMP-0801).

Authority: Strategy_and_Decision_Framework_v0.5 — section 13.2 signal family
TECHNICAL (structure, levels, trend, breakout, relative strength) and section
7.3 BREAKOUT core features (breakout level, relative volume, false-breakout
risk, spread, distance to resistance; eligibility requires bars history,
liquidity and a suitable session); Decision_Contract 13.2 (the canonical
``SignalAssessment`` output). Inputs are FROZEN only: an IMP-0703
FeatureSnapshot, an IMP-0702 BarSeries and (optionally) the IMP-0704 session
status — never a live provider (ADR-0014).

Hard rules implemented here:

- **No hardcoded thresholds**: every threshold/lookback/tolerance/weight is a
  required field of ``TechnicalPolicy`` (config input, no defaults). A
  missing policy cannot fall back silently — construction fails.
- **Deterministic**: same snapshot + bars + policy + as_of => identical
  assessment (pure Decimal arithmetic, no randomness, no clock reads).
- **Point-in-time**: any bar or feature observed after ``as_of`` aborts
  (lookahead is a hard error, not a warning).
- **Fail-closed**: a missing/UNAVAILABLE/STALE/UNKNOWN feature can never
  produce a TRIGGERED signal — the assessment becomes INCONCLUSIVE with
  reason codes. An unsuitable session or failed liquidity guard BLOCKS.
- Output is the canonical assessment plus an explanation object (reason
  codes, component outcomes, computed levels) for dashboards/debug traces.
  No Candidate/Decision/Order/OrderIntent is created here, and the final
  trade score belongs to PHASE-9 — ``score`` is only the policy-weighted
  fraction of satisfied technical components. Everything stays
  fixtures-validated: ``calibration_status`` is UNVERIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from trading_contracts.decision import SignalAssessment
from trading_contracts.enums import (
    AssessmentStatus,
    CalibrationStatus,
    FreshnessClass,
    MarketSessionStatus,
    SignalDirection,
    SignalFamily,
)
from trading_market_data_core import BarSeries, FeatureSnapshot
from trading_market_data_core.snapshot import FEATURE_AVAILABLE

ENGINE_MODEL_ID = "technical-engine"
ENGINE_MODEL_VERSION = "1.0.0"

_UNUSABLE_FRESHNESS = (FreshnessClass.STALE, FreshnessClass.UNKNOWN)


class TechnicalEngineError(Exception):
    """Fail-closed engine failure (bad inputs, lookahead, mismatch)."""


@dataclass(frozen=True, slots=True)
class TechnicalPolicy:
    """All technical thresholds/weights — configuration input, no defaults."""

    policy_version: str
    level_lookback_bars: int
    trend_lookback_bars: int
    min_relative_volume: Decimal
    max_spread: Decimal
    max_atr_extension: Decimal  # distance beyond the level, in ATR units
    min_trend_change: Decimal  # relative close-to-close change for a trend
    weight_breakout: Decimal
    weight_volume: Decimal
    weight_trend: Decimal

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise TechnicalEngineError("policy_version is mandatory")
        if self.level_lookback_bars < 2 or self.trend_lookback_bars < 1:
            raise TechnicalEngineError("lookback windows must be positive")
        total = self.weight_breakout + self.weight_volume + self.weight_trend
        if total <= 0:
            raise TechnicalEngineError("component weights must sum to a positive value")


@dataclass(frozen=True, slots=True)
class TechnicalEvaluation:
    """Canonical assessment + explanation for dashboard/debug tracing."""

    assessment: SignalAssessment
    reason_codes: tuple[str, ...]
    components: tuple[tuple[str, bool], ...]
    support_level: Decimal | None
    resistance_level: Decimal | None


class TechnicalEngine:
    """Deterministic TECHNICAL SignalAssessment producer over frozen inputs."""

    def __init__(self, policy: TechnicalPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        instrument_id: UUID,
        *,
        snapshot: FeatureSnapshot,
        bars: BarSeries,
        as_of: datetime,
        session: MarketSessionStatus | None = None,
    ) -> TechnicalEvaluation:
        policy = self._policy
        if as_of.tzinfo is None:
            raise TechnicalEngineError("as_of must be timezone-aware")
        if snapshot.instrument_id != str(instrument_id):
            raise TechnicalEngineError("snapshot belongs to a different instrument")
        if bars.instrument_id != str(instrument_id):
            raise TechnicalEngineError("bars belong to a different instrument")
        if snapshot.as_of > as_of:
            raise TechnicalEngineError("snapshot as_of is in the future of as_of")
        for bar in bars.bars:
            if bar.observed_at > as_of:
                raise TechnicalEngineError(
                    "lookahead: bar observed after as_of offered to the engine"
                )

        reason_codes: list[str] = []
        evidence = [
            f"policy:{policy.policy_version}",
            f"snapshot:{snapshot.content_hash()[:16]}",
            f"bars:{bars.provider_id}:{bars.interval}:{len(bars.bars)}",
        ]

        # -- session eligibility (7.3: suitable session required) -------------
        if session is not None and session is not MarketSessionStatus.REGULAR:
            reason_codes.append(f"SESSION_{session.value}")
            return self._finish(
                instrument_id, snapshot, as_of, evidence, reason_codes,
                status=AssessmentStatus.BLOCKED,
                direction=SignalDirection.NEUTRAL,
                components=(), score=Decimal(0),
                support=None, resistance=None,
            )

        # -- required frozen features (fail closed on anything unusable) ------
        feature_values: dict[str, Decimal] = {}
        for name in ("spread", "relative_volume", "atr"):
            try:
                feature = snapshot.get(name)
            except KeyError:
                feature = None
            if feature is None or feature.status != FEATURE_AVAILABLE:
                reason_codes.append(f"FEATURE_UNAVAILABLE_{name.upper()}")
                continue
            if feature.freshness in _UNUSABLE_FRESHNESS:
                reason_codes.append(f"FEATURE_FRESHNESS_{name.upper()}")
                continue
            assert feature.value is not None
            feature_values[name] = feature.value
        history_needed = max(policy.level_lookback_bars, policy.trend_lookback_bars) + 1
        if len(bars.bars) < history_needed:
            reason_codes.append("INSUFFICIENT_BARS")
        if len(feature_values) < 3 or "INSUFFICIENT_BARS" in reason_codes:
            # Missing data can never become a positive signal.
            return self._finish(
                instrument_id, snapshot, as_of, evidence, reason_codes,
                status=AssessmentStatus.INCONCLUSIVE,
                direction=SignalDirection.NEUTRAL,
                components=(), score=Decimal(0),
                support=None, resistance=None,
            )

        # -- levels from frozen bars (support/resistance, current bar excluded)
        window = bars.bars[-(policy.level_lookback_bars + 1) : -1]
        last = bars.bars[-1]
        resistance = max(bar.high for bar in window)
        support = min(bar.low for bar in window)

        # -- components --------------------------------------------------------
        breakout_up = last.close > resistance
        breakout_down = last.close < support
        volume_confirmed = feature_values["relative_volume"] >= policy.min_relative_volume
        trend_reference = bars.bars[-(policy.trend_lookback_bars + 1)].close
        trend_change = (last.close - trend_reference) / trend_reference
        trend_up = trend_change >= policy.min_trend_change
        trend_down = trend_change <= -policy.min_trend_change

        # -- guards (7.3: liquidity + false-breakout risk) ---------------------
        blocked = False
        if feature_values["spread"] > policy.max_spread:
            reason_codes.append("WIDE_SPREAD")
            blocked = True
        atr = feature_values["atr"]
        false_breakout_risk = False
        if breakout_up and atr > 0:
            extension = (last.close - resistance) / atr
            if extension > policy.max_atr_extension:
                reason_codes.append("FALSE_BREAKOUT_RISK")
                false_breakout_risk = True

        components = (
            ("breakout_up", breakout_up),
            ("breakout_down", breakout_down),
            ("volume_confirmed", volume_confirmed),
            ("trend_up", trend_up),
            ("trend_down", trend_down),
        )

        # -- status/direction ---------------------------------------------------
        if blocked:
            status = AssessmentStatus.BLOCKED
            direction = SignalDirection.NEUTRAL
        elif breakout_up and volume_confirmed and not false_breakout_risk:
            status = AssessmentStatus.TRIGGERED
            direction = SignalDirection.LONG
            reason_codes.append("BREAKOUT_CONFIRMED")
        elif breakout_down and volume_confirmed:
            status = AssessmentStatus.TRIGGERED
            direction = SignalDirection.NEGATIVE
            reason_codes.append("BREAKDOWN_CONFIRMED")
        else:
            status = AssessmentStatus.ELIGIBLE
            if trend_up:
                direction = SignalDirection.LONG
                reason_codes.append("TREND_UP")
            elif trend_down:
                direction = SignalDirection.NEGATIVE
                reason_codes.append("TREND_DOWN")
            else:
                direction = SignalDirection.NEUTRAL
                reason_codes.append("NO_TECHNICAL_SIGNAL")

        # -- policy-weighted component score (not a PHASE-9 trade score) --------
        satisfied = Decimal(0)
        if breakout_up or breakout_down:
            satisfied += self._policy.weight_breakout
        if volume_confirmed:
            satisfied += self._policy.weight_volume
        if trend_up or trend_down:
            satisfied += self._policy.weight_trend
        total = policy.weight_breakout + policy.weight_volume + policy.weight_trend
        score = satisfied / total

        return self._finish(
            instrument_id, snapshot, as_of, evidence, reason_codes,
            status=status, direction=direction, components=components,
            score=score, support=support, resistance=resistance,
        )

    def _finish(
        self,
        instrument_id: UUID,
        snapshot: FeatureSnapshot,
        as_of: datetime,
        evidence: list[str],
        reason_codes: list[str],
        *,
        status: AssessmentStatus,
        direction: SignalDirection,
        components: tuple[tuple[str, bool], ...],
        score: Decimal,
        support: Decimal | None,
        resistance: Decimal | None,
    ) -> TechnicalEvaluation:
        observed_candidates = [
            feature.source_observed_at
            for feature in snapshot.features
            if feature.source_observed_at is not None
        ]
        observed_at = max(observed_candidates) if observed_candidates else snapshot.as_of
        assessment = SignalAssessment(
            instrument_id=instrument_id,
            signal_family=SignalFamily.TECHNICAL,
            model_id=ENGINE_MODEL_ID,
            model_version=ENGINE_MODEL_VERSION,
            direction=direction,
            status=status,
            score=score,
            calibrated_probability=None,
            calibration_status=CalibrationStatus.UNVERIFIED,  # fixtures-validated only
            observed_at=observed_at,
            available_at=as_of,
            evidence_refs=tuple(evidence),
            risk_flags=tuple(code for code in reason_codes if code.isupper() or "_" in code),
        )
        return TechnicalEvaluation(
            assessment=assessment,
            reason_codes=tuple(reason_codes),
            components=components,
            support_level=support,
            resistance_level=resistance,
        )


__all__ = [
    "ENGINE_MODEL_ID",
    "ENGINE_MODEL_VERSION",
    "TechnicalEngine",
    "TechnicalEngineError",
    "TechnicalEvaluation",
    "TechnicalPolicy",
]
