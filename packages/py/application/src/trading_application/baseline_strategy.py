"""Baseline trend-breakout strategy evaluator (MVP trading strategy).

Deterministic realization of the OWNER-FROZEN strategy
``baseline-trend-breakout-v1`` (``docs/strategies/baseline-trend-breakout-v1.yaml``,
FROZEN 2026-08-13, fingerprint
``sha256:75f299b89e408ef332cd39a46f92d417ef637b3c5a5c1712801774c9e84dfed0``).
This module implements the *entry* setup/trigger/entry-cap/structural-stop
semantics of that record; the strategy-driven *exit* semantics live in
``baseline_exit``.

Ownership boundary (mission section 3): the strategy answers ONE question —
"is there a valid trading opportunity?" — and nothing else. It never authorizes
execution, never submits, never sizes the final position (only the Risk Engine
does, IMP-0904), never invents account/P&L/portfolio state and never bypasses
market-data readiness. A NO-TRADE result carries reason codes and produces NO
canonical ``SignalAssessment`` in the ``TRIGGERED`` state, so it can never be
turned into a candidate/decision/order downstream.

Hard rules (mirroring the canonical engines):

- **No invented parameters.** Every lookback, period and ATR multiple is a
  REQUIRED field of :class:`BaselineTrendBreakoutPolicy` — a configuration
  input carrying the owner-frozen value and the frozen fingerprint. Nothing is
  hardcoded; a missing policy cannot fall back silently (construction fails).
- **Deterministic.** Same frozen bars + policy + as_of => identical evaluation
  (pure Decimal arithmetic, no clock reads, no randomness).
- **Point-in-time.** Any bar observed after ``as_of`` aborts (lookahead is a
  hard error, not a warning); every value used satisfies available_at <=
  decision_at.
- **Fail-closed.** Stale/missing/insufficient data, an out-of-session
  evaluation, a non-contracting base, or a risk-per-share wider than the owner
  cap can never produce a ``TRIGGERED`` signal — the result is an explicit
  NO-TRADE with reason codes and a non-triggered assessment.
- **Mandatory predicates are AND conditions.** Trend AND contracting-base AND
  breakout-trigger must all hold. The failure of ANY one is NO TRADE; there is
  never a "mostly valid" candidate.
"""

from __future__ import annotations

import uuid
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
from trading_market_data_core.features import FeatureComputationError, atr, sma_close
from trading_market_data_core.types import Bar, BarSeries

STRATEGY_ENGINE_VERSION = "baseline-strategy-1.0.0"
#: model_id / model_version carried on the emitted assessment. The manifest key
#: the frozen fusion manifest REQUIRES is ``technical.breakout`` (owner-approved
#: verbatim 2026-08-13); this is the model that produces it.
STRATEGY_MODEL_ID = "baseline-trend-breakout"
STRATEGY_MODEL_VERSION = "1.0.0"
#: The canonical assessment key the frozen fusion manifest requires TRIGGERED.
TECHNICAL_BREAKOUT_KEY = "technical.breakout"

#: Deterministic namespace for content-derived candidate ids.
_CANDIDATE_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000ba5e1")

#: Freshness classes that can never support a new-entry signal (staleness
#: behaviour: STALE/UNKNOWN => BLOCK_NEW_SIGNALS).
_UNUSABLE_FRESHNESS: tuple[FreshnessClass, ...] = (
    FreshnessClass.STALE,
    FreshnessClass.UNKNOWN,
)

#: The owner-ratified base-length selection rule. The frozen record states
#: "base duration 10-20 trading days" but does not state which valid length to
#: use when several qualify; the owner resolved this ambiguity on 2026-08-31
#: (OWNER-EYAL-2026-08-31-BASELINE-BASE-SELECTION-LARGEST) as: use the LARGEST
#: valid window (search 20 down to 10). It is an explicit, attributed rule here,
#: never a silently-chosen implementation detail. Any other value fails closed.
BASE_SELECTION_LARGEST = "LARGEST_VALID_WINDOW"


class BaselineStrategyError(Exception):
    """Fail-closed strategy failure (bad inputs, lookahead, mismatch)."""


