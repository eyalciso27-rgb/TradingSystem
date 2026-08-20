"""Final pre-submit check (IMP-0906).

Authority: System_Functional_Specification_v0.7 section 40.4 (the Final
Pre-Submit Workflow: refresh Market Snapshot, Account Snapshot and Trading
Calendar; check Decision expiry, Approval expiry and Strategy promotion
state; check Kill State, Broker Health and Reconciliation state; run the
final Risk Engine and mint a ONE-TIME SubmitAuthorization that only the
Execution Agent may consume); Decision_Contract DC-007 (Risk holds absolute
veto); Security spec section 12 (kill switch blocks entries) via the
IMP-0404 halt gate.

Deterministic, pure check over FROZEN inputs (ADR-0014): the IMP-0905
``CandidateAllocation`` (the upper bound), the IMP-0904 ``RiskResult``, the
IMP-0903 ``DecisionResult``, refreshed market/account fixtures, approval and
execution-context fixtures and a ``PreSubmitPolicy``. Output is a
``PreSubmitCheckResult`` only — never a broker action, real order or real
order id. On ALLOW/RESIZE it mints a single-use ``SubmitAuthorization``
(not a submission; execution-agent-only).

Research/shadow callers must use ``evaluate_only`` instead: the same
deterministic gate logic (one shared implementation), returning a
``PreSubmitEvaluation`` that carries only ALLOW / RESIZE / CANCEL /
NEEDS_REVIEW plus the explanation — no authorization object is
constructed anywhere on that path.

Hard rules:

- A rejected/vetoed allocation or risk VETO can never become ALLOW.
- An active kill switch/halt, unhealthy broker or pending reconciliation
  cancels absolutely.
- The authorized quantity is NEVER above the allocation, and is rounded
  DOWN — the check only cancels or reduces, never increases.
- Every threshold (slippage, risk increase, freshness/expiry windows) is a
  policy value; missing/invalid policy or missing/stale refreshed data fails
  closed. No number is invented in code.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum

from trading_contracts.enums import (
    DecisionOutcome,
    Environment,
    OrderType,
    RiskDecisionOutcome,
)

from trading_application.decision_engine import EngineDecision
from trading_application.emergency import EntryContext, HaltRecord, match_halt
from trading_application.execution_identity import (
    allocation_identity,
    decision_identity,
    plan_identity,
    pre_submit_policy_identity,
    risk_request_identity,
    risk_result_identity,
)
from trading_application.execution_plan import ExecutablePlan
from trading_application.portfolio_allocator import CandidateAllocation
from trading_application.risk_engine import RiskResult, TradeRiskRequest

PRE_SUBMIT_VERSION = "pre-submit-check-1.0.0"


class PreSubmitError(Exception):
    """Fail-closed pre-submit failure (bad policy/inputs, lookahead)."""


class PreSubmitAction(StrEnum):
    """Terminal action of the pre-submit check (section 40.4 outcomes)."""

    ALLOW = "ALLOW"
    RESIZE = "RESIZE"
    CANCEL = "CANCEL"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ChangeAction(StrEnum):
    """Policy response to an adverse price/risk change."""

    RESIZE = "RESIZE"
    CANCEL = "CANCEL"


class StaleAction(StrEnum):
    """Policy response to missing/stale refreshed data."""

    NEEDS_REVIEW = "NEEDS_REVIEW"
    CANCEL = "CANCEL"


@dataclass(frozen=True, slots=True)
class PreSubmitPolicy:
    """All pre-submit thresholds — configuration input, nothing in code."""

    policy_version: str
    max_price_slippage_pct: Decimal
    max_risk_increase_pct: Decimal
    market_max_staleness_seconds: int
    account_max_staleness_seconds: int
    decision_max_age_seconds: int
    approval_max_age_seconds: int
    require_manual_approval: bool
    price_change_action: ChangeAction
    risk_change_action: ChangeAction
    stale_data_action: StaleAction

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise PreSubmitError("policy_version is mandatory")
        if self.max_price_slippage_pct < 0 or self.max_risk_increase_pct < 0:
            raise PreSubmitError("change thresholds must be non-negative")
        windows = (
            self.market_max_staleness_seconds,
            self.account_max_staleness_seconds,
            self.decision_max_age_seconds,
            self.approval_max_age_seconds,
        )
        if any(window <= 0 for window in windows):
            raise PreSubmitError("staleness/age windows must be positive")


@dataclass(frozen=True, slots=True)
class RefreshedMarket:
    """Refreshed market snapshot (fixture)."""

    price: Decimal | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RefreshedAccount:
    """Refreshed account snapshot (fixture)."""

    risk_budget_equity: Decimal | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ApprovalState:
    """Manual-approval state (fixture)."""

    approved: bool
    approved_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.approved_at is not None and self.approved_at.tzinfo is None:
            raise ValueError("approved_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Kill/halt + broker/reconciliation state (fixture)."""

    active_halts: tuple[HaltRecord, ...] = ()
    broker_healthy: bool = True
    reconciliation_required: bool = False


