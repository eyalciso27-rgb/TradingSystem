"""Execution Agent (IMP-1003) — the sole consumer of SubmitAuthorization.

Authority: System_Functional 40.4 (only the Execution Agent may consume the
one-time SubmitAuthorization), Integration 9.2 (separate runtime; only it
touches the broker adapter; no new orders after disconnect until successful
reconciliation), Security spec 23.2 (halt checked at queue consumption and
before submit; a halted queued intent becomes CANCELLED_BY_POLICY),
Decision_Contract 12.5 (UNKNOWN broker state is resolved only by
reconciliation). Task DoD: **no blind retry on unknown state**.

The agent consumes queue items (``ExecutionRequest``: the final pre-submit
result + the single-use authorization + the frozen order parameters) and
talks to the broker ONLY through the ``BrokerPort`` boundary — never the
SDK. In development and CI the port is always a fake; no real broker, no
LIVE, no real PAPER_AUTO (ADR-0014 — running against the real paper gateway
is the separately-approved POI-IBKR-001 experiment).

Gate order for every submit (all fail closed):

1. idempotency — a ``client_order_id`` that was already processed returns
   the ORIGINAL outcome; nothing is re-sent and nothing is re-consumed;
2. pre-submit action must be ALLOW/RESIZE with a minted authorization —
   CANCEL/NEEDS_REVIEW is refused (the agent can never resurrect it);
3. kill/halt check at queue consumption (Security 23.2) — a matching halt
   refuses the entry as CANCELLED_BY_POLICY;
4. environment must be PAPER (LIVE is refused here too);
5. broker health — unhealthy refuses, nothing is sent;
6. the single-use authorization is consumed exactly once — an
   already-consumed authorization refuses (never a second submit);
7. quantity sent is EXACTLY the authorized quantity — the agent never
   resizes upward and never edits the plan;
8. broker errors map to terminal outcomes: UNKNOWN_BROKER_RESPONSE ⇒
   NEEDS_REVIEW (reconcile before anything else — never a blind retry);
   everything else ⇒ FAILED (also terminal — no automatic retry).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from trading_application.emergency import EntryCheckpoint, EntryContext, HaltRecord, match_halt
from trading_application.execution_identity import (
    authorization_identity,
    plan_identity,
)
from trading_application.execution_plan import ExecutablePlan
from trading_application.pre_submit_check import (
    AuthorizationConsumedError,
    PreSubmitAction,
    PreSubmitCheckResult,
    SingleUseAuthorization,
    authorization_matches,
)
from trading_broker_core.port import (
    BrokerErrorCode,
    BrokerOrderReceipt,
    BrokerPort,
    BrokerPortError,
    CancelOrderRequest,
    OrderIntent,
    OrderSide,
    ProtectiveBracket,
    TimeInForce,
)
from trading_contracts.enums import Environment, OrderType

from trading_execution_agent.reconciliation_latch import EntryGate

AGENT_VERSION = "execution-agent-1.0.0"


class ExecutionStatus(StrEnum):
    """Terminal outcome of processing one execution request."""

    SUBMITTED = "SUBMITTED"
    REFUSED = "REFUSED"
    CANCELLED_BY_POLICY = "CANCELLED_BY_POLICY"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCEL_SENT = "CANCEL_SENT"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """One queue item: the SEALED plan, its pre-submit result and its token.

    The order shape used to live here as separate fields — symbol, side, order
    type, prices, time in force — beside a pre-submit result that had proved a
    decision, a risk result, an allocation and a quantity but nothing about the
    order itself. So a queue item could pair a genuine authorization with a
    different order, and every reference would still line up.

    There is now exactly one order in this request: ``plan``. Everything the
    agent sends is read from it, and the plan carries the identity the
    pre-submit check sealed, so an order that differs anywhere is an order that
    was never authorized.
    """

    correlation_id: str
    pre_submit: PreSubmitCheckResult
    authorization: SingleUseAuthorization | None
    #: THE order. Not "some order parameters that accompany an authorization".
    plan: ExecutablePlan

    def __post_init__(self) -> None:
        if not self.correlation_id.strip():
            raise ValueError("correlation_id is mandatory")

    # -- the order shape, read from the sealed plan and nowhere else ---------
    # Properties rather than fields on purpose: a field could be set to
    # something the plan does not say, and that is precisely the substitution
    # this class exists to make impossible.

    @property
    def client_order_id(self) -> str:
        """OUR idempotency key, from the sealed plan.

        Ours, not the broker's: IBKR does not deduplicate on ``orderRef``, so
        two orders carrying the same one are two orders. Duplicate protection
        comes from the durable execution history and the agent's own outcome
        map -- which is precisely why a settable field here was dangerous. One
        authorized plan sent under two ids would have become two live orders,
        with nothing at the broker to stop it.
        """
        return self.plan.client_order_id

    @property
    def environment(self) -> Environment:
        return self.plan.environment

    @property
    def account_alias(self) -> str:
        return self.plan.account_alias

    @property
    def symbol(self) -> str:
        return self.plan.symbol

    @property
    def broker_instrument_ref(self) -> str:
        """The sealed security. A ticker is not a security identity."""
        return self.plan.broker_instrument_ref

    @property
    def side(self) -> OrderSide:
        return OrderSide(self.plan.side)

    @property
    def order_type(self) -> OrderType:
        return self.plan.order_type

    @property
    def quantity(self) -> Decimal:
        return self.plan.quantity

    @property
    def limit_price(self) -> Decimal | None:
        return self.plan.limit_price

    @property
    def stop_price(self) -> Decimal | None:
        return self.plan.stop_price

    @property
    def strategy_id(self) -> str:
        return self.plan.strategy_id

    @property
    def source_id(self) -> str:
        return self.plan.source_id

    @property
    def time_in_force(self) -> TimeInForce:
        return TimeInForce(self.plan.time_in_force)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Terminal, audit-ready record of what the agent did (or refused)."""

    client_order_id: str
    status: ExecutionStatus
    reason_codes: tuple[str, ...]
    receipt: BrokerOrderReceipt | None
    authorization_ref: str
    decision_ref: str
    risk_ref: str
    allocation_ref: str
    policy_ref: str
    correlation_id: str
    agent_version: str
    processed_at: datetime

    def __post_init__(self) -> None:
        if self.processed_at.tzinfo is None:
            raise ValueError("processed_at must be timezone-aware")