@dataclass(frozen=True, slots=True)
class BaselineTrendBreakoutPolicy:
    """Every numeric parameter of the frozen strategy — configuration input.

    No field has a default: the caller supplies the owner-frozen values (see
    ``config/strategies/baseline-trend-breakout-v1.execution-params.yaml`` and
    :func:`trading_application.baseline_strategy_config.baseline_trend_breakout_v1_policy`).
    ``frozen_fingerprint`` binds this policy to the immutable freeze record, so
    a silent drift from the owner-approved numbers is detectable.
    """

    strategy_id: str
    version_id: str
    frozen_fingerprint: str
    sma_trend_period: int  # SMA150 trend filter (owner setup.definition)
    sma_trend_rising_lookback: int  # SMA150(today) vs SMA150(N days ago)
    base_min_bars: int  # contracting base duration lower bound (10)
    base_max_bars: int  # contracting base duration upper bound (20)
    range_window: int  # Range(last 5) vs Range(previous 5)
    atr_short_period: int  # ATR10
    atr_long_period: int  # ATR20 (drives entry cap and structural stop)
    entry_atr_multiple: Decimal  # MaxEntryPrice = Close + 0.25 * ATR20
    stop_atr_multiple: Decimal  # StructuralStop = BaseLow - 0.25 * ATR20
    max_risk_atr_multiple: Decimal  # SKIP if EntryPrice - Stop > 2.5 * ATR20
    #: Owner-ratified base-length selection rule (see BASE_SELECTION_LARGEST).
    #: Explicit config, not a silent default: only the ratified value is
    #: accepted; anything else fails closed.
    base_selection_rule: str

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.version_id.strip():
            raise BaselineStrategyError("strategy_id and version_id are mandatory")
        if not self.frozen_fingerprint.startswith("sha256:"):
            raise BaselineStrategyError("frozen_fingerprint must be a sha256 reference")
        if self.base_selection_rule != BASE_SELECTION_LARGEST:
            raise BaselineStrategyError(
                "base_selection_rule must be the owner-ratified"
                f" {BASE_SELECTION_LARGEST!r}; no other rule is supported"
            )
        periods = (
            self.sma_trend_period,
            self.sma_trend_rising_lookback,
            self.base_min_bars,
            self.base_max_bars,
            self.range_window,
            self.atr_short_period,
            self.atr_long_period,
        )
        if any(period < 1 for period in periods):
            raise BaselineStrategyError("all periods/lookbacks must be positive")
        if self.base_min_bars > self.base_max_bars:
            raise BaselineStrategyError("base_min_bars must not exceed base_max_bars")
        if self.base_min_bars < 2 * self.range_window:
            raise BaselineStrategyError(
                "base_min_bars must be at least 2 * range_window for the Range"
                " contraction comparison"
            )
        if self.atr_short_period >= self.atr_long_period:
            raise BaselineStrategyError("atr_short_period must be shorter than atr_long_period")
        multiples = (
            self.entry_atr_multiple,
            self.stop_atr_multiple,
            self.max_risk_atr_multiple,
        )
        if any(multiple <= 0 for multiple in multiples):
            raise BaselineStrategyError("all ATR multiples must be positive")

    @property
    def min_history_bars(self) -> int:
        """Smallest bar count that can evaluate every mandatory predicate.

        The trend-rising comparison needs ``sma_trend_period`` closes ending
        ``sma_trend_rising_lookback`` bars before the breakout bar, plus the
        breakout bar itself; the base + ATR20 need fewer, so the trend term
        dominates.
        """
        trend = self.sma_trend_period + self.sma_trend_rising_lookback + 1
        base = self.base_max_bars + 1
        atr_need = self.atr_long_period + 1
        return max(trend, base, atr_need)


@dataclass(frozen=True, slots=True)
class BaselineComponents:
    """Every mandatory predicate outcome (dashboard/debug explanation)."""

    session_ok: bool
    data_fresh: bool
    sufficient_history: bool
    trend_ok: bool
    contracting_base_ok: bool
    breakout_trigger_ok: bool
    risk_within_cap: bool

    @property
    def all_satisfied(self) -> bool:
        return (
            self.session_ok
            and self.data_fresh
            and self.sufficient_history
            and self.trend_ok
            and self.contracting_base_ok
            and self.breakout_trigger_ok
            and self.risk_within_cap
        )


@dataclass(frozen=True, slots=True)
class BaselineSignal:
    """The strategy's opportunity proposal — NEVER a final size or an order.

    Prices are the owner-frozen structural values; sizing is left entirely to
    the Risk Engine. ``max_entry_price`` is the marketable-limit cap
    (do-not-chase); ``planned_entry_price`` is the breakout close used as the
    consistent EntryPrice reference for the structural risk-per-share.
    """

    strategy_id: str
    version_id: str
    direction: SignalDirection
    breakout_close: Decimal
    resistance: Decimal
    base_low: Decimal
    atr_long: Decimal
    planned_entry_price: Decimal
    max_entry_price: Decimal
    structural_stop: Decimal
    per_share_risk: Decimal
    breakout_bar_end_at: datetime

    def __post_init__(self) -> None:
        if self.per_share_risk <= 0:
            raise BaselineStrategyError("a signal must carry positive per-share risk")
        if self.structural_stop <= 0 or self.structural_stop >= self.planned_entry_price:
            raise BaselineStrategyError("structural stop must be positive and below entry")