@dataclass(frozen=True, slots=True)
class PreSubmitRequest:
    """The frozen candidate being re-checked immediately before submit."""

    allocation: CandidateAllocation
    risk_result: RiskResult
    decision: EngineDecision
    risk_request: TradeRiskRequest
    symbol: str
    strategy_id: str
    source_id: str
    environment: Environment
    planned_entry_price: Decimal
    planned_stop_price: Decimal
    #: The rest of the broker-bound order. These used to live outside the check
    #: entirely: the service chose the side, account, order type and time in
    #: force AFTER the authorization was minted, so the check could approve one
    #: order while a different one was sent. They are inputs now, and the
    #: authorized plan is sealed over them.
    side: str
    account_alias: str
    order_type: OrderType
    time_in_force: str
    #: The broker-bound order id. Part of the order, therefore part of what is
    #: checked and part of what is sealed.
    client_order_id: str

    def __post_init__(self) -> None:
        if not self.client_order_id.strip():
            raise PreSubmitError("client_order_id is mandatory")
        if self.planned_entry_price <= 0 or self.planned_stop_price <= 0:
            raise PreSubmitError("planned entry/stop prices must be positive")
        if self.planned_entry_price == self.planned_stop_price:
            raise PreSubmitError("planned stop must differ from entry")
        # The allocation must belong to THIS decision and THIS risk evaluation.
        # Taking its action and quantity on trust would let an allocation sized
        # against a different decision -- or a different evaluation of the same
        # decision -- authorize this one.
        expected_decision = decision_identity(self.decision)
        if self.allocation.decision_ref != expected_decision:
            raise PreSubmitError(
                f"the allocation names decision {self.allocation.decision_ref!r}"
                f" but this request is for {expected_decision!r}"
            )
        if self.risk_result.decision_ref != expected_decision:
            raise PreSubmitError(
                f"the risk result names decision {self.risk_result.decision_ref!r}"
                f" but this request is for {expected_decision!r}"
            )
        expected_risk = risk_result_identity(self.risk_result)
        if self.allocation.risk_ref != expected_risk:
            raise PreSubmitError(
                "the allocation was sized against a different risk evaluation"
                f" ({self.allocation.risk_ref!r} != {expected_risk!r})"
            )
        # The risk result must answer THE question this request describes. A
        # result is a set of output numbers, and two different questions can
        # produce identical numbers -- so without this, a risk evaluation of a
        # different symbol, source or size could authorize this order purely
        # because the figures happened to coincide.
        expected_request = risk_request_identity(self.risk_request)
        if self.risk_result.request_ref != expected_request:
            raise PreSubmitError(
                "the risk result answers a different risk request"
                f" ({self.risk_result.request_ref!r} != {expected_request!r})"
            )
        for name, mine, theirs in (
            ("symbol", self.symbol, self.risk_request.symbol),
            ("strategy", self.strategy_id, self.risk_request.strategy_id),
            ("source", self.source_id, self.risk_request.source_id),
            ("environment", self.environment.value, self.risk_request.environment.value),
        ):
            if mine != theirs:
                raise PreSubmitError(
                    f"the risk request was evaluated for a different {name}"
                    f" ({theirs!r} != {mine!r})"
                )


