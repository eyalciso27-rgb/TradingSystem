"""Runtime trading readiness: the eleven gates computed from real state.

PR #90 defined the contract; this computes it. The distinction that matters:
**no gate here is satisfied by a caller passing ``True``**. Each one is derived
from a runtime observation object, and anything unknown, stale or absent is
BLOCKED. ``evaluate_runtime_readiness`` never accepts a boolean gate map.

Existing components are reused rather than re-implemented:

* ``RiskEngine`` decides ``risk_limits`` AND ``position_sizing`` — its
  ``RiskResult`` already carries the approved quantity, so sizing is read from
  the same authority that vetoes, not computed a second time here;
* ``HaltRecord`` supplies halt/emergency state to that engine;
* broker connectivity, account mode, contract resolution, quotes, capabilities
  and audit persistence arrive through Protocols, so this module drives the real
  adapters without importing them and without a parallel safety implementation;
* the final verdict is delegated to ``evaluate_trading_readiness`` in
  market-data-core. There is exactly one readiness implementation.

**Readiness is not authorization.** ``PAPER_READY`` means the technical safety
prerequisites hold; it does not authorize Paper Auto, and ``LIVE_READY`` does
not authorize Live. The project's standing prohibitions are separate and
unchanged by this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from trading_market_data_core.trading_readiness import (
    MANDATORY_TRADING_GATES,
    GateOutcome,
    GicsMode,
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    TradingReadinessReport,
    UniverseListStatus,
    evaluate_trading_readiness,
)

RUNTIME_READINESS_VERSION = "trading-readiness-runtime-1.0.0"

#: A quote older than this cannot describe the market you are about to trade.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 15.0


class RuntimeReadinessError(Exception):
    """Fail-closed runtime readiness failure."""


# -- runtime observations (what the adapters actually report) -----------------


@dataclass(frozen=True, slots=True)
class BrokerSessionState:
    """Observed broker session. Every field is an observation, not a claim."""

    connected: bool
    account_id: str | None
    account_alias: str | None
    #: The mode the SESSION is really in, as reported by the broker.
    session_mode: TradingMode | None
    observed_at: datetime | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ContractState:
    """Resolved instrument, as the broker actually resolved it."""

    resolved: bool
    con_id: int | None
    symbol: str | None
    security_type: str | None
    exchange: str | None
    currency: str | None
    tradable: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class QuoteState:
    """A live quote observation. Absence and staleness are both failures."""

    bid: Decimal | None
    ask: Decimal | None
    observed_at: datetime | None
    detail: str = ""

    @property
    def midpoint(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class MarketDataRules:
    """Strategy/config-driven thresholds. No arbitrary hardcoded values."""

    max_spread_fraction: Decimal | None = None
    min_price: Decimal | None = None
    min_bid_size: int | None = None
    allow_crossed_or_locked: bool = False
    max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS


@dataclass(frozen=True, slots=True)
class ProtectionState:
    """Whether the intended order can actually receive its protection."""

    #: The broker capability for the required protective order type.
    protective_order_supported: bool
    #: The plan genuinely carries a protective stop.
    plan_has_protective_stop: bool
    #: The strategy/order class requires protection at all.
    protection_required: bool = True
    detail: str = ""


@runtime_checkable
class AuditSink(Protocol):
    """Where the decision record is persisted. Unavailable audit blocks."""

    def can_persist(self) -> bool: ...

    def persist(self, record: Mapping[str, object]) -> str: ...


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    """Everything the evaluator observes. No booleans-as-verdicts."""

    mode: TradingMode
    requirements: StrategyDataRequirements
    broker: BrokerSessionState
    contract: ContractState
    quote: QuoteState
    rules: MarketDataRules
    protection: ProtectionState
    #: The REAL risk outcome, produced by RiskEngine.evaluate(...).
    risk_result: object | None
    audit: AuditSink | None
    evaluated_at: datetime
    universe_status: UniverseListStatus | None = None
    gics_sector: str | None = None
    pit_membership_available: bool = False
    expected_account_prefix: str = "DU"
    expected_account_alias: str | None = None
    expected_symbol: str | None = None
    allowed_security_types: tuple[str, ...] = ("STK",)
    allowed_currencies: tuple[str, ...] = ("USD",)


@dataclass(frozen=True, slots=True)
class TradingReadinessRuntimeReport:
    """The one authoritative runtime readiness report."""

    mode: TradingMode
    strategy_id: str
    readiness: TradingReadiness
    security_id: str | None
    symbol: str | None
    gates: tuple[GateOutcome, ...]
    blockers: tuple[str, ...]
    universe_status: UniverseListStatus | None
    optional_context: Mapping[str, str]
    evaluated_at: datetime
    runtime_refs: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def may_trade(self) -> bool:
        return self.readiness is not TradingReadiness.BLOCKED

    def to_dict(self) -> dict[str, object]:
        return {
            "version": RUNTIME_READINESS_VERSION,
            "mode": self.mode.value,
            "strategy_id": self.strategy_id,
            "security_id": self.security_id,
            "symbol": self.symbol,
            "trading_readiness": self.readiness.value,
            "gates": [gate.to_dict() for gate in self.gates],
            "blockers": list(self.blockers),
            "universe_status": (
                self.universe_status.value if self.universe_status else None
            ),
            "optional_context": dict(sorted(self.optional_context.items())),
            "evaluated_at": self.evaluated_at.isoformat(),
            "runtime_refs": dict(sorted(self.runtime_refs.items())),
            "notes": list(self.notes),
            "authorization_note": (
                "Readiness means the technical safety prerequisites are"
                " satisfied. It does NOT authorize Paper Auto or Live trading;"
                " those remain governed by the project's separate prohibitions."
            ),
        }


# -- the eleven gate evaluators ------------------------------------------------


def _gate_ibkr_connectivity(inputs: RuntimeInputs) -> GateOutcome:
    broker = inputs.broker
    if not broker.connected:
        return GateOutcome("ibkr_connectivity_account_safety", False, "broker not connected")
    if broker.observed_at is None:
        return GateOutcome(
            "ibkr_connectivity_account_safety", False, "session state has no observation time"
        )
    if not broker.account_id:
        return GateOutcome(
            "ibkr_connectivity_account_safety", False, "no account id reported by the session"
        )
    if inputs.expected_account_alias and broker.account_alias != inputs.expected_account_alias:
        return GateOutcome(
            "ibkr_connectivity_account_safety",
            False,
            f"account alias {broker.account_alias!r} is not the expected"
            f" {inputs.expected_account_alias!r}",
        )
    return GateOutcome(
        "ibkr_connectivity_account_safety", True, f"connected as {broker.account_alias}"
    )


def _gate_account_mode(inputs: RuntimeInputs) -> GateOutcome:
    """Paper must PROVE paper; Live must prove live. Unknown never passes."""
    broker = inputs.broker
    name = "account_mode_safety"
    if broker.session_mode is None or not broker.account_id:
        return GateOutcome(name, False, "session mode or account id is unknown")
    if broker.session_mode is not inputs.mode:
        return GateOutcome(
            name, False,
            f"session is {broker.session_mode.value} but readiness was requested for"
            f" {inputs.mode.value}",
        )
    if inputs.mode is TradingMode.PAPER:
        if not broker.account_id.startswith(inputs.expected_account_prefix):
            return GateOutcome(
                name, False,
                f"paper mode requires an account starting {inputs.expected_account_prefix!r}",
            )
        return GateOutcome(name, True, "paper account confirmed")
    # LIVE: a paper-prefixed account in live mode is a wiring error, not live.
    if broker.account_id.startswith(inputs.expected_account_prefix):
        return GateOutcome(
            name, False, "live mode reported a paper-prefixed account; refusing"
        )
    return GateOutcome(name, True, "live account confirmed")


def _gate_contract_identity(inputs: RuntimeInputs) -> GateOutcome:
    contract = inputs.contract
    name = "contract_identity_valid"
    if not contract.resolved or contract.con_id is None:
        return GateOutcome(name, False, contract.detail or "contract did not resolve")
    if inputs.expected_symbol and contract.symbol != inputs.expected_symbol:
        return GateOutcome(
            name, False,
            f"resolved symbol {contract.symbol!r} does not match the expected"
            f" {inputs.expected_symbol!r}",
        )
    if contract.security_type not in inputs.allowed_security_types:
        return GateOutcome(
            name, False, f"security type {contract.security_type!r} is not allowed"
        )
    if contract.currency not in inputs.allowed_currencies:
        return GateOutcome(name, False, f"currency {contract.currency!r} is not allowed")
    return GateOutcome(name, True, f"conId={contract.con_id} {contract.symbol}")


def _gate_tradable(inputs: RuntimeInputs) -> GateOutcome:
    name = "security_currently_tradable"
    if not inputs.contract.resolved:
        return GateOutcome(name, False, "contract unresolved, so tradability is unknown")
    if not inputs.contract.tradable:
        return GateOutcome(
            name, False, inputs.contract.detail or "broker reports the security as not tradable"
        )
    return GateOutcome(name, True, "broker reports the security as tradable")


def _gate_market_data(inputs: RuntimeInputs) -> GateOutcome:
    """Runtime quote state. A past qualification is not a current quote."""
    quote = inputs.quote
    rules = inputs.rules
    name = "market_data_usable"
    if quote.observed_at is None:
        return GateOutcome(name, False, "no quote observation time")
    age = (inputs.evaluated_at - quote.observed_at).total_seconds()
    if age < 0:
        return GateOutcome(name, False, "quote timestamp is in the future")
    if age > rules.max_quote_age_seconds:
        return GateOutcome(
            name, False, f"quote is stale: {age:.1f}s > {rules.max_quote_age_seconds}s"
        )
    if quote.bid is None or quote.ask is None:
        return GateOutcome(name, False, "quote is missing a bid or an ask")
    if quote.bid <= 0 or quote.ask <= 0:
        return GateOutcome(name, False, "quote has a non-positive bid or ask")
    if quote.ask < quote.bid and not rules.allow_crossed_or_locked:
        return GateOutcome(name, False, "market is crossed and crossed markets are prohibited")
    if quote.ask == quote.bid and not rules.allow_crossed_or_locked:
        return GateOutcome(name, False, "market is locked and locked markets are prohibited")
    if quote.midpoint is None or quote.midpoint <= 0:
        return GateOutcome(name, False, "midpoint is unusable")
    return GateOutcome(name, True, f"bid={quote.bid} ask={quote.ask} age={age:.1f}s")


def _gate_price_spread_liquidity(inputs: RuntimeInputs) -> GateOutcome:
    """Strategy/config-driven rules only. Unset rules are not invented."""
    quote = inputs.quote
    rules = inputs.rules
    name = "price_spread_liquidity_rules"
    midpoint = quote.midpoint
    spread = quote.spread
    if midpoint is None or spread is None:
        return GateOutcome(name, False, "cannot evaluate rules without a usable quote")
    if rules.min_price is not None and midpoint < rules.min_price:
        return GateOutcome(
            name, False, f"midpoint {midpoint} is below the configured minimum {rules.min_price}"
        )
    if rules.max_spread_fraction is not None:
        fraction = spread / midpoint if midpoint else None
        if fraction is None or fraction > rules.max_spread_fraction:
            return GateOutcome(
                name, False,
                f"spread fraction {fraction} exceeds the configured maximum"
                f" {rules.max_spread_fraction}",
            )
    return GateOutcome(name, True, "configured price/spread/liquidity rules satisfied")


def _risk_outcome(inputs: RuntimeInputs) -> tuple[str | None, Decimal | None]:
    """Read the REAL RiskResult without importing a second risk implementation."""
    result = inputs.risk_result
    if result is None:
        return None, None
    outcome = getattr(getattr(result, "outcome", None), "value", None)
    quantity = getattr(result, "approved_quantity", None)
    return (str(outcome) if outcome is not None else None), quantity


def _gate_position_sizing(inputs: RuntimeInputs) -> GateOutcome:
    """Sizing comes from the risk engine's approved quantity, not a recompute."""
    name = "position_sizing"
    outcome, quantity = _risk_outcome(inputs)
    if outcome is None:
        return GateOutcome(name, False, "no risk evaluation was supplied")
    if quantity is None:
        return GateOutcome(name, False, "risk evaluation carries no approved quantity")
    if quantity <= 0:
        return GateOutcome(name, False, f"approved quantity is {quantity}")
    return GateOutcome(name, True, f"approved quantity {quantity} (from the risk engine)")