@dataclass(frozen=True, slots=True)
class BaselineEvaluation:
    """Canonical assessment + strategy proposal + full explanation.

    ``signal`` is present ONLY when every mandatory predicate holds. The
    ``assessment`` is always emitted for provenance, but its status is
    ``TRIGGERED`` only for a real opportunity — a NO-TRADE assessment is
    ``BLOCKED`` (a hard guard failed) or ``INCONCLUSIVE`` (missing/insufficient
    data), so it can never satisfy the manifest's REQUIRED/TRIGGERED gate.
    """

    strategy_id: str
    version_id: str
    candidate_id: UUID
    assessment: SignalAssessment
    signal: BaselineSignal | None
    components: BaselineComponents
    reason_codes: tuple[str, ...]
    no_trade_reasons: tuple[str, ...]

    @property
    def is_trade(self) -> bool:
        return self.signal is not None


class BaselineTrendBreakoutStrategy:
    """Deterministic ``baseline-trend-breakout-v1`` opportunity evaluator."""

    def __init__(self, policy: BaselineTrendBreakoutPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> BaselineTrendBreakoutPolicy:
        return self._policy

    def evaluate(
        self,
        instrument_id: UUID,
        *,
        bars: BarSeries,
        as_of: datetime,
        session: MarketSessionStatus | None = None,
    ) -> BaselineEvaluation:
        policy = self._policy
        if as_of.tzinfo is None:
            raise BaselineStrategyError("as_of must be timezone-aware")
        if bars.instrument_id != str(instrument_id):
            raise BaselineStrategyError("bars belong to a different instrument")
        ordered = bars.bars
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.start_at <= earlier.start_at:
                raise BaselineStrategyError("bars must be strictly time-ordered")
        for bar in ordered:
            if bar.observed_at > as_of:
                raise BaselineStrategyError(
                    "lookahead: a bar observed after as_of was offered to the strategy"
                )

        reasons: list[str] = []
        no_trade: list[str] = []

        # -- session eligibility: new entries RTH only, fail closed on unknown
        # An authoritative REGULAR session is REQUIRED. A missing/unknown session
        # (None) is never eligible — signals come from completed RTH bars only and
        # extended-hours entries are forbidden, so unknown must fail closed.
        session_ok = session is MarketSessionStatus.REGULAR
        if not session_ok:
            no_trade.append(
                f"SESSION_NOT_RTH_{session.value}" if session is not None
                else "SESSION_UNKNOWN"
            )

        # -- data freshness: STALE/UNKNOWN never signals ----------------------
        data_fresh = bool(ordered) and ordered[-1].freshness not in _UNUSABLE_FRESHNESS
        if not ordered:
            no_trade.append("NO_BARS")
        elif not data_fresh:
            no_trade.append(f"DATA_STALE_{ordered[-1].freshness.value}")

        # -- minimum history --------------------------------------------------
        sufficient_history = len(ordered) >= policy.min_history_bars
        if not sufficient_history:
            no_trade.append(
                f"INSUFFICIENT_HISTORY_{len(ordered)}_LT_{policy.min_history_bars}"
            )

        # A hard guard (session/freshness/history) that fails means we cannot
        # trust the structural computations at all: fail closed as BLOCKED /
        # INCONCLUSIVE without computing a positive signal.
        if not (session_ok and data_fresh and sufficient_history):
            status = (
                AssessmentStatus.BLOCKED
                if (not session_ok or not data_fresh)
                else AssessmentStatus.INCONCLUSIVE
            )
            components = BaselineComponents(
                session_ok=session_ok,
                data_fresh=data_fresh,
                sufficient_history=sufficient_history,
                trend_ok=False,
                contracting_base_ok=False,
                breakout_trigger_ok=False,
                risk_within_cap=False,
            )
            return self._finish(
                instrument_id, ordered, as_of, status, SignalDirection.NEUTRAL,
                components, reasons, no_trade, signal=None,
            )

        breakout_bar = ordered[-1]
        breakout_close = breakout_bar.close

        # -- trend filter (Close > SMA150 AND SMA150 rising) ------------------
        try:
            sma_today = sma_close(ordered, period=policy.sma_trend_period)
            sma_prior = sma_close(
                ordered[: -policy.sma_trend_rising_lookback], period=policy.sma_trend_period
            )
            # ATR20 for the entry cap and structural stop is the value "known at
            # the completion of the breakout bar" (owner entry.rule/exit.stop_rule),
            # so it includes the breakout bar.
            atr_long = atr(ordered, period=policy.atr_long_period)
        except FeatureComputationError as error:  # pragma: no cover - guarded by history
            raise BaselineStrategyError(f"feature computation failed: {error}") from error

        trend_above = breakout_close > sma_today
        trend_rising = sma_today > sma_prior
        trend_ok = trend_above and trend_rising
        if not trend_above:
            no_trade.append("TREND_CLOSE_BELOW_SMA")
        if not trend_rising:
            no_trade.append("TREND_SMA_NOT_RISING")

        # -- contracting base (breakout bar EXCLUDED) -------------------------
        pre_breakout = ordered[:-1]
        base = self._find_contracting_base(pre_breakout, sma_today)
        contracting_base_ok = base is not None
        if base is None:
            no_trade.append("NO_CONTRACTING_BASE")
            resistance = base_low = None
        else:
            resistance = max(bar.high for bar in base)
            base_low = min(bar.low for bar in base)

        # -- breakout trigger (completed close > base resistance) -------------
        breakout_trigger_ok = resistance is not None and breakout_close > resistance
        if resistance is not None and not breakout_trigger_ok:
            no_trade.append("NO_BREAKOUT_CLOSE_NOT_ABOVE_RESISTANCE")

        # -- entry cap / structural stop / risk cap ---------------------------
        risk_within_cap = False
        signal: BaselineSignal | None = None
        if (
            trend_ok
            and breakout_trigger_ok
            and base_low is not None
            and resistance is not None
        ):
            # The order is a BUY marketable limit whose cap is
            # MaxEntryPrice = BreakoutClose + 0.25*ATR20 ("do not chase" above
            # it). The cap is the worst-case fill, so it is the single,
            # consistent EntryPrice used for both the structural risk-per-share
            # and the risk-cap skip — sizing on the worst case is conservative
            # (a larger per-share risk yields a smaller position, never larger).
            max_entry_price = breakout_close + policy.entry_atr_multiple * atr_long
            structural_stop = base_low - policy.stop_atr_multiple * atr_long
            per_share_risk = max_entry_price - structural_stop
            if structural_stop <= 0:
                no_trade.append("INVALID_STRUCTURAL_STOP")
            elif per_share_risk <= 0:
                no_trade.append("NON_POSITIVE_RISK_PER_SHARE")
            elif per_share_risk > policy.max_risk_atr_multiple * atr_long:
                no_trade.append("RISK_PER_SHARE_EXCEEDS_CAP")
            else:
                risk_within_cap = True
                signal = BaselineSignal(
                    strategy_id=policy.strategy_id,
                    version_id=policy.version_id,
                    direction=SignalDirection.LONG,
                    breakout_close=breakout_close,
                    resistance=resistance,
                    base_low=base_low,
                    atr_long=atr_long,
                    planned_entry_price=max_entry_price,
                    max_entry_price=max_entry_price,
                    structural_stop=structural_stop,
                    per_share_risk=per_share_risk,
                    breakout_bar_end_at=breakout_bar.end_at,
                )

        components = BaselineComponents(
            session_ok=True,
            data_fresh=True,
            sufficient_history=True,
            trend_ok=trend_ok,
            contracting_base_ok=contracting_base_ok,
            breakout_trigger_ok=breakout_trigger_ok,
            risk_within_cap=risk_within_cap,
        )

        if components.all_satisfied and signal is not None:
            reasons.append("BREAKOUT_CONFIRMED")
            status = AssessmentStatus.TRIGGERED
            direction = SignalDirection.LONG
        else:
            # Every mandatory predicate is an AND: any single failure is a hard
            # NO TRADE. A completed evaluation with an unmet predicate is
            # ELIGIBLE-but-not-triggered => never approvable downstream.
            status = AssessmentStatus.ELIGIBLE
            direction = SignalDirection.NEUTRAL
            signal = None
            reasons.append("NO_TRADE_MANDATORY_PREDICATE_UNMET")

        return self._finish(
            instrument_id, ordered, as_of, status, direction,
            components, reasons, no_trade, signal=signal,
        )

    def _find_contracting_base(
        self,
        pre_breakout: tuple[Bar, ...],
        sma_today: Decimal,
    ) -> tuple[Bar, ...] | None:
        """Largest valid contracting base ending just before the breakout bar.

        Selection rule: OWNER-RATIFIED ``BASE_SELECTION_LARGEST``
        (OWNER-EYAL-2026-08-31-BASELINE-BASE-SELECTION-LARGEST) — when several
        lengths in the owner's [min, max] range each satisfy the contraction
        predicates, use the LARGEST. The frozen record left this ambiguous; the
        owner resolved it explicitly, and ``policy.base_selection_rule`` records
        the ratified value rather than the code choosing silently.

        Deterministic search from ``base_max_bars`` down to ``base_min_bars``
        (largest first): the base is the window of ``base_len`` completed bars
        immediately preceding the breakout bar that satisfies every owner
        contraction predicate — Range(last N) < Range(previous N), ATR10 <
        ATR20, and price held above SMA150 throughout. ``None`` when no length
        in the owner's [min, max] range qualifies.

        The ATR contraction is a property of the BASE, so it is computed on the
        pre-breakout series (the breakout bar's own range, which can be large,
        never contaminates a base-setup measurement).
        """
        policy = self._policy
        # ATR10 < ATR20 is a single volatility-contraction gate for the base,
        # measured before the breakout bar.
        try:
            atr_short = atr(pre_breakout, period=policy.atr_short_period)
            atr_long = atr(pre_breakout, period=policy.atr_long_period)
        except FeatureComputationError:  # pragma: no cover - guarded by history
            return None
        if not (atr_short < atr_long):
            return None
        window = policy.range_window
        for base_len in range(policy.base_max_bars, policy.base_min_bars - 1, -1):
            if len(pre_breakout) < base_len:
                continue
            base = pre_breakout[-base_len:]
            if any(bar.close <= sma_today for bar in base):
                continue  # price must remain above the trend filter
            recent = base[-window:]
            previous = base[-2 * window : -window]
            recent_range = max(b.high for b in recent) - min(b.low for b in recent)
            previous_range = max(b.high for b in previous) - min(b.low for b in previous)
            if recent_range < previous_range:
                return base
        return None

    def _finish(
        self,
        instrument_id: UUID,
        ordered: tuple[Bar, ...],
        as_of: datetime,
        status: AssessmentStatus,
        direction: SignalDirection,
        components: BaselineComponents,
        reasons: list[str],
        no_trade: list[str],
        *,
        signal: BaselineSignal | None,
    ) -> BaselineEvaluation:
        policy = self._policy
        observed_at = ordered[-1].observed_at if ordered else as_of
        breakout_end = ordered[-1].end_at.isoformat() if ordered else as_of.isoformat()
        score = Decimal(1) if status is AssessmentStatus.TRIGGERED else Decimal(0)
        evidence = (
            f"strategy:{policy.strategy_id}@{policy.version_id}",
            f"fingerprint:{policy.frozen_fingerprint}",
            f"bars:{ordered[-1].provider_id}:{len(ordered)}" if ordered else "bars:none",
        )
        assessment = SignalAssessment(
            instrument_id=instrument_id,
            signal_family=SignalFamily.TECHNICAL,
            model_id=STRATEGY_MODEL_ID,
            model_version=STRATEGY_MODEL_VERSION,
            direction=direction,
            status=status,
            score=score,
            calibrated_probability=None,
            calibration_status=CalibrationStatus.UNVERIFIED,
            observed_at=observed_at,
            available_at=as_of,
            evidence_refs=evidence,
            risk_flags=tuple(no_trade),
        )
        candidate_id = uuid.uuid5(
            _CANDIDATE_NAMESPACE,
            f"{policy.strategy_id}@{policy.version_id}|{instrument_id}|{breakout_end}",
        )
        return BaselineEvaluation(
            strategy_id=policy.strategy_id,
            version_id=policy.version_id,
            candidate_id=candidate_id,
            assessment=assessment,
            signal=signal,
            components=components,
            reason_codes=tuple(dict.fromkeys(reasons)),
            no_trade_reasons=tuple(dict.fromkeys(no_trade)),
        )


__all__ = [
    "BASE_SELECTION_LARGEST",
    "STRATEGY_ENGINE_VERSION",
    "STRATEGY_MODEL_ID",
    "STRATEGY_MODEL_VERSION",
    "TECHNICAL_BREAKOUT_KEY",
    "BaselineComponents",
    "BaselineEvaluation",
    "BaselineSignal",
    "BaselineStrategyError",
    "BaselineTrendBreakoutStrategy",
    "BaselineTrendBreakoutPolicy",
]