@dataclass(frozen=True, slots=True)
class SubmitAuthorization:
    """One-time authorization; execution-agent-only, NOT a submission."""

    candidate_id: str
    authorized_quantity: Decimal
    authorized_at: datetime
    #: The EXACT order this authorizes. Not a quantity that some later
    #: component pairs with an order of its own choosing: an authorization
    #: whose plan reference does not match the order being sent authorizes a
    #: different order, which is to say it does not authorize this one.
    plan_ref: str = ""
    note: str = "execution-agent-only; single-use; not a broker submission"


@dataclass(frozen=True, slots=True)
class PreSubmitCheck:
    name: str
    passed: bool
    limit: str
    observed: str
    effect: str  # "PASS" | "RESIZE" | "CANCEL" | "NEEDS_REVIEW"


@dataclass(frozen=True, slots=True)
class PreSubmitCheckResult:
    """Canonical pre-submit result + full explanation (never an order)."""

    candidate_id: str
    action: PreSubmitAction
    authorized_quantity: Decimal
    authorization: SubmitAuthorization | None
    allocation_ref: str
    risk_ref: str
    decision_ref: str
    policy_ref: str
    checks: tuple[PreSubmitCheck, ...]
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]
    #: The sealed order this result authorizes, and its identity. Present only
    #: when an authorization was minted -- a CANCEL authorizes no plan at all.
    authorized_plan: ExecutablePlan | None = None
    plan_ref: str = ""


@dataclass(frozen=True, slots=True)
class PreSubmitEvaluation:
    """Evaluation-only pre-submit outcome for research/shadow callers.

    Deliberately carries NO authorization field of any kind: ALLOW/RESIZE
    here is a research outcome, never an execution permission.
    """

    candidate_id: str
    action: PreSubmitAction
    evaluated_quantity: Decimal
    allocation_ref: str
    risk_ref: str
    decision_ref: str
    policy_ref: str
    checks: tuple[PreSubmitCheck, ...]
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]
    research_only: bool = True

    def __post_init__(self) -> None:
        if not self.research_only:
            raise PreSubmitError("a pre-submit evaluation is research-only; fail closed")


