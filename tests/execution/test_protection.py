"""Broker-side protection drills (IMP-1005): fake capabilities, no broker.

DoD: no required position remains unprotected — checked via the
unprotected_positions() invariant after every scenario.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_broker_core.fake import DEFAULT_CAPABILITIES
from trading_broker_core.port import (
    BrokerCapabilities,
    BrokerCapabilityMode,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderType
from trading_execution_agent.protection import (
    FillEvent,
    MissingCapabilityAction,
    PlanStatus,
    ProtectionActionType,
    ProtectionError,
    ProtectionManager,
    ProtectionPolicy,
    ProtectionStructure,
)

AS_OF = datetime(2026, 7, 10, 16, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(seconds=30)

BRACKET_POLICY = ProtectionPolicy(
    policy_version="protection-policy-test-1",
    structure=ProtectionStructure.BRACKET,
    missing_capability_action=MissingCapabilityAction.MANUAL,
)
PLAIN_POLICY = ProtectionPolicy(
    policy_version="protection-policy-test-2",
    structure=ProtectionStructure.PLAIN_STOP,
    missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
)

#: IMP-1002-shaped matrix: bracket/OCO NOT implemented.
NO_BRACKET_CAPABILITIES = BrokerCapabilities(
    order_types=dict(DEFAULT_CAPABILITIES.order_types),
    bracket=BrokerCapabilityMode.UNSUPPORTED,
    oco=BrokerCapabilityMode.UNSUPPORTED,
    trailing=BrokerCapabilityMode.MANUAL_ONLY,
    fractional=BrokerCapabilityMode.DISABLED,
    outside_rth=BrokerCapabilityMode.DISABLED,
    what_if=BrokerCapabilityMode.NATIVE,
    streaming_events=BrokerCapabilityMode.NATIVE,
)


def fill(
    fill_id: str = "fill-1",
    *,
    quantity: str = "60",
    target: str | None = "12",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        position_ref="pos-1",
        symbol="AAPL",
        environment=Environment.PAPER,
        entry_side=OrderSide.BUY,
        filled_quantity=Decimal(quantity),
        stop_price=Decimal("9"),
        target_price=Decimal(target) if target else None,
        observed_at=EARLIER,
        correlation_id="corr-1",
    )


# -- protect every fill / partial fills ---------------------------------------------


def test_first_fill_places_protection_for_exact_filled_quantity() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    plan = manager.on_fill(fill(quantity="60"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    assert plan.status is PlanStatus.OK
    stop = next(a for a in plan.actions if a.action is ProtectionActionType.PLACE_STOP)
    assert stop.spec.quantity == Decimal("60")  # actual fill, not requested
    assert stop.spec.side is OrderSide.SELL
    assert stop.spec.order_type is OrderType.STOP
    assert manager.unprotected_positions() == ()  # DoD invariant


def test_partial_fill_adjusts_protection_to_total_filled() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    manager.on_fill(fill("fill-1", quantity="60"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    plan = manager.on_fill(fill("fill-2", quantity="40"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    replace = next(a for a in plan.actions if a.action is ProtectionActionType.REPLACE_STOP)
    assert replace.spec.quantity == Decimal("100")  # 60 + 40
    assert replace.replaces_spec_ref is not None  # replace, not a second stop
    assert plan.protected_quantity == plan.filled_quantity == Decimal("100")
    assert manager.unprotected_positions() == ()


def test_bracket_native_emits_linked_target_in_oca_group() -> None:
    plan = ProtectionManager(BRACKET_POLICY).on_fill(
        fill(), DEFAULT_CAPABILITIES, as_of=AS_OF
    )
    kinds = {a.action for a in plan.actions}
    assert kinds == {ProtectionActionType.PLACE_STOP, ProtectionActionType.PLACE_TARGET}
    groups = {a.spec.oca_group for a in plan.actions}
    assert groups == {"oca:pos-1"}  # both legs linked - no double fill
    assert "STRUCTURE_BRACKET_NATIVE" in plan.reason_codes


# -- capability honesty: no silent emulation ------------------------------------------


def test_missing_bracket_capability_no_silent_emulation_stop_still_placed() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    plan = manager.on_fill(fill(), NO_BRACKET_CAPABILITIES, as_of=AS_OF)
    assert plan.status is PlanStatus.MANUAL  # policy missing_capability_action
    kinds = {a.action for a in plan.actions}
    assert kinds == {ProtectionActionType.PLACE_STOP}  # no target leg emitted
    assert "NO_SILENT_EMULATION" in plan.reason_codes
    assert "BRACKET_CAPABILITY_UNSUPPORTED" in plan.reason_codes
    # the stop leg is unlinked (no fake OCA) and the position is protected
    stop = plan.actions[0]
    assert stop.spec.oca_group is None
    assert manager.unprotected_positions() == ()  # DoD even without bracket


def test_missing_capability_needs_review_when_policy_says_so() -> None:
    policy = ProtectionPolicy(
        policy_version="p3",
        structure=ProtectionStructure.OCA,
        missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
    )
    plan = ProtectionManager(policy).on_fill(fill(), NO_BRACKET_CAPABILITIES, as_of=AS_OF)
    assert plan.status is PlanStatus.NEEDS_REVIEW
    assert "OCA_CAPABILITY_UNSUPPORTED" in plan.reason_codes


def test_plain_stop_policy_never_emits_target() -> None:
    plan = ProtectionManager(PLAIN_POLICY).on_fill(fill(), DEFAULT_CAPABILITIES, as_of=AS_OF)
    assert {a.action for a in plan.actions} == {ProtectionActionType.PLACE_STOP}
    assert plan.actions[0].spec.oca_group is None


def test_missing_stop_capability_fails_to_needs_review_never_unprotected_silently() -> None:
    broken = BrokerCapabilities(
        order_types={OrderType.LIMIT: BrokerCapabilityMode.NATIVE},  # no STOP
        bracket=BrokerCapabilityMode.UNSUPPORTED,
        oco=BrokerCapabilityMode.UNSUPPORTED,
        trailing=BrokerCapabilityMode.UNSUPPORTED,
        fractional=BrokerCapabilityMode.DISABLED,
        outside_rth=BrokerCapabilityMode.DISABLED,
        what_if=BrokerCapabilityMode.NATIVE,
        streaming_events=BrokerCapabilityMode.NATIVE,
    )
    manager = ProtectionManager(PLAIN_POLICY)
    plan = manager.on_fill(fill(), broken, as_of=AS_OF)
    assert plan.status is PlanStatus.NEEDS_REVIEW
    assert plan.actions == ()
    assert "POSITION_WOULD_BE_UNPROTECTED" in plan.reason_codes
    # the invariant makes the gap loudly visible instead of hiding it
    assert manager.unprotected_positions() == ("pos-1",)


# -- idempotency / duplicates ----------------------------------------------------------


def test_duplicate_fill_event_is_idempotent_no_double_protection() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    first = manager.on_fill(fill("dup-1"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    second = manager.on_fill(fill("dup-1"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    assert second == first  # same plan object content, nothing new
    assert first.filled_quantity == Decimal("60")  # not double counted
    assert manager.unprotected_positions() == ()


# -- partial target execution / KEEP_PROTECTION ----------------------------------------


def test_partial_target_execution_reduces_stop_to_remaining() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    manager.on_fill(fill(quantity="100"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    spec = manager.on_target_partial_execution("pos-1", Decimal("30"), as_of=AS_OF)
    assert spec.quantity == Decimal("70")  # stop reduced to the remainder
    assert manager.unprotected_positions() == ()


def test_reduce_protection_below_position_refused_keep_protection() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    manager.on_fill(fill(quantity="100"), DEFAULT_CAPABILITIES, as_of=AS_OF)
    with pytest.raises(ProtectionError, match="KEEP_PROTECTION"):
        manager.reduce_protection("pos-1", Decimal("40"))


def test_unknown_position_state_requires_reconcile() -> None:
    manager = ProtectionManager(BRACKET_POLICY)
    with pytest.raises(ProtectionError, match="reconcile first"):
        manager.on_target_partial_execution("ghost", Decimal("10"), as_of=AS_OF)


# -- fail-closed inputs ------------------------------------------------------------------


def test_invalid_inputs_fail_closed() -> None:
    from dataclasses import replace

    with pytest.raises(ProtectionError, match="stop_price"):
        replace(fill(), stop_price=Decimal("0"))
    with pytest.raises(ProtectionError, match="filled_quantity"):
        replace(fill(), filled_quantity=Decimal("0"))
    with pytest.raises(ProtectionError, match="policy_version"):
        ProtectionPolicy(
            policy_version=" ",
            structure=ProtectionStructure.PLAIN_STOP,
            missing_capability_action=MissingCapabilityAction.MANUAL,
        )
    with pytest.raises(ProtectionError, match="lookahead"):
        ProtectionManager(PLAIN_POLICY).on_fill(
            fill(), DEFAULT_CAPABILITIES, as_of=EARLIER - timedelta(minutes=5)
        )
    with pytest.raises(ProtectionError, match="timezone-aware"):
        ProtectionManager(PLAIN_POLICY).on_fill(
            fill(), DEFAULT_CAPABILITIES, as_of=datetime(2026, 7, 10, 16, 0)  # noqa: DTZ001
        )


def test_same_inputs_same_plans() -> None:
    def run() -> tuple[object, ...]:
        manager = ProtectionManager(BRACKET_POLICY)
        first = manager.on_fill(fill("f-1", quantity="60"), DEFAULT_CAPABILITIES, as_of=AS_OF)
        second = manager.on_fill(fill("f-2", quantity="40"), DEFAULT_CAPABILITIES, as_of=AS_OF)
        return (first, second)

    assert run() == run()


# -- hygiene -------------------------------------------------------------------------------


def test_protection_source_has_no_sdk_network_or_secret_access() -> None:
    from pathlib import Path

    import trading_execution_agent.protection as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord"):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for forbidden in ("ib" + "api", "httpx", "requests", "socket", "trading_broker_ibkr"):
            assert forbidden not in line, line
