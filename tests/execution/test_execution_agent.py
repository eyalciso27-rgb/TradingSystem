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
    OrderSide,
)
from trading_contracts.enums import Environment, HaltScope, OrderState, OrderType
from trading_execution_agent.agent import (
    ExecutionAgent,
    ExecutionRequest,
    ExecutionStatus,
)

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


def pre_submit_result(
    *,
    action: PreSubmitAction = PreSubmitAction.ALLOW,
    quantity: str = "100",
) -> tuple[PreSubmitCheckResult, SingleUseAuthorization | None]:
    authorization = (
        SubmitAuthorization(
            candidate_id="cand-1",
            authorized_quantity=Decimal(quantity),
            authorized_at=AS_OF,
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
) -> ExecutionRequest:
    result, auto_token = pre_submit_result(action=action, quantity=quantity)
    resolved = auto_token if token == "auto" else token
    assert resolved is None or isinstance(resolved, SingleUseAuthorization)
    return ExecutionRequest(
        client_order_id=client_order_id,
        correlation_id=f"corr-{client_order_id}",
        pre_submit=result,
        authorization=resolved,
        environment=environment,
        account_alias="paper-main",
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10"),
        stop_price=None,
        strategy_id="strat-1",
        source_id="src-1",
    )


def make_agent(broker: Any | None = None) -> ExecutionAgent:
    return ExecutionAgent(
        broker if broker is not None else make_broker(),
        clock=lambda: AS_OF,
    )


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
    _, token = pre_submit_result()
    assert token is not None
    token.consume()  # consumed elsewhere first
    outcome = agent.process(request("ord-3", token=token))
    assert outcome.status is ExecutionStatus.REFUSED
    assert "AUTHORIZATION_ALREADY_CONSUMED" in outcome.reason_codes
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
    # unknown account alias in the intent -> ACCOUNT_UNKNOWN from the port
    bad = request("bad-acct")
    object.__setattr__(bad, "account_alias", "nope")
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
    agent = ExecutionAgent(broker, clock=lambda: datetime(2026, 7, 10, 14, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        agent.process(request("naive-1"))