def _floor(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


class FinalPreSubmitCheck:
    """Deterministic final pre-submit gate over frozen refreshed state."""

    def __init__(self, policy: PreSubmitPolicy) -> None:
        self._policy = policy

    def check(
        self,
        request: PreSubmitRequest,
        market: RefreshedMarket,
        account: RefreshedAccount,
        approval: ApprovalState,
        context: ExecutionContext,
        *,
        as_of: datetime,
    ) -> PreSubmitCheckResult:
        """Production path: evaluate, then mint the one-time authorization.

        The authorization exists ONLY here — the shared evaluation core
        (``_evaluate_result``) never constructs one.
        """
        result = self._evaluate_result(
            request, market, account, approval, context, as_of=as_of
        )
        if (
            result.action in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE)
            and result.authorized_quantity > 0
        ):
            # THE seal, and the only place a sealed plan is ever built. It is
            # constructed from the request that was just checked, using the
            # quantity that was just authorized -- so what a later component
            # sends cannot differ from what was checked without differing from
            # this hash. A caller cannot supply a plan of its own, because a
            # caller never constructs one.
            plan = ExecutablePlan(
                client_order_id=request.client_order_id,
                decision_ref=result.decision_ref,
                risk_request_ref=request.risk_result.request_ref,
                risk_ref=result.risk_ref,
                allocation_ref=result.allocation_ref,
                strategy_id=request.strategy_id,
                source_id=request.source_id,
                environment=request.environment,
                account_alias=request.account_alias,
                symbol=request.symbol,
                side=request.side,
                quantity=result.authorized_quantity,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                limit_price=request.planned_entry_price,
                stop_price=request.planned_stop_price,
            )
            sealed = plan_identity(plan)
            return replace(
                result,
                authorized_plan=plan,
                plan_ref=sealed,
                authorization=SubmitAuthorization(
                    candidate_id=result.candidate_id,
                    authorized_quantity=result.authorized_quantity,
                    authorized_at=as_of,
                    plan_ref=sealed,
                ),
            )
        return result

    def evaluate_only(
        self,
        request: PreSubmitRequest,
        market: RefreshedMarket,
        account: RefreshedAccount,
        approval: ApprovalState,
        context: ExecutionContext,
        *,
        as_of: datetime,
    ) -> PreSubmitEvaluation:
        """Research/shadow path: the SAME gate logic, no authorization.

        Returns only ALLOW / RESIZE / CANCEL / NEEDS_REVIEW with the full
        explanation. No authorization constructor runs on this path.
        """
        result = self._evaluate_result(
            request, market, account, approval, context, as_of=as_of
        )
        return PreSubmitEvaluation(
            candidate_id=result.candidate_id,
            action=result.action,
            evaluated_quantity=result.authorized_quantity,
            allocation_ref=result.allocation_ref,
            risk_ref=result.risk_ref,
            decision_ref=result.decision_ref,
            policy_ref=result.policy_ref,
            checks=result.checks,
            reason_codes=result.reason_codes,
            input_refs=result.input_refs,
        )

    def _evaluate_result(
        self,
        request: PreSubmitRequest,
        market: RefreshedMarket,
        account: RefreshedAccount,
        approval: ApprovalState,
        context: ExecutionContext,
        *,
        as_of: datetime,
    ) -> PreSubmitCheckResult:
        if as_of.tzinfo is None:
            raise PreSubmitError("as_of must be timezone-aware")
        policy = self._policy
        for stamp in (
            market.observed_at,
            account.observed_at,
            request.decision.result.decided_at,
        ):
            if stamp > as_of:
                raise PreSubmitError("lookahead: a refreshed/decision input is newer than as_of")

        allocation = request.allocation
        checks: list[PreSubmitCheck] = []
        reasons: list[str] = []

        def result(
            action: PreSubmitAction, quantity: Decimal, code: str | None = None
        ) -> PreSubmitCheckResult:
            if code:
                reasons.append(code)
            # The evaluation core NEVER constructs an authorization; the
            # production ``check`` wrapper mints it afterwards.
            return PreSubmitCheckResult(
                candidate_id=allocation.candidate_id,
                action=action,
                authorized_quantity=quantity,
                authorization=None,
                # THE exact allocation, content-addressed. The candidate plus
                # the action could never prove WHICH allocation this was sized
                # against: many allocations for one candidate share an action.
                allocation_ref=allocation_identity(allocation),
                # The EXACT risk result, not merely its outcome: every
                # approving evaluation in the system shares "risk:APPROVE", so
                # that could never prove WHICH evaluation was authorized.
                risk_ref=risk_result_identity(request.risk_result),
                # The same decision reference the risk engine emits, so a
                # consumer can actually compare them.
                decision_ref=decision_identity(request.decision),
                # The PRE-SUBMIT policy, in its own namespace. Never the risk
                # policy: they govern different checks.
                policy_ref=pre_submit_policy_identity(policy),
                checks=tuple(checks),
                reason_codes=tuple(dict.fromkeys(reasons)),
                input_refs=tuple(allocation.input_refs),
            )

        # -- 1. the allocation must itself be a positive, approved allocation --
        if (
            allocation.allocated_quantity <= 0
            or allocation.action.value in ("REJECT", "MANUAL")
        ):
            checks.append(
                PreSubmitCheck(
                    "allocation_gate", False, "ALLOW/REDUCE > 0",
                    f"{allocation.action.value}:{allocation.allocated_quantity}", "CANCEL"
                )
            )
            return result(PreSubmitAction.CANCEL, Decimal(0), "ALLOCATION_NOT_APPROVED")
        checks.append(
            PreSubmitCheck("allocation_gate", True, "ALLOW/REDUCE > 0", "ok", "PASS")
        )

        # -- 2. risk veto is absolute -----------------------------------------
        if request.risk_result.outcome is RiskDecisionOutcome.VETO:
            checks.append(PreSubmitCheck("risk_gate", False, "!= VETO", "VETO", "CANCEL"))
            return result(PreSubmitAction.CANCEL, Decimal(0), "RISK_VETOED")
        if request.decision.result.outcome is not DecisionOutcome.APPROVE:
            checks.append(
                PreSubmitCheck(
                    "decision_gate", False, "APPROVE",
                    request.decision.result.outcome.value, "CANCEL"
                )
            )
            return result(PreSubmitAction.CANCEL, Decimal(0), "DECISION_NOT_APPROVED")
        checks.append(PreSubmitCheck("risk_gate", True, "!= VETO", "ok", "PASS"))

        # -- 3. kill switch / halt (absolute) ----------------------------------
        halt = match_halt(
            context.active_halts,
            EntryContext(
                environment=request.environment,
                strategy_id=request.strategy_id,
                symbol=request.symbol,
                source_id=request.source_id,
            ),
        )
        if halt is not None:
            checks.append(PreSubmitCheck("halt_gate", False, "no halt", halt.scope.value, "CANCEL"))
            return result(PreSubmitAction.CANCEL, Decimal(0), f"HALT_ACTIVE_{halt.scope.value}")
        checks.append(PreSubmitCheck("halt_gate", True, "no halt", "none", "PASS"))

        # -- 4. broker health + reconciliation ---------------------------------
        if not context.broker_healthy:
            checks.append(PreSubmitCheck("broker_health", False, "healthy", "unhealthy", "CANCEL"))
            return result(PreSubmitAction.CANCEL, Decimal(0), "BROKER_UNHEALTHY")
        if context.reconciliation_required:
            checks.append(
                PreSubmitCheck("reconciliation", False, "not required", "required", "CANCEL")
            )
            return result(PreSubmitAction.CANCEL, Decimal(0), "RECONCILIATION_REQUIRED")
        checks.append(PreSubmitCheck("broker_health", True, "healthy", "ok", "PASS"))

        # -- 5. freshness of refreshed snapshots -------------------------------
        stale = self._stale_action(policy.stale_data_action)
        if market.price is None or market.price <= 0:
            checks.append(PreSubmitCheck("market_snapshot", False, "present", "missing", "CANCEL"))
            return result(stale[0], Decimal(0), "MARKET_DATA_MISSING")
        if (as_of - market.observed_at) > timedelta(seconds=policy.market_max_staleness_seconds):
            checks.append(
                PreSubmitCheck(
                    "market_freshness", False,
                    f"<= {policy.market_max_staleness_seconds}s",
                    str((as_of - market.observed_at).total_seconds()), stale[1]
                )
            )
            return result(stale[0], Decimal(0), "MARKET_DATA_STALE")
        equity = account.risk_budget_equity
        if equity is None or equity <= 0:
            checks.append(PreSubmitCheck("account_snapshot", False, "present", "missing", "CANCEL"))
            return result(stale[0], Decimal(0), "ACCOUNT_DATA_MISSING")
        if (as_of - account.observed_at) > timedelta(seconds=policy.account_max_staleness_seconds):
            checks.append(
                PreSubmitCheck(
                    "account_freshness", False,
                    f"<= {policy.account_max_staleness_seconds}s",
                    str((as_of - account.observed_at).total_seconds()), stale[1]
                )
            )
            return result(stale[0], Decimal(0), "ACCOUNT_DATA_STALE")
        checks.append(PreSubmitCheck("freshness", True, "within windows", "ok", "PASS"))

        # -- 6. decision + approval expiry -------------------------------------
        decision_age = as_of - request.decision.result.decided_at
        if decision_age > timedelta(seconds=policy.decision_max_age_seconds):
            checks.append(
                PreSubmitCheck(
                    "decision_expiry", False, f"<= {policy.decision_max_age_seconds}s",
                    str(decision_age.total_seconds()), "CANCEL"
                )
            )
            return result(PreSubmitAction.CANCEL, Decimal(0), "DECISION_EXPIRED")
        if policy.require_manual_approval:
            if not approval.approved or approval.approved_at is None:
                checks.append(PreSubmitCheck("approval", False, "approved", "missing", "CANCEL"))
                return result(PreSubmitAction.NEEDS_REVIEW, Decimal(0), "APPROVAL_MISSING")
            approval_age = as_of - approval.approved_at
            if approval_age > timedelta(seconds=policy.approval_max_age_seconds):
                checks.append(
                    PreSubmitCheck(
                        "approval_expiry", False, f"<= {policy.approval_max_age_seconds}s",
                        str(approval_age.total_seconds()), "CANCEL"
                    )
                )
                return result(PreSubmitAction.NEEDS_REVIEW, Decimal(0), "APPROVAL_EXPIRED")
        checks.append(PreSubmitCheck("approval", True, "valid", "ok", "PASS"))

        # -- 7. price slippage + risk change (resize-only, never increase) -----
        quantity = allocation.allocated_quantity
        resized = False

        slippage = abs(market.price - request.planned_entry_price) / request.planned_entry_price
        if slippage > policy.max_price_slippage_pct:
            if policy.price_change_action is ChangeAction.CANCEL:
                checks.append(
                    PreSubmitCheck(
                        "price_slippage", False,
                        f"<= {policy.max_price_slippage_pct}", str(slippage), "CANCEL"
                    )
                )
                return result(PreSubmitAction.CANCEL, Decimal(0), "PRICE_SLIPPAGE_EXCEEDED")
            resized = True
            reasons.append("RESIZED_PRICE_SLIPPAGE")
            checks.append(
                PreSubmitCheck(
                    "price_slippage", False,
                    f"<= {policy.max_price_slippage_pct}", str(slippage), "RESIZE"
                )
            )
        else:
            checks.append(
                PreSubmitCheck(
                    "price_slippage", True,
                    f"<= {policy.max_price_slippage_pct}", str(slippage), "PASS"
                )
            )

        original_risk = abs(request.planned_entry_price - request.planned_stop_price)
        new_risk = abs(market.price - request.planned_stop_price)
        risk_increase = (
            (new_risk - original_risk) / original_risk if original_risk > 0 else Decimal(0)
        )
        if risk_increase > policy.max_risk_increase_pct:
            if policy.risk_change_action is ChangeAction.CANCEL:
                checks.append(
                    PreSubmitCheck(
                        "risk_change", False,
                        f"<= {policy.max_risk_increase_pct}", str(risk_increase), "CANCEL"
                    )
                )
                return result(PreSubmitAction.CANCEL, Decimal(0), "RISK_INCREASE_EXCEEDED")
            # resize so the approved risk budget is not exceeded at the new risk
            if new_risk > 0:
                budget_units = _floor(request.risk_result.approved_risk_amount / new_risk)
                quantity = min(quantity, budget_units)
            resized = True
            reasons.append("RESIZED_RISK_INCREASE")
            checks.append(
                PreSubmitCheck(
                    "risk_change", False,
                    f"<= {policy.max_risk_increase_pct}", str(risk_increase), "RESIZE"
                )
            )
        else:
            checks.append(
                PreSubmitCheck(
                    "risk_change", True,
                    f"<= {policy.max_risk_increase_pct}", str(risk_increase), "PASS"
                )
            )

        # authorized quantity never exceeds the allocation and is floored
        quantity = min(_floor(quantity), allocation.allocated_quantity)
        if quantity <= 0:
            return result(PreSubmitAction.CANCEL, Decimal(0), "RESIZE_TO_ZERO")
        if resized or quantity < allocation.allocated_quantity:
            return result(PreSubmitAction.RESIZE, quantity)
        return result(PreSubmitAction.ALLOW, quantity, "PRE_SUBMIT_CLEARED")

    @staticmethod
    def _stale_action(action: StaleAction) -> tuple[PreSubmitAction, str]:
        if action is StaleAction.CANCEL:
            return PreSubmitAction.CANCEL, "CANCEL"
        return PreSubmitAction.NEEDS_REVIEW, "NEEDS_REVIEW"


