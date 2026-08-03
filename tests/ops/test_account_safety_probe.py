"""WP1: the read-only account-safety probe (fakes only; no IBKR, no orders).

The probe's whole job is to refuse to say CLEAN unless it actually proved it.
These tests attack that from both directions: every failure mode must degrade
to UNKNOWN (never CLEAN), and every "something exists" case must be NOT_CLEAN
rather than being rounded down to a warning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerErrorCode,
    BrokerOrderReceipt,
    BrokerPortError,
    BrokerPosition,
    IntegrationHealth,
)
from trading_contracts.enums import Environment, OrderState
from trading_execution_agent.account_safety import (
    FORBIDDEN_BROKER_VERBS,
    AccountSafety,
    AccountSafetyReport,
    ProbeConfig,
    ReadOnlyBrokerFacade,
    SafetyReason,
    count_non_zero_positions,
    count_working_orders,
    probe_account_safety,
    sanitize_detail,
    validate_endpoint,
)
from trading_execution_agent.reconciliation import (
    Finding,
    FindingKind,
    Recommendation,
    ReconciliationReport,
    ReconciliationTrigger,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
ALIAS = "paper-poi"


def _snapshot(alias: str = ALIAS, env: Environment = Environment.PAPER) -> AccountSnapshot:
    return AccountSnapshot(
        account_alias=alias,
        environment=env,
        net_liquidation_value=Decimal("100000"),
        cash=Decimal("60000"),
        buying_power=Decimal("120000"),
        observed_at=NOW,
    )


def _receipt(state: OrderState, client_order_id: str = "ord-1") -> BrokerOrderReceipt:
    return BrokerOrderReceipt(
        client_order_id=client_order_id,
        broker_order_ref="ibkr-paper:1",
        state=state,
        authorization_ref="auth",
        correlation_id="corr",
        received_at=NOW,
    )


def _clean_reconciliation(**over: Any) -> ReconciliationReport:
    fields: dict[str, Any] = {
        "trigger": ReconciliationTrigger.PERIODIC,
        "account_alias": ALIAS,
        "reconciled": True,
        "findings": (),
        "entry_block_required": False,
        "recommended_halt": None,
        "policy_ref": "p",
        "engine_version": "v",
        "input_refs": (),
        "run_at": NOW,
    }
    fields.update(over)
    return ReconciliationReport(**fields)


class FakeView:
    """A read-only broker view whose every step can be made to fail."""

    def __init__(
        self,
        *,
        orders: tuple[BrokerOrderReceipt, ...] = (),
        positions: tuple[BrokerPosition, ...] = (),
        reconciliation: ReconciliationReport | None = None,
        healthy: bool = True,
        snapshot: AccountSnapshot | None = None,
        fail_on: str | None = None,
        failure: Exception | None = None,
    ) -> None:
        self._orders = orders
        self._positions = positions
        self._reconciliation = reconciliation or _clean_reconciliation()
        self._healthy = healthy
        self._snapshot = snapshot or _snapshot()
        self._fail_on = fail_on
        self._failure = failure or BrokerPortError(
            BrokerErrorCode.UNKNOWN_BROKER_RESPONSE, "boom"
        )
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self._fail_on == name:
            raise self._failure

    def connect(self) -> None:
        self._maybe_fail("connect")

    def disconnect(self) -> None:
        self._maybe_fail("disconnect")

    def health_check(self) -> IntegrationHealth:
        self._maybe_fail("health_check")
        return IntegrationHealth(healthy=self._healthy, detail="fake", checked_at=NOW)

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        self._maybe_fail("get_account_snapshot")
        return self._snapshot

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        self._maybe_fail("get_open_orders")
        return self._orders

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        self._maybe_fail("get_positions")
        return self._positions

    def order_events(self) -> Any:
        self._maybe_fail("order_events")
        return iter(())

    def reconcile(self, account_alias: str) -> Any:  # pragma: no cover - engine path
        self._maybe_fail("reconcile")
        return self._reconciliation


def probe(view: FakeView, **over: Any) -> AccountSafetyReport:
    """Run the probe with the reconciliation engine stubbed to the fake's report."""
    import trading_execution_agent.account_safety as module

    config = ProbeConfig(account_alias=ALIAS, **over)
    original = module._reconcile

    def stub(v: Any, c: Any, *, now: Any) -> Any:
        return v._reconciliation

    module._reconcile = stub  # type: ignore[assignment]
    try:
        return probe_account_safety(view, config, now=NOW)
    finally:
        module._reconcile = original


# -- CLEAN -------------------------------------------------------------------


def test_a_complete_empty_snapshot_is_clean() -> None:
    report = probe(FakeView())
    assert report.safety is AccountSafety.CLEAN
    assert report.reasons == (SafetyReason.ALL_CHECKS_PASSED,)
    assert report.working_orders == 0
    assert report.non_zero_positions == 0
    assert report.unresolved_states == 0
    assert report.reconciled is True
    assert report.connect_ok and report.api_round_trip_ok and report.disconnect_ok
    assert report.permits_unattended_action is True


