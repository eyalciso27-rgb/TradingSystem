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

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from trading_application.decision_engine import EngineDecision
from trading_application.emergency import EntryCheckpoint, EntryContext, HaltRecord, match_halt
from trading_application.pre_submit_check import (
    PreSubmitAction,
    PreSubmitCheckResult,
    SingleUseAuthorization,
)
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
    BrokerCapabilityMode,
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

#: Chain action names, mirrored so the application layer can read its own
#: history without importing the persistence package.
ACTION_EVALUATED = "EXECUTION_EVALUATED"
ACTION_SUBMIT_PREPARED = "EXECUTION_SUBMIT_PREPARED"
ACTION_OUTCOME = "EXECUTION_OUTCOME"
#: Historic names, still readable from an existing chain.
ACTION_PRE_SUBMIT = "EXECUTION_PRE_SUBMIT"
ACTION_POST_SUBMIT = "EXECUTION_POST_SUBMIT"

#: Only these mean "the broker may already hold this order".
AMBIGUOUS_ACTIONS: frozenset[str] = frozenset(
    {ACTION_SUBMIT_PREPARED, ACTION_PRE_SUBMIT}
)
TERMINAL_ACTIONS: frozenset[str] = frozenset({ACTION_OUTCOME, ACTION_POST_SUBMIT})

EXECUTION_SERVICE_VERSION = "execution-service-1.0.0"

LOGGER = logging.getLogger("trading_execution_agent.execution_service")

#: The one legal pairing. A mode is a readiness axis and an environment is a
#: deployment axis, but they are never independently chosen.
MODE_ENVIRONMENT: Mapping[TradingMode, Environment] = {
    TradingMode.PAPER: Environment.PAPER,
    TradingMode.LIVE: Environment.LIVE,
}

#: Classification we could not establish. Modelled as a value rather than
#: guessed from the candidate: an existing position's sector is not the new
#: candidate's sector, and pretending otherwise corrupts every sector, cluster
#: and source cap the risk engine computes.
UNKNOWN_CLASSIFICATION = "UNKNOWN"