def _gate_risk_limits(inputs: RuntimeInputs) -> GateOutcome:
    """The risk engine has absolute veto; this reads its verdict."""
    name = "risk_limits"
    outcome, _ = _risk_outcome(inputs)
    if outcome is None:
        return GateOutcome(name, False, "no risk evaluation was supplied")
    if outcome not in ("APPROVE", "REDUCE"):
        reasons = getattr(inputs.risk_result, "reason_codes", ())
        return GateOutcome(name, False, f"risk outcome {outcome}: {list(reasons)}")
    return GateOutcome(name, True, f"risk outcome {outcome}")


def _gate_broker_protection(inputs: RuntimeInputs) -> GateOutcome:
    """The broker must actually be able to carry the protection."""
    name = "broker_side_protection"
    protection = inputs.protection
    if not protection.protection_required:
        return GateOutcome(name, True, "no protection required by this order class")
    if not protection.protective_order_supported:
        return GateOutcome(
            name, False,
            protection.detail or "the broker cannot carry the required protective order",
        )
    return GateOutcome(name, True, "broker can carry the required protective order")


def _gate_protective_order(inputs: RuntimeInputs) -> GateOutcome:
    """The PLAN must carry protection, not merely be capable of it."""
    name = "protective_order_requirements"
    protection = inputs.protection
    if not protection.protection_required:
        return GateOutcome(name, True, "no protective order required by this order class")
    if not protection.plan_has_protective_stop:
        return GateOutcome(name, False, "the trade plan carries no protective stop")
    return GateOutcome(name, True, "trade plan carries a protective stop")


