"""Position coordinator: single writer, concurrent triggers (IMP-1004)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.position_coordinator import (
    CoordinatorPolicy,
    ExitTrigger,
    PlanAction,
    PlanStatus,
    PositionCoordinator,
    PositionCoordinatorError,
    PositionSnapshot,
)
from trading_contracts.enums import Environment, ExitReason, ExitTriggerType

AS_OF = datetime(2026, 7, 10, 15, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=1)

# Test-fixture priorities (limits/ranking are config): RISK/system safety on
# top per section 31 ("reduction/close at top priority"), stop above target.
POLICY = CoordinatorPolicy(
    policy_version="coordinator-policy-test-1",
    trigger_priorities={
        ExitTriggerType.SYSTEM_SAFETY_EXIT: 90,
        ExitTriggerType.RISK_REDUCTION: 80,
        ExitTriggerType.PROTECTIVE_STOP: 70,
        ExitTriggerType.THESIS_INVALIDATED: 60,
        ExitTriggerType.MANUAL_EXIT: 50,
        ExitTriggerType.TAKE_PROFIT: 40,
        ExitTriggerType.TIME_EXIT: 30,
        ExitTriggerType.NEW_MATERIAL_EVENT: 20,
        ExitTriggerType.TRAILING_POLICY: 10,
    },
)


def position(*, quantity: str = "100", known: bool = True) -> PositionSnapshot:
    return PositionSnapshot(
        position_ref="pos-1",
        symbol="AAPL",
        environment=Environment.PAPER,
        open_quantity=Decimal(quantity),
        protective_order_refs=("stop-order-1",),
        target_order_refs=("target-order-1",),
        observed_at=EARLIER,
        state_known=known,
    )


def trigger(
    trigger_id: str,
    trigger_type: ExitTriggerType,
    *,
    reduce: str | None = None,
    observed_at: datetime = EARLIER,
) -> ExitTrigger:
    return ExitTrigger(
        trigger_id=trigger_id,
        trigger_type=trigger_type,
        position_ref="pos-1",
        observed_at=observed_at,
        reduce_quantity=Decimal(reduce) if reduce else None,
        source_ref=f"src:{trigger_id}",
    )


def make() -> PositionCoordinator:
    return PositionCoordinator(POLICY)


# -- DoD: concurrent triggers -> one coherent plan ---------------------------------


def test_concurrent_exit_triggers_produce_one_coherent_plan() -> None:
    coordinator = make()
    plan = coordinator.coordinate(
        position(),
        [
            trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP),
            trigger("t-target", ExitTriggerType.TAKE_PROFIT),
            trigger("t-time", ExitTriggerType.TIME_EXIT),
        ],
        as_of=AS_OF,
    )
    assert plan.status is PlanStatus.PENDING
    assert plan.action is PlanAction.EXIT_FULL
    assert plan.winning_trigger_id == "t-stop"  # highest policy priority
    assert plan.exit_reason is ExitReason.STOP
    assert {r.trigger_id for r in plan.rejected_triggers} == {"t-target", "t-time"}
    assert all(r.reason == "TRIGGER_LOST_PRIORITY" for r in plan.rejected_triggers)
    # one action; the opposing working orders are cancelled - no contradiction
    assert set(plan.cancel_order_refs) == {"stop-order-1", "target-order-1"}
    assert plan.quantity == Decimal("100")


def test_stop_and_target_simultaneously_no_contradicting_actions() -> None:
    plan = make().coordinate(
        position(),
        [
            trigger("t-target", ExitTriggerType.TAKE_PROFIT),
            trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP),
        ],
        as_of=AS_OF,
    )
    assert plan.winning_trigger_type is ExitTriggerType.PROTECTIVE_STOP
    assert plan.action is PlanAction.EXIT_FULL
    assert len(plan.rejected_triggers) == 1  # target rejected, not executed
    assert "target-order-1" in plan.cancel_order_refs  # no double fill


def test_reduction_absorbed_by_full_exit() -> None:
    plan = make().coordinate(
        position(),
        [
            trigger("t-risk", ExitTriggerType.RISK_REDUCTION, reduce="30"),
            trigger("t-safety", ExitTriggerType.SYSTEM_SAFETY_EXIT),  # full exit
        ],
        as_of=AS_OF,
    )
    assert plan.action is PlanAction.EXIT_FULL
    assert plan.quantity == Decimal("100")
    assert plan.rejected_triggers[0].reason == "ABSORBED_BY_FULL_EXIT"


def test_reduce_plan_caps_at_open_quantity_and_cancels_targets_only() -> None:
    coordinator = make()
    plan = coordinator.coordinate(
        position(),
        [trigger("t-risk", ExitTriggerType.RISK_REDUCTION, reduce="30")],
        as_of=AS_OF,
    )
    assert plan.action is PlanAction.REDUCE
    assert plan.quantity == Decimal("30")
    assert plan.cancel_order_refs == ("target-order-1",)  # protection stays
    over = make().coordinate(
        position(),
        [trigger("t-risk", ExitTriggerType.RISK_REDUCTION, reduce="500")],
        as_of=AS_OF,
    )
    assert over.action is PlanAction.EXIT_FULL  # never more than the position
    assert over.quantity == Decimal("100")


def test_duplicate_trigger_ids_do_not_duplicate_actions() -> None:
    duplicate = trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP)
    plan = make().coordinate(position(), [duplicate, duplicate, duplicate], as_of=AS_OF)
    assert plan.rejected_triggers == ()
    assert plan.action is PlanAction.EXIT_FULL


def test_same_inputs_same_plan() -> None:
    triggers = [
        trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP),
        trigger("t-target", ExitTriggerType.TAKE_PROFIT),
    ]
    first = make().coordinate(position(), triggers, as_of=AS_OF)
    second = make().coordinate(position(), list(reversed(triggers)), as_of=AS_OF)
    assert first == second  # order of arrival never changes the plan


# -- single writer: pending plans -----------------------------------------------------


def test_second_lower_priority_plan_refused_while_pending() -> None:
    coordinator = make()
    first = coordinator.coordinate(
        position(), [trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP)], as_of=AS_OF
    )
    assert first.status is PlanStatus.PENDING
    second = coordinator.coordinate(
        position(), [trigger("t-target", ExitTriggerType.TAKE_PROFIT)], as_of=AS_OF
    )
    assert second.status is PlanStatus.REFUSED
    assert second.action is PlanAction.NO_ACTION
    assert "PLAN_ALREADY_PENDING" in second.reason_codes
    # the pending plan is untouched
    active = coordinator.active_plan("pos-1")
    assert active is not None and active.plan_id == first.plan_id


def test_higher_priority_supersedes_and_cancels_previous_plan() -> None:
    coordinator = make()
    first = coordinator.coordinate(
        position(), [trigger("t-target", ExitTriggerType.TAKE_PROFIT)], as_of=AS_OF
    )
    superseding = coordinator.coordinate(
        position(), [trigger("t-safety", ExitTriggerType.SYSTEM_SAFETY_EXIT)], as_of=AS_OF
    )
    assert superseding.status is PlanStatus.PENDING
    assert superseding.supersedes_plan_id == first.plan_id
    assert f"SUPERSEDES:{first.plan_id}" in superseding.reason_codes


def test_terminal_plan_never_resurrected() -> None:
    coordinator = make()
    coordinator.coordinate(
        position(), [trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP)], as_of=AS_OF
    )
    coordinator.mark("pos-1", PlanStatus.CANCELLED)
    with pytest.raises(PositionCoordinatorError, match="never resurrected"):
        coordinator.mark("pos-1", PlanStatus.COMPLETED)
    # a new trigger starts a NEW plan (new sequence), not the old one
    fresh = coordinator.coordinate(
        position(), [trigger("t-stop-2", ExitTriggerType.PROTECTIVE_STOP)], as_of=AS_OF
    )
    assert fresh.status is PlanStatus.PENDING
    assert fresh.supersedes_plan_id is None


# -- unknown state / fail-closed ------------------------------------------------------


def test_unknown_position_state_needs_review_no_action() -> None:
    plan = make().coordinate(
        position(known=False),
        [trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP)],
        as_of=AS_OF,
    )
    assert plan.status is PlanStatus.NEEDS_REVIEW
    assert plan.action is PlanAction.NO_ACTION
    assert plan.quantity == Decimal(0)
    assert "RECONCILE_BEFORE_ANY_ACTION" in plan.reason_codes
    assert plan.cancel_order_refs == ()  # nothing touched blindly


def test_zero_quantity_position_needs_review() -> None:
    plan = make().coordinate(
        position(quantity="0"),
        [trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP)],
        as_of=AS_OF,
    )
    assert plan.status is PlanStatus.NEEDS_REVIEW


def test_policy_missing_priority_fails_closed() -> None:
    partial = CoordinatorPolicy(
        policy_version="partial",
        trigger_priorities={ExitTriggerType.PROTECTIVE_STOP: 70},
    )
    with pytest.raises(PositionCoordinatorError, match="does not rank"):
        PositionCoordinator(partial).coordinate(
            position(), [trigger("t-target", ExitTriggerType.TAKE_PROFIT)], as_of=AS_OF
        )
    with pytest.raises(PositionCoordinatorError, match="policy_version"):
        CoordinatorPolicy(policy_version=" ", trigger_priorities={})


def test_lookahead_and_naive_rejected() -> None:
    with pytest.raises(PositionCoordinatorError, match="lookahead"):
        make().coordinate(
            position(),
            [trigger("t-f", ExitTriggerType.PROTECTIVE_STOP, observed_at=AS_OF + timedelta(1))],
            as_of=AS_OF,
        )
    with pytest.raises(PositionCoordinatorError, match="timezone-aware"):
        make().coordinate(
            position(),
            [trigger("t-stop", ExitTriggerType.PROTECTIVE_STOP)],
            as_of=datetime(2026, 7, 10, 15, 0),  # noqa: DTZ001
        )


def test_wrong_position_trigger_rejected() -> None:
    foreign = ExitTrigger(
        trigger_id="t-x",
        trigger_type=ExitTriggerType.PROTECTIVE_STOP,
        position_ref="pos-OTHER",
        observed_at=EARLIER,
    )
    with pytest.raises(PositionCoordinatorError, match="different position"):
        make().coordinate(position(), [foreign], as_of=AS_OF)


# -- hygiene ---------------------------------------------------------------------------


def test_coordinator_is_pure_no_broker_or_network_imports() -> None:
    import trading_application.position_coordinator as module

    assert module.__file__ is not None
    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in (
            "trading_broker", "ib" + "api", "httpx", "requests", "socket", "trading_persistence",
        ):
            assert forbidden not in line, line
