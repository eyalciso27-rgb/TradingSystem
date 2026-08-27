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
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trading_contracts.enums import RiskDecisionOutcome
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

from trading_application.risk_engine import RiskResult

RUNTIME_READINESS_VERSION = "trading-readiness-runtime-1.0.0"

#: A quote older than this cannot describe the market you are about to trade.
DEFAULT_MAX_QUOTE_AGE_SECONDS = 15.0

#: A health observation older than this cannot prove the session is up NOW.
DEFAULT_MAX_SESSION_AGE_SECONDS = 60.0

#: Clock tolerance for a quote timestamped slightly ahead of us. Host and feed
#: clocks disagree by milliseconds in practice; anything beyond this is not
#: skew, it is a wrong timestamp, and pricing a decision on it is not safe.
DEFAULT_MAX_QUOTE_FUTURE_SKEW_SECONDS = 2.0


class RuntimeReadinessError(Exception):
    """Fail-closed runtime readiness failure."""


# -- runtime observations (what the adapters actually report) -----------------


@dataclass(frozen=True, slots=True)
class BrokerSessionState:
    """Observed broker session. Every field is an observation, not a claim."""

    connected: bool
    #: The canonical account identity the port exposes. ``AccountSnapshot`` has
    #: no ``account_id`` -- the IBKR adapter deliberately keeps the raw account
    #: number inside itself -- so none is invented here.
    account_alias: str | None
    #: Derived from the canonical ``AccountSnapshot.environment``: the port's
    #: own statement of which environment this session is in.
    session_mode: TradingMode | None
    #: When the broker's own health check last succeeded. A stale observation
    #: cannot prove CURRENT connectivity, so this is checked for age rather
    #: than mere presence.
    observed_at: datetime | None = None
    #: The canonical BrokerPort.health_check() verdict.
    health_ok: bool = True
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ContractState:
    """Resolved instrument, mirroring the canonical ``BrokerInstrument``.

    That contract carries ``broker_instrument_ref``, ``symbol``, ``exchange``
    and ``currency``, and nothing else: no ``con_id``, no ``security_type``, no
    ``tradable``. Reading fields the port does not define would either block a
    healthy order or, worse, let an unknown safety fact default to True.

    ``broker_instrument_ref`` is OPAQUE. It is carried as the broker identity
    and is never parsed to recover a broker-specific id.
    """

    resolved: bool
    #: The opaque canonical identity. Never parsed, never decoded.
    broker_instrument_ref: str | None
    symbol: str | None
    exchange: str | None
    currency: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TradabilityState:
    """Whether the security is CURRENTLY tradable -- proven, not asserted.

    ``BrokerInstrument`` has no tradability flag, so this is established through
    the broker's own order-validation surface (``preview_order``, a what-if that
    places nothing) rather than an invented attribute. When the broker offers no
    such surface the answer is UNKNOWN, and UNKNOWN blocks: an unproven safety
    fact is never optimism.
    """

    proven_tradable: bool
    #: False when no canonical surface was available to prove it either way.
    evidence_available: bool = True
    detail: str = ""


class MarketDataState(StrEnum):
    """What the market-data feed is ACTUALLY serving, as the broker reports it.

    IBKR answers a quote request with the data it is entitled to serve, which is
    not always the data that was asked for: an account without a realtime
    subscription silently receives delayed ticks. Collapsing that into "we got a
    quote" is how a delayed price becomes a realtime decision, so the served
    state is modelled explicitly and carried all the way to the gate.

    ``UNKNOWN`` is not a mild case of ``REALTIME``. It means the feed never said
    what it was serving, and it blocks.
    """

    REALTIME = "REALTIME"
    DELAYED = "DELAYED"
    FROZEN = "FROZEN"
    DELAYED_FROZEN = "DELAYED_FROZEN"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


#: Which feed states may support a trading decision, per mode. Paper tolerates
#: delayed data because its purpose is exercising the path; Live does not, and
#: the difference is deliberate rather than incidental. Neither mode ever
#: accepts UNKNOWN or UNAVAILABLE — a policy that admits "we do not know" is not
#: a policy. Override per strategy through ``MarketDataRules``.
DEFAULT_MARKET_DATA_POLICY: Mapping[TradingMode, tuple[MarketDataState, ...]] = {
    TradingMode.PAPER: (MarketDataState.REALTIME, MarketDataState.DELAYED),
    TradingMode.LIVE: (MarketDataState.REALTIME,),
}