class AuthorizationConsumedError(PreSubmitError):
    """A one-time SubmitAuthorization was consumed more than once."""


class SingleUseAuthorization:
    """Wraps a SubmitAuthorization so it can be consumed exactly once.

    Only the Execution Agent should hold/consume this (section 40.4); the
    single-use guarantee is enforced here, the identity guarantee at wiring.
    """

    def __init__(self, authorization: SubmitAuthorization) -> None:
        self._authorization = authorization
        self._consumed = False

    def peek(self) -> SubmitAuthorization:
        """Read the wrapped authorization WITHOUT consuming the single use.

        Verification must not spend the one use it is verifying.
        """
        return self._authorization

    def consume(self) -> SubmitAuthorization:
        if self._consumed:
            raise AuthorizationConsumedError(
                f"authorization for {self._authorization.candidate_id!r} already consumed"
            )
        self._consumed = True
        return self._authorization


def single_use_authorization(result: PreSubmitCheckResult) -> SingleUseAuthorization:
    """Derive the single-use wrapper FROM the result that minted it.

    ``AuthorizationDecision`` carries a ``PreSubmitCheckResult`` and a
    ``SingleUseAuthorization``. Accepting those as two independently created
    objects would let a buggy or malicious authority present a result approving
    1 alongside an authorization for 10 — the consumer would check the result
    and the agent would submit the authorization.

    Deriving one from the other removes the gap: there is nothing to
    substitute, because there is only one object.
    """
    if result.authorization is None:
        raise PreSubmitError(
            f"pre-submit result for {result.candidate_id!r} minted no"
            f" authorization ({result.action.value}); there is nothing to consume"
        )
    return SingleUseAuthorization(result.authorization)


