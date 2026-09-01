"""Universe / liquidity / price / spread eligibility gate (mission section, B3).

The frozen ``baseline-trend-breakout-v1`` universe rules are load-bearing for
whether a new entry is allowed:

- S&P 500 membership (Point-in-Time for a historical decision);
- 20-day Average Dollar Volume >= USD 50,000,000;
- Price >= USD 10;
- Spread <= 10 bps (0.10%).

This module does NOT build a competing universe subsystem. It is a fail-closed
GATE over *authoritative facts* supplied by the canonical sources
(``trading_market_data_core`` S&P membership snapshots and the feature engine's
``adv_dollar`` / ``spread``). The policy boundary must be safe even if called
directly by the runtime, so every fact is nullable and any UNKNOWN fact — or a
fact that fails an owner threshold — blocks the new entry. Nothing is assumed to
have been filtered upstream.

The authoritative GICS sector travels with the eligibility result, because the
downstream sector caps (max 3 positions and 0.75% planned risk per GICS sector)
must use the authoritative sector, never a free-text candidate field.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

UNIVERSE_ELIGIBILITY_VERSION = "universe-eligibility-1.0.0"


class UniverseEligibilityError(Exception):
    """Fail-closed eligibility failure (bad policy/inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class UniverseEligibilityPolicy:
    """Owner-frozen universe thresholds — configuration input, no defaults."""

    policy_version: str
    min_adv20_usd: Decimal  # universe.min_liquidity (USD 50,000,000)
    min_price: Decimal  # universe.min_price (USD 10)
    max_spread_bps: Decimal  # universe.max_spread (10 bps / 0.10%)
    require_point_in_time_membership: bool  # universe.definition (PIT for history)

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise UniverseEligibilityError("policy_version is mandatory")
        if self.min_adv20_usd <= 0 or self.min_price <= 0 or self.max_spread_bps <= 0:
            raise UniverseEligibilityError("universe thresholds must be positive")


@dataclass(frozen=True, slots=True)
class UniverseFacts:
    """Authoritative eligibility facts for one candidate (frozen fixtures).

    Each fact is nullable: ``None`` means "not authoritatively known", which is
    never treated as passing. ``membership_is_point_in_time`` records whether the
    membership fact is point-in-time-correct for THIS decision (current
    membership is PIT-correct for a live decision; a backtest must use a PIT
    snapshot).
    """

    symbol: str
    is_sp500_member: bool | None
    membership_is_point_in_time: bool
    gics_sector: str | None
    adv20_usd: Decimal | None
    last_price: Decimal | None
    spread_bps: Decimal | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise UniverseEligibilityError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class UniverseEligibility:
    """Result: eligible or not, the authoritative sector, and reason codes."""

    eligible: bool
    gics_sector: str | None
    reason_codes: tuple[str, ...]
    version: str = UNIVERSE_ELIGIBILITY_VERSION


def evaluate_universe_eligibility(
    facts: UniverseFacts,
    policy: UniverseEligibilityPolicy,
    *,
    as_of: datetime,
) -> UniverseEligibility:
    """Fail-closed evaluation of the frozen universe rules over authoritative facts."""
    if as_of.tzinfo is None:
        raise UniverseEligibilityError("as_of must be timezone-aware")
    if facts.observed_at > as_of:
        raise UniverseEligibilityError("lookahead: universe facts are newer than as_of")

    reasons: list[str] = []

    # -- S&P 500 membership (PIT for a historical decision) -------------------
    if facts.is_sp500_member is None:
        reasons.append("SP500_MEMBERSHIP_UNKNOWN")
    elif not facts.is_sp500_member:
        reasons.append("NOT_SP500_MEMBER")
    elif policy.require_point_in_time_membership and not facts.membership_is_point_in_time:
        reasons.append("MEMBERSHIP_NOT_POINT_IN_TIME")

    # -- authoritative GICS sector (needed for the sector caps) --------------
    sector = facts.gics_sector
    if sector is None or not sector.strip():
        reasons.append("GICS_SECTOR_UNKNOWN")
        sector = None

    # -- liquidity: 20-day average dollar volume -----------------------------
    if facts.adv20_usd is None:
        reasons.append("ADV20_UNKNOWN")
    elif facts.adv20_usd < policy.min_adv20_usd:
        reasons.append("ADV20_BELOW_MIN")

    # -- minimum price -------------------------------------------------------
    if facts.last_price is None:
        reasons.append("PRICE_UNKNOWN")
    elif facts.last_price < policy.min_price:
        reasons.append("PRICE_BELOW_MIN")

    # -- spread --------------------------------------------------------------
    if facts.spread_bps is None:
        reasons.append("SPREAD_UNKNOWN")
    elif facts.spread_bps > policy.max_spread_bps:
        reasons.append("SPREAD_TOO_WIDE")

    return UniverseEligibility(
        eligible=not reasons,
        gics_sector=sector if not reasons else None,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


__all__ = [
    "UNIVERSE_ELIGIBILITY_VERSION",
    "UniverseEligibility",
    "UniverseEligibilityError",
    "UniverseEligibilityPolicy",
    "UniverseFacts",
    "evaluate_universe_eligibility",
]
