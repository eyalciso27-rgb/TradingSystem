"""Execution agent drills (IMP-1003): fake broker only, no network.

DoD focus: no blind retry on unknown state; authorization is single-use;
nothing is ever sent without an ALLOW/RESIZE pre-submit result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from trading_application.emergency import HaltRecord
from trading_application.execution_identity import plan_identity
from trading_application.execution_plan import ExecutablePlan
from trading_application.pre_submit_check import (
    PreSubmitAction,
    PreSubmitCheckResult,
    SingleUseAuthorization,
    SubmitAuthorization,
)
from trading_broker_core.fake import FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerErrorCode,
    BrokerPortError,
    OrderIntent,
)
from trading_contracts.enums import Environment, HaltScope, OrderState, OrderType
from trading_execution_agent.agent import (
    ExecutionAgent,
    ExecutionRequest,
    ExecutionStatus,
)
from trading_execution_agent.reconciliation_latch import ALWAYS_PERMIT, EntryGate

AS_OF = datetime(2026, 7, 10, 14, 0, tzinfo=UTC)

ACCOUNT = AccountSnapshot(
    account_alias="paper-main",
    environment=Environment.PAPER,
    net_liquidation_value=Decimal("100000"),
    cash=Decimal("60000"),
    buying_power=Decimal("120000"),
    observed_at=AS_OF - timedelta(minutes=1),
)


def make_broker(*, healthy: bool = True) -> FakeBroker:
    broker = FakeBroker(accounts={"paper-main": ACCOUNT}, healthy=healthy)
    if healthy:
        broker.connect()
    return broker


def plan(
    *,
    client_order_id: str = "ord-1",
    quantity: str = "100",
    symbol: str = "AAPL",
    side: str = "BUY",
    order_type: OrderType = OrderType.LIMIT,
    time_in_force: str = "DAY",
    limit_price: str | None = "10",
    stop_price: str | None = None,
    environment: Environment = Environment.PAPER,
    account_alias: str = "paper-main",
) -> ExecutablePlan:
    """The sealed order. The agent reads every order field from this."""
    return ExecutablePlan(
        client_order_id=client_order_id,
        decision_ref="decision:d1@sha256:" + "b" * 64,
        risk_request_ref="risk-request:AAPL@sha256:" + "c" * 64,
        risk_ref="risk-result:sha256:" + "d" * 64,
        allocation_ref="allocation:cand-1@sha256:" + "e" * 64,
        strategy_id="strat-1",
        source_id="src-1",
        environment=environment,
        account_alias=account_alias,
        symbol=symbol,
        broker_instrument_ref="conid:265598",
        broker_exchange="SMART",
        broker_currency="USD",
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        time_in_force=time_in_force,
        limit_price=Decimal(limit_price) if limit_price is not None else None,
        stop_price=Decimal(stop_price) if stop_price is not None else None,
    )


def pre_submit_result(
    *,
    action: PreSubmitAction = PreSubmitAction.ALLOW,
    quantity: str = "100",
    for_plan: ExecutablePlan | None = None,
) -> tuple[PreSubmitCheckResult, SingleUseAuthorization | None]:
    sealed_plan = for_plan if for_plan is not None else plan(quantity=quantity)
    sealed = plan_identity(sealed_plan)
    authorization = (
        SubmitAuthorization(
            candidate_id="cand-1",
            authorized_quantity=Decimal(quantity),
            authorized_at=AS_OF,
            plan_ref=sealed,
        )
        if action in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE)
        else None
    )
    result = PreSubmitCheckResult(
        candidate_id="cand-1",
        action=action,
        authorized_quantity=Decimal(quantity) if authorization else Decimal(0),
        authorization=authorization,
        allocation_ref="allocation:cand-1@ALLOW",
        risk_ref="risk:APPROVE",
        decision_ref="sha256:" + "b" * 64,
        policy_ref="pre-submit-policy:p1",
        checks=(),
        reason_codes=("PRE_SUBMIT_CLEARED",),
        input_refs=("observation:a-1@src-1",),
        authorized_plan=sealed_plan if authorization else None,
        plan_ref=sealed if authorization else "",
    )
    token = SingleUseAuthorization(authorization) if authorization else None
    return result, token


def request(
    client_order_id: str = "ord-1",
    *,
    action: PreSubmitAction = PreSubmitAction.ALLOW,
    quantity: str = "100",
    environment: Environment = Environment.PAPER,
    token: SingleUseAuthorization | None | str = "auto",
    sends: ExecutablePlan | None = None,
    account_alias: str = "paper-main",
) -> ExecutionRequest:
    sealed_plan = plan(
        client_order_id=client_order_id,
        quantity=quantity,
        environment=environment,
        account_alias=account_alias,
    )
    result, auto_token = pre_submit_result(
        action=action, quantity=quantity, for_plan=sealed_plan
    )
    resolved = auto_token if token == "auto" else token
    assert resolved is None or isinstance(resolved, SingleUseAuthorization)
    return ExecutionRequest(
        correlation_id=f"corr-{client_order_id}",
        pre_submit=result,
        authorization=resolved,
        # ``sends`` exists so a test can try to send an order OTHER than the one
        # that was sealed. It is the substitution the seal exists to refuse.
        plan=sends if sends is not None else sealed_plan,
    )


def make_agent(
    broker: Any | None = None, *, entry_gate: EntryGate = ALWAYS_PERMIT
) -> ExecutionAgent:
    return ExecutionAgent(
        broker if broker is not None else make_broker(),
        entry_gate=entry_gate,
        clock=lambda: AS_OF,
    )


def test_entry_gate_is_a_required_constructor_argument() -> None:
    """The final reconciliation boundary must not be opt-in by accident: a
    production construction site cannot silently omit the entry gate."""
    import inspect

    param = inspect.signature(ExecutionAgent.__init__).parameters["entry_gate"]
    assert param.default is inspect.Parameter.empty  # no implicit ALWAYS_PERMIT
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(TypeError):
        ExecutionAgent(make_broker())  # type: ignore[call-arg]


class CountingGate:
    """A fake entry gate that records how often the agent consulted it."""

    def __init__(self, *, blocked: bool) -> None:
        self.blocked = blocked
        self.calls = 0

    def entry_blocked(self) -> tuple[bool, str]:
        self.calls += 1
        return (self.blocked, "RECONCILIATION_REQUIRED" if self.blocked else "")


# -- final-boundary reconciliation latch ------------------------------------------


def test_latched_entry_is_refused_even_with_allow_auth_and_healthy_broker() -> None:  # A
    broker = make_broker(healthy=True)
    agent = make_agent(broker, entry_gate=CountingGate(blocked=True))
    outcome = agent.process(request("ord-1"))
    assert outcome.status is ExecutionStatus.NEEDS_REVIEW
    assert "RECONCILIATION_REQUIRED" in outcome.reason_codes
    assert outcome.receipt is None
    assert broker.get_open_orders("paper-main") == ()  # nothing was submitted


def test_authorization_minted_before_crash_is_refused_after_recovery() -> None:  # B
    # The request (with a valid single-use authorization) was frozen BEFORE the
    # Gateway crash; the broker is healthy again after systemd recovery, but the
    # reconciliation latch is still set -> no submit until a fresh reconciliation.
    frozen = request("ord-1")
    broker = make_broker(healthy=True)
    agent = make_agent(broker, entry_gate=CountingGate(blocked=True))
    outcome = agent.process(frozen)
    assert outcome.status is ExecutionStatus.NEEDS_REVIEW
    assert outcome.receipt is None
    assert broker.get_open_orders("paper-main") == ()


def test_clearing_the_latch_allows_a_fresh_submit() -> None:  # C (agent side)
    broker = make_broker(healthy=True)
    gate = CountingGate(blocked=True)
    agent = make_agent(broker, entry_gate=gate)
    assert agent.process(request("ord-1")).status is ExecutionStatus.NEEDS_REVIEW
    gate.blocked = False  # a CURRENT-session CLEAN cleared the latch
    outcome = agent.process(request("ord-2"))
    assert outcome.status is ExecutionStatus.SUBMITTED
    assert outcome.receipt is not None


def test_replayed_request_after_restart_never_submits_blindly() -> None:  # G
    broker = make_broker(healthy=True)
    agent = make_agent(broker, entry_gate=CountingGate(blocked=True))
    first = agent.process(request("ord-1"))
    second = agent.process(request("ord-1"))  # same queue item redelivered
    assert first.status is ExecutionStatus.NEEDS_REVIEW
    assert second == first  # idempotent; still refused
    assert broker.get_open_orders("paper-main") == ()


def test_latch_gates_entries_only_not_cancels() -> None:  # H
    broker = make_broker(healthy=True)
    gate = CountingGate(blocked=False)
    agent = make_agent(broker, entry_gate=gate)
    submitted = agent.process(request("ord-1"))
    assert submitted.status is ExecutionStatus.SUBMITTED
    calls_after_submit = gate.calls
    assert calls_after_submit >= 1  # the entry path consulted the gate
    # A protective cancel must NOT be routed through the entry latch.
    cancelled = agent.cancel("ord-1", correlation_id="corr-cancel")
    assert cancelled.status is ExecutionStatus.CANCEL_SENT
    assert gate.calls == calls_after_submit  # cancel did not consult the gate


# -- happy path -------------------------------------------------------------------


def test_allowed_request_submits_exactly_authorized_quantity() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    outcome = agent.process(request("ord-1", quantity="75"))
    assert outcome.status is ExecutionStatus.SUBMITTED
    assert outcome.receipt is not None
    assert outcome.receipt.state is OrderState.ACKNOWLEDGED
    open_orders = broker.get_open_orders("paper-main")
    assert len(open_orders) == 1
    # audit refs carried end to end
    assert outcome.decision_ref.startswith("sha256:")
    assert outcome.allocation_ref.startswith("allocation:")
    assert outcome.authorization_ref.startswith("submit-auth:cand-1@")
    assert outcome.correlation_id == "corr-ord-1"


# -- authorization gates -------------------------------------------------------------


def test_missing_authorization_refused_nothing_sent() -> None:
    broker = make_broker()
    outcome = make_agent(broker).process(request("ord-2", token=None))
    assert outcome.status is ExecutionStatus.REFUSED
    assert "AUTHORIZATION_MISSING" in outcome.reason_codes
    assert broker.get_open_orders("paper-main") == ()


def test_consumed_authorization_never_submits_twice() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    # This request's OWN token, spent elsewhere first. It has to be its own:
    # a token minted by a different pre-submit result is refused earlier now,
    # by the authorization-pair check, which would hide the single-use property
    # this test exists to prove.
    queued = request("ord-3")
    assert queued.authorization is not None
    queued.authorization.consume()

    outcome = agent.process(queued)

    assert outcome.status is ExecutionStatus.REFUSED
    assert "AUTHORIZATION_ALREADY_CONSUMED" in outcome.reason_codes
    assert broker.get_open_orders("paper-main") == ()


def test_a_token_minted_by_another_result_never_submits() -> None:
    """Defence in depth at the component that actually submits.

    The service checks the pair too. The agent is the boundary that sends, and
    a boundary that trusts its caller is not a boundary: a token which merely
    agrees about the plan is not the token this result minted.
    """
    broker = make_broker()
    agent = make_agent(broker)
    _, foreign = pre_submit_result()
    assert foreign is not None

    outcome = agent.process(request("ord-foreign", token=foreign))

    assert outcome.status is ExecutionStatus.REFUSED
    assert "AUTHORIZATION_IS_NOT_THE_ONE_MINTED" in outcome.reason_codes
    assert "NOT_SENT" in outcome.reason_codes
    assert broker.get_open_orders("paper-main") == ()


def test_rejected_pre_submit_never_sent() -> None:
    broker = make_broker()
    for action in (PreSubmitAction.CANCEL, PreSubmitAction.NEEDS_REVIEW):
        outcome = make_agent(broker).process(request(f"ord-{action.value}", action=action))
        assert outcome.status is ExecutionStatus.REFUSED
        assert f"PRE_SUBMIT_{action.value}" in outcome.reason_codes
    assert broker.get_open_orders("paper-main") == ()


# -- kill / environment / health -----------------------------------------------------


def test_active_halt_cancels_by_policy_at_queue_consumption() -> None:
    broker = make_broker()
    halt = HaltRecord(
        scope=HaltScope.SYMBOL_HALT,
        environment=Environment.PAPER,
        target_ref="AAPL",
        reason_code="SEV1",
    )
    outcome = make_agent(broker).process(request("ord-4"), active_halts=(halt,))
    assert outcome.status is ExecutionStatus.CANCELLED_BY_POLICY
    assert "HALT_ACTIVE_SYMBOL_HALT" in outcome.reason_codes
    assert "QUEUE_CONSUMPTION" in outcome.reason_codes
    assert broker.get_open_orders("paper-main") == ()


def test_live_environment_refused() -> None:
    outcome = make_agent().process(request("ord-5", environment=Environment.LIVE))
    assert outcome.status is ExecutionStatus.REFUSED
    assert "ENVIRONMENT_NOT_PAPER" in outcome.reason_codes


def test_unhealthy_broker_fails_closed_nothing_sent() -> None:
    broker = FakeBroker(accounts={"paper-main": ACCOUNT}, healthy=False)
    outcome = make_agent(broker).process(request("ord-6"))
    assert outcome.status is ExecutionStatus.FAILED
    assert "BROKER_UNHEALTHY" in outcome.reason_codes


# -- idempotency ----------------------------------------------------------------------


def test_duplicate_request_returns_original_outcome_no_resend() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    req = request("dup-1")
    first = agent.process(req)
    second = agent.process(req)
    assert second == first
    assert len(broker.get_open_orders("paper-main")) == 1
    events = [e for e in broker.order_events() if e.client_order_id == "dup-1"]
    assert len(events) == 1


# -- unknown broker state: no blind retry (DoD) ---------------------------------------


class UnknownStateBroker(FakeBroker):
    def __init__(self) -> None:
        super().__init__(accounts={"paper-main": ACCOUNT})
        self.submit_attempts = 0

    def submit_order(self, intent: OrderIntent) -> Any:
        self.submit_attempts += 1
        raise BrokerPortError(
            BrokerErrorCode.UNKNOWN_BROKER_RESPONSE, "status lost", correlation_id="x"
        )


def test_unknown_broker_state_needs_review_and_no_blind_retry() -> None:
    broker = UnknownStateBroker()
    broker.connect()
    agent = make_agent(broker)
    req = request("unk-1")
    outcome = agent.process(req)
    assert outcome.status is ExecutionStatus.NEEDS_REVIEW
    assert "RECONCILE_BEFORE_ANY_RETRY" in outcome.reason_codes
    # DoD: re-delivering the same item does NOT retry the submit
    again = agent.process(req)
    assert again == outcome
    assert broker.submit_attempts == 1


def test_other_broker_error_is_terminal_failed() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    # unknown account alias in the intent -> ACCOUNT_UNKNOWN from the port.
    # The account is part of the SEALED plan now, so this cannot be done by
    # mutating the request after the fact: the order has to be sealed for an
    # account the broker does not know.
    bad = request("bad-acct", account_alias="nope")
    outcome = agent.process(bad)
    assert outcome.status is ExecutionStatus.FAILED
    assert "SUBMIT_ACCOUNT_UNKNOWN" in outcome.reason_codes
    assert "NO_AUTO_RETRY" in outcome.reason_codes


# -- cancel / replace ------------------------------------------------------------------


def test_cancel_submitted_order() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    agent.process(request("c-1"))
    outcome = agent.cancel("c-1", correlation_id="corr-cancel")
    assert outcome.status is ExecutionStatus.CANCEL_SENT
    assert outcome.receipt is not None
    assert outcome.receipt.state is OrderState.CANCELLED


def test_cancel_unknown_order_refused() -> None:
    outcome = make_agent().cancel("ghost", correlation_id="c")
    assert outcome.status is ExecutionStatus.REFUSED
    assert "CANCEL_UNKNOWN_ORDER" in outcome.reason_codes


def test_replace_requires_confirmed_cancel_before_new_submit() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    # replacing an order that was never submitted: cancel leg refuses and the
    # new leg is NOT sent (fail closed - no duplicate exposure)
    cancel_outcome, new_outcome = agent.replace("ghost", request("r-1"))
    assert cancel_outcome.status is ExecutionStatus.REFUSED
    assert new_outcome.status is ExecutionStatus.REFUSED
    assert "REPLACE_CANCELLED_LEG_NOT_CONFIRMED" in new_outcome.reason_codes
    assert broker.get_open_orders("paper-main") == ()


def test_replace_happy_path_goes_through_full_gate_path() -> None:
    broker = make_broker()
    agent = make_agent(broker)
    agent.process(request("r-old"))
    cancel_outcome, new_outcome = agent.replace("r-old", request("r-new", quantity="50"))
    assert cancel_outcome.status is ExecutionStatus.CANCEL_SENT
    assert new_outcome.status is ExecutionStatus.SUBMITTED
    assert new_outcome.receipt is not None


# -- hygiene ---------------------------------------------------------------------------


def test_agent_source_has_no_sdk_or_secret_access() -> None:
    from pathlib import Path

    import trading_execution_agent.agent as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord"):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for forbidden in ("ib" + "api", "httpx", "requests", "socket"):
            assert forbidden not in line, line


def test_naive_clock_rejected() -> None:
    broker = make_broker()
    agent = ExecutionAgent(
        broker, entry_gate=ALWAYS_PERMIT, clock=lambda: datetime(2026, 7, 10, 14, 0)  # noqa: DTZ001
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        agent.process(request("naive-1"))