def test_a_cancelled_order_is_not_a_working_order() -> None:
    report = probe(FakeView(orders=(_receipt(OrderState.CANCELLED),)))
    assert report.safety is AccountSafety.CLEAN
    assert report.working_orders == 0


def test_a_zero_quantity_row_is_not_a_position() -> None:
    """The D-CLOCK-02 lesson, enforced here too."""
    positions = (BrokerPosition(symbol="DVLT", quantity=Decimal(0), average_price=Decimal(2)),)
    report = probe(FakeView(positions=positions))
    assert report.safety is AccountSafety.CLEAN
    assert report.non_zero_positions == 0


# -- NOT_CLEAN ---------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        OrderState.SUBMITTING,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.CANCEL_PENDING,
    ],
)
def test_any_working_order_is_not_clean(state: OrderState) -> None:
    report = probe(FakeView(orders=(_receipt(state),)))
    assert report.safety is AccountSafety.NOT_CLEAN
    assert SafetyReason.WORKING_ORDER_PRESENT in report.reasons
    assert report.permits_unattended_action is False


def test_a_non_zero_position_is_not_clean() -> None:
    positions = (
        BrokerPosition(symbol="DVLT", quantity=Decimal("1352"), average_price=Decimal(2)),
    )
    report = probe(FakeView(positions=positions))
    assert report.safety is AccountSafety.NOT_CLEAN
    assert SafetyReason.NON_ZERO_POSITION_PRESENT in report.reasons
    assert report.non_zero_positions == 1


def test_a_short_position_is_also_not_clean() -> None:
    positions = (
        BrokerPosition(symbol="F", quantity=Decimal("-5"), average_price=Decimal(12)),
    )
    assert probe(FakeView(positions=positions)).safety is AccountSafety.NOT_CLEAN


def test_an_unresolved_broker_state_is_not_clean() -> None:
    finding = Finding(
        kind=FindingKind.UNRESOLVED_UNKNOWN_ORDER,
        subject_ref="ord-x",
        expected="resolved",
        observed="UNKNOWN",
        recommendation=Recommendation.NEEDS_REVIEW,
    )
    report = probe(
        FakeView(
            reconciliation=_clean_reconciliation(
                reconciled=False, findings=(finding,), entry_block_required=True
            )
        )
    )
    assert report.safety is AccountSafety.NOT_CLEAN
    assert SafetyReason.UNRESOLVED_STATE_PRESENT in report.reasons
    assert report.unresolved_states == 1


def test_any_reconciliation_finding_is_not_clean() -> None:
    finding = Finding(
        kind=FindingKind.UNEXPECTED_BROKER_ORDER,
        subject_ref="someone-else",
        expected="none",
        observed="working",
        recommendation=Recommendation.BLOCK_NEW_ENTRIES,
    )
    report = probe(
        FakeView(
            reconciliation=_clean_reconciliation(
                reconciled=False, findings=(finding,), entry_block_required=True
            )
        )
    )
    assert report.safety is AccountSafety.NOT_CLEAN
    assert SafetyReason.RECONCILIATION_BLOCKING_FINDING in report.reasons


# -- UNKNOWN -----------------------------------------------------------------


def test_connection_failure_is_unknown_never_clean() -> None:
    report = probe(FakeView(fail_on="connect"))
    assert report.safety is AccountSafety.UNKNOWN
    assert report.reasons == (SafetyReason.CONNECT_FAILED,)
    assert report.permits_unattended_action is False
    # Counts stay None: "unknown" must never be readable as "none".
    assert report.working_orders is None
    assert report.non_zero_positions is None


@pytest.mark.parametrize(
    ("fail_on", "reason"),
    [
        ("health_check", SafetyReason.HEALTH_CHECK_FAILED),
        ("get_account_snapshot", SafetyReason.API_ROUND_TRIP_FAILED),
        ("get_open_orders", SafetyReason.OPEN_ORDERS_UNREADABLE),
        ("get_positions", SafetyReason.POSITIONS_UNREADABLE),
        ("disconnect", SafetyReason.DISCONNECT_FAILED),
    ],
)
def test_every_incomplete_snapshot_step_is_unknown(
    fail_on: str, reason: SafetyReason
) -> None:
    report = probe(FakeView(fail_on=fail_on))
    assert report.safety is AccountSafety.UNKNOWN
    assert reason in report.reasons


def test_an_arbitrary_exception_is_unknown_not_a_crash() -> None:
    report = probe(FakeView(fail_on="get_positions", failure=TimeoutError("slow")))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.POSITIONS_UNREADABLE in report.reasons
    assert "TimeoutError" in report.detail