class ExecutionAgent:
    """Consumes execution requests; the only component that may submit."""

    def __init__(
        self,
        broker: BrokerPort,
        *,
        entry_gate: EntryGate,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._clock = clock or (lambda: datetime.now(tz=UTC))
        # Final-boundary reconciliation latch. REQUIRED — the boundary must not
        # be opt-in by accident: production/owner-host paths pass
        # ReconciliationLatch(); tests/non-runtime contexts pass ALWAYS_PERMIT
        # explicitly. A frozen pre-crash authorization must never submit into a
        # freshly recovered broker session; entry is blocked until a
        # CURRENT-session account-safety CLEAN clears it. Only NEW entries are
        # gated — cancel() and protective paths are not.
        self._entry_gate = entry_gate
        self._outcomes: dict[str, ExecutionOutcome] = {}

    def process(
        self,
        request: ExecutionRequest,
        *,
        active_halts: Sequence[HaltRecord] = (),
    ) -> ExecutionOutcome:
        """Run the full gate path for one queued request (idempotent)."""
        existing = self._outcomes.get(request.client_order_id)
        if existing is not None:
            # Idempotency: same queue item delivered twice returns the
            # original outcome; nothing is re-sent, nothing re-consumed.
            return existing

        outcome = self._process_new(request, active_halts)
        self._outcomes[request.client_order_id] = outcome
        return outcome

    def outcome_for(self, client_order_id: str) -> ExecutionOutcome | None:
        """The terminal outcome already recorded for this order, if any.

        Read-only view of the idempotency map. Callers use it to recognise a
        retry BEFORE re-doing work, which matters because re-planning a decision
        that already reached the broker means re-previewing an order id the
        broker has already seen -- and the broker rightly refuses that when the
        authorization differs.
        """
        return self._outcomes.get(client_order_id)

    def cancel(self, client_order_id: str, *, correlation_id: str) -> ExecutionOutcome:
        """Cancel a previously submitted order via the port (fail closed)."""
        submitted = self._outcomes.get(client_order_id)
        if submitted is None or submitted.receipt is None:
            return self._record_free_outcome(
                client_order_id,
                correlation_id,
                ExecutionStatus.REFUSED,
                ("CANCEL_UNKNOWN_ORDER",),
            )
        try:
            receipt = self._broker.cancel_order(
                CancelOrderRequest(client_order_id=client_order_id, correlation_id=correlation_id)
            )
        except BrokerPortError as error:
            status = (
                ExecutionStatus.NEEDS_REVIEW
                if error.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE
                else ExecutionStatus.FAILED
            )
            # Terminal either way: no blind retry; UNKNOWN goes to
            # reconciliation, not to a second cancel.
            return self._record_free_outcome(
                client_order_id, correlation_id, status, (f"CANCEL_{error.code.value}",)
            )
        return ExecutionOutcome(
            client_order_id=client_order_id,
            status=ExecutionStatus.CANCEL_SENT,
            reason_codes=("CANCEL_ACCEPTED",),
            receipt=BrokerOrderReceipt(
                client_order_id=receipt.client_order_id,
                broker_order_ref=receipt.broker_order_ref,
                state=receipt.state,
                authorization_ref=submitted.authorization_ref,
                correlation_id=correlation_id,
                received_at=receipt.received_at,
            ),
            authorization_ref=submitted.authorization_ref,
            decision_ref=submitted.decision_ref,
            risk_ref=submitted.risk_ref,
            allocation_ref=submitted.allocation_ref,
            policy_ref=submitted.policy_ref,
            correlation_id=correlation_id,
            agent_version=AGENT_VERSION,
            processed_at=self._clock(),
        )

    def replace(
        self,
        cancel_client_order_id: str,
        new_request: ExecutionRequest,
        *,
        active_halts: Sequence[HaltRecord] = (),
    ) -> tuple[ExecutionOutcome, ExecutionOutcome]:
        """Modify = cancel + a NEW fully-authorized request; never in-place.

        A modification without a fresh decision→risk→pre-submit pipeline
        (i.e. a fresh SubmitAuthorization) is impossible by construction:
        the new request goes through the exact same gate path.
        """
        cancel_outcome = self.cancel(
            cancel_client_order_id, correlation_id=new_request.correlation_id
        )
        if cancel_outcome.status is not ExecutionStatus.CANCEL_SENT:
            # Cancel did not confirm: do NOT submit the replacement (that
            # would risk a duplicate live exposure) — fail closed.
            refusal = self._record_free_outcome(
                new_request.client_order_id,
                new_request.correlation_id,
                ExecutionStatus.REFUSED,
                ("REPLACE_CANCELLED_LEG_NOT_CONFIRMED",),
            )
            return cancel_outcome, refusal
        return cancel_outcome, self.process(new_request, active_halts=active_halts)

    # -- internals -----------------------------------------------------------
    def _process_new(
        self, request: ExecutionRequest, active_halts: Sequence[HaltRecord]
    ) -> ExecutionOutcome:
        def refuse(status: ExecutionStatus, *codes: str) -> ExecutionOutcome:
            return self._outcome(request, status, codes, receipt=None)

        pre_submit = request.pre_submit
        if pre_submit.action not in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE):
            return refuse(
                ExecutionStatus.REFUSED, f"PRE_SUBMIT_{pre_submit.action.value}", "NOT_SENT"
            )
        if request.authorization is None:
            return refuse(ExecutionStatus.REFUSED, "AUTHORIZATION_MISSING")

        halt = match_halt(
            active_halts,
            EntryContext(
                environment=request.environment,
                strategy_id=request.strategy_id,
                symbol=request.symbol,
                source_id=request.source_id,
            ),
        )
        if halt is not None:
            # Security 23.2: queued intent stopped by policy at consumption.
            return refuse(
                ExecutionStatus.CANCELLED_BY_POLICY,
                f"HALT_ACTIVE_{halt.scope.value}",
                EntryCheckpoint.QUEUE_CONSUMPTION.value,
            )
        if request.environment is not Environment.PAPER:
            return refuse(ExecutionStatus.REFUSED, "ENVIRONMENT_NOT_PAPER")

        health = self._broker.health_check()
        if not health.healthy:
            return refuse(ExecutionStatus.FAILED, "BROKER_UNHEALTHY", "NOT_SENT")

        # THE seal, verified by the component that actually sends the order and
        # BEFORE the single use is spent. The service checks this too; the agent
        # checks it again because the agent is the one thing that can submit,
        # and a boundary that trusts its caller is not a boundary.
        broken = self._seal_failure(request)
        if broken is not None:
            return refuse(ExecutionStatus.REFUSED, broken, "NOT_SENT")

        # FINAL-BOUNDARY reconciliation latch — checked immediately before the
        # single-use authorization is consumed. Even with pre_submit ALLOW, a
        # valid authorization and a healthy broker, a new entry is REFUSED while
        # reconciliation is required for the current (possibly just-recovered)
        # broker session. A frozen pre-crash authorization cannot slip through
        # after a Gateway restart; UNKNOWN stays reconciliation-only, never a
        # blind retry. This does NOT gate cancel()/protective paths.
        entry_blocked, reason = self._entry_gate.entry_blocked()
        if entry_blocked:
            return refuse(
                ExecutionStatus.NEEDS_REVIEW,
                reason or "RECONCILIATION_REQUIRED",
                "RECONCILE_BEFORE_ANY_SUBMIT",
                "NOT_SENT",
            )

        try:
            authorization = request.authorization.consume()
        except AuthorizationConsumedError:
            # Single-use stays single-use: never a second submit.
            return refuse(ExecutionStatus.REFUSED, "AUTHORIZATION_ALREADY_CONSUMED")

        # Every order field comes from the SEALED plan, whose identity was
        # just verified against both the pre-submit result and the token. There
        # is no field here the agent could source from somewhere else.
        plan = request.plan
        # A protected entry (ADR-0019) is rebuilt from the seal as a native
        # bracket: the protective stop rides the bracket child, so the parent
        # stays a clean LIMIT/MARKET and the broker establishes both legs
        # atomically. The child id and the stop are both sealed, so the bracket
        # the broker receives is exactly the one that was authorized.
        protective: ProtectiveBracket | None = None
        entry_stop_price = plan.stop_price
        if plan.protective_stop_client_order_id is not None and plan.stop_price is not None:
            protective = ProtectiveBracket(
                stop_price=plan.stop_price,
                stop_client_order_id=plan.protective_stop_client_order_id,
            )
            entry_stop_price = None
        intent = OrderIntent(
            client_order_id=plan.client_order_id,
            # The CANONICAL authorization identity, content-addressed over the
            # authorization itself -- including its plan_ref. The old
            # "submit-auth:{candidate}@{timestamp}" string proved neither which
            # authorization this was nor which plan it authorized, and every
            # authorization minted in the same second for one candidate shared
            # it.
            authorization_ref=authorization_identity(authorization),
            correlation_id=request.correlation_id,
            environment=plan.environment,
            account_alias=plan.account_alias,
            symbol=plan.symbol,
            # THE security, from the seal. The symbol travels beside it for
            # logs and receipts; the reference is what identifies the order.
            broker_instrument_ref=plan.broker_instrument_ref,
            side=OrderSide(plan.side),
            order_type=plan.order_type,
            quantity=plan.quantity,
            limit_price=plan.limit_price,
            stop_price=entry_stop_price,
            time_in_force=TimeInForce(plan.time_in_force),
            protective=protective,
        )
        try:
            receipt = self._broker.submit_order(intent)
        except BrokerPortError as error:
            if error.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE:
                # DoD: no blind retry on unknown state — reconciliation only.
                return refuse(
                    ExecutionStatus.NEEDS_REVIEW,
                    "UNKNOWN_BROKER_STATE",
                    "RECONCILE_BEFORE_ANY_RETRY",
                )
            return refuse(ExecutionStatus.FAILED, f"SUBMIT_{error.code.value}", "NO_AUTO_RETRY")
        return self._outcome(request, ExecutionStatus.SUBMITTED, ("SUBMITTED",), receipt=receipt)

    def _seal_failure(self, request: ExecutionRequest) -> str | None:
        """Is the plan about to be sent the plan that was authorized?

        Equality over one content-addressed identity, never a prefix and never
        a field-by-field comparison that could omit a field. Both the result
        that was checked and the token that permits the send must name it, so
        neither a re-checked result carrying a stale token nor a fresh token
        carrying a substituted order can pass.
        """
        sealed = plan_identity(request.plan)
        pre_submit = request.pre_submit
        if not pre_submit.plan_ref:
            return "PRE_SUBMIT_SEALED_NO_PLAN"
        if pre_submit.plan_ref != sealed:
            return "PLAN_NOT_THE_ONE_CHECKED"
        assert request.authorization is not None  # noqa: S101 - checked above
        # The token must be the one THIS result minted, not merely a token that
        # agrees about the plan. Two wrappers can name the same plan; only one
        # of them was minted by this check, and the service verifying that is
        # not the same as the component that submits verifying it.
        mismatch = authorization_matches(pre_submit, request.authorization)
        if mismatch is not None:
            return "AUTHORIZATION_IS_NOT_THE_ONE_MINTED"
        token = request.authorization.peek()
        if token.plan_ref != sealed:
            return "PLAN_NOT_THE_ONE_AUTHORIZED"
        if token.authorized_quantity != request.plan.quantity:
            return "AUTHORIZED_QUANTITY_IS_NOT_THE_PLAN_QUANTITY"
        return None

    def _outcome(
        self,
        request: ExecutionRequest,
        status: ExecutionStatus,
        codes: tuple[str, ...],
        *,
        receipt: BrokerOrderReceipt | None,
    ) -> ExecutionOutcome:
        pre_submit = request.pre_submit
        return ExecutionOutcome(
            client_order_id=request.client_order_id,
            status=status,
            reason_codes=codes,
            receipt=receipt,
            authorization_ref=receipt.authorization_ref if receipt else "",
            decision_ref=pre_submit.decision_ref,
            risk_ref=pre_submit.risk_ref,
            allocation_ref=pre_submit.allocation_ref,
            policy_ref=pre_submit.policy_ref,
            correlation_id=request.correlation_id,
            agent_version=AGENT_VERSION,
            processed_at=self._clock(),
        )

    def _record_free_outcome(
        self,
        client_order_id: str,
        correlation_id: str,
        status: ExecutionStatus,
        codes: tuple[str, ...],
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            client_order_id=client_order_id,
            status=status,
            reason_codes=codes,
            receipt=None,
            authorization_ref="",
            decision_ref="",
            risk_ref="",
            allocation_ref="",
            policy_ref="",
            correlation_id=correlation_id,
            agent_version=AGENT_VERSION,
            processed_at=self._clock(),
        )


__all__ = [
    "AGENT_VERSION",
    "ExecutionAgent",
    "ExecutionOutcome",
    "ExecutionRequest",
    "ExecutionStatus",
]
