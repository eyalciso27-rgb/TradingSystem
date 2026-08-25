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
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from threading import Lock
from typing import Protocol, runtime_checkable

from trading_application.decision_engine import EngineDecision
from trading_application.emergency import EntryCheckpoint, EntryContext, HaltRecord, match_halt
from trading_application.execution_identity import (
    allocation_identity,
    decision_identity,
    plan_identity,
    risk_policy_identity,
    risk_request_identity,
    risk_result_identity,
)
from trading_application.execution_plan import ExecutablePlan
from trading_application.portfolio_allocator import CandidateAllocation
from trading_application.pre_submit_check import (
    PreSubmitAction,
    PreSubmitCheckResult,
    SingleUseAuthorization,
    authorization_matches,
)
from trading_application.readiness_guarded_broker import (
    GuardConfig,
    ProtectedEntryLifecycle,
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
    MarketDataRules,
    QuoteState,
    TradingReadinessRuntimeReport,
)
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerInstrument,
    BrokerPort,
    BrokerPortError,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
    RecoveredOrder,
    SessionPnlStatus,
    TimeInForce,
)
from trading_contracts.enums import Environment, OrderType, RiskDecisionOutcome
from trading_contracts.execution_fingerprint import (
    PREPARED_ORDER_FIELDS,
    REQUEST_IDENTITY_FIELDS,
)
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
#: Exceptions from these module roots are ours: we wrote their messages, so
#: they say what went wrong in our own vocabulary rather than repeating a DSN,
#: a filesystem path, a hostname or an account number back to the operator.
_OUR_MODULES = ("trading_",)

#: Even our own messages are bounded. A message is a diagnostic, not a dump.
_MAX_DETAIL = 200

#: Patterns that must never reach owner-visible output, wherever they came
#: from. Account numbers are the important one -- an IB account id in a probe
#: transcript is an account id in whatever the operator pastes it into.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Broker account identifiers. Digits-only was wrong: the first real
    # owner-host run produced an identifier with an alphabetic character after
    # the prefix. Kept in step with safe_logging's pattern -- this one is
    # defence in depth for probe evidence details, and the owner-output
    # boundary remains the load-bearing check.
    (
        re.compile(
            r"\b(?:DU|DF|U)(?=[A-Za-z0-9]{5,}\b)(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{5,}\b"
        ),
        "[account]",
    ),
    (re.compile(r"\b[a-zA-Z]+://[^\s]+"), "[uri]"),
    (re.compile(r"[A-Za-z]:\\[^\s]+"), "[path]"),
    (re.compile(r"(?<![\w.])/(?:[\w.-]+/){2,}[\w.-]*"), "[path]"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "[host]"),
)


def _safe_detail(error: BaseException) -> str:
    """A diagnostic that is safe to print, or nothing.

    Exception text from a broker SDK, a database driver or the filesystem is
    written by someone else and routinely contains a DSN, a path, a hostname or
    an account number. The probe's output is read by an operator and pasted
    into issues and chats, so that text is not printed at all.

    What IS printed: the exception type always; a canonical error code when the
    exception carries one, because a code is a fixed vocabulary; and, for our
    own exceptions only, a bounded and redacted message.
    """
    parts: list[str] = []
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    if isinstance(code_value, str) and code_value.strip():
        parts.append(f"code={code_value}")
    module = type(error).__module__
    if module.startswith(_OUR_MODULES):
        detail = str(error)
        for pattern, replacement in _REDACTIONS:
            detail = pattern.sub(replacement, detail)
        detail = " ".join(detail.split())[:_MAX_DETAIL]
        if detail:
            parts.append(detail)
    return "; ".join(parts)


def _probe_failure(name: str, error: BaseException) -> str:
    """What a failed read-only observation reports, instead of raising."""
    detail = _safe_detail(error)
    suffix = f" ({detail})" if detail else ""
    return (
        f"NOT OBSERVED: the {name} check itself failed with"
        f" {type(error).__name__}{suffix}"
    )


UNKNOWN_CLASSIFICATION = "UNKNOWN"


