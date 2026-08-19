"""The canonical execution path for ONE trading decision.

Every piece of this already existed and none of it was joined up. A decision
could be risk-checked, a quote could be fetched, readiness could be computed and
an order could be submitted — but nothing carried one decision through all of
them, so "the safety architecture works" was a statement about components rather
than about a trade.

This is that path, and it is deliberately the only one:

    intent (business only)
      -> halt / emergency authority
      -> universe policy (ADR-0017)
      -> broker contract resolution
      -> live quote
      -> trade plan + protection
      -> RiskEngine (the authority, invoked for THIS decision)
      -> broker what-if (proves tradability, places nothing)
      -> runtime readiness report
      -> authorization boundary (DENY by default)
      -> durable pre-submit audit
      -> ExecutionAgent -> ReadinessGuardedBroker -> broker
      -> durable post-submit audit

Three properties are worth stating outright, because each is a mistake that
would otherwise be easy to make here.

**The caller supplies intent, never verdicts.** ``ExecutionIntent`` carries a
symbol, a side and a strategy. It cannot carry "risk passed" or "the quote is
fresh"; those are observations this service makes from the components that own
them. A safety fact that arrives as a parameter is not a safety fact.

**This service does not submit.** The submit call site stays exactly where it
was — ``ExecutionAgent``, the sole consumer of a single-use authorization — and
the agent's port is the readiness guard. Adding a second submit path here would
have created precisely the competing execution route this package exists to
prevent. What this service adds is everything that must be true *before* the
agent is handed a request.

**Readiness computed here is a report, not the gate.** The guard re-evaluates at
the submission boundary with a freshly fetched quote, so a market that moved
between planning and submitting is caught there. The evaluation below exists so
an operator can see why something was refused; it never substitutes for the
just-in-time check.

Authorization defaults to DENY and there is no production implementation that
says otherwise. ``PAPER_READY`` is a statement about technical prerequisites; it
is not permission to trade, and nothing in this module changes that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trading_application.decision_engine import EngineDecision
from trading_application.emergency import EntryCheckpoint, EntryContext, HaltRecord, match_halt
from trading_application.readiness_guarded_broker import (
    GuardConfig,
    QuoteSource,
    ReadinessBlockedError,
    ReadinessGuardedBroker,
)
from trading_application.risk_engine import (
    AccountState,
    PositionExposure,
    RiskEngine,
    RiskEngineError,
    RiskPolicy,
    RiskResult,
    TradeRiskRequest,
)
from trading_application.trading_readiness_runtime import (
    AuditSink,
    MarketDataRules,
    QuoteState,
    TradingReadinessRuntimeReport,
)
from trading_broker_core.port import (
    BrokerInstrument,
    BrokerPort,
    BrokerPortError,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderType, RiskDecisionOutcome
from trading_market_data_core.sp500_current_universe import ConstituentSnapshot
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    UniverseListStatus,
    UniverseType,
)

from trading_execution_agent.agent import ExecutionAgent, ExecutionOutcome, ExecutionRequest

EXECUTION_SERVICE_VERSION = "execution-service-1.0.0"

LOGGER = logging.getLogger("trading_execution_agent.execution_service")


class ExecutionStage(StrEnum):
    """How far one decision travelled. The refusal point, named."""

    HALTED = "HALTED"
    UNIVERSE_REFUSED = "UNIVERSE_REFUSED"
    CONTRACT_UNRESOLVED = "CONTRACT_UNRESOLVED"
    NO_QUOTE = "NO_QUOTE"
    PLAN_REFUSED = "PLAN_REFUSED"
    RISK_REFUSED = "RISK_REFUSED"
    READINESS_BLOCKED = "READINESS_BLOCKED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    READY_NOT_AUTHORIZED = "READY_NOT_AUTHORIZED"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_REFUSED = "SUBMISSION_REFUSED"


class ExecutionServiceError(Exception):
    """Fail-closed execution failure."""


# -- what the caller may ask for ----------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Business intent for one decision. No safety verdicts, by construction.

    There is deliberately no field here for readiness, risk, tradability, quote
    freshness or audit health. Those are produced by the components that own
    them; a caller that could assert them could bypass every gate by asserting
    them wrongly.
    """

    decision_id: str
    strategy_id: str
    #: The strategy's OWN decision, as the canonical decision engine produced
    #: it. Business intent, not a safety verdict: the risk engine reads its
    #: outcome and vetoes anything that is not an APPROVE, so supplying one
    #: cannot buy a pass -- but fabricating one here would have meant this
    #: service inventing the very decision it is supposed to be executing.
    decision: EngineDecision
    symbol: str
    side: OrderSide
    #: The strategy's own requested size — an UPPER BOUND, never an approval.
    #: The risk engine may reduce it and can never be asked to raise it.
    requested_quantity: Decimal
    correlation_id: str
    #: Stable per decision: the broker's idempotency key. A retry of the same
    #: decision reuses it, which is what makes a retry safe.
    client_order_id: str
    source_id: str = "strategy"

    def __post_init__(self) -> None:
        for name in ("decision_id", "strategy_id", "symbol", "correlation_id"):
            if not str(getattr(self, name)).strip():
                raise ExecutionServiceError(f"{name} is mandatory")
        if self.requested_quantity <= 0:
            raise ExecutionServiceError("requested_quantity must be positive")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Canonical policy for one strategy/account, not per-call opinion."""

    mode: TradingMode
    environment: Environment
    account_alias: str
    requirements: StrategyDataRequirements
    rules: MarketDataRules
    risk_policy: RiskPolicy
    #: Mandatory risk input; the risk engine refuses to invent an equity cap.
    simulation_equity_cap: Decimal
    #: Protective stop distance below (BUY) or above (SELL) the entry. The plan
    #: is refused rather than submitted unprotected when this cannot be applied.
    stop_loss_fraction: Decimal = Decimal("0.02")
    #: How much of an EXISTING position's notional to treat as open risk when
    #: nothing better is known. ``BrokerPort`` does not carry each position's
    #: protective stop, so its true open risk is genuinely unknown here.
    #:
    #: Treating the whole notional as at risk sounds safe and is not: it makes
    #: any account holding a position untradeable, which is the kind of
    #: unusable conservatism that gets weakened later under pressure. The
    #: assumption made instead is the one the system itself enforces on every
    #: new position -- that it is protected by a stop at this distance. Supply
    #: ``portfolio`` to replace the estimate with real per-position risk.
    assumed_open_risk_fraction: Decimal | None = None
    order_type: OrderType = OrderType.LIMIT
    protection_required: bool = True
    sector: str = "UNKNOWN"
    cluster: str = "UNKNOWN"
    allowed_currencies: tuple[str, ...] = ("USD",)
    allowed_exchanges: tuple[str, ...] = ("SMART",)

    def __post_init__(self) -> None:
        if self.stop_loss_fraction <= 0 or self.stop_loss_fraction >= 1:
            raise ExecutionServiceError("stop_loss_fraction must be within (0, 1)")


# -- the authorization boundary -----------------------------------------------


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """Whether this decision may cross the submission boundary.

    Separate from readiness on purpose. Readiness answers "would this be safe";
    authorization answers "are we permitted to". Conflating them is how
    ``PAPER_READY`` would quietly become Paper Auto.
    """

    allowed: bool
    reason: str
    #: Present only on ALLOW. Opaque here: minting one is the pre-submit
    #: check's job, and consuming it is the execution agent's.
    pre_submit: object | None = None
    authorization: object | None = None


@runtime_checkable
class ExecutionAuthorization(Protocol):
    """The authority that permits submission. Default production answer: no."""

    def authorize(
        self, intent: ExecutionIntent, report: TradingReadinessRuntimeReport
    ) -> AuthorizationDecision: ...


class DenyAllExecutionAuthorization:
    """The only production authorization: DENY.

    Paper Auto and Live are prohibited, so autonomous submission is not merely
    unimplemented — it is refused, by the component whose job is to refuse it.
    Enabling submission means replacing this with an authority that mints a
    single-use authorization through the canonical pre-submit check, not
    loosening anything here.
    """

    reason = (
        "autonomous submission is not enabled: Paper Auto and Live are"
        " prohibited, and readiness is not authorization"
    )

    def authorize(
        self, intent: ExecutionIntent, report: TradingReadinessRuntimeReport
    ) -> AuthorizationDecision:
        return AuthorizationDecision(allowed=False, reason=self.reason)


# -- risk, scoped to one decision ---------------------------------------------


class DecisionScopedRiskSource:
    """Supplies the guard the risk result for THIS decision, and no other.

    The guard asks its risk source at the submission boundary. If that source
    could answer with any recent result, a veto on one symbol could be dodged by
    a pass on another. This answers only when the intent it is asked about is
    the intent it was built for; anything else returns ``None``, which blocks.
    """

    def __init__(self, client_order_id: str, symbol: str, result: RiskResult) -> None:
        self._client_order_id = client_order_id
        self._symbol = symbol
        self._result = result

    def risk_result_for(self, intent: OrderIntent) -> RiskResult | None:
        if intent.client_order_id != self._client_order_id or intent.symbol != self._symbol:
            # Not this decision. Refusing to answer is the whole point.
            return None
        return self._result


class DecisionRiskRegistry:
    """The same scoping, for a guard that outlives one decision.

    The execution agent's idempotency guarantee is per-instance state: a fresh
    agent per decision would forget every order it had already processed, so one
    agent -- and therefore one guard -- lives for the service's lifetime. That
    guard still needs a risk source, and it must not become a place where any
    decision's result can answer for any other.

    Results are therefore keyed by ``(client_order_id, symbol)`` and an
    unregistered order gets ``None``, which blocks. Registering the same key
    again replaces the entry, so a re-planned decision uses its NEW risk result
    and never a stale one.
    """

    def __init__(self) -> None:
        self._results: dict[tuple[str, str], RiskResult] = {}

    def register(self, client_order_id: str, symbol: str, result: RiskResult) -> None:
        self._results[(client_order_id, symbol)] = result

    def risk_result_for(self, intent: OrderIntent) -> RiskResult | None:
        return self._results.get((intent.client_order_id, intent.symbol))


# -- observations -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionEvaluation:
    """Everything one decision produced, refused or not."""

    intent: ExecutionIntent
    stage: ExecutionStage
    reasons: tuple[str, ...]
    instrument: BrokerInstrument | None = None
    quote: QuoteState | None = None
    risk_result: RiskResult | None = None
    approved_quantity: Decimal | None = None
    stop_price: Decimal | None = None
    entry_price: Decimal | None = None
    universe_status: UniverseListStatus | None = None
    readiness: TradingReadinessRuntimeReport | None = None
    authorization: AuthorizationDecision | None = None
    audit_ref: str | None = None
    outcome: ExecutionOutcome | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    #: Recorded so a refusal that never reached readiness still names its mode.
    intent_mode: str = "UNKNOWN"

    @property
    def submitted(self) -> bool:
        return self.stage is ExecutionStage.SUBMITTED

    def to_dict(self) -> dict[str, object]:
        """The audit record for this decision. No secrets, no account numbers."""
        record: dict[str, object] = {
            "version": EXECUTION_SERVICE_VERSION,
            "decision_id": self.intent.decision_id,
            "correlation_id": self.intent.correlation_id,
            "client_order_id": self.intent.client_order_id,
            "strategy_id": self.intent.strategy_id,
            "symbol": self.intent.symbol,
            "side": self.intent.side.value,
            "stage": self.stage.value,
            "reasons": list(self.reasons),
            "evaluated_at": self.evaluated_at.isoformat(),
            "security_id": (
                self.instrument.broker_instrument_ref if self.instrument else None
            ),
            "exchange": self.instrument.exchange if self.instrument else None,
            "currency": self.instrument.currency if self.instrument else None,
            "universe_status": (
                self.universe_status.value if self.universe_status else None
            ),
            "entry_price": str(self.entry_price) if self.entry_price is not None else None,
            "stop_price": str(self.stop_price) if self.stop_price is not None else None,
            "approved_quantity": (
                str(self.approved_quantity) if self.approved_quantity is not None else None
            ),
        }
        if self.quote is not None:
            record["quote"] = {
                "bid": str(self.quote.bid) if self.quote.bid is not None else None,
                "ask": str(self.quote.ask) if self.quote.ask is not None else None,
                "bid_size": self.quote.bid_size,
                "ask_size": self.quote.ask_size,
                "observed_at": (
                    self.quote.observed_at.isoformat() if self.quote.observed_at else None
                ),
                "market_data_state": self.quote.market_data_state.value,
                "detail": self.quote.detail,
            }
        if self.risk_result is not None:
            record["risk"] = {
                "outcome": self.risk_result.outcome.value,
                "approved_quantity": str(self.risk_result.approved_quantity),
                "decision_ref": self.risk_result.decision_ref,
                "policy_ref": self.risk_result.policy_ref,
                "engine_version": self.risk_result.engine_version,
                "reason_codes": list(self.risk_result.reason_codes),
            }
        if self.readiness is not None:
            readiness = self.readiness.to_dict()
            record["trading_readiness"] = readiness["trading_readiness"]
            record["gates"] = readiness["gates"]
            record["blockers"] = readiness["blockers"]
            record["mode"] = readiness["mode"]
            record["authorization_note"] = readiness["authorization_note"]
        else:
            record["mode"] = self.intent_mode
        if self.authorization is not None:
            record["authorization"] = {
                "allowed": self.authorization.allowed,
                "reason": self.authorization.reason,
            }
        if self.outcome is not None:
            record["submission_outcome"] = self.outcome.status.value
            record["submission_reasons"] = list(self.outcome.reason_codes)
            record["broker_order_ref"] = (
                self.outcome.receipt.broker_order_ref if self.outcome.receipt else None
            )
        return record


# -- the service ---------------------------------------------------------------


class ExecutionService:
    """Drives one decision through the canonical path. Submits nothing itself."""

    def __init__(
        self,
        *,
        broker: BrokerPort,
        quotes: QuoteSource,
        audit: AuditSink,
        risk_engine: RiskEngine,
        config: ExecutionConfig,
        agent_factory: Callable[[BrokerPort], ExecutionAgent],
        authorization: ExecutionAuthorization | None = None,
        universe: Callable[[], ConstituentSnapshot | None] | None = None,
        portfolio: Callable[[], tuple[PositionExposure, ...]] | None = None,
        universe_status: Callable[[], UniverseListStatus | None] | None = None,
        halts: Callable[[], Sequence[HaltRecord]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._quotes = quotes
        self._audit = audit
        self._risk_engine = risk_engine
        self._config = config
        self._agent_factory = agent_factory
        # DENY unless something explicitly says otherwise, and nothing in
        # production does.
        self._authorization = authorization or DenyAllExecutionAuthorization()
        # ONE guard and ONE agent for the service's lifetime. A fresh agent per
        # decision would reset the idempotency map that makes a retry safe.
        self._risk_registry = DecisionRiskRegistry()
        self._guard = ReadinessGuardedBroker(
            broker,
            config=GuardConfig(
                mode=config.mode,
                requirements=config.requirements,
                rules=config.rules,
                account_alias=config.account_alias,
                allowed_currencies=config.allowed_currencies,
                allowed_exchanges=config.allowed_exchanges,
                protection_required=config.protection_required,
            ),
            quotes=quotes,
            risk=self._risk_registry,
            audit=audit,
            universe_status=universe_status,
            now=now,
        )
        self._agent = agent_factory(self._guard)
        self._universe = universe
        #: Authoritative open-risk view when one exists; otherwise the estimate
        #: below is used and is documented as an estimate.
        self._portfolio = portfolio
        self._universe_status = universe_status
        self._halts = halts or (lambda: ())
        self._now = now or (lambda: datetime.now(tz=UTC))

    # -- the read-only path ---------------------------------------------------

    def evaluate(self, intent: ExecutionIntent) -> ExecutionEvaluation:
        """Observe everything, submit nothing.

        Safe to run against the live owner host: the only broker calls it makes
        are reads and a what-if preview, which places nothing.
        """
        return self._observe(intent)

    def execute(self, intent: ExecutionIntent) -> ExecutionEvaluation:
        """Observe, then submit ONLY if authorized and ready.

        Named so it cannot be mistaken for a probe. It still cannot submit
        without an authority that says yes, and the production authority says no.
        """
        settled = self._agent.outcome_for(intent.client_order_id)
        if settled is not None:
            # Idempotency FIRST. Re-planning a decision the broker has already
            # answered would re-preview an order id it has seen, which it
            # refuses -- and would turn a safe retry into a spurious refusal.
            LOGGER.info(
                "duplicate decision returns the original outcome",
                extra=self._context(intent, stage="IDEMPOTENT_RETRY"),
            )
            return ExecutionEvaluation(
                intent=intent,
                stage=(
                    ExecutionStage.SUBMITTED
                    if settled.status.value == "SUBMITTED"
                    else ExecutionStage.SUBMISSION_REFUSED
                ),
                reasons=("idempotent retry: the original outcome stands",)
                + settled.reason_codes,
                outcome=settled,
                evaluated_at=self._now(),
                intent_mode=self._config.mode.value,
            )
        evaluation = self._observe(intent)
        if evaluation.stage is not ExecutionStage.READY_NOT_AUTHORIZED:
            return evaluation
        return self._submit(intent, evaluation)

    # -- observation ----------------------------------------------------------

    def _observe(self, intent: ExecutionIntent) -> ExecutionEvaluation:
        config = self._config
        LOGGER.info(
            "execution decision received", extra=self._context(intent, stage="RECEIVED")
        )

        halt = match_halt(
            self._halts(),
            EntryContext(
                environment=config.environment,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                source_id=intent.source_id,
            ),
        )
        if halt is not None:
            return self._refuse(
                intent,
                ExecutionStage.HALTED,
                f"halt active ({halt.scope.value})",
                EntryCheckpoint.PRE_SUBMIT.value,
            )

        universe_status = self._universe_status() if self._universe_status else None
        membership_problem = self._universe_refusal(intent, universe_status)
        if membership_problem is not None:
            return self._refuse(
                intent, ExecutionStage.UNIVERSE_REFUSED, membership_problem,
                universe_status=universe_status,
            )

        try:
            instrument = self._broker.resolve_instrument(InstrumentQuery(symbol=intent.symbol))
        except BrokerPortError as error:
            return self._refuse(
                intent, ExecutionStage.CONTRACT_UNRESOLVED,
                f"broker refused to resolve {intent.symbol}: {error}",
                universe_status=universe_status,
            )
        except Exception as error:  # noqa: BLE001
            return self._refuse(
                intent, ExecutionStage.CONTRACT_UNRESOLVED,
                f"resolve_instrument raised {type(error).__name__}",
                universe_status=universe_status,
            )
        LOGGER.info(
            "contract resolved",
            extra=self._context(intent, stage="CONTRACT", ref=instrument.broker_instrument_ref),
        )

        quote = self._quotes.current_quote(intent.symbol)
        entry_price = quote.midpoint
        LOGGER.info(
            "quote observed",
            extra=self._context(
                intent, stage="QUOTE", market_data_state=quote.market_data_state.value
            ),
        )
        if entry_price is None or entry_price <= 0:
            return self._refuse(
                intent, ExecutionStage.NO_QUOTE,
                quote.detail or "no usable quote: cannot price the decision",
                instrument=instrument, quote=quote, universe_status=universe_status,
            )

        stop_price = self._stop_price(intent, entry_price)
        if stop_price is None and config.protection_required:
            return self._refuse(
                intent, ExecutionStage.PLAN_REFUSED,
                "no valid protective stop could be constructed for this plan",
                instrument=instrument, quote=quote, entry_price=entry_price,
                universe_status=universe_status,
            )

        try:
            risk_result = self._evaluate_risk(intent, entry_price, stop_price)
        except (RiskEngineError, BrokerPortError) as error:
            return self._refuse(
                intent, ExecutionStage.RISK_REFUSED,
                f"risk evaluation could not be completed: {error}",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )
        LOGGER.info(
            "risk result",
            extra=self._context(
                intent, stage="RISK", risk_outcome=risk_result.outcome.value
            ),
        )

        approved = risk_result.approved_quantity
        if risk_result.outcome is RiskDecisionOutcome.VETO or approved <= 0:
            return self._refuse(
                intent, ExecutionStage.RISK_REFUSED,
                f"risk outcome {risk_result.outcome.value}:"
                f" {list(risk_result.reason_codes)}",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, risk_result=risk_result,
                universe_status=universe_status,
            )
        if approved > intent.requested_quantity:
            # The engine reduces; it never raises. A larger approval than was
            # requested means the wiring is wrong, and acting on it would mean
            # sizing from something other than the strategy's own bound.
            return self._refuse(
                intent, ExecutionStage.RISK_REFUSED,
                f"approved quantity {approved} exceeds the requested"
                f" {intent.requested_quantity}; refusing to size upward",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, risk_result=risk_result,
                universe_status=universe_status,
            )

        # Scope this decision's risk result to this decision before the guard
        # can be asked about it.
        self._risk_registry.register(intent.client_order_id, intent.symbol, risk_result)
        order_intent = self._order_intent(intent, approved, entry_price, stop_price)

        # Readiness is computed here through the SAME guard the agent will use,
        # so the report an operator reads is produced by the enforcement path
        # rather than by a parallel re-implementation of it.
        report = self._guard.evaluate_readiness(order_intent)
        LOGGER.info(
            "readiness result",
            extra=self._context(intent, stage="READINESS", readiness=report.readiness.value),
        )

        authorization = self._authorization.authorize(intent, report)
        LOGGER.info(
            "authorization result",
            extra=self._context(
                intent, stage="AUTHORIZATION", allowed=str(authorization.allowed)
            ),
        )

        if report.readiness is TradingReadiness.BLOCKED:
            stage = ExecutionStage.READINESS_BLOCKED
            reasons: tuple[str, ...] = report.blockers
        elif not authorization.allowed:
            stage = ExecutionStage.AUTHORIZATION_DENIED
            reasons = (authorization.reason,)
        else:
            stage = ExecutionStage.READY_NOT_AUTHORIZED
            reasons = ("readiness satisfied and authorization granted",)

        evaluation = ExecutionEvaluation(
            intent=intent, stage=stage, reasons=reasons, instrument=instrument,
            quote=quote, risk_result=risk_result, approved_quantity=approved,
            stop_price=stop_price, entry_price=entry_price,
            universe_status=universe_status, readiness=report,
            authorization=authorization, evaluated_at=self._now(),
            intent_mode=self._config.mode.value,
        )
        return self._audit_evaluation(evaluation)

    # -- submission -----------------------------------------------------------

    def _submit(
        self, intent: ExecutionIntent, evaluation: ExecutionEvaluation
    ) -> ExecutionEvaluation:
        """Hand the request to the agent. The agent owns the submit call site."""
        authorization = evaluation.authorization
        assert authorization is not None and authorization.allowed  # noqa: S101
        if authorization.pre_submit is None or authorization.authorization is None:
            # An ALLOW without a minted single-use authorization is not an
            # authorization at all; the agent would refuse it, and saying so
            # here keeps the refusal legible.
            return self._replace_stage(
                evaluation,
                ExecutionStage.AUTHORIZATION_DENIED,
                ("authorization carried no single-use submit authorization",),
            )
        assert evaluation.risk_result is not None  # noqa: S101
        assert evaluation.approved_quantity is not None  # noqa: S101

        request = ExecutionRequest(
            client_order_id=intent.client_order_id,
            correlation_id=intent.correlation_id,
            pre_submit=authorization.pre_submit,  # type: ignore[arg-type]
            authorization=authorization.authorization,  # type: ignore[arg-type]
            environment=self._config.environment,
            account_alias=self._config.account_alias,
            symbol=intent.symbol,
            side=intent.side,
            order_type=self._config.order_type,
            limit_price=evaluation.entry_price,
            stop_price=evaluation.stop_price,
            strategy_id=intent.strategy_id,
            source_id=intent.source_id,
        )
        LOGGER.info("submit attempt", extra=self._context(intent, stage="SUBMIT"))
        try:
            outcome = self._agent.process(request, active_halts=self._halts())
        except ReadinessBlockedError as error:
            # The guard's just-in-time check refused at the boundary, after this
            # service had already seen a satisfied report. That is the check
            # doing its job: the market moved.
            return self._audit_evaluation(
                self._replace_stage(
                    evaluation, ExecutionStage.READINESS_BLOCKED, error.report.blockers
                )
            )

        submitted = outcome.status.value == "SUBMITTED"
        final = ExecutionEvaluation(
            intent=intent,
            stage=ExecutionStage.SUBMITTED if submitted else ExecutionStage.SUBMISSION_REFUSED,
            reasons=outcome.reason_codes,
            instrument=evaluation.instrument, quote=evaluation.quote,
            risk_result=evaluation.risk_result,
            approved_quantity=evaluation.approved_quantity,
            stop_price=evaluation.stop_price, entry_price=evaluation.entry_price,
            universe_status=evaluation.universe_status, readiness=evaluation.readiness,
            authorization=authorization, audit_ref=evaluation.audit_ref,
            outcome=outcome, evaluated_at=self._now(),
            intent_mode=self._config.mode.value,
        )
        LOGGER.info(
            "submit result",
            extra=self._context(intent, stage="SUBMIT", outcome=outcome.status.value),
        )
        # Post-submit audit NEVER replaces the pre-submit record; it is appended.
        # A failure here is operationally critical and must not become a second
        # submission attempt, so the outcome is returned either way.
        self._persist_outcome(final)
        return final

    # -- internals ------------------------------------------------------------

    @property
    def guarded_broker(self) -> ReadinessGuardedBroker:
        """The guarded port the agent submits through. Exposed for inspection."""
        return self._guard

    def _order_intent(
        self,
        intent: ExecutionIntent,
        quantity: Decimal,
        entry_price: Decimal,
        stop_price: Decimal | None,
    ) -> OrderIntent:
        return OrderIntent(
            client_order_id=intent.client_order_id,
            # Provisional: the real one is minted by the pre-submit check and
            # substituted by the agent. Never treated as permission here.
            authorization_ref=f"unauthorized-probe:{intent.decision_id}",
            correlation_id=intent.correlation_id,
            environment=self._config.environment,
            account_alias=self._config.account_alias,
            symbol=intent.symbol,
            side=intent.side,
            order_type=self._config.order_type,
            quantity=quantity,
            limit_price=entry_price,
            stop_price=stop_price,
        )

    def _stop_price(self, intent: ExecutionIntent, entry: Decimal) -> Decimal | None:
        """Protection derived from configuration, never improvised per call."""
        fraction = self._config.stop_loss_fraction
        offset = (entry * fraction).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        stop = entry - offset if intent.side is OrderSide.BUY else entry + offset
        if stop <= 0 or stop == entry:
            return None
        return stop

    def _evaluate_risk(
        self, intent: ExecutionIntent, entry_price: Decimal, stop_price: Decimal | None
    ) -> RiskResult:
        """Invoke the REAL engine with REAL account state for THIS decision."""
        snapshot = self._broker.get_account_snapshot(self._config.account_alias)
        account = AccountState(
            broker_net_liquidation_value=snapshot.net_liquidation_value,
            simulation_equity_cap=self._config.simulation_equity_cap,
            session_pnl=Decimal(0),
            open_positions=self._open_positions(intent),
            observed_at=snapshot.observed_at,
        )
        request = TradeRiskRequest(
            symbol=intent.symbol,
            sector=self._config.sector,
            cluster=self._config.cluster,
            source_id=intent.source_id,
            strategy_id=intent.strategy_id,
            environment=self._config.environment,
            requested_quantity=intent.requested_quantity,
            entry_price=entry_price,
            stop_price=stop_price,
        )
        return self._risk_engine.evaluate(
            intent.decision,
            request,
            account,
            active_halts=self._halts(),
            as_of=self._now(),
        )

    def _open_positions(self, intent: ExecutionIntent) -> tuple[PositionExposure, ...]:
        """Existing exposure, from the authoritative source when there is one."""
        if self._portfolio is not None:
            return self._portfolio()
        fraction = self._config.assumed_open_risk_fraction or self._config.stop_loss_fraction
        return tuple(
            PositionExposure(
                symbol=position.symbol,
                sector=self._config.sector,
                cluster=self._config.cluster,
                source_id=intent.source_id,
                strategy_id=intent.strategy_id,
                # ESTIMATE, and labelled as one: the port carries no stop for an
                # existing position, so open risk is assumed to be the same
                # fraction of notional this system enforces on new entries.
                open_risk_amount=(
                    abs(position.quantity * position.average_price) * fraction
                ),
                notional=abs(position.quantity * position.average_price),
            )
            for position in self._broker.get_positions(self._config.account_alias)
        )

    def _universe_refusal(
        self, intent: ExecutionIntent, status: UniverseListStatus | None
    ) -> str | None:
        """ADR-0017 exactly: membership matters only to an index strategy."""
        if self._config.requirements.universe is not UniverseType.SP500_CURRENT:
            return None
        if status is not UniverseListStatus.CURRENT_LIST_VALID:
            return (
                f"strategy trades the current S&P 500 but the list is"
                f" {status.value if status else 'UNAVAILABLE'}"
            )
        snapshot = self._universe() if self._universe else None
        if snapshot is None:
            return "strategy trades the current S&P 500 but no snapshot is loaded"
        if intent.symbol not in snapshot.tickers():
            return f"{intent.symbol} is not a current S&P 500 constituent"
        return None

    def _audit_evaluation(self, evaluation: ExecutionEvaluation) -> ExecutionEvaluation:
        """Durably record the decision. Failure is itself a refusal."""
        try:
            if not self._audit.can_persist():
                return self._replace_stage(
                    evaluation,
                    ExecutionStage.AUDIT_UNAVAILABLE,
                    ("audit sink reports it cannot persist; refusing to proceed",),
                )
            reference = self._audit.persist(evaluation.to_dict())
        except Exception as error:  # noqa: BLE001 - unrecorded is unsubmittable
            return self._replace_stage(
                evaluation,
                ExecutionStage.AUDIT_UNAVAILABLE,
                (f"pre-submit audit failed ({type(error).__name__})",),
            )
        LOGGER.info(
            "audit persisted",
            extra=self._context(evaluation.intent, stage="AUDIT", audit_ref=reference),
        )
        return _with(evaluation, audit_ref=str(reference))

    def _persist_outcome(self, evaluation: ExecutionEvaluation) -> None:
        record = dict(evaluation.to_dict())
        record["checkpoint"] = "POST_SUBMIT"
        persist_outcome = getattr(self._audit, "persist_outcome", None)
        try:
            if callable(persist_outcome):
                persist_outcome(record)
            else:
                self._audit.persist(record)
        except Exception as error:  # noqa: BLE001
            # The broker may already hold the order. Retrying the SUBMISSION
            # here would risk a duplicate; this is an operator problem, loudly.
            LOGGER.error(
                "post-submit audit failed after a broker submission; operator"
                " attention required and the submission is NOT retried",
                extra=self._context(
                    evaluation.intent, stage="POST_SUBMIT_AUDIT_FAILED",
                    error=type(error).__name__,
                ),
            )

    def _refuse(
        self,
        intent: ExecutionIntent,
        stage: ExecutionStage,
        *reasons: str,
        instrument: BrokerInstrument | None = None,
        quote: QuoteState | None = None,
        entry_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        risk_result: RiskResult | None = None,
        universe_status: UniverseListStatus | None = None,
    ) -> ExecutionEvaluation:
        """A refusal is a decision, and it is audited like one."""
        evaluation = ExecutionEvaluation(
            intent=intent, stage=stage, reasons=reasons, instrument=instrument,
            quote=quote, entry_price=entry_price, stop_price=stop_price,
            risk_result=risk_result, universe_status=universe_status,
            evaluated_at=self._now(), intent_mode=self._config.mode.value,
        )
        LOGGER.info(
            "execution refused",
            extra=self._context(intent, stage=stage.value, reasons="; ".join(reasons)),
        )
        return self._audit_evaluation(evaluation)

    def _replace_stage(
        self, evaluation: ExecutionEvaluation, stage: ExecutionStage, reasons: Sequence[str]
    ) -> ExecutionEvaluation:
        return _with(evaluation, stage=stage, reasons=tuple(reasons))

    def _context(self, intent: ExecutionIntent, **extra: str) -> Mapping[str, str]:
        """Correlation only. No account numbers, no credentials, ever."""
        return {
            "decision_id": intent.decision_id,
            "client_order_id": intent.client_order_id,
            "correlation_id": intent.correlation_id,
            "symbol": intent.symbol,
            "strategy_id": intent.strategy_id,
            **extra,
        }


def _with(evaluation: ExecutionEvaluation, **changes: object) -> ExecutionEvaluation:
    from dataclasses import replace

    return replace(evaluation, **changes)  # type: ignore[arg-type]


__all__ = [
    "EXECUTION_SERVICE_VERSION",
    "AuthorizationDecision",
    "DecisionRiskRegistry",
    "DecisionScopedRiskSource",
    "DenyAllExecutionAuthorization",
    "ExecutionAuthorization",
    "ExecutionConfig",
    "ExecutionEvaluation",
    "ExecutionIntent",
    "ExecutionService",
    "ExecutionServiceError",
    "ExecutionStage",
]