def policy_identity(policy: RiskPolicy) -> str:
    """A hash over EVERY limit, not just the version string.

    Two policies sharing a version and differing in a limit are two policies,
    and a version-only check would let the audit report limits that were never
    enforced.
    """
    canonical = json.dumps(
        {
            field.name: str(getattr(policy, field.name))
            for field in dataclass_fields(policy)
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


class ExecutionStage(StrEnum):
    """How far one decision travelled. The refusal point, named."""

    HALTED = "HALTED"
    UNIVERSE_REFUSED = "UNIVERSE_REFUSED"
    CONTRACT_UNRESOLVED = "CONTRACT_UNRESOLVED"
    NO_QUOTE = "NO_QUOTE"
    PLAN_REFUSED = "PLAN_REFUSED"
    RISK_REFUSED = "RISK_REFUSED"
    ACCOUNT_STATE_INCOMPLETE = "ACCOUNT_STATE_INCOMPLETE"
    PORTFOLIO_UNCLASSIFIED = "PORTFOLIO_UNCLASSIFIED"
    PROTECTION_UNAVAILABLE = "PROTECTION_UNAVAILABLE"
    CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
    READINESS_BLOCKED = "READINESS_BLOCKED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    READY_NOT_AUTHORIZED = "READY_NOT_AUTHORIZED"
    SUBMITTED = "SUBMITTED"
    SUBMISSION_REFUSED = "SUBMISSION_REFUSED"
    #: A prior attempt is durably recorded but its broker outcome is not. Never
    #: retried blindly -- reconciliation resolves it, exactly as an UNKNOWN
    #: broker state is resolved everywhere else in this system.
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"
    #: The durable record disagrees with this request about what it IS.
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    #: The broker answered but we could not record it. Operationally critical.
    POST_SUBMIT_AUDIT_FAILED = "POST_SUBMIT_AUDIT_FAILED"


class ExecutionServiceError(Exception):
    """Fail-closed execution failure."""


class IncompleteAccountStateError(ExecutionServiceError):
    """A fact the risk engine needs was not reported. Never substituted."""


# -- what the caller may ask for ----------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Business intent for one decision. No safety verdicts, by construction.

    There is deliberately no field here for readiness, risk, tradability, quote
    freshness or audit health. Those are produced by the components that own
    them; a caller that could assert them could bypass every gate by asserting
    them wrongly.
    """

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

    @property
    def decision_id(self) -> str:
        """THE canonical decision identity, derived and never duplicated.

        This used to be a separate string field beside the ``EngineDecision``,
        with nothing proving the two referred to the same decision — and the
        owner probe genuinely produced two: a ``probe-...`` string and a fresh
        UUID inside the decision. That split the risk result's ``decision_ref``,
        the audit ``target_ref``, the durable history key and the execution
        identity across two names for one thing.

        There is now one source: the decision itself.
        """
        return str(self.decision.result.decision_id)

    def __post_init__(self) -> None:
        for name in ("strategy_id", "symbol", "correlation_id"):
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

    #: Refuse a decision whose existing portfolio cannot be classified. The
    #: sector, cluster and source caps are meaningless over positions whose
    #: sector, cluster and source are unknown, and passing them silently is
    #: worse than refusing. Supply ``portfolio`` to satisfy this honestly.
    require_classified_portfolio: bool = True

    def __post_init__(self) -> None:
        if self.stop_loss_fraction <= 0 or self.stop_loss_fraction >= 1:
            raise ExecutionServiceError("stop_loss_fraction must be within (0, 1)")
        # Mode and environment are two names for one fact, and a configuration
        # that disagrees with itself would evaluate PAPER readiness while acting
        # in another environment. There is no valid mixed combination.
        expected = MODE_ENVIRONMENT[self.mode]
        if self.environment is not expected:
            raise ExecutionServiceError(
                f"mode {self.mode.value} requires environment {expected.value},"
                f" not {self.environment.value}"
            )


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
    #: Present only on ALLOW, and typed. These were ``object | None`` with a
    #: ``type: ignore`` at the handoff -- an untyped hole at the single most
    #: safety-critical boundary in the system, where a wrong shape would be
    #: found by the broker rather than the compiler.
    pre_submit: PreSubmitCheckResult | None = None
    authorization: SingleUseAuthorization | None = None


@runtime_checkable
class ExecutionHistoryLike(Protocol):
    """One durable lifecycle record: what happened, and what it said."""

    @property
    def action(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object]: ...


@runtime_checkable
class DurableExecutionHistory(Protocol):
    """What the durable audit knows about a decision, across restarts.

    In-process idempotency dies with the process. The agent's outcome map and
    the adapter's client-order map are both memory, so a crash between submit
    and acknowledgement leaves a system that has forgotten it ever acted — and
    a replayed decision would submit a second time.

    The durable chain already records every PRE_SUBMIT and POST_SUBMIT, so it
    can answer that question after a restart. This is the read side of it.
    """

    def records_for(self, decision_id: str) -> Sequence[ExecutionHistoryLike]: ...


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
        self._results: dict[str, tuple[ExecutionFingerprint, RiskResult]] = {}

    def register(self, fingerprint: ExecutionFingerprint, result: RiskResult) -> None:
        self._results[fingerprint.client_order_id] = (fingerprint, result)

    def risk_result_for(self, intent: OrderIntent) -> RiskResult | None:
        """The result for THIS order, and only if the order still matches it.

        Identity alone is not enough. The final ``OrderIntent.quantity`` comes
        from the authorization, not from the risk engine, so an authority that
        minted 10 against an approval of 1 would otherwise sail through a gate
        that only checks ``approved_quantity > 0``. The size is bound here, at
        the last boundary before submission: an order larger than the engine
        approved is not this decision's order, and gets nothing.
        """
        entry = self._results.get(intent.client_order_id)
        if entry is None:
            return None
        fingerprint, result = entry
        # The order must be the SAME execution, not merely one wearing the same
        # order id. A second decision sharing an id and symbol must not be able
        # to spend the first decision's risk approval.
        if (
            intent.symbol != fingerprint.symbol
            or intent.side.value != fingerprint.side
            or intent.environment.value != fingerprint.environment
            or intent.account_alias != fingerprint.account_alias
        ):
            return None
        if intent.quantity > result.approved_quantity:
            return None
        return result


# -- observations -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionFingerprint:
    """The immutable identity of ONE attempted execution.

    Idempotency keyed on a client order id alone answers "have I seen this
    id?", which is not the question. The question is "is this the same
    execution?" — and an id reused for a different decision, symbol, side or
    account is a different execution wearing a familiar name.

    Every field here is fixed before submission and cannot change without the
    execution being a different one.
    """

    decision_id: str
    client_order_id: str
    strategy_id: str
    symbol: str
    side: str
    environment: str
    account_alias: str

    def to_dict(self) -> dict[str, str]:
        return {
            "decision_id": self.decision_id,
            "client_order_id": self.client_order_id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "side": self.side,
            "environment": self.environment,
            "account_alias": self.account_alias,
        }

    def digest(self) -> str:
        """A stable reference for the whole identity, for logs and records."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()

    @staticmethod
    def from_dict(payload: Mapping[str, object]) -> ExecutionFingerprint | None:
        raw = payload.get("execution_fingerprint")
        if not isinstance(raw, dict):
            return None
        try:
            return ExecutionFingerprint(
                decision_id=str(raw["decision_id"]),
                client_order_id=str(raw["client_order_id"]),
                strategy_id=str(raw["strategy_id"]),
                symbol=str(raw["symbol"]),
                side=str(raw["side"]),
                environment=str(raw["environment"]),
                account_alias=str(raw["account_alias"]),
            )
        except KeyError:
            return None


@dataclass(frozen=True, slots=True)
class BrokerProtectionCapability:
    """Whether the broker can hold protection for a new entry, and why not."""

    can_protect: bool
    detail: str


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
    #: The immutable identity of this attempt, carried into every record so a
    #: replay can tell "the same execution" from "the same name".
    fingerprint: ExecutionFingerprint | None = None
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
            "execution_fingerprint": (
                self.fingerprint.to_dict() if self.fingerprint else None
            ),
            "execution_fingerprint_digest": (
                self.fingerprint.digest() if self.fingerprint else None
            ),
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
        history: DurableExecutionHistory | None = None,
        entry_latch: object | None = None,
        universe_status: Callable[[], UniverseListStatus | None] | None = None,
        halts: Callable[[], Sequence[HaltRecord]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._quotes = quotes
        self._audit = audit
        self._risk_engine = risk_engine
        self._config = config
        # The configuration names a policy and the engine applies one. If they
        # differ in ANY limit, every number reported in the audit belongs to a
        # policy that was never applied -- and comparing only the version string
        # would let two materially different policies share a name and pass.
        engine_policy = risk_engine.policy
        if policy_identity(engine_policy) != policy_identity(config.risk_policy):
            raise ExecutionServiceError(
                "risk policy mismatch: the configured policy and the policy the"
                " engine actually applies are not the same policy"
                f" (configured {config.risk_policy.policy_version!r} ->"
                f" {policy_identity(config.risk_policy)}, engine"
                f" {engine_policy.policy_version!r} ->"
                f" {policy_identity(engine_policy)})"
            )
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
        #: Survives a restart, unlike the agent's in-memory outcome map.
        self._history = history
        #: The canonical entry gate, latched shut when a submission's outcome
        #: cannot be recorded. Cancellation is never gated by it.
        self._entry_latch = entry_latch
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
        durable = self._durable_verdict(intent)
        if durable is not None:
            return durable

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
            fingerprint=self._fingerprint(intent),
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

        unclassified = self._unclassified_positions()
        if unclassified and self._config.require_classified_portfolio:
            return self._refuse(
                intent, ExecutionStage.PORTFOLIO_UNCLASSIFIED,
                f"{len(unclassified)} open position(s) have no sector/cluster/source"
                " classification, so the sector, cluster and source risk caps"
                " cannot be evaluated; supply a portfolio authority",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )

        try:
            risk_result = self._evaluate_risk(intent, entry_price, stop_price)
        except IncompleteAccountStateError as error:
            return self._refuse(
                intent, ExecutionStage.ACCOUNT_STATE_INCOMPLETE, str(error),
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )
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

        protection = self._protection_state()
        if self._config.protection_required and not protection.can_protect:
            # FAIL CLOSED. The entry would rest at the broker with nothing
            # protecting it, and "we will place the stop right after" is not a
            # protection mechanism -- it is a window with unbounded exposure in
            # it. See ADR-0018 and the note on IBKR paper capabilities.
            return self._refuse(
                intent, ExecutionStage.PROTECTION_UNAVAILABLE, protection.detail,
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, risk_result=risk_result,
                universe_status=universe_status,
            )

        # Scope this decision's risk result to this decision before the guard
        # can be asked about it.
        self._risk_registry.register(self._fingerprint(intent), risk_result)
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
            fingerprint=self._fingerprint(intent),
        )
        return self._audit_evaluation(evaluation)

    def _fingerprint(self, intent: ExecutionIntent) -> ExecutionFingerprint:
        return ExecutionFingerprint(
            decision_id=intent.decision_id,
            client_order_id=intent.client_order_id,
            strategy_id=intent.strategy_id,
            symbol=intent.symbol,
            side=intent.side.value,
            environment=self._config.environment.value,
            account_alias=self._config.account_alias,
        )

    def _durable_verdict(self, intent: ExecutionIntent) -> ExecutionEvaluation | None:
        """What the durable record says about this execution. None = proceed.

        Four answers, and only the last permits continuing:

        * a terminal record exists -> this execution already reached a broker
          outcome. RECONSTRUCT it and return it. Crucially, that outcome may be
          SUBMITTED, REFUSED, FAILED or NEEDS_REVIEW; reading every terminal
          record as SUBMITTED would invent an order that never existed.
        * a SUBMIT_PREPARED record exists with no outcome -> we crossed the point
          of no return and never recorded what happened. The broker may hold the
          order. Reconcile; never retry blindly. An EVALUATED record does NOT
          mean this, which is the whole reason the two are separate actions.
        * a record exists whose fingerprint disagrees with this request -> the
          same name is being used for a different execution. Refuse.
        * nothing recorded -> no prior submit occurred.
        """
        if self._history is None:
            return None
        try:
            records = tuple(self._history.records_for(intent.decision_id))
        except Exception as error:  # noqa: BLE001 - unknown history blocks
            return self._bare(
                intent,
                ExecutionStage.NEEDS_RECONCILIATION,
                (
                    f"durable execution history is unreadable"
                    f" ({type(error).__name__}); refusing to act without knowing"
                    " whether this execution already submitted",
                ),
            )
        if not records:
            return None

        fingerprint = self._fingerprint(intent)
        conflict = self._fingerprint_conflict(records, fingerprint)
        if conflict is not None:
            LOGGER.error(
                "durable history disagrees with this request about its identity",
                extra=self._context(intent, stage="IDEMPOTENCY_CONFLICT"),
            )
            return self._bare(intent, ExecutionStage.IDEMPOTENCY_CONFLICT, (conflict,))

        terminal = next(
            (record for record in records if record.action in TERMINAL_ACTIONS), None
        )
        if terminal is not None:
            return self._reconstruct(intent, terminal)

        if any(record.action in AMBIGUOUS_ACTIONS for record in records):
            LOGGER.info(
                "a submission was prepared and never resolved; reconcile first",
                extra=self._context(intent, stage="NEEDS_RECONCILIATION"),
            )
            recovered = self._recover_from_broker(intent)
            if recovered is not None:
                return recovered
            return self._bare(
                intent,
                ExecutionStage.NEEDS_RECONCILIATION,
                (
                    "this execution was durably PREPARED for submission and no"
                    " broker outcome was recorded; the broker may already hold"
                    " this order. Reconcile before any further attempt --"
                    " never retry blindly.",
                ),
            )

        # Only evaluations so far: refused before the point of no return, so it
        # replays as an ordinary decision rather than as an incident.
        LOGGER.info(
            "durable history holds evaluations only; nothing was ever submitted",
            extra=self._context(intent, stage="DURABLE_REPLAY"),
        )
        return None

    @staticmethod
    def _fingerprint_conflict(
        records: Sequence[ExecutionHistoryLike], fingerprint: ExecutionFingerprint
    ) -> str | None:
        """Does any stored record describe a DIFFERENT execution?"""
        for record in records:
            stored = ExecutionFingerprint.from_dict(record.payload)
            if stored is None or stored == fingerprint:
                continue
            differing = [
                field
                for field, value in stored.to_dict().items()
                if value != fingerprint.to_dict()[field]
            ]
            return (
                "a durable record for this decision describes a different"
                f" execution (differs in {differing}); refusing rather than"
                " treating two executions as one"
            )
        return None

    def _reconstruct(
        self, intent: ExecutionIntent, record: ExecutionHistoryLike
    ) -> ExecutionEvaluation:
        """Rebuild the prior terminal outcome from what was actually stored."""
        payload = record.payload
        stage_name = str(payload.get("stage", ""))
        outcome = str(payload.get("submission_outcome", ""))
        try:
            stage = ExecutionStage(stage_name)
        except ValueError:
            # An unrecognised stored stage is not an excuse to guess SUBMITTED.
            stage = (
                ExecutionStage.SUBMITTED
                if outcome == "SUBMITTED"
                else ExecutionStage.SUBMISSION_REFUSED
            )
        stored_reasons = payload.get("submission_reasons") or payload.get("reasons")
        reasons: tuple[object, ...] = (
            tuple(stored_reasons) if isinstance(stored_reasons, list | tuple) else ()
        )
        LOGGER.info(
            "durable history holds a terminal outcome; not submitting again",
            extra=self._context(intent, stage="DURABLE_REPLAY", outcome=outcome),
        )
        return self._bare(
            intent,
            stage,
            (
                "a terminal outcome for this execution is already durably"
                f" recorded ({outcome or stage.value}); returning it rather"
                " than submitting again",
                *(str(reason) for reason in reasons),
            ),
        )

    def _recover_from_broker(
        self, intent: ExecutionIntent
    ) -> ExecutionEvaluation | None:
        """Ask the broker whether it already holds this order.

        An ambiguous durable state must not stay an abstract database condition
        when the broker can answer it. This uses the adapter's OWN same-client
        open-order recovery -- the canonical mechanism -- rather than inventing
        a second one, and it never submits anything.
        """
        recover = getattr(self._broker, "recover_cancellable_orders", None)
        if not callable(recover):
            return None
        try:
            recovered = recover(self._config.account_alias)
        except Exception as error:  # noqa: BLE001 - unknown stays unknown
            LOGGER.warning(
                "broker recovery failed; the state remains ambiguous",
                extra=self._context(
                    intent, stage="RECOVERY_FAILED", error=type(error).__name__
                ),
            )
            return None
        for candidate in recovered:
            if getattr(candidate, "client_order_id", None) != intent.client_order_id:
                continue
            LOGGER.info(
                "the broker still holds this order; recovered without resubmitting",
                extra=self._context(intent, stage="RECOVERED"),
            )
            return self._bare(
                intent,
                ExecutionStage.SUBMITTED,
                (
                    "the broker already holds a working order with this"
                    " canonical identity; recovered rather than duplicated",
                ),
            )
        LOGGER.info(
            "the broker holds no working order with this identity",
            extra=self._context(intent, stage="RECOVERY_NO_MATCH"),
        )
        return None

    def _bare(
        self, intent: ExecutionIntent, stage: ExecutionStage, reasons: tuple[str, ...]
    ) -> ExecutionEvaluation:
        return ExecutionEvaluation(
            intent=intent, stage=stage, reasons=reasons,
            evaluated_at=self._now(), intent_mode=self._config.mode.value,
            fingerprint=self._fingerprint(intent),
        )

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

        binding = self._authorization_binding(
            intent, authorization.pre_submit, evaluation.risk_result
        )
        if binding is not None:
            return self._replace_stage(
                evaluation, ExecutionStage.AUTHORIZATION_DENIED, (binding,)
            )

        request = ExecutionRequest(
            client_order_id=intent.client_order_id,
            correlation_id=intent.correlation_id,
            pre_submit=authorization.pre_submit,
            authorization=authorization.authorization,
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
        # THE point of no return. Committed before the submission path is
        # entered, so a crash between here and an outcome means exactly one
        # thing: the broker may hold this order. An ordinary evaluation record
        # must never mean that, which is why this is a distinct action.
        prepared = self._persist_prepared(evaluation)
        if prepared is not None:
            return prepared

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
            fingerprint=self._fingerprint(intent),
        )
        LOGGER.info(
            "submit result",
            extra=self._context(intent, stage="SUBMIT", outcome=outcome.status.value),
        )
        # The outcome record NEVER replaces the earlier ones; it is appended.
        if self._persist_outcome(final):
            return final

        # The broker answered and we could not record it. That is operationally
        # critical: the receipt is preserved, the submission is NEVER retried,
        # and new entries are latched shut until a human reconciles. Cancel and
        # exposure-reduction paths are deliberately untouched.
        self._require_reconciliation(
            "post-submit audit failed after a broker submission"
        )
        return _with(
            final,
            stage=ExecutionStage.POST_SUBMIT_AUDIT_FAILED,
            reasons=(
                "the broker accepted this order and the outcome could NOT be"
                " durably recorded; the submission is not retried, new entries"
                " are latched until reconciliation, and the broker receipt is"
                " preserved below",
                *final.reasons,
            ),
        )

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
        if snapshot.session_pnl is None:
            # The risk engine computes the daily soft and hard halts from this.
            # Substituting zero would present an account that lost money today
            # as flat, and silently disable both halts.
            raise IncompleteAccountStateError(
                "the broker reported no session P&L; the daily halt limits"
                " cannot be evaluated and UNKNOWN is not zero"
            )
        account = AccountState(
            broker_net_liquidation_value=snapshot.net_liquidation_value,
            simulation_equity_cap=self._config.simulation_equity_cap,
            session_pnl=snapshot.session_pnl,
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

    def _protection_state(self) -> BrokerProtectionCapability:
        """Can the broker actually hold protection for a NEW entry?

        A supported STOP *order type* proves nothing: it says the broker would
        accept a stop order if one were sent, not that one will exist. The entry
        this service builds is a single LIMIT order that happens to carry a stop
        price, and the IBKR adapter maps that stop price to the order's aux
        price -- one order, no resting protection behind it.

        Real protection for a brand-new position requires the broker to
        establish it ATOMICALLY with the entry: a native bracket or OCA. The
        IBKR paper adapter declares both UNSUPPORTED (deliberately -- it has no
        bracket wiring, and it refuses to claim NATIVE by promise), so a
        protected entry is not currently possible through it and this returns
        False. That is a real blocker, and reporting it is the point.
        """
        try:
            capabilities = self._broker.capabilities()
        except Exception as error:  # noqa: BLE001
            return BrokerProtectionCapability(
                False, f"broker capabilities unavailable ({type(error).__name__})"
            )
        atomic = {
            "bracket": capabilities.bracket,
            "oco": capabilities.oco,
        }
        supported = [
            name
            for name, mode in atomic.items()
            if mode is BrokerCapabilityMode.NATIVE
        ]
        if supported:
            return BrokerProtectionCapability(
                True, f"broker can establish protection atomically via {supported}"
            )
        return BrokerProtectionCapability(
            False,
            "the broker cannot establish protection atomically with the entry"
            f" (bracket={capabilities.bracket.value}, oco={capabilities.oco.value});"
            " a supported STOP order TYPE is not a resting protective order, and"
            " submitting an unprotected entry is refused",
        )

    def _open_positions(self, intent: ExecutionIntent) -> tuple[PositionExposure, ...]:
        """Existing exposure, from the authoritative source when there is one.

        Without that authority the broker port gives a symbol, a quantity and an
        average price — and nothing about sector, cluster, source or owning
        strategy. Those are therefore ``UNKNOWN``, explicitly.

        They are emphatically NOT the candidate's. Labelling every open position
        with the new decision's sector and source would corrupt the sector cap,
        the cluster cap, the source-risk cap and strategy attribution all at
        once, in whichever direction happened to be wrong. An unknown
        classification is refused by default (see
        ``require_classified_portfolio``) rather than guessed.

        The open-risk AMOUNT remains an explicit estimate (ADR-0018): the port
        carries no protective stop for an existing position, so it is assumed to
        be protected at the same distance this system enforces on new entries.
        """
        if self._portfolio is not None:
            return self._portfolio()
        fraction = self._config.assumed_open_risk_fraction or self._config.stop_loss_fraction
        return tuple(
            PositionExposure(
                symbol=position.symbol,
                sector=UNKNOWN_CLASSIFICATION,
                cluster=UNKNOWN_CLASSIFICATION,
                source_id=UNKNOWN_CLASSIFICATION,
                strategy_id=UNKNOWN_CLASSIFICATION,
                open_risk_amount=(
                    abs(position.quantity * position.average_price) * fraction
                ),
                notional=abs(position.quantity * position.average_price),
            )
            for position in self._broker.get_positions(self._config.account_alias)
        )

    def _unclassified_positions(self) -> tuple[str, ...]:
        """Symbols whose classification we cannot establish. Empty is good."""
        if self._portfolio is not None:
            return ()
        return tuple(
            position.symbol
            for position in self._broker.get_positions(self._config.account_alias)
            if position.quantity != 0
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

    def _authorization_binding(
        self,
        intent: ExecutionIntent,
        pre_submit: PreSubmitCheckResult,
        risk_result: RiskResult,
    ) -> str | None:
        """Does this authorization actually belong to THIS decision and risk?

        An authorization is a permission to act on one specific evaluation. If
        its decision, risk or policy references point somewhere else, then
        something was authorized -- just not this. Returning a reason here is a
        refusal; returning None means the references line up.
        """
        if pre_submit.action not in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE):
            return (
                f"the pre-submit result is {pre_submit.action.value}, which never"
                " permits submission"
            )
        expected = {
            "decision_ref": (pre_submit.decision_ref, risk_result.decision_ref),
            "policy_ref": (pre_submit.policy_ref, risk_result.policy_ref),
        }
        for name, (authorized, actual) in expected.items():
            if authorized and actual and authorized != actual:
                return (
                    f"the authorization's {name} ({authorized!r}) does not match"
                    f" this decision's risk result ({actual!r}); something was"
                    " authorized, but not this"
                )
        if pre_submit.authorized_quantity > risk_result.approved_quantity:
            return (
                f"the authorization permits {pre_submit.authorized_quantity} but"
                f" risk approved {risk_result.approved_quantity}; an authority"
                " may reduce, never raise"
            )
        return None

    def _persist_prepared(
        self, evaluation: ExecutionEvaluation
    ) -> ExecutionEvaluation | None:
        """Durably mark the point of no return. Failure means no submission."""
        record = dict(evaluation.to_dict())
        record["checkpoint"] = "SUBMIT_PREPARED"
        prepare = getattr(self._audit, "persist_prepared", None)
        try:
            if callable(prepare):
                prepare(record)
            else:
                self._audit.persist(record)
        except Exception as error:  # noqa: BLE001 - unrecorded is unsubmittable
            return self._replace_stage(
                evaluation,
                ExecutionStage.AUDIT_UNAVAILABLE,
                (
                    f"the submission could not be durably prepared"
                    f" ({type(error).__name__}); refusing to submit an order"
                    " whose intent to act was never recorded",
                ),
            )
        return None

    def _persist_outcome(self, evaluation: ExecutionEvaluation) -> bool:
        """Record the broker's answer. False means it could not be recorded."""
        record = dict(evaluation.to_dict())
        record["checkpoint"] = "POST_SUBMIT"
        persist_outcome = getattr(self._audit, "persist_outcome", None)
        try:
            if callable(persist_outcome):
                persist_outcome(record)
            else:
                self._audit.persist(record)
        except Exception as error:  # noqa: BLE001
            LOGGER.error(
                "post-submit audit failed after a broker submission; operator"
                " attention required and the submission is NOT retried",
                extra=self._context(
                    evaluation.intent, stage="POST_SUBMIT_AUDIT_FAILED",
                    error=type(error).__name__,
                ),
            )
            return False
        return True

    def _require_reconciliation(self, reason: str) -> None:
        """Latch new entries shut through the canonical gate, if one is wired.

        Reuses the execution agent's own reconciliation latch rather than
        inventing a second blocking mechanism; cancellation and exposure
        reduction are not gated by it, by design.
        """
        latch = self._entry_latch
        require = getattr(latch, "require", None)
        if not callable(require):
            LOGGER.error(
                "no reconciliation latch is wired; new entries are NOT latched"
                " and this state requires manual operator attention: %s",
                reason,
            )
            return
        try:
            require(reason)
        except Exception as error:  # noqa: BLE001
            LOGGER.error(
                "could not latch new entries (%s) after: %s",
                type(error).__name__, reason,
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
            fingerprint=self._fingerprint(intent),
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
    "ACTION_POST_SUBMIT",
    "ACTION_PRE_SUBMIT",
    "BrokerProtectionCapability",
    "DecisionRiskRegistry",
    "DurableExecutionHistory",
    "IncompleteAccountStateError",
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
