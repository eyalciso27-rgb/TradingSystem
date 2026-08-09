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
from trading_application.pre_submit_check import (
    AuthorizationConsumedError,
    PreSubmitAction,
    PreSubmitCheckResult,
    SingleUseAuthorization,
)
from trading_broker_core.port import (
    BrokerErrorCode,
    BrokerOrderReceipt,
    BrokerPort,
    BrokerPortError,
    CancelOrderRequest,
    OrderIntent,
    OrderSide,
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
    """One queue item: pre-submit output + authorization + frozen order args.

    The agent never invents or edits any of these — they are the sealed
    output of the decision → risk → allocation → final pre-submit pipeline.
    """

    client_order_id: str
    correlation_id: str
    pre_submit: PreSubmitCheckResult
    authorization: SingleUseAuthorization | None
    environment: Environment
    account_alias: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    limit_price: Decimal | None
    stop_price: Decimal | None
    strategy_id: str
    source_id: str
    #: Time-in-force for the order. Defaults to DAY; a protective exit that must
    #: outlive the session (so it cannot expire while the position is open)
    #: is submitted GTC by its caller — never left to a session-bound default.
    time_in_force: TimeInForce = TimeInForce.DAY

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise ValueError("client_order_id is mandatory")
        if not self.correlation_id.strip():
            raise ValueError("correlation_id is mandatory")


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

        intent = OrderIntent(
            client_order_id=request.client_order_id,
            authorization_ref=(
                f"submit-auth:{authorization.candidate_id}"
                f"@{authorization.authorized_at.isoformat()}"
            ),
            correlation_id=request.correlation_id,
            environment=request.environment,
            account_alias=request.account_alias,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            quantity=authorization.authorized_quantity,  # exactly as authorized
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            time_in_force=request.time_in_force,
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