class _SingleFlight:
    """One in-flight execution per identity, within THIS process.

    The durable history answers "did this already happen". It cannot answer "is
    this happening right now in another thread", because the first attempt has
    not written anything yet at the moment the second one reads. Two concurrent
    calls could therefore both find an empty history, both prepare, and both
    submit.

    So a claim is taken before the durable check and released after the outcome
    is recorded. Both the decision id and the client order id are claimed: two
    different order ids racing for one decision must serialise (the loser then
    meets the durable conflict check and is refused), and one order id racing
    across two decisions must serialise for the same reason.

    **Scope, stated honestly: this is a single-PROCESS guarantee.** The
    execution agent runs as one process today, and this makes that process safe.
    It does NOT provide multi-process or multi-host atomic ownership -- that
    needs a durable claim with a uniqueness constraint, which this system does
    not yet have for the canonical execution path. A second execution-agent
    process would not be protected by this, and nothing here should be read as
    claiming otherwise.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._claimed: set[str] = set()

    def claim(self, *keys: str) -> tuple[str, ...] | None:
        """Take ALL of these keys, or none of them. None means refused."""
        wanted = tuple(key for key in keys if key)
        with self._lock:
            if any(key in self._claimed for key in wanted):
                return None
            self._claimed.update(wanted)
        return wanted

    def release(self, keys: tuple[str, ...]) -> None:
        with self._lock:
            self._claimed.difference_update(keys)


#: THE registry, one per process. Every ExecutionService uses this one, so two
#: services in the same process genuinely serialise against each other instead
#: of each holding a private lock that the other cannot see.
_PROCESS_SINGLE_FLIGHT = _SingleFlight()

#: The terminal outcome of a prepared attempt the FINAL boundary refused before
#: the broker call. Distinct from a refusal by the broker: this one is proof
#: that nothing was sent, and a replay must reconstruct it as such rather than
#: as an unresolved submission the broker might be holding.
NOT_SENT_OUTCOME = "NOT_SENT"


def _plan_differences(mine: ExecutablePlan, theirs: ExecutablePlan) -> list[str]:
    """Which order facts differ, so a refusal says what was substituted.

    Diagnostic only. The refusal itself is decided by comparing ONE identity;
    this never decides anything, because a field-by-field decision could omit
    a field and a hash cannot.
    """
    return [
        field.name
        for field in dataclass_fields(mine)
        if getattr(mine, field.name) != getattr(theirs, field.name)
    ]

#: The order shape a broker MUST report for a recovered order to be recognised.
#: A field the broker leaves out is a fact we do not have, and an order we
#: cannot recognise is reconciled rather than claimed.
REQUIRED_RECOVERY_FIELDS: tuple[str, ...] = (
    "symbol", "side", "order_type", "quantity",
)


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
    #: The observation path itself failed. Not a verdict about the market or
    #: the account -- a statement that we could not form one.
    OBSERVATION_FAILED = "OBSERVATION_FAILED"
    #: This execution identity is already being executed right now. Refused
    #: rather than queued: a duplicate is not a thing to retry into.
    ALREADY_IN_FLIGHT = "ALREADY_IN_FLIGHT"
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
    #: Stable per decision: OUR idempotency key. A retry of the same
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

    order_type: OrderType = OrderType.LIMIT
    #: Part of the request identity: a changed TIF is a changed order.
    time_in_force: TimeInForce = TimeInForce.DAY
    protection_required: bool = True
    sector: str = "UNKNOWN"
    cluster: str = "UNKNOWN"
    allowed_currencies: tuple[str, ...] = ("USD",)
    allowed_exchanges: tuple[str, ...] = ("SMART",)

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
    #: The allocation the pre-submit result was sized against. Required on
    #: ALLOW: without it the service can check the authorized quantity against
    #: the RISK ceiling only, so a faulty authority could authorize a size the
    #: PORTFOLIO never allocated -- 50 shares under a risk approval of 100 and
    #: a portfolio allocation of 10 would have passed every reference check.
    allocation: CandidateAllocation | None = None


@runtime_checkable
class ExecutionAuditSink(Protocol):
    """The execution lifecycle audit. Stronger than the readiness AuditSink.

    The readiness guard needs only "can you persist, and persist this". An
    execution needs three DISTINCT lifecycle records, and the difference between
    them is load-bearing: an EVALUATED record means a decision was considered,
    a SUBMIT_PREPARED record means the broker may already hold the order.

    This was previously the generic sink plus ``getattr`` probes with a fallback
    to ``persist`` -- so a sink that could not record the point of no return
    would have silently recorded it as an ordinary evaluation, and a restart
    would then have replayed a possible live order as a routine refusal.
    Requiring the methods means mypy rejects such a sink instead.
    """

    def can_persist(self) -> bool: ...

    def persist(self, record: Mapping[str, object]) -> str: ...

    def persist_prepared(self, record: Mapping[str, object]) -> str: ...

    def persist_outcome(self, record: Mapping[str, object]) -> str: ...


@runtime_checkable
class ReconciliationGate(Protocol):
    """The canonical entry gate, typed. No ``object``, no getattr."""

    def require(self, reason: str) -> None: ...

    def entry_blocked(self) -> tuple[bool, str]: ...


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

    def records_for_order(
        self, client_order_id: str
    ) -> Sequence[ExecutionHistoryLike]: ...


@dataclass(frozen=True, slots=True)
class PlanContext:
    """The runtime order this execution intends to send, minus the size.

    Handed to the authority so it checks THE order rather than an order of its
    own construction. The authority previously received only the intent, the
    readiness report and the risk result -- so it chose its own entry price,
    stop, side, order type, account and time in force, and the service then
    built a different order from its live quote and sent that. Both looked
    correct because nothing compared them.

    The size is deliberately absent: it is what the authority decides.
    """

    decision_ref: str
    #: The risk QUESTION itself, not a reference to it. The final pre-submit
    #: check re-verifies that the risk result answers this exact request, and
    #: it cannot do that from a hash alone.
    risk_request: TradeRiskRequest
    risk_ref: str
    #: The broker-bound order id. Part of the order, so part of the seal.
    client_order_id: str
    #: The security the broker resolved for this execution. The whole object,
    #: so the authority checks WHICH security it is authorizing and not merely
    #: a ticker the feed might resolve differently.
    instrument: BrokerInstrument
    symbol: str
    side: str
    strategy_id: str
    source_id: str
    environment: Environment
    account_alias: str
    order_type: OrderType
    time_in_force: str
    entry_price: Decimal
    stop_price: Decimal | None

    def sealed_with(self, allocation_ref: str, quantity: Decimal) -> ExecutablePlan:
        """The executable plan these runtime facts make, at this size.

        The service and the authority both build the plan through this method,
        so a difference between them can only be a difference in the FACTS --
        which is exactly what the seal comparison is for.
        """
        return ExecutablePlan(
            client_order_id=self.client_order_id,
            broker_instrument_ref=self.instrument.broker_instrument_ref,
            broker_exchange=self.instrument.exchange,
            broker_currency=self.instrument.currency,
            decision_ref=self.decision_ref,
            risk_request_ref=risk_request_identity(self.risk_request),
            risk_ref=self.risk_ref,
            allocation_ref=allocation_ref,
            strategy_id=self.strategy_id,
            source_id=self.source_id,
            environment=self.environment,
            account_alias=self.account_alias,
            symbol=self.symbol,
            side=self.side,
            quantity=quantity,
            order_type=self.order_type,
            time_in_force=self.time_in_force,
            limit_price=self.entry_price,
            stop_price=self.stop_price,
        )


@runtime_checkable
class ExecutionAuthorization(Protocol):
    """The authority that permits submission. Default production answer: no."""

    def authorize(
        self,
        intent: ExecutionIntent,
        report: TradingReadinessRuntimeReport,
        risk_result: RiskResult,
        plan: PlanContext,
    ) -> AuthorizationDecision: ...

    def pre_submit_policy_identity(self) -> str:
        """The EXACT pre-submit policy this authority mints under.

        A namespace prefix proves only that the reference is a pre-submit
        identity; it says nothing about WHICH policy. A different policy with
        the same version must not be able to authorize this execution, so the
        authority states its policy and the service checks equality.
        """
        ...


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
        self,
        intent: ExecutionIntent,
        report: TradingReadinessRuntimeReport,
        risk_result: RiskResult,
        plan: PlanContext,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(allowed=False, reason=self.reason)

    def pre_submit_policy_identity(self) -> str:
        """Never reached: this authority mints nothing."""
        return "pre-submit-policy:none@deny-all"


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
    source_id: str
    symbol: str
    side: str
    environment: str
    account_alias: str
    #: What the CALLER asked for, and the order shape the configuration would
    #: build. Known before planning, so a changed request is detectable on
    #: replay without a fresh quote.
    requested_quantity: str = ""
    configured_order_type: str = ""
    configured_time_in_force: str = ""
    #: The order that was actually prepared. An id that comes back with a
    #: different quantity, price or order shape is not the same execution --
    #: it is a new one wearing an old name, and treating it as a replay would
    #: silently accept a changed order.
    order_type: str = ""
    quantity: str = ""
    limit_price: str = ""
    stop_price: str = ""
    time_in_force: str = ""
    #: The SECURITY that was prepared. Known only once the contract resolves,
    #: so it belongs with the frozen order rather than with the request
    #: identity -- but it is compared exactly as strictly, because a replay
    #: that agrees on ticker and disagrees on security is not a replay.
    broker_instrument_ref: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "decision_id": self.decision_id,
            "client_order_id": self.client_order_id,
            "strategy_id": self.strategy_id,
            "source_id": self.source_id,
            "symbol": self.symbol,
            "side": self.side,
            "environment": self.environment,
            "account_alias": self.account_alias,
            "requested_quantity": self.requested_quantity,
            "configured_order_type": self.configured_order_type,
            "configured_time_in_force": self.configured_time_in_force,
            "order_type": self.order_type,
            "quantity": self.quantity,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "time_in_force": self.time_in_force,
            "broker_instrument_ref": self.broker_instrument_ref,
        }

    #: The fields that say WHICH execution this is, and what the caller ASKED
    #: for. All are known before planning, so all are always comparable -- which
    #: matters because a replay has not planned its order yet, and a changed
    #: request must be detectable without re-reading the market.
    #: Not restated here. The durable reader validates records against the
    #: SAME contract, and two tuples that happen to match today are not a
    #: contract -- a field added here and forgotten there would mean the reader
    #: validates an identity the writer no longer writes.
    IDENTITY_FIELDS = REQUEST_IDENTITY_FIELDS
    #: The frozen order. Known only once one is planned, so compared only when
    #: BOTH sides have it -- a replay has not planned its order yet when the
    #: identity check runs.
    ORDER_FIELDS = PREPARED_ORDER_FIELDS

    def differences(self, other: ExecutionFingerprint) -> tuple[str, ...]:
        """Fields where these two describe different executions."""
        mine, theirs = self.to_dict(), other.to_dict()
        differing = [
            field for field in self.IDENTITY_FIELDS if mine[field] != theirs[field]
        ]
        differing.extend(
            field
            for field in self.ORDER_FIELDS
            if mine[field] and theirs[field] and mine[field] != theirs[field]
        )
        return tuple(differing)

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
                source_id=str(raw.get("source_id", "")),
                symbol=str(raw["symbol"]),
                side=str(raw["side"]),
                environment=str(raw["environment"]),
                account_alias=str(raw["account_alias"]),
                requested_quantity=str(raw.get("requested_quantity", "")),
                configured_order_type=str(raw.get("configured_order_type", "")),
                configured_time_in_force=str(raw.get("configured_time_in_force", "")),
                order_type=str(raw.get("order_type", "")),
                quantity=str(raw.get("quantity", "")),
                limit_price=str(raw.get("limit_price", "")),
                stop_price=str(raw.get("stop_price", "")),
                time_in_force=str(raw.get("time_in_force", "")),
                broker_instrument_ref=str(raw.get("broker_instrument_ref", "")),
            )
        except KeyError:
            return None


@dataclass
class ExecutionEvidence:
    """Independent read-only observations from one probe run.

    Deliberately mutable and deliberately not a decision: it is a diagnosis. A
    check that could not run says so, with the reason, rather than being
    silently absent or being reported as a failure of the thing it depends on.
    """

    evaluated_at: datetime
    observations: list[tuple[str, bool, str]] = field(default_factory=list)
    evaluation: ExecutionEvaluation | None = None

    def record(self, name: str, satisfied: bool, detail: str) -> None:
        self.observations.append((name, satisfied, detail))

    def lines(self) -> list[str]:
        return [
            f"  [{'OK  ' if satisfied else 'FAIL'}] {name}: {detail}"
            for name, satisfied, detail in self.observations
        ]


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
    #: The runtime order facts handed to the authority, if it was reached.
    plan_context: PlanContext | None = None
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
        audit: ExecutionAuditSink,
        risk_engine: RiskEngine,
        config: ExecutionConfig,
        entry_gate: ReconciliationGate,
        authorization: ExecutionAuthorization | None = None,
        universe: Callable[[], ConstituentSnapshot | None] | None = None,
        portfolio: Callable[[], tuple[PositionExposure, ...]] | None = None,
        history: DurableExecutionHistory,
        protection: ProtectedEntryLifecycle | None = None,
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
        # DENY unless something explicitly says otherwise, and nothing in
        # production does.
        self._authorization = authorization or DenyAllExecutionAuthorization()
        #: ONE protection authority, shared with the guard below, so the early
        #: report and the final gate can never disagree.
        self._protection = protection
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
            protection=protection,
            universe_status=universe_status,
            now=now,
        )
        # ONE gate, and no way to install a second one. The service and the
        # agent MUST hold the same instance: latching a gate the agent does not
        # consult would let POST_SUBMIT_AUDIT_FAILED promise that new entries
        # are blocked while the next entry sails straight through.
        #
        # This used to take an ``agent_factory``, which made the guarantee a
        # convention: the wiring passed the gate through, but nothing stopped a
        # caller writing ``lambda port, gate: ExecutionAgent(port,
        # entry_gate=ALWAYS_PERMIT)``, and no type could catch it. The service
        # constructs its own agent instead, so the only gate that exists is
        # this one. A clock is a clock, and is injected; a safety dependency is
        # not something a caller may replace.
        self._entry_gate = entry_gate
        self._agent = ExecutionAgent(self._guard, entry_gate=entry_gate, clock=now)
        #: Set when a submission's outcome could not be recorded. Independent of
        #: the gate, so a latch that itself fails cannot reopen entries.
        self._critical_state: str | None = None
        self._universe = universe
        #: Authoritative open-risk view when one exists; otherwise the estimate
        #: below is used and is documented as an estimate.
        self._portfolio = portfolio
        # Survives a restart, unlike the agent's in-memory outcome map, and
        # therefore MANDATORY. It used to default to None, and `_durable_verdict`
        # returned None when it was absent -- so an execution-capable service
        # could be constructed with no restart-safe idempotency at all, merely by
        # supplying an authority. A submission path whose duplicate protection
        # dies with the process is not a submission path we are willing to build.
        if history is None:
            raise ExecutionServiceError(
                "an execution service requires a durable execution history;"
                " without it a restart cannot tell a replay from a new order"
            )
        self._history: DurableExecutionHistory = history

        self._universe_status = universe_status
        #: Concurrency safety for THIS process. Deliberately the module-level
        #: registry rather than a fresh instance: two ExecutionService objects
        #: in one process used to hold different locks and different claimed
        #: sets, so they could not see each other race -- which made the code's
        #: "single-process" claim stronger than the implementation. There is now
        #: one registry per process, and no constructor parameter through which
        #: a second one could be installed.
        self._in_flight = _PROCESS_SINGLE_FLIGHT
        self._halts = halts or (lambda: ())
        self._now = now or (lambda: datetime.now(tz=UTC))

    # -- the read-only path ---------------------------------------------------

    def evaluate(self, intent: ExecutionIntent) -> ExecutionEvaluation:
        """Observe everything, submit nothing.

        Safe to run against the live owner host: the only broker calls it makes
        are reads and a what-if preview, which places nothing.
        """
        return self._observe(intent)

    def collect_evidence(self, intent: ExecutionIntent) -> ExecutionEvidence:
        """Every INDEPENDENT read-only observation, not just the first refusal.

        ``evaluate`` stops at the first blocker, which is right for a decision
        and wrong for a diagnosis: one owner-host run that stops at "no session
        P&L" tells us nothing about whether the quote feed works, whether the
        contract resolves, or whether the broker would validate the order. Those
        facts do not depend on each other, so they are gathered independently.

        Nothing here weakens a gate. A check whose dependency genuinely failed
        is reported as NOT EVALUATED, with the reason.
        """
        evidence = ExecutionEvidence(evaluated_at=self._now())

        # Every observation gets its OWN boundary. Independent facts must fail
        # independently: a quote source that raises used to abort the whole run,
        # so the owner learned nothing about the contract, the universe, the
        # portfolio or the audit sink -- exactly the facts a broken host needs.
        evidence.record("halts", *self._isolated("halts", self._halt_evidence, intent))
        # ONE account snapshot feeds BOTH the account-metadata and the
        # session-P&L records. They used to take a snapshot each -- two full
        # reqPnL subscriptions in the diagnostic phase, back to back, before
        # canonical evaluation even began. They genuinely share that one
        # observation, so they share its result (and its failure). The canonical
        # evaluation below still makes its OWN fresh observation, which is what
        # exercises the real decision path.
        account_snapshot, account_ok, account_note = self._isolated_account_snapshot()
        evidence.record("account", account_ok, account_note)
        evidence.record("session_pnl", *self._session_pnl_view(account_snapshot))

        instrument: BrokerInstrument | None = None
        try:
            instrument, contract_note = self._contract_evidence(intent)
        except Exception as error:  # noqa: BLE001 - one failed probe read
            contract_note = _probe_failure("contract", error)
        evidence.record("contract", instrument is not None, contract_note)

        quote: QuoteState | None = None
        try:
            if instrument is None:
                raise ExecutionServiceError(
                    "no resolved security to quote; a price for an unidentified"
                    " instrument is not evidence about this execution"
                )
            quote = self._quotes.current_quote(instrument)
            quote_note = self._quote_note(quote)
            quote_ok = quote.bid is not None and quote.ask is not None
        except Exception as error:  # noqa: BLE001 - one failed probe read
            quote_ok, quote_note = False, _probe_failure("quote", error)
        evidence.record("quote", quote_ok, quote_note)

        evidence.record("universe", *self._isolated("universe", self._universe_evidence, intent))
        evidence.record("portfolio", *self._isolated("portfolio", self._portfolio_evidence))
        evidence.record(
            "protection",
            *self._isolated(
                "protection", self._protection_evidence, intent, quote, instrument
            ),
        )
        evidence.record("audit", *self._isolated("audit", self._audit_evidence))

        # The full decision path, which may legitimately stop early. Its own
        # refusal reason is recorded alongside the independent observations, and
        # its failure does not discard the observations already gathered.
        try:
            evidence.evaluation = self._observe(intent)
        except Exception as error:  # noqa: BLE001 - the diagnosis survives it
            evidence.record("evaluation", False, _probe_failure("evaluation", error))
            evidence.evaluation = self._bare(
                intent,
                ExecutionStage.OBSERVATION_FAILED,
                (_probe_failure("evaluation", error),),
            )
        return evidence

    def _isolated(
        self,
        name: str,
        observe: Callable[..., tuple[bool, str]],
        *args: object,
    ) -> tuple[bool, str]:
        """One read-only observation, whose failure stays its own.

        A probe exists to report what is broken. An observation that raises IS
        the finding, so it is recorded as a failed check with its error rather
        than being allowed to end the run.
        """
        try:
            return observe(*args)
        except Exception as error:  # noqa: BLE001 - reported, not propagated
            LOGGER.warning(
                "probe observation failed", extra={"observation": name}, exc_info=True
            )
            return (False, _probe_failure(name, error))

    def execute(self, intent: ExecutionIntent) -> ExecutionEvaluation:
        """Observe, then submit ONLY if authorized and ready.

        Named so it cannot be mistaken for a probe. It still cannot submit
        without an authority that says yes, and the production authority says no.
        """
        if self._critical_state is not None:
            # A previous submission's outcome could not be recorded. Until that
            # is reconciled, no NEW entry may be attempted -- and this refusal
            # does not depend on the latch having worked.
            LOGGER.error(
                "refusing a new entry while an unreconciled critical state"
                " stands",
                extra=self._context(intent, stage="CRITICAL_STATE"),
            )
            return self._bare(
                intent,
                ExecutionStage.POST_SUBMIT_AUDIT_FAILED,
                (
                    "a previous submission's outcome could not be durably"
                    f" recorded ({self._critical_state}); new entries are"
                    " blocked until an operator reconciles. Cancellation and"
                    " exposure reduction remain available.",
                ),
            )

        # Claimed BEFORE the durable read, because the durable read is exactly
        # where a race is decided: two callers both see an empty history, both
        # prepare, both submit. Released only after the outcome is recorded.
        claim = self._in_flight.claim(
            f"decision:{intent.decision_id}", f"order:{intent.client_order_id}"
        )
        if claim is None:
            LOGGER.warning(
                "refusing a duplicate execution that is already in flight",
                extra=self._context(intent, stage="ALREADY_IN_FLIGHT"),
            )
            return self._bare(
                intent,
                ExecutionStage.ALREADY_IN_FLIGHT,
                (
                    "this execution identity is already being executed; a"
                    " duplicate is refused rather than queued, so one"
                    " authorization can never become two broker orders",
                ),
            )
        try:
            return self._execute_claimed(intent)
        finally:
            self._in_flight.release(claim)

    def _execute_claimed(self, intent: ExecutionIntent) -> ExecutionEvaluation:
        """The execution path proper, with the single-flight claim held."""
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

    # -- independent read-only observations (probe/diagnosis only) ------------

    def _halt_evidence(self, intent: ExecutionIntent) -> tuple[bool, str]:
        halt = match_halt(
            self._halts(),
            EntryContext(
                environment=self._config.environment,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                source_id=intent.source_id,
            ),
        )
        if halt is None:
            return True, "no active halt matches this entry"
        return False, f"halt active ({halt.scope.value})"

    def _isolated_account_snapshot(self) -> tuple[AccountSnapshot | None, bool, str]:
        """ONE account observation for the whole diagnostic phase.

        Returns the snapshot (or None on failure) alongside the account record.
        The session-P&L record is a VIEW of the same snapshot, so the two
        together cost one ``reqPnL``, not two. Its failure is its own finding --
        reported, never propagated -- exactly as ``_isolated`` guarantees for
        every other observation.
        """
        try:
            snapshot = self._broker.get_account_snapshot(self._config.account_alias)
        except Exception as error:  # noqa: BLE001 - reported, not propagated
            LOGGER.warning(
                "probe observation failed", extra={"observation": "account"},
                exc_info=True,
            )
            return None, False, _probe_failure("account", error)
        return snapshot, True, (
            f"alias {snapshot.account_alias} environment"
            f" {snapshot.environment.value} observed"
            f" {snapshot.observed_at.isoformat()}"
        )

    def _session_pnl_view(self, snapshot: AccountSnapshot | None) -> tuple[bool, str]:
        """The session-P&L finding, read from the ONE account snapshot above.

        No second observation and no second ``reqPnL``. UNKNOWN stays None and
        fails closed -- it is never turned into zero.
        """
        if snapshot is None:
            return False, (
                "NOT EVALUATED: the account snapshot could not be observed, so"
                " daily P&L could not be read"
            )
        if snapshot.session_pnl is not None:
            return True, f"session P&L {snapshot.session_pnl} ({SessionPnlStatus.OK})"
        # No value. Report WHICH kind of unknown, so the owner can tell a slow
        # or absent subscription from a real broker rejection -- and so nobody
        # reads "no permission" into a bounded timeout. Every one of these still
        # fails the decision closed; the halt limits cannot be evaluated and
        # UNKNOWN is not zero.
        status = snapshot.session_pnl_status
        blocked = (
            "This blocks execution: the daily halt limits cannot be evaluated"
            " and UNKNOWN is not zero"
        )
        # The adapter's safe, code-only detail (a broker error code, or the
        # bounded-timeout note) travels through the snapshot, so the owner sees
        # WHICH broker code occurred without another code change -- and it is
        # already free of account id, raw text, DSN and traceback.
        detail = f" [{snapshot.session_pnl_detail}]" if snapshot.session_pnl_detail else ""
        if status is SessionPnlStatus.TIMEOUT:
            return False, (
                f"{SessionPnlStatus.TIMEOUT}: no reqPnL update within the bounded"
                f" window (NOT a permission fact).{detail} {blocked}"
            )
        if status is SessionPnlStatus.REQUEST_ERROR:
            return False, (
                f"{SessionPnlStatus.REQUEST_ERROR}: the gateway rejected the"
                f" reqPnL subscription.{detail} {blocked}"
            )
        if status is SessionPnlStatus.UNSET:
            return False, (
                f"{SessionPnlStatus.UNSET}: reqPnL returned no usable daily"
                f" value.{detail} {blocked}"
            )
        return False, f"the gateway served no daily P&L (reqPnL).{detail} {blocked}"

    def _contract_evidence(
        self, intent: ExecutionIntent
    ) -> tuple[BrokerInstrument | None, str]:
        try:
            instrument = self._broker.resolve_instrument(
                InstrumentQuery(symbol=intent.symbol)
            )
        except Exception as error:  # noqa: BLE001
            return None, f"resolve_instrument refused: {error}"
        return instrument, (
            f"{instrument.symbol} -> {instrument.broker_instrument_ref}"
            f" ({instrument.exchange} {instrument.currency})"
        )

    @staticmethod
    def _quote_note(quote: QuoteState) -> str:
        return (
            f"bid={quote.bid} ask={quote.ask} sizes={quote.bid_size}/{quote.ask_size}"
            f" feed={quote.market_data_state.value}"
            f" observed_at={quote.observed_at.isoformat() if quote.observed_at else 'NONE'}"
            f" {quote.detail}"
        ).strip()

    def _universe_evidence(self, intent: ExecutionIntent) -> tuple[bool, str]:
        status = self._universe_status() if self._universe_status else None
        refusal = self._universe_refusal(intent, status)
        if refusal is not None:
            return False, refusal
        return True, (
            f"status {status.value}" if status else "not required by this strategy"
        )

    def _portfolio_evidence(self) -> tuple[bool, str]:
        unclassified = self._unclassified_positions()
        if not unclassified:
            source = "authority" if self._portfolio is not None else "no open positions"
            return True, f"classified exposure available ({source})"
        return False, (
            f"{len(unclassified)} open position(s) with no sector/cluster/source"
            " classification; the caps cannot be evaluated"
        )

    def _protection_evidence(
        self,
        intent: ExecutionIntent,
        quote: QuoteState | None,
        instrument: BrokerInstrument | None,
    ) -> tuple[bool, str]:
        if quote is None or instrument is None:
            return False, (
                "NOT EVALUATED: protection cannot be priced without a resolved"
                " security and a quote for it"
            )
        midpoint = quote.midpoint
        if midpoint is None or midpoint <= 0:
            return False, "NOT EVALUATED: no usable quote to price a protective order"
        stop = self._stop_price(intent, midpoint)
        if stop is None:
            return False, "no valid protective stop could be constructed"
        order = self._order_intent(
            intent, intent.requested_quantity, midpoint, stop,
            instrument.broker_instrument_ref,
        )
        capability = self._protection_state(order)
        return capability.can_protect, capability.detail

    def _audit_evidence(self) -> tuple[bool, str]:
        try:
            available = self._audit.can_persist()
        except Exception as error:  # noqa: BLE001
            return False, f"audit sink raised {type(error).__name__}"
        return available, (
            "audit sink is available" if available else "audit sink cannot persist"
        )

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

        # The security, not the ticker: the contract is resolved above, and
        # the price this decision is built from must belong to THAT security.
        quote = self._quotes.current_quote(instrument)
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

        # The positions observation can fail against a real broker (the owner
        # run timed out here). That is an EXPECTED safety-observation failure,
        # so it is handled HERE, fail-closed, preserving the facts already
        # observed -- instrument, quote, entry/stop price -- and audited like
        # any other refusal. Letting it escape to collect_evidence's last-resort
        # handler discarded the contract and quote from the owner summary and
        # skipped the durable audit. A safe reason names the observation class
        # and the broker error TYPE only, never the account id or raw text.
        try:
            unclassified = self._unclassified_positions()
        except BrokerPortError as error:
            return self._refuse(
                intent, ExecutionStage.OBSERVATION_FAILED,
                f"the positions observation could not be completed"
                f" ({error.code.value}); cannot evaluate portfolio risk caps",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )
        except Exception as error:  # noqa: BLE001 - fail closed, keep the facts
            return self._refuse(
                intent, ExecutionStage.OBSERVATION_FAILED,
                f"the positions observation raised {type(error).__name__};"
                " cannot evaluate portfolio risk caps",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )
        if unclassified:
            return self._refuse(
                intent, ExecutionStage.PORTFOLIO_UNCLASSIFIED,
                f"{len(unclassified)} open position(s) have no sector/cluster/source"
                " classification, so the sector, cluster and source risk caps"
                " cannot be evaluated; supply a portfolio authority",
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )

        try:
            risk_request, risk_result = self._evaluate_risk(
                intent, entry_price, stop_price
            )
        except IncompleteAccountStateError as error:
            return self._refuse(
                intent, ExecutionStage.ACCOUNT_STATE_INCOMPLETE, str(error),
                instrument=instrument, quote=quote, entry_price=entry_price,
                stop_price=stop_price, universe_status=universe_status,
            )
        except (RiskEngineError, BrokerPortError, ExecutionServiceError) as error:
            # ExecutionServiceError covers the integrity check below: an engine
            # that answers a different question than the one asked has not
            # evaluated this execution, and that refuses rather than escaping.
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

        order_intent = self._order_intent(
            intent, approved, entry_price, stop_price,
            instrument.broker_instrument_ref,
        )

        # Ask the protection authority about the ACTUAL order, not a sketch of
        # one: whether protection can be established depends on the order.
        protection = self._protection_state(order_intent)
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

        # Readiness is computed here through the SAME guard the agent will use,
        # so the report an operator reads is produced by the enforcement path
        # rather than by a parallel re-implementation of it.
        report = self._guard.evaluate_readiness(order_intent)
        LOGGER.info(
            "readiness result",
            extra=self._context(intent, stage="READINESS", readiness=report.readiness.value),
        )

        # The authority is handed the risk result it must authorize AGAINST,
        # so an authorization can be minted about this exact evaluation rather
        # than about a decision in the abstract.
        plan_context = PlanContext(
            client_order_id=intent.client_order_id,
            instrument=instrument,
            decision_ref=decision_identity(intent.decision),
            risk_request=risk_request,
            risk_ref=risk_result_identity(risk_result),
            symbol=intent.symbol,
            side=intent.side.value,
            strategy_id=intent.strategy_id,
            source_id=intent.source_id,
            environment=self._config.environment,
            account_alias=self._config.account_alias,
            order_type=self._config.order_type,
            time_in_force=self._config.time_in_force.value,
            entry_price=entry_price,
            stop_price=stop_price,
        )
        try:
            authorization = self._authorization.authorize(
                intent, report, risk_result, plan_context
            )
        except Exception as error:  # noqa: BLE001 - an authority that fails denies
            # An authority that raises has not authorized anything. The canonical
            # producers refuse a mismatched lineage by raising -- a risk result
            # for another symbol or environment, say -- and that refusal must
            # arrive here as a denial, not as an unhandled exception escaping the
            # service with the decision half-evaluated.
            LOGGER.error(
                "the authorization authority raised; treating as DENIED",
                extra=self._context(
                    intent, stage="AUTHORIZATION", error=type(error).__name__
                ),
            )
            authorization = AuthorizationDecision(
                allowed=False,
                reason=(
                    "the authorization authority raised"
                    f" {type(error).__name__}; an authority that cannot answer"
                    " has not authorized anything"
                ),
            )
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
            # The EXACT facts the authority was handed. Carried rather than
            # rebuilt, so the seal check compares against what was actually
            # asked for and not against a second derivation of it.
            plan_context=plan_context,
        )
        return self._audit_evaluation(evaluation)

    def _fingerprint(
        self, intent: ExecutionIntent, order: OrderIntent | None = None
    ) -> ExecutionFingerprint:
        """The execution identity, including the order once one exists.

        Before an order is planned this identifies the decision; at
        SUBMIT_PREPARED it also carries the frozen order, which is what makes a
        changed quantity or price a conflict rather than a replay. Numbers are
        normalised through ``str`` so equal values compare equal regardless of
        Decimal exponent.
        """
        return ExecutionFingerprint(
            decision_id=intent.decision_id,
            client_order_id=intent.client_order_id,
            strategy_id=intent.strategy_id,
            source_id=intent.source_id,
            symbol=intent.symbol,
            side=intent.side.value,
            environment=self._config.environment.value,
            account_alias=self._config.account_alias,
            requested_quantity=_normal(intent.requested_quantity),
            configured_order_type=self._config.order_type.value,
            configured_time_in_force=self._config.time_in_force.value,
            order_type=order.order_type.value if order else "",
            quantity=_normal(order.quantity) if order else "",
            limit_price=_normal(order.limit_price) if order else "",
            stop_price=_normal(order.stop_price) if order else "",
            time_in_force=order.time_in_force.value if order else "",
            broker_instrument_ref=order.broker_instrument_ref if order else "",
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
        try:
            records = tuple(self._history.records_for(intent.decision_id))
            # ALSO by order id: a client order id reused by a DIFFERENT decision
            # after a restart is invisible from the new decision's side, and the
            # collision would only surface at the broker.
            records += tuple(
                self._history.records_for_order(intent.client_order_id)
            )
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
            recovered = self._recover_from_broker(intent, records)
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
            if stored is None:
                continue
            differing = stored.differences(fingerprint)
            if not differing:
                continue
            return (
                "a durable record describes a different execution under the"
                f" same identity (differs in {list(differing)}); refusing"
                " rather than treating two executions as one"
            )
        return None

    def _prepared_order_conflict(
        self, intent: ExecutionIntent, prepared: ExecutionFingerprint
    ) -> str | None:
        """Was a DIFFERENT order already prepared under this identity?"""
        try:
            records = tuple(self._history.records_for(intent.decision_id))
            records += tuple(self._history.records_for_order(intent.client_order_id))
        except Exception:  # noqa: BLE001 - handled by the earlier verdict
            return None
        for record in records:
            if record.action not in AMBIGUOUS_ACTIONS:
                continue
            stored = ExecutionFingerprint.from_dict(record.payload)
            if stored is None:
                continue
            differing = stored.differences(prepared)
            if differing:
                return (
                    "a different order was already prepared under this identity"
                    f" (differs in {list(differing)}); refusing to treat a"
                    " changed order as the same execution"
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
        self, intent: ExecutionIntent, records: Sequence[ExecutionHistoryLike]
    ) -> ExecutionEvaluation | None:
        """Ask the broker whether it already holds the order we prepared.

        An ambiguous durable state must not stay an abstract database condition
        when the broker can answer it. This uses the adapter's OWN same-client
        recovery -- the canonical mechanism, statically typed on the port -- and
        never submits anything.

        Identity is proven from what the BROKER reports, compared to the frozen
        order in the durable SUBMIT_PREPARED record. The previous version
        compared correlation ids, which the broker never stored: the real
        adapter therefore could not have satisfied it, and only a test double
        fabricating the original correlation ever did.
        """
        prepared = self._prepared_fingerprint(records)
        try:
            working = self._broker.recover_working_orders(self._config.account_alias)
        except Exception as error:  # noqa: BLE001 - unknown stays unknown
            LOGGER.warning(
                "broker recovery failed; the state remains ambiguous",
                extra=self._context(
                    intent, stage="RECOVERY_FAILED", error=type(error).__name__
                ),
            )
            return None
        for candidate in working:
            if candidate.client_order_id != intent.client_order_id:
                continue
            if prepared is None:
                return self._bare(
                    intent,
                    ExecutionStage.NEEDS_RECONCILIATION,
                    (
                        "the broker holds a working order with this order id but"
                        " no prepared-order record describes what we sent;"
                        " reconcile rather than claim it",
                    ),
                )
            differing = self._broker_order_differences(candidate, prepared)
            if differing:
                LOGGER.warning(
                    "a working order shares this order id but not its shape",
                    extra=self._context(
                        intent, stage="RECOVERY_IDENTITY_MISMATCH"
                    ),
                )
                return self._bare(
                    intent,
                    ExecutionStage.NEEDS_RECONCILIATION,
                    (
                        "the broker holds a working order with this order id but"
                        f" a different shape (differs in {list(differing)}); the"
                        " identity cannot be proven, so this is reconciled, not"
                        " claimed",
                    ),
                )
            LOGGER.info(
                "the broker still holds this order; recovered without resubmitting",
                extra=self._context(intent, stage="RECOVERED"),
            )
            return self._bare(
                intent,
                ExecutionStage.SUBMITTED,
                (
                    "the broker already holds a working order matching the"
                    " prepared execution; recovered rather than duplicated",
                ),
            )
        LOGGER.info(
            "the broker holds no working order with this identity",
            extra=self._context(intent, stage="RECOVERY_NO_MATCH"),
        )
        return None

    @staticmethod
    def _prepared_fingerprint(
        records: Sequence[ExecutionHistoryLike],
    ) -> ExecutionFingerprint | None:
        """The frozen order from the durable point-of-no-return record."""
        for record in records:
            if record.action not in AMBIGUOUS_ACTIONS:
                continue
            stored = ExecutionFingerprint.from_dict(record.payload)
            if stored is not None:
                return stored
        return None

    @staticmethod
    def _broker_order_differences(
        candidate: RecoveredOrder, prepared: ExecutionFingerprint
    ) -> tuple[str, ...]:
        """Where the broker's working order differs from what we prepared.

        Only facts the broker actually returned are compared. Time in force is
        not among them -- the recovery feed does not report it -- and comparing
        against a value the broker never gave would make recovery impossible
        rather than safe.

        The core shape must be reported, though: an order whose symbol, side,
        quantity or type the broker did not state is an order we cannot
        recognise, and that is reported as a difference rather than passed over.
        """
        observed = {
            "symbol": candidate.symbol,
            # The broker's own security identity, when it reported one. If it
            # did not, this is empty and is skipped below -- absent is absent.
            "broker_instrument_ref": candidate.broker_instrument_ref,
            "side": candidate.side.value,
            "order_type": candidate.order_type.value,
            "quantity": _normal(candidate.quantity),
            "limit_price": _normal(candidate.limit_price),
            "stop_price": _normal(candidate.stop_price),
            "account_alias": candidate.account_alias,
        }
        expected = prepared.to_dict()
        differing = [
            field
            for field in REQUIRED_RECOVERY_FIELDS
            if not observed[field] or observed[field] != expected.get(field, "")
        ]
        differing.extend(
            field
            for field, value in observed.items()
            if field not in REQUIRED_RECOVERY_FIELDS
            and value
            and expected.get(field, "")
            and value != expected[field]
        )
        return tuple(differing)

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

        # The two objects must be ONE authorization, not two substitutable ones:
        # a result approving 1 presented with an authorization for 10 would
        # otherwise be checked as 1 and submitted as 10.
        sealed = authorization_matches(
            authorization.pre_submit, authorization.authorization
        )
        if sealed is not None:
            return self._replace_stage(
                evaluation, ExecutionStage.AUTHORIZATION_DENIED, (sealed,)
            )

        binding = self._authorization_binding(
            intent, authorization.pre_submit, evaluation.risk_result
        )
        if binding is not None:
            return self._replace_stage(
                evaluation, ExecutionStage.AUTHORIZATION_DENIED, (binding,)
            )

        # THE seal. The service builds the plan it intends to execute from
        # its OWN runtime facts and requires it to be, bit for bit, the plan the
        # pre-submit check approved. Anything the service would have varied on
        # its own -- a different entry from a moved quote, a different stop,
        # side, order type, time in force, symbol, account or environment --
        # changes this identity and refuses here, before anything is prepared.
        context = evaluation.plan_context
        if context is None:
            return self._replace_stage(
                evaluation,
                ExecutionStage.AUTHORIZATION_DENIED,
                (
                    "no plan context was recorded for this evaluation, so the"
                    " authorized order cannot be compared to the order this"
                    " execution would send",
                ),
            )
        sealed = self._plan_seal_failure(
            intent, evaluation, context, authorization
        )
        if sealed is not None:
            return self._replace_stage(
                evaluation, ExecutionStage.AUTHORIZATION_DENIED, (sealed,)
            )
        assert authorization.pre_submit.authorized_plan is not None  # noqa: S101
        plan = authorization.pre_submit.authorized_plan
        request_order = self._order_intent_for_plan(intent, plan)
        request = ExecutionRequest(
            correlation_id=intent.correlation_id,
            pre_submit=authorization.pre_submit,
            authorization=authorization.authorization,
            # The SEALED plan itself, not a copy of its fields. There is
            # nothing left for the agent to source from anywhere else.
            plan=plan,
        )
        # THE point of no return. Committed before the submission path is
        # entered, so a crash between here and an outcome means exactly one
        # thing: the broker may hold this order. An ordinary evaluation record
        # must never mean that, which is why this is a distinct action.
        prepared_fingerprint = self._fingerprint(intent, request_order)
        # Now that the order exists, compare it against any order this
        # execution previously prepared. Same id with a different quantity,
        # price or order shape is a NEW execution wearing an old name.
        order_conflict = self._prepared_order_conflict(intent, prepared_fingerprint)
        if order_conflict is not None:
            return self._replace_stage(
                evaluation, ExecutionStage.IDEMPOTENCY_CONFLICT, (order_conflict,)
            )

        prepared = self._persist_prepared(
            _with(evaluation, fingerprint=prepared_fingerprint)
        )
        if prepared is not None:
            return prepared

        LOGGER.info("submit attempt", extra=self._context(intent, stage="SUBMIT"))
        try:
            outcome = self._agent.process(request, active_halts=self._halts())
        except ReadinessBlockedError as error:
            # The guard's just-in-time check refused at the boundary, after this
            # service had already seen a satisfied report. That is the check
            # doing its job: the market moved.
            #
            # It also PROVES the order was never sent -- the guard raised before
            # the broker call. So the durable lifecycle must be resolved, not
            # left as a prepared attempt with no outcome: after a restart, a
            # bare SUBMIT_PREPARED means "the broker may hold this order", and
            # reporting an order we know was never sent as an ambiguous incident
            # is a false one.
            return self._resolve_not_sent(
                _with(evaluation, fingerprint=prepared_fingerprint),
                error.report.blockers,
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
            plan_context=context,
            # The SAME full prepared-order identity the SUBMIT_PREPARED marker
            # carried. Recomputing the pre-plan fingerprint here would have
            # dropped the frozen order from the terminal record, so the durable
            # lifecycle would not have preserved one identity end to end.
            fingerprint=prepared_fingerprint,
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
    def entry_gate(self) -> ReconciliationGate:
        """The ONE gate this service and its agent share."""
        return self._entry_gate

    @property
    def critical_state(self) -> str | None:
        """Set when a submission's outcome could not be recorded."""
        return self._critical_state

    @property
    def guarded_broker(self) -> ReadinessGuardedBroker:
        """The guarded port the agent submits through. Exposed for inspection."""
        return self._guard

    def _plan_seal_failure(
        self,
        intent: ExecutionIntent,
        evaluation: ExecutionEvaluation,
        context: PlanContext,
        authorization: AuthorizationDecision,
    ) -> str | None:
        """Is the authorized plan the plan this service is about to run?"""
        pre_submit = authorization.pre_submit
        assert pre_submit is not None  # noqa: S101 - checked by the caller
        plan = pre_submit.authorized_plan
        if plan is None or not pre_submit.plan_ref:
            return (
                "the authorization sealed no executable plan; a quantity"
                " without an order is not an authorization to send one"
            )
        if plan_identity(plan) != pre_submit.plan_ref:
            return (
                "the sealed plan does not match its own identity"
                f" ({pre_submit.plan_ref!r}); the authorization was altered"
                " after it was minted"
            )
        token = authorization.authorization
        if token is None or token.peek().plan_ref != pre_submit.plan_ref:
            return (
                "the single-use token authorizes a different plan than the"
                " pre-submit result checked"
            )

        allocation = authorization.allocation
        if allocation is None:
            return (
                "the authorization carried no portfolio allocation, so the"
                " authorized size cannot be checked against the size the"
                " portfolio actually allocated"
            )
        expected_allocation = allocation_identity(allocation)
        if pre_submit.allocation_ref != expected_allocation:
            return (
                "the authorization was sized against a different allocation"
                f" ({pre_submit.allocation_ref!r} != {expected_allocation!r})"
            )
        canonical_decision = decision_identity(intent.decision)
        if allocation.decision_ref != canonical_decision:
            return (
                f"the allocation belongs to decision {allocation.decision_ref!r},"
                f" not to {canonical_decision!r}"
            )
        assert evaluation.risk_result is not None  # noqa: S101
        expected_risk = risk_result_identity(evaluation.risk_result)
        if allocation.risk_ref != expected_risk:
            return (
                "the allocation was sized against a different risk evaluation"
                f" ({allocation.risk_ref!r} != {expected_risk!r})"
            )
        if pre_submit.authorized_quantity > allocation.allocated_quantity:
            return (
                f"the authorization permits {pre_submit.authorized_quantity} but"
                f" the portfolio allocated {allocation.allocated_quantity}; a"
                " risk ceiling is not a portfolio allocation, and an authority"
                " may reduce, never raise"
            )

        # The comparison that closes the substitution: OUR plan, built from the
        # runtime facts this service would actually use, against THEIR seal.
        mine = context.sealed_with(
            expected_allocation, pre_submit.authorized_quantity
        )
        if plan_identity(mine) != pre_submit.plan_ref:
            differing = _plan_differences(mine, plan)
            return (
                "the authorized plan is not the plan this execution would send"
                f" (differs in {differing}); an authorization for one order is"
                " not an authorization for another"
            )
        return None

    def _order_intent_for_plan(
        self, intent: ExecutionIntent, plan: ExecutablePlan
    ) -> OrderIntent:
        """The prepared order, read from the sealed plan and nowhere else."""
        return self._order_intent(
            intent,
            plan.quantity,
            plan.limit_price or Decimal(0),
            plan.stop_price,
            plan.broker_instrument_ref,
        )

    def _order_intent(
        self,
        intent: ExecutionIntent,
        quantity: Decimal,
        entry_price: Decimal,
        stop_price: Decimal | None,
        broker_instrument_ref: str,
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
            broker_instrument_ref=broker_instrument_ref,
            side=intent.side,
            order_type=self._config.order_type,
            quantity=quantity,
            limit_price=entry_price,
            stop_price=stop_price,
            time_in_force=self._config.time_in_force,
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
    ) -> tuple[TradeRiskRequest, RiskResult]:
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
        result = self._risk_engine.evaluate(
            intent.decision,
            request,
            account,
            active_halts=self._halts(),
            as_of=self._now(),
        )
        # The engine must answer THE question this execution asked. A risk
        # result is a set of output numbers; two materially different requests
        # can produce identical ones, so without this an evaluation of another
        # symbol, source or size could pass for this one on matching figures.
        expected = risk_request_identity(request)
        if result.request_ref != expected:
            raise ExecutionServiceError(
                "the risk engine returned a result for a different risk request"
                f" ({result.request_ref!r} != {expected!r})"
            )
        return request, result

    def _protection_state(self, order: OrderIntent) -> BrokerProtectionCapability:
        """The SAME authority the final guard uses. Never a capability flag.

        This used to read ``bracket``/``oco`` capabilities, which recreated the
        capability-flag inference one layer earlier than the guard and gave the
        system two different answers to one question. There is now one
        authority: the ``ProtectedEntryLifecycle``, which must name the
        protective orders it established.

        No lifecycle wired means protection cannot be established or proven, so
        a protection-required entry is refused -- and the early report and the
        final gate agree, because they are asking the same object.
        """
        if self._protection is None:
            return BrokerProtectionCapability(
                False,
                "no protected-entry lifecycle is wired, so broker-side"
                " protection cannot be established or proven; a supported STOP"
                " order type is not a resting protective order, and submitting"
                " an unprotected entry is refused",
            )
        try:
            evidence = self._protection.protection_for(order)
        except Exception as error:  # noqa: BLE001 - unproven is unprotected
            return BrokerProtectionCapability(
                False, f"protection lifecycle raised {type(error).__name__}"
            )
        proven = bool(evidence.established and evidence.protective_order_refs)
        return BrokerProtectionCapability(
            proven,
            evidence.detail
            or (
                f"protection via {evidence.mechanism}:"
                f" {list(evidence.protective_order_refs)}"
                if proven
                else "the lifecycle established no broker-resident protection"
            ),
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

        The open-risk AMOUNT is no longer estimated either. The port carries no
        protective stop for an existing position, so its true open risk is
        genuinely unknown, and converting unknown protection into an assumed
        stop is the same class of fabrication as an assumed session P&L. An
        account with positions and no exposure authority therefore refuses new
        entries; an account with NO positions has a known-zero open risk and
        trades normally.

        The estimate model is not gone, it is out of the execution path: it
        belongs to research and simulation, where being wrong costs nothing.
        """
        if self._portfolio is not None:
            return self._portfolio()
        # No authority, so the only honest answer is "nothing known". A real
        # position reaching here is refused earlier by the classification gate;
        # an EMPTY portfolio is genuinely zero open risk and is fine.
        return ()

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

        # The DECISION: both producers now emit the same canonical reference, so
        # this compares like with like. It previously compared the pre-submit
        # check's bare decision hash against the risk engine's id@hash form, and
        # would have rejected every real authorization.
        canonical_decision = decision_identity(intent.decision)
        for name, actual in (
            ("pre-submit", pre_submit.decision_ref),
            ("risk result", risk_result.decision_ref),
        ):
            if actual != canonical_decision:
                return (
                    f"the {name} names decision {actual!r} but this execution is"
                    f" for {canonical_decision!r}; something was authorized, but"
                    " not this"
                )

        # The RISK RESULT itself, not its outcome. Every approving evaluation
        # shares an outcome, so an outcome could never prove WHICH evaluation
        # was authorized.
        expected_risk = risk_result_identity(risk_result)
        if pre_submit.risk_ref != expected_risk:
            return (
                "the authorization was minted against a different risk"
                f" evaluation ({pre_submit.risk_ref!r} != {expected_risk!r})"
            )

        # The RISK POLICY is verified against the risk result, NOT against the
        # pre-submit result: `pre_submit.policy_ref` names the PRE-SUBMIT
        # policy, which is a different fact governing different checks, and
        # comparing the two is what made a valid authorization look invalid.
        expected_policy = risk_policy_identity(self._config.risk_policy)
        if risk_result.policy_ref != expected_policy:
            return (
                "this risk result was produced under a different risk policy"
                f" ({risk_result.policy_ref!r} != {expected_policy!r})"
            )
        expected_pre_submit = self._authorization.pre_submit_policy_identity()
        if pre_submit.policy_ref != expected_pre_submit:
            return (
                "the authorization was minted under a different pre-submit"
                f" policy ({pre_submit.policy_ref!r} !="
                f" {expected_pre_submit!r}); a policy sharing a version is"
                " still a different policy"
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
        try:
            self._audit.persist_prepared(record)
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

    def _resolve_not_sent(
        self, evaluation: ExecutionEvaluation, blockers: Sequence[str]
    ) -> ExecutionEvaluation:
        """Durably close a prepared attempt the final guard proved never went.

        Appended BESIDE the SUBMIT_PREPARED marker, never over it, and carrying
        the same full prepared fingerprint -- so a replay reconstructs "refused
        at the boundary, nothing sent" rather than "possibly live, reconcile".

        If this record cannot be committed we fail closed: after a restart the
        durable store could not prove the not-sent fact, and a fact only this
        process knows is not a durable fact.
        """
        resolved = self._replace_stage(
            evaluation,
            ExecutionStage.READINESS_BLOCKED,
            (
                "the final readiness boundary refused BEFORE the broker call;"
                " this order was never sent",
                *blockers,
            ),
        )
        record = dict(resolved.to_dict())
        record["checkpoint"] = "POST_SUBMIT"
        # Terminal, and explicitly not a submission: no receipt, no broker
        # reference, and an outcome string a replay can reconstruct.
        record["submission_outcome"] = NOT_SENT_OUTCOME
        record["submission_reasons"] = list(resolved.reasons)
        record["broker_order_ref"] = None
        try:
            self._audit.persist_outcome(record)
        except Exception as error:  # noqa: BLE001 - unproven is unresolved
            self._require_reconciliation(
                "a prepared submission was refused at the final boundary and the"
                " not-sent resolution could not be durably recorded"
            )
            return self._replace_stage(
                resolved,
                ExecutionStage.POST_SUBMIT_AUDIT_FAILED,
                (
                    "the final boundary refused before the broker call, and that"
                    f" resolution could NOT be durably recorded ({type(error).__name__});"
                    " a restart cannot prove this order was never sent, so it is"
                    " treated as unresolved and new entries are latched",
                    *resolved.reasons,
                ),
            )
        return resolved

    def _persist_outcome(self, evaluation: ExecutionEvaluation) -> bool:
        """Record the broker's answer. False means it could not be recorded."""
        record = dict(evaluation.to_dict())
        record["checkpoint"] = "POST_SUBMIT"
        try:
            self._audit.persist_outcome(record)
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
        """Latch new entries shut through the canonical gate the agent uses.

        The service's own critical state is set FIRST and independently: a
        latch that throws must not be able to leave entries open, and logging
        the failure is not the same as blocking. Cancellation and exposure
        reduction are deliberately not gated by either.
        """
        self._critical_state = reason
        try:
            self._entry_gate.require(reason)
        except Exception as error:  # noqa: BLE001
            LOGGER.error(
                "the reconciliation latch itself failed (%s) after: %s;"
                " new entries remain blocked by the service's own critical"
                " state",
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


def _normal(value: Decimal | None) -> str:
    """A canonical number string: 1, 1.0 and 1.00 must not look different."""
    if value is None:
        return ""
    return format(value.normalize(), "f")


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
    "ExecutionEvidence",
    "ExecutionIntent",
    "ExecutionService",
    "ExecutionServiceError",
    "ExecutionStage",
]