@dataclass(frozen=True, slots=True)
class QuoteState:
    """A live quote observation. Absence and staleness are both failures."""

    bid: Decimal | None
    ask: Decimal | None
    observed_at: datetime | None
    #: Sizes are OPTIONAL on the quote surface, but a configured size rule may
    #: never be silently ignored: an unknown size fails the rule closed.
    bid_size: int | None = None
    ask_size: int | None = None
    #: What the feed said it was serving. Defaults to UNKNOWN, which blocks:
    #: a quote whose provenance was never stated cannot be trusted to price a
    #: decision, and a caller that forgets to set this must not get a pass.
    market_data_state: MarketDataState = MarketDataState.UNKNOWN
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
    #: Feed states this strategy accepts. ``None`` means "use the mode default"
    #: (see DEFAULT_MARKET_DATA_POLICY); an EMPTY tuple accepts nothing and
    #: blocks, which is the correct reading of a policy that permits no state.
    allowed_market_data_states: tuple[MarketDataState, ...] | None = None
    #: How far a quote observation may sit in the future before it is refused.
    #: Small, non-zero: clocks drift, but a quote from the future is a bug.
    max_quote_future_skew_seconds: float = DEFAULT_MAX_QUOTE_FUTURE_SKEW_SECONDS
    #: How old the broker health observation may be. Sourced from the canonical
    #: BrokerPort.health_check(), not invented per call site.
    max_session_age_seconds: float = DEFAULT_MAX_SESSION_AGE_SECONDS


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
    tradability: TradabilityState
    quote: QuoteState
    rules: MarketDataRules
    protection: ProtectionState
    #: The REAL risk outcome, produced by RiskEngine.evaluate(...). Typed,
    #: because ``RiskResult`` lives in this very package: reading it through
    #: ``getattr`` would be the same mistake as reading a broker field that does
    #: not exist, with the same failure mode of a default nobody chose.
    risk_result: RiskResult | None
    audit: AuditSink | None
    evaluated_at: datetime
    universe_status: UniverseListStatus | None = None
    gics_sector: str | None = None
    pit_membership_available: bool = False
    expected_account_alias: str | None = None
    expected_symbol: str | None = None
    allowed_currencies: tuple[str, ...] = ("USD",)
    #: The IBKR adapter routes via SMART (paper_adapter sets contract.exchange
    #: to SMART), so that is the canonical routing rather than an invented
    #: allow-list. An empty tuple disables the check.
    allowed_exchanges: tuple[str, ...] = ("SMART",)


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
    if not broker.health_ok:
        return GateOutcome(
            "ibkr_connectivity_account_safety", False,
            broker.detail or "broker health_check reports unhealthy",
        )
    age = (inputs.evaluated_at - broker.observed_at).total_seconds()
    if age < 0:
        return GateOutcome(
            "ibkr_connectivity_account_safety", False, "session observation is in the future"
        )
    if age > inputs.rules.max_session_age_seconds:
        return GateOutcome(
            "ibkr_connectivity_account_safety", False,
            f"session observation is stale: {age:.1f}s >"
            f" {inputs.rules.max_session_age_seconds}s; a stale observation cannot"
            " prove current connectivity",
        )
    if not broker.account_alias:
        return GateOutcome(
            "ibkr_connectivity_account_safety", False,
            "no account alias reported by the session",
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
    if broker.session_mode is None or not broker.account_alias:
        return GateOutcome(name, False, "session environment or account alias is unknown")
    if broker.session_mode is not inputs.mode:
        # The port's own environment disagreeing with the requested mode is a
        # wiring error, and the port's word wins.
        return GateOutcome(
            name, False,
            f"broker reports environment {broker.session_mode.value} but readiness"
            f" was requested for {inputs.mode.value}",
        )
    return GateOutcome(
        name, True,
        f"canonical account environment confirms {inputs.mode.value}"
        f" (alias {broker.account_alias})",
    )


def _gate_contract_identity(inputs: RuntimeInputs) -> GateOutcome:
    contract = inputs.contract
    name = "contract_identity_valid"
    if not contract.resolved or not contract.broker_instrument_ref:
        return GateOutcome(name, False, contract.detail or "contract did not resolve")
    if inputs.expected_symbol and contract.symbol != inputs.expected_symbol:
        return GateOutcome(
            name, False,
            f"resolved symbol {contract.symbol!r} does not match the expected"
            f" {inputs.expected_symbol!r}",
        )
    # Security TYPE is not on the canonical contract. The IBKR adapter is its
    # authority and resolves equities only, so that invariant stays where it is
    # enforced instead of being re-tested here against a field the port lacks.
    if contract.currency not in inputs.allowed_currencies:
        return GateOutcome(name, False, f"currency {contract.currency!r} is not allowed")
    if inputs.allowed_exchanges and contract.exchange not in inputs.allowed_exchanges:
        return GateOutcome(
            name, False,
            f"routing {contract.exchange!r} is outside the canonical"
            f" {list(inputs.allowed_exchanges)}",
        )
    return GateOutcome(
        name, True,
        f"{contract.symbol} resolved to broker ref {contract.broker_instrument_ref}"
        f" on {contract.exchange}",
    )


def _gate_tradable(inputs: RuntimeInputs) -> GateOutcome:
    name = "security_currently_tradable"
    if not inputs.contract.resolved:
        return GateOutcome(name, False, "contract unresolved, so tradability is unknown")
    tradability = inputs.tradability
    if not tradability.evidence_available:
        return GateOutcome(
            name, False,
            tradability.detail
            or "no canonical surface proved current tradability, and UNKNOWN"
            " never becomes optimism",
        )
    if not tradability.proven_tradable:
        return GateOutcome(
            name, False, tradability.detail or "the broker refused to validate this order"
        )
    return GateOutcome(name, True, tradability.detail or "validated by the broker")


def _gate_market_data(inputs: RuntimeInputs) -> GateOutcome:
    """Runtime quote state. A past qualification is not a current quote."""
    quote = inputs.quote
    rules = inputs.rules
    name = "market_data_usable"
    allowed = (
        rules.allowed_market_data_states
        if rules.allowed_market_data_states is not None
        else DEFAULT_MARKET_DATA_POLICY[inputs.mode]
    )
    if quote.market_data_state not in allowed:
        return GateOutcome(
            name, False,
            f"market data is {quote.market_data_state.value}; {inputs.mode.value}"
            f" accepts {[state.value for state in allowed]}",
        )
    if quote.observed_at is None:
        return GateOutcome(name, False, "no quote observation time")
    age = (inputs.evaluated_at - quote.observed_at).total_seconds()
    if age < -rules.max_quote_future_skew_seconds:
        return GateOutcome(
            name, False,
            f"quote is timestamped {-age:.1f}s in the future, beyond the"
            f" {rules.max_quote_future_skew_seconds}s clock tolerance",
        )
    age = max(age, 0.0)
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
    return GateOutcome(
        name, True,
        f"bid={quote.bid} ask={quote.ask} age={age:.1f}s"
        f" feed={quote.market_data_state.value}",
    )


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
    if rules.min_bid_size is not None:
        # A configured safety rule that cannot be evaluated must BLOCK, never
        # pass silently. Previously this field existed and was never read.
        if quote.bid_size is None:
            return GateOutcome(
                name, False,
                "a minimum bid size is configured but the quote carries no size;"
                " refusing to ignore a configured rule",
            )
        if quote.bid_size < rules.min_bid_size:
            return GateOutcome(
                name, False,
                f"bid size {quote.bid_size} is below the configured minimum"
                f" {rules.min_bid_size}",
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


#: Risk verdicts that permit an order. REDUCE still permits one, at the
#: quantity the engine approved rather than the quantity requested.
_PERMITTING_RISK_OUTCOMES = (
    RiskDecisionOutcome.APPROVE,
    RiskDecisionOutcome.REDUCE,
)


def _risk_outcome(
    inputs: RuntimeInputs,
) -> tuple[RiskDecisionOutcome | None, Decimal | None]:
    """Read the REAL RiskResult. One risk implementation, read directly."""
    result = inputs.risk_result
    if result is None:
        return None, None
    return result.outcome, result.approved_quantity


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
    if outcome not in _PERMITTING_RISK_OUTCOMES:
        reasons = inputs.risk_result.reason_codes if inputs.risk_result else ()
        return GateOutcome(
            name, False, f"risk outcome {outcome.value}: {list(reasons)}"
        )
    return GateOutcome(name, True, f"risk outcome {outcome.value}")


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
        "broker_instrument_ref": str(
            inputs.contract.broker_instrument_ref or "unresolved"
        ),
        "risk_outcome": outcome.value if outcome else "none",
        "approved_quantity": str(quantity if quantity is not None else "none"),
        "quote_observed_at": (
            inputs.quote.observed_at.isoformat() if inputs.quote.observed_at else "none"
        ),
        "market_data_state": inputs.quote.market_data_state.value,
    }

    return TradingReadinessRuntimeReport(
        mode=inputs.mode,
        strategy_id=inputs.requirements.strategy_id,
        readiness=contract_report.readiness,
        security_id=inputs.contract.broker_instrument_ref,
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
    "DEFAULT_MARKET_DATA_POLICY",
    "DEFAULT_MAX_QUOTE_AGE_SECONDS",
    "DEFAULT_MAX_QUOTE_FUTURE_SKEW_SECONDS",
    "DEFAULT_MAX_SESSION_AGE_SECONDS",
    "RUNTIME_READINESS_VERSION",
    "AuditSink",
    "BrokerSessionState",
    "ContractState",
    # The element type of TradingReadinessRuntimeReport.gates -- part of the
    # report's public shape, so a caller reading gates should not have to reach
    # for an unexported name.
    "GateOutcome",
    "MarketDataRules",
    "MarketDataState",
    "ProtectionState",
    "TradabilityState",
    "QuoteState",
    "RuntimeInputs",
    "RuntimeReadinessError",
    "TradingReadinessRuntimeReport",
    "evaluate_runtime_readiness",
    "persist_readiness_decision",
    "quote_age_seconds",
    "within_freshness",
]