def authorization_matches(
    result: PreSubmitCheckResult, authorization: SingleUseAuthorization
) -> str | None:
    """Is this the authorization THAT result minted? A reason means no.

    For callers that receive the pair rather than deriving it. Compares the
    identity of the wrapped ``SubmitAuthorization`` against the result's own,
    without consuming the single use.
    """
    if result.authorization is None:
        return "the pre-submit result minted no authorization"
    wrapped = authorization.peek()
    if wrapped is not result.authorization and wrapped != result.authorization:
        return (
            "the single-use authorization does not wrap the SubmitAuthorization"
            f" this pre-submit result minted (candidate {result.candidate_id!r})"
        )
    if wrapped.authorized_quantity != result.authorized_quantity:
        return (
            f"the authorization permits {wrapped.authorized_quantity} but the"
            f" result authorized {result.authorized_quantity}"
        )
    return None


__all__ = [
    "PRE_SUBMIT_VERSION",
    "authorization_matches",
    "single_use_authorization",
    "ApprovalState",
    "AuthorizationConsumedError",
    "ChangeAction",
    "ExecutionContext",
    "FinalPreSubmitCheck",
    "PreSubmitAction",
    "PreSubmitCheck",
    "PreSubmitCheckResult",
    "PreSubmitError",
    "PreSubmitEvaluation",
    "PreSubmitPolicy",
    "PreSubmitRequest",
    "RefreshedAccount",
    "RefreshedMarket",
    "SingleUseAuthorization",
    "StaleAction",
    "SubmitAuthorization",
]