def test_an_unhealthy_broker_is_unknown() -> None:
    report = probe(FakeView(healthy=False))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.HEALTH_CHECK_FAILED in report.reasons


def test_a_non_paper_environment_is_unknown() -> None:
    report = probe(FakeView(snapshot=_snapshot(env=Environment.LIVE)))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ACCOUNT_NOT_PAPER in report.reasons


def test_an_account_mismatch_is_unknown() -> None:
    report = probe(FakeView(snapshot=_snapshot(alias="someone-else")))
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ACCOUNT_MISMATCH in report.reasons


def test_a_failing_current_time_probe_is_unknown() -> None:
    def boom() -> object:
        raise OSError("no answer")

    report = probe(FakeView(), current_time=boom)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.CURRENT_TIME_FAILED in report.reasons


def test_an_empty_current_time_response_is_unknown() -> None:
    report = probe(FakeView(), current_time=lambda: "")
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.CURRENT_TIME_FAILED in report.reasons


# -- endpoint envelope --------------------------------------------------------


def test_the_live_port_is_refused_before_connecting() -> None:
    view = FakeView()
    report = probe(view, port=4001)
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ENDPOINT_IS_LIVE_PORT in report.reasons
    assert view.calls == [], "nothing may be contacted on a refused endpoint"


def test_a_remote_host_is_refused_before_connecting() -> None:
    view = FakeView()
    report = probe(view, host="10.0.0.5")
    assert report.safety is AccountSafety.UNKNOWN
    assert SafetyReason.ENDPOINT_NOT_LOCALHOST in report.reasons
    assert view.calls == []


def test_any_other_port_is_refused() -> None:
    assert validate_endpoint("127.0.0.1", 7497) is SafetyReason.ENDPOINT_NOT_PAPER_PORT
    assert validate_endpoint("localhost", 4002) is None


# -- read-only structure ------------------------------------------------------


def test_the_facade_exposes_no_order_surface() -> None:
    """Structural, not conventional: the attribute must not exist."""

    class FullAdapter:
        def submit_order(self, intent: object) -> None: ...
        def cancel_order(self, request: object) -> None: ...
        def connect(self) -> None: ...

    facade = ReadOnlyBrokerFacade(FullAdapter())
    for verb in FORBIDDEN_BROKER_VERBS:
        assert not hasattr(facade, verb), verb
    with pytest.raises(AttributeError):
        facade.cancel_order(None)  # type: ignore[attr-defined]


def test_the_facade_forwards_only_reads() -> None:
    view = FakeView()
    facade = ReadOnlyBrokerFacade(view)
    facade.connect()
    assert facade.get_open_orders(ALIAS) == ()
    assert "connect" in view.calls


def test_the_facade_keeps_its_broker_private() -> None:
    facade = ReadOnlyBrokerFacade(FakeView())
    assert facade.__slots__ == ("_broker",)


def test_the_probe_never_calls_a_mutating_verb() -> None:
    view = FakeView()
    probe(view)
    for verb in FORBIDDEN_BROKER_VERBS:
        assert verb not in view.calls


# -- report hygiene -----------------------------------------------------------


def test_details_are_bounded_and_collapsed() -> None:
    assert sanitize_detail("a\n  b\tc") == "a b c"
    assert len(sanitize_detail("z" * 999)) == 200


def test_the_report_serializes_deterministically() -> None:
    payload = probe(FakeView()).as_dict()
    assert payload["safety"] == "CLEAN"
    assert payload["permits_unattended_action"] is True
    assert payload["reasons"] == ["ALL_CHECKS_PASSED"]
    assert str(payload["checked_at"]).endswith("+00:00")


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        AccountSafetyReport(
            safety=AccountSafety.UNKNOWN,
            reasons=(),
            checked_at=datetime(2026, 8, 3, 12, 0),  # noqa: DTZ001 - the point
        )


def test_only_clean_permits_an_unattended_action() -> None:
    for safety in AccountSafety:
        report = AccountSafetyReport(safety=safety, reasons=(), checked_at=NOW)
        assert report.permits_unattended_action is (safety is AccountSafety.CLEAN)


# -- counting helpers ---------------------------------------------------------


def test_counting_helpers_use_canonical_states() -> None:
    orders = (
        _receipt(OrderState.ACKNOWLEDGED, "a"),
        _receipt(OrderState.FILLED, "b"),
        _receipt(OrderState.CANCEL_PENDING, "c"),
    )
    assert count_working_orders(orders) == 2
    positions = (
        BrokerPosition(symbol="A", quantity=Decimal(0), average_price=Decimal(1)),
        BrokerPosition(symbol="B", quantity=Decimal(3), average_price=Decimal(1)),
        BrokerPosition(symbol="C", quantity=Decimal(-1), average_price=Decimal(1)),
    )
    assert count_non_zero_positions(positions) == 2