def _gate_audit(inputs: RuntimeInputs) -> GateOutcome:
    """Readiness requires that the decision can actually be recorded."""
    name = "audit_trail_decision_logging"
    if inputs.audit is None:
        return GateOutcome(name, False, "no audit sink is configured")
    try:
        available = bool(inputs.audit.can_persist())
    except Exception as error:  # noqa: BLE001 - unknown audit failure => blocked
        return GateOutcome(name, False, f"audit sink raised {type(error).__name__}")
    if not available:
        return GateOutcome(name, False, "audit sink reports it cannot persist")
    return GateOutcome(name, True, "audit sink is available")


_EVALUATORS = {
    "ibkr_connectivity_account_safety": _gate_ibkr_connectivity,
    "account_mode_safety": _gate_account_mode,
    "contract_identity_valid": _gate_contract_identity,
    "security_currently_tradable": _gate_tradable,
    "market_data_usable": _gate_market_data,
    "price_spread_liquidity_rules": _gate_price_spread_liquidity,
    "position_sizing": _gate_position_sizing,
    "risk_limits": _gate_risk_limits,
    "broker_side_protection": _gate_broker_protection,
    "protective_order_requirements": _gate_protective_order,
    "audit_trail_decision_logging": _gate_audit,
}


def evaluate_runtime_readiness(inputs: RuntimeInputs) -> TradingReadinessRuntimeReport:
    """Compute readiness from runtime state. No caller-supplied verdicts.

    Every mandatory gate is derived by its evaluator; the final decision is then
    delegated to the single contract implementation in market-data-core, so
    there is no second readiness algorithm.
    """
    if inputs.evaluated_at.tzinfo is None:
        raise RuntimeReadinessError("evaluated_at must be timezone-aware")
    missing = set(MANDATORY_TRADING_GATES) - set(_EVALUATORS)
    if missing:
        raise RuntimeReadinessError(
            f"no runtime evaluator for mandatory gate(s): {sorted(missing)}"
        )

    outcomes = [_EVALUATORS[name](inputs) for name in MANDATORY_TRADING_GATES]
    gate_results = {outcome.name: outcome.satisfied for outcome in outcomes}
    gate_details = {outcome.name: outcome.detail for outcome in outcomes}

    contract_report: TradingReadinessReport = evaluate_trading_readiness(
        mode=inputs.mode,
        requirements=inputs.requirements,
        gate_results=gate_results,
        gate_details=gate_details,
        universe_status=inputs.universe_status,
        gics_available=inputs.gics_sector is not None,
        pit_membership_available=inputs.pit_membership_available,
    )

    notes = list(contract_report.notes)
    if inputs.requirements.gics_mode is not GicsMode.DISABLED and inputs.gics_sector is None:
        notes.append("sector context is UNKNOWN; this does not block trading")

    outcome, quantity = _risk_outcome(inputs)
    runtime_refs = {
        "broker_account_alias": str(inputs.broker.account_alias or "unknown"),
        "con_id": str(inputs.contract.con_id or "unresolved"),
        "risk_outcome": str(outcome or "none"),
        "approved_quantity": str(quantity if quantity is not None else "none"),
        "quote_observed_at": (
            inputs.quote.observed_at.isoformat() if inputs.quote.observed_at else "none"
        ),
    }

    return TradingReadinessRuntimeReport(
        mode=inputs.mode,
        strategy_id=inputs.requirements.strategy_id,
        readiness=contract_report.readiness,
        security_id=(
            f"ibkr-conid:{inputs.contract.con_id}" if inputs.contract.con_id else None
        ),
        symbol=inputs.contract.symbol,
        gates=tuple(outcomes),
        blockers=contract_report.blockers,
        universe_status=inputs.universe_status,
        optional_context=contract_report.optional_context,
        evaluated_at=inputs.evaluated_at,
        runtime_refs=runtime_refs,
        notes=tuple(notes),
    )


def persist_readiness_decision(
    report: TradingReadinessRuntimeReport, audit: AuditSink
) -> str:
    """Write the decision record. Called after evaluation, never before."""
    return audit.persist(report.to_dict())


def quote_age_seconds(quote: QuoteState, now: datetime) -> float | None:
    if quote.observed_at is None:
        return None
    return (now - quote.observed_at).total_seconds()


def within_freshness(observed_at: datetime | None, now: datetime, max_age: timedelta) -> bool:
    return observed_at is not None and timedelta(0) <= (now - observed_at) <= max_age


__all__ = [
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "RUNTIME_READINESS_VERSION",
    "AuditSink",
    "BrokerSessionState",
    "ContractState",
    "MarketDataRules",
    "ProtectionState",
    "QuoteState",
    "RuntimeInputs",
    "RuntimeReadinessError",
    "TradingReadinessRuntimeReport",
    "evaluate_runtime_readiness",
    "persist_readiness_decision",
    "quote_age_seconds",
    "within_freshness",
]
