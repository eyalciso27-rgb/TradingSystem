"""Owner-frozen configuration for ``baseline-trend-breakout-v1`` (MVP).

Every value here is transcribed VERBATIM from the immutable freeze record
``docs/strategies/baseline-trend-breakout-v1.yaml`` (FROZEN 2026-08-13,
fingerprint ``sha256:75f299b8…e84dfed0``). The repository invents no trading
parameter: each numeric below cites the owner parameter it comes from, and
``tests/strategy/test_baseline_strategy_config.py`` binds this module to the
frozen record — the manifest here must equal the frozen fusion manifest and the
fingerprint must equal the frozen fingerprint, so any drift fails closed.

A material change to any of these numbers is a change to the strategy's business
meaning and therefore requires a NEW frozen ``version_id`` (supersede), never an
edit here.
"""

from __future__ import annotations

from decimal import Decimal

from trading_contracts.enums import AssessmentStatus

from trading_application.baseline_strategy import (
    BASE_SELECTION_LARGEST,
    BaselineTrendBreakoutPolicy,
)
from trading_application.strategy_manifest import (
    ManifestEntry,
    ManifestRole,
    StrategyManifest,
)
from trading_application.universe_eligibility import UniverseEligibilityPolicy

#: Identity of the frozen strategy record this configuration realizes.
BASELINE_STRATEGY_ID = "baseline-trend-breakout-v1"
#: The freeze-lifecycle version LABEL (as recorded in the immutable freeze
#: record). Distinct from the canonical semver ``strategy_version`` the decision
#: contracts require — the two are different representations of the SAME frozen
#: strategy, not a second version of it.
BASELINE_VERSION_ID = "v1"
#: The canonical semantic ``strategy_version`` carried through the decision/risk
#: contracts (which validate a semver pattern; the freeze label "v1" does not).
BASELINE_STRATEGY_VERSION = "1.0.0"
#: The owner-approved deterministic fingerprint (invariant across DRAFT->FROZEN).
BASELINE_FROZEN_FINGERPRINT = (
    "sha256:75f299b89e408ef332cd39a46f92d417ef637b3c5a5c1712801774c9e84dfed0"
)
#: Owner ratification of the base-length selection ambiguity (the frozen record
#: says "10-20 trading days" but not which valid length to pick). This does NOT
#: change the frozen record or its fingerprint — it is a separate, dated
#: clarification recorded here and in the execution-params file.
BASELINE_BASE_SELECTION_APPROVAL_REF = "OWNER-EYAL-2026-08-31-BASELINE-BASE-SELECTION-LARGEST"


def baseline_trend_breakout_v1_policy() -> BaselineTrendBreakoutPolicy:
    """The frozen numeric parameters, transcribed with owner provenance.

    Provenance (frozen owner parameter -> field):

    * ``setup.definition``  -> ``sma_trend_period=150`` (Close > SMA150), the
      trend-rising comparison window ``sma_trend_rising_lookback=20``
      ("SMA150(today) > SMA150(20 trading days ago)"), the contracting-base
      duration ``base_min_bars=10`` / ``base_max_bars=20`` ("base duration
      10-20 trading days"), the ``range_window=5`` ("Range(last 5 days) <
      Range(previous 5 days)") and the ATR contraction periods
      ``atr_short_period=10`` / ``atr_long_period=20`` ("ATR10 < ATR20").
    * ``entry.rule``        -> ``entry_atr_multiple=0.25`` ("MaximumEntryPrice =
      BreakoutClose + 0.25 * ATR20").
    * ``exit.stop_rule``    -> ``stop_atr_multiple=0.25`` ("StructuralStop =
      BaseLow - 0.25 * ATR20") and ``max_risk_atr_multiple=2.5`` ("If
      EntryPrice - StructuralStop > 2.5 * ATR20 then SKIP TRADE").
    """
    return BaselineTrendBreakoutPolicy(
        strategy_id=BASELINE_STRATEGY_ID,
        version_id=BASELINE_VERSION_ID,
        frozen_fingerprint=BASELINE_FROZEN_FINGERPRINT,
        sma_trend_period=150,
        sma_trend_rising_lookback=20,
        base_min_bars=10,
        base_max_bars=20,
        range_window=5,
        atr_short_period=10,
        atr_long_period=20,
        entry_atr_multiple=Decimal("0.25"),
        stop_atr_multiple=Decimal("0.25"),
        max_risk_atr_multiple=Decimal("2.5"),
        # Owner-ratified 2026-08-31 (BASELINE_BASE_SELECTION_APPROVAL_REF).
        base_selection_rule=BASE_SELECTION_LARGEST,
    )


def baseline_trend_breakout_v1_manifest() -> StrategyManifest:
    """The owner-approved fusion manifest — a pure technical breakout gate.

    Verbatim from the frozen record's ``fusion_manifest`` (owner-approved
    2026-08-13): a single REQUIRED entry ``technical.breakout`` that must be
    ``TRIGGERED``. No thresholds, lookbacks, weights or calibration conditions
    are added. The version carried here is the canonical semver
    ``strategy_version`` the decision contracts require; the ENTRIES are
    identical to the frozen record's fusion manifest, which
    ``tests/strategy/test_baseline_strategy_config.py`` asserts.
    """
    return StrategyManifest(
        strategy_id=BASELINE_STRATEGY_ID,
        version=BASELINE_STRATEGY_VERSION,
        entries=(
            ManifestEntry(
                assessment_key="technical.breakout",
                role=ManifestRole.REQUIRED,
                required_status=AssessmentStatus.TRIGGERED,
            ),
        ),
    )


def baseline_universe_eligibility_policy() -> UniverseEligibilityPolicy:
    """Owner-frozen universe/liquidity/price/spread thresholds.

    Provenance (baseline-trend-breakout-v1):

    * ``universe.min_liquidity`` "20-day Average Dollar Volume >= USD 50,000,000"
      -> ``min_adv20_usd``.
    * ``universe.min_price`` "Price >= USD 10" -> ``min_price``.
    * ``universe.max_spread`` "Spread <= 10 bps / 0.10%" -> ``max_spread_bps``.
    * ``universe.definition`` "Historical membership MUST be Point-in-Time" ->
      ``require_point_in_time_membership``.
    """
    return UniverseEligibilityPolicy(
        policy_version=BASELINE_STRATEGY_VERSION,
        min_adv20_usd=Decimal("50000000"),
        min_price=Decimal("10"),
        max_spread_bps=Decimal("10"),
        require_point_in_time_membership=True,
    )


__all__ = [
    "BASELINE_BASE_SELECTION_APPROVAL_REF",
    "BASELINE_FROZEN_FINGERPRINT",
    "BASELINE_STRATEGY_ID",
    "BASELINE_STRATEGY_VERSION",
    "BASELINE_VERSION_ID",
    "baseline_trend_breakout_v1_manifest",
    "baseline_trend_breakout_v1_policy",
    "baseline_universe_eligibility_policy",
]
