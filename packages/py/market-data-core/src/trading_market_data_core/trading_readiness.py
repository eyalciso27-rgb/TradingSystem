"""Trading readiness and backtest data quality — two separate questions.

ADR-0017. Dataset V1 previously held ten capabilities mandatory, so
``asof_gics`` and ``pit_sp500_membership`` — neither obtainable from free
official sources — blocked everything. That conflated two different questions:

* **can we place a safe trade now?** — broker connectivity, contract identity,
  tradability, usable market data, sizing, risk limits, protective orders;
* **how faithful is a historical backtest?** — point-in-time membership, as-of
  classification, delisting and corporate-action completeness.

Historical sector and historical index membership answer the second question.
They are therefore optional context here and can never block trading.

Requirements are **strategy-derived**: a capability is mandatory only when the
selected strategy and mode actually depend on it. A strategy that ignores sector
never needs GICS; a strategy trading all eligible US equities never needs index
membership; an ``SP500_CURRENT`` strategy needs a sufficiently fresh CURRENT
list — which is never substituted by historical PIT membership.

Nothing here relaxes broker safety. Every mandatory gate below stays mandatory
for Paper and Live, and this module grants no authorisation to trade: Paper Auto
and Live remain prohibited by the project rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

TRADING_READINESS_VERSION = "trading-readiness-1.0.0"


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class TradingReadiness(StrEnum):
    """Whether the system may place a trade in a given mode."""

    PAPER_READY = "PAPER_READY"
    LIVE_READY = "LIVE_READY"
    BLOCKED = "BLOCKED"


class BacktestQuality(StrEnum):
    """How faithful a historical run is. Never inferred — always declared."""

    #: Current/reconstructed universe and/or free classification context. Runs,
    #: but survivorship and look-ahead limitations must be reported.
    APPROXIMATE = "APPROXIMATE"
    #: Full point-in-time evidence. Keeps every strict evidence rule.
    PIT_STRICT = "PIT_STRICT"
    #: The requested quality could not be met by the available evidence.
    INCOMPLETE = "INCOMPLETE"


class GicsMode(StrEnum):
    """How sector classification may be used. Default is DISABLED."""

    DISABLED = "DISABLED"
    #: Free/public current classification. Non-authoritative, not point-in-time.
    FREE_RECONSTRUCTED = "FREE_RECONSTRUCTED"
    #: A licensed company-level historical series. Not required anywhere.
    LICENSED = "LICENSED"


class UniverseType(StrEnum):
    ALL_ELIGIBLE_US_EQUITIES = "ALL_ELIGIBLE_US_EQUITIES"
    SP500_CURRENT = "SP500_CURRENT"


class UniverseListStatus(StrEnum):
    """Freshness of the CURRENT constituent list. Distinct from PIT history."""

    CURRENT_LIST_VALID = "CURRENT_LIST_VALID"
    CURRENT_LIST_STALE = "CURRENT_LIST_STALE"
    CURRENT_LIST_UNAVAILABLE = "CURRENT_LIST_UNAVAILABLE"


class OptionalCapabilityState(StrEnum):
    """How an OPTIONAL capability is reported. Never a trading blocker."""

    OPTIONAL_DISABLED = "OPTIONAL_DISABLED"
    OPTIONAL_UNAVAILABLE = "OPTIONAL_UNAVAILABLE"
    OPTIONAL_AVAILABLE = "OPTIONAL_AVAILABLE"


#: Gates genuinely required to place a safe trade. Mandatory in BOTH modes.
#: Historical sector and historical index membership are deliberately absent:
#: neither affects whether a trade placed now is safe.
MANDATORY_TRADING_GATES: tuple[str, ...] = (
    "ibkr_connectivity_account_safety",
    "account_mode_safety",
    "contract_identity_valid",
    "security_currently_tradable",
    "market_data_usable",
    "price_spread_liquidity_rules",
    "position_sizing",
    "risk_limits",
    "broker_side_protection",
    "protective_order_requirements",
    "audit_trail_decision_logging",
)

#: Capabilities that are OPTIONAL for trading. They may still be required by a
#: strict backtest — that is decided per strategy, not globally.
OPTIONAL_FOR_TRADING: tuple[str, ...] = ("asof_gics", "pit_sp500_membership")


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    """How old a CURRENT universe list may be, and whether stale is usable."""

    max_age_days: int
    allow_stale: bool
    #: Extra grace beyond ``max_age_days`` during which a stale list may still
    #: be used when ``allow_stale`` is set.
    stale_grace_days: int = 0

    def __post_init__(self) -> None:
        if self.max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        if self.stale_grace_days < 0:
            raise ValueError("stale_grace_days cannot be negative")


#: Owner-approved defaults: Paper tolerates a graced last-known-good list, Live
#: is stricter. Neither has anything to do with historical PIT membership.
DEFAULT_FRESHNESS: Mapping[TradingMode, FreshnessPolicy] = {
    TradingMode.PAPER: FreshnessPolicy(max_age_days=45, allow_stale=True, stale_grace_days=45),
    TradingMode.LIVE: FreshnessPolicy(max_age_days=35, allow_stale=False),
}


@dataclass(frozen=True, slots=True)
class StrategyDataRequirements:
    """What ONE strategy actually depends on. The source of all gating."""

    strategy_id: str
    universe: UniverseType = UniverseType.ALL_ELIGIBLE_US_EQUITIES
    #: True only when the strategy genuinely reads sector (exposure caps,
    #: diversification rules, sector-conditioned signals).
    uses_sector: bool = False
    gics_mode: GicsMode = GicsMode.DISABLED
    #: The quality a historical run is being asked for.
    requested_backtest_quality: BacktestQuality = BacktestQuality.APPROXIMATE

    @property
    def requires_current_universe(self) -> bool:
        """A CURRENT list is mandatory only for an index-restricted strategy."""
        return self.universe is UniverseType.SP500_CURRENT

    @property
    def requires_gics_for_trading(self) -> bool:
        """Never. Sector is context; it cannot make a trade unsafe."""
        return False

    def requires_pit_membership(self, quality: BacktestQuality) -> bool:
        """PIT history matters only for a strict backtest of an index strategy."""
        return (
            quality is BacktestQuality.PIT_STRICT
            and self.universe is UniverseType.SP500_CURRENT
        )

    def requires_historical_gics(self, quality: BacktestQuality) -> bool:
        """Historical GICS matters only if the strategy actually uses sector."""
        return quality is BacktestQuality.PIT_STRICT and self.uses_sector


@dataclass(frozen=True, slots=True)
class GateOutcome:
    name: str
    satisfied: bool
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "satisfied": self.satisfied, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TradingReadinessReport:
    """The trading decision, with optional context reported but never gating."""

    mode: TradingMode
    readiness: TradingReadiness
    strategy_id: str
    gates: tuple[GateOutcome, ...] = ()
    blockers: tuple[str, ...] = ()
    universe_status: UniverseListStatus | None = None
    optional_context: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def may_trade(self) -> bool:
        return self.readiness is not TradingReadiness.BLOCKED

    def to_dict(self) -> dict[str, object]:
        return {
            "version": TRADING_READINESS_VERSION,
            "mode": self.mode.value,
            "trading_readiness": self.readiness.value,
            "strategy_id": self.strategy_id,
            "gates": [gate.to_dict() for gate in self.gates],
            "blockers": list(self.blockers),
            "universe_status": (
                self.universe_status.value if self.universe_status else None
            ),
            "optional_context": dict(sorted(self.optional_context.items())),
            "notes": list(self.notes),
        }

    def plain_language(self) -> tuple[str, ...]:
        """Human-readable lines that never imply optional data blocks trading."""
        lines = [f"Trading readiness ({self.mode.value}): {self.readiness.value}"]
        if self.universe_status is not None:
            lines.append(f"Current universe list: {self.universe_status.value}")
        for name, state in sorted(self.optional_context.items()):
            lines.append(f"Optional {name}: {state} (does not block trading)")
        lines.extend(f"BLOCKER: {blocker}" for blocker in self.blockers)
        return tuple(lines)


def evaluate_trading_readiness(
    *,
    mode: TradingMode,
    requirements: StrategyDataRequirements,
    gate_results: Mapping[str, bool],
    universe_status: UniverseListStatus | None = None,
    gics_available: bool = False,
    pit_membership_available: bool = False,
    freshness: Mapping[TradingMode, FreshnessPolicy] | None = None,
    gate_details: Mapping[str, str] | None = None,
) -> TradingReadinessReport:
    """Decide trading readiness from MANDATORY gates only.

    ``asof_gics`` and ``pit_sp500_membership`` are reported as optional context
    and can never appear as blockers. A missing mandatory gate blocks; an
    unsatisfied strategy-derived requirement (a stale/absent CURRENT list for an
    index strategy) blocks.
    """
    details = dict(gate_details or {})
    gates: list[GateOutcome] = []
    blockers: list[str] = []
    notes: list[str] = []

    for name in MANDATORY_TRADING_GATES:
        satisfied = bool(gate_results.get(name, False))
        gates.append(GateOutcome(name=name, satisfied=satisfied, detail=details.get(name, "")))
        if not satisfied:
            blockers.append(f"mandatory gate not satisfied: {name}")

    # Strategy-derived: a CURRENT list only matters to an index-restricted
    # strategy, and PIT history is never an acceptable substitute for it.
    policy = (freshness or DEFAULT_FRESHNESS).get(mode, DEFAULT_FRESHNESS[mode])
    if requirements.requires_current_universe:
        unavailable = UniverseListStatus.CURRENT_LIST_UNAVAILABLE
        if universe_status is None or universe_status is unavailable:
            blockers.append(
                f"{requirements.universe.value} requires a current constituent list,"
                " and none is available"
            )
        elif universe_status is UniverseListStatus.CURRENT_LIST_STALE:
            if policy.allow_stale:
                notes.append(
                    "current universe list is STALE but the configured policy for"
                    f" {mode.value} allows last-known-good within the grace period"
                )
            else:
                blockers.append(
                    f"current universe list is STALE and {mode.value} policy does not"
                    " allow a stale list"
                )
    elif universe_status is not None:
        notes.append(
            "current universe list is informational for this strategy"
            f" ({requirements.universe.value})"
        )

    optional_context = {
        "asof_gics": _optional_state(
            enabled=requirements.gics_mode is not GicsMode.DISABLED,
            available=gics_available,
        ).value,
        "pit_sp500_membership": _optional_state(
            enabled=True, available=pit_membership_available
        ).value,
    }
    notes.append(
        "historical sector and historical index membership are optional context;"
        " they never block trading (ADR-0017)"
    )
    if requirements.gics_mode is GicsMode.FREE_RECONSTRUCTED:
        notes.append(
            "GICS context is FREE_RECONSTRUCTED: non-authoritative, not"
            " point-in-time, and usable for analytics only"
        )

    readiness = TradingReadiness.BLOCKED
    if not blockers:
        readiness = (
            TradingReadiness.LIVE_READY
            if mode is TradingMode.LIVE
            else TradingReadiness.PAPER_READY
        )
    return TradingReadinessReport(
        mode=mode,
        readiness=readiness,
        strategy_id=requirements.strategy_id,
        gates=tuple(gates),
        blockers=tuple(blockers),
        universe_status=universe_status,
        optional_context=optional_context,
        notes=tuple(notes),
    )


def _optional_state(*, enabled: bool, available: bool) -> OptionalCapabilityState:
    if not enabled:
        return OptionalCapabilityState.OPTIONAL_DISABLED
    return (
        OptionalCapabilityState.OPTIONAL_AVAILABLE
        if available
        else OptionalCapabilityState.OPTIONAL_UNAVAILABLE
    )


@dataclass(frozen=True, slots=True)
class BacktestQualityReport:
    """What a historical run may honestly claim."""

    requested: BacktestQuality
    achieved: BacktestQuality
    strategy_id: str
    limitations: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()

    @property
    def is_strict(self) -> bool:
        return self.achieved is BacktestQuality.PIT_STRICT

    def to_dict(self) -> dict[str, object]:
        return {
            "version": TRADING_READINESS_VERSION,
            "strategy_id": self.strategy_id,
            "requested_quality": self.requested.value,
            "achieved_quality": self.achieved.value,
            "limitations": list(self.limitations),
            "missing_evidence": list(self.missing_evidence),
        }


def evaluate_backtest_quality(
    *,
    requirements: StrategyDataRequirements,
    requested: BacktestQuality | None = None,
    pit_membership_available: bool = False,
    historical_gics_available: bool = False,
    delisted_history_complete: bool = False,
    corporate_actions_complete: bool = False,
) -> BacktestQualityReport:
    """Decide the quality a historical run may claim. Never inflates it.

    ``PIT_STRICT`` demands the evidence its name implies; when that evidence is
    absent the result is ``INCOMPLETE``, not a quietly downgraded run labelled
    strict. ``APPROXIMATE`` always runs but states its limitations.
    """
    wanted = requested or requirements.requested_backtest_quality
    missing: list[str] = []
    limitations: list[str] = []

    if wanted is BacktestQuality.PIT_STRICT:
        if requirements.requires_pit_membership(wanted) and not pit_membership_available:
            missing.append("pit_sp500_membership")
        if requirements.requires_historical_gics(wanted) and not historical_gics_available:
            missing.append("asof_gics")
        if not delisted_history_complete:
            missing.append("delisted_security_history")
        if not corporate_actions_complete:
            missing.append("corporate_actions")
        if missing:
            return BacktestQualityReport(
                requested=wanted,
                achieved=BacktestQuality.INCOMPLETE,
                strategy_id=requirements.strategy_id,
                limitations=(
                    "PIT_STRICT was requested but the evidence it requires is not"
                    " available; the run is NOT downgraded to APPROXIMATE"
                    " automatically, because a strict request deserves a strict"
                    " answer",
                ),
                missing_evidence=tuple(sorted(missing)),
            )
        return BacktestQualityReport(
            requested=wanted, achieved=BacktestQuality.PIT_STRICT,
            strategy_id=requirements.strategy_id,
        )

    # APPROXIMATE: runs, but the limitations are stated rather than implied.
    if not pit_membership_available and requirements.universe is UniverseType.SP500_CURRENT:
        limitations.append(
            "universe is the CURRENT constituent list, not point-in-time:"
            " results carry survivorship bias and index-membership look-ahead"
        )
    if requirements.uses_sector and requirements.gics_mode is GicsMode.FREE_RECONSTRUCTED:
        limitations.append(
            "sector context is FREE_RECONSTRUCTED and reflects CURRENT"
            " classification: sector-conditioned results carry look-ahead"
        )
    if requirements.uses_sector and requirements.gics_mode is GicsMode.DISABLED:
        limitations.append(
            "the strategy reads sector but GICS is DISABLED: sector rules are"
            " inert in this run"
        )
    if not delisted_history_complete:
        limitations.append(
            "delisted-security history is incomplete: names that left the market"
            " may be under-represented"
        )
    if not corporate_actions_complete:
        limitations.append(
            "corporate-action coverage is incomplete: some splits, dividends or"
            " reorganisations may be unadjusted"
        )
    return BacktestQualityReport(
        requested=wanted, achieved=BacktestQuality.APPROXIMATE,
        strategy_id=requirements.strategy_id, limitations=tuple(limitations),
    )


__all__ = [
    "DEFAULT_FRESHNESS",
    "MANDATORY_TRADING_GATES",
    "OPTIONAL_FOR_TRADING",
    "TRADING_READINESS_VERSION",
    "BacktestQuality",
    "BacktestQualityReport",
    "FreshnessPolicy",
    "GateOutcome",
    "GicsMode",
    "OptionalCapabilityState",
    "StrategyDataRequirements",
    "TradingMode",
    "TradingReadiness",
    "TradingReadinessReport",
    "UniverseListStatus",
    "UniverseType",
    "evaluate_backtest_quality",
    "evaluate_trading_readiness",
]
