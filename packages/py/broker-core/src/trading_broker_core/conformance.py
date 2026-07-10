"""Reusable BrokerPort conformance suite (IMP-1001).

Every broker adapter — the fake today, IBKR Paper (IMP-1002) and any later
broker — must pass this suite unchanged. An adapter's test module inherits
``BrokerPortConformance`` and implements the two factory hooks; pytest then
collects every ``test_conformance_*`` method against that adapter.

The suite asserts the PORT contract only (Integration 9.1/9.2/9.4/20.4,
Decision_Contract 12.5): fail-closed error taxonomy, authorization required
before preview/submit, idempotent duplicate submits, canonical cancel and
state mapping, append-only replayable events, capability discovery,
health/reconciliation behavior, tz-aware timestamps and determinism. It
never asserts anything broker-specific and never touches a network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

import pytest
from trading_contracts.enums import OrderState

from trading_broker_core.port import (
    BrokerErrorCode,
    BrokerPort,
    BrokerPortError,
    CancelOrderRequest,
    OrderIntent,
)


class BrokerPortConformance(ABC):
    """Inherit in an adapter's test module and implement the two hooks."""

    @abstractmethod
    def make_broker(self, *, healthy: bool = True) -> BrokerPort:
        """A fresh, NOT-yet-connected adapter over fixture state."""

    @abstractmethod
    def make_intent(self, client_order_id: str = "ord-1", **overrides: object) -> OrderIntent:
        """A valid intent for the fixture account/instrument of ``make_broker``."""

    # -- connection is a hard precondition ---------------------------------
    def test_conformance_operations_fail_closed_before_connect(self) -> None:
        broker = self.make_broker()
        with pytest.raises(BrokerPortError) as excinfo:
            broker.submit_order(self.make_intent())
        assert excinfo.value.code is BrokerErrorCode.NOT_CONNECTED

    def test_conformance_capability_discovery(self) -> None:
        broker = self.make_broker()
        broker.connect()
        capabilities = broker.capabilities()
        assert capabilities.order_types  # every adapter must declare its matrix
        # MVP hard rules (Integration 20.4)
        assert capabilities.fractional.value == "DISABLED"
        assert capabilities.outside_rth.value == "DISABLED"

    # -- authorization + idempotency ----------------------------------------
    def test_conformance_submit_requires_authorization(self) -> None:
        broker = self.make_broker()
        broker.connect()
        intent = self.make_intent(authorization_ref=" ")
        with pytest.raises(BrokerPortError) as excinfo:
            broker.submit_order(intent)
        assert excinfo.value.code is BrokerErrorCode.AUTHORIZATION_MISSING

    def test_conformance_duplicate_submit_is_idempotent(self) -> None:
        broker = self.make_broker()
        broker.connect()
        first = broker.submit_order(self.make_intent("dup-1"))
        second = broker.submit_order(self.make_intent("dup-1"))
        assert second == first  # original receipt, nothing new created
        events = list(broker.order_events())
        assert len([e for e in events if e.client_order_id == "dup-1"]) == 1

    def test_conformance_receipt_has_provenance_and_canonical_state(self) -> None:
        broker = self.make_broker()
        broker.connect()
        receipt = broker.submit_order(self.make_intent("ord-2"))
        assert receipt.state in tuple(OrderState)
        assert receipt.state is OrderState.ACKNOWLEDGED
        assert receipt.authorization_ref
        assert receipt.correlation_id
        assert receipt.broker_order_ref
        assert receipt.received_at.tzinfo is not None

    # -- cancel + status mapping ---------------------------------------------
    def test_conformance_cancel_returns_canonical_state(self) -> None:
        broker = self.make_broker()
        broker.connect()
        broker.submit_order(self.make_intent("ord-3"))
        receipt = broker.cancel_order(
            CancelOrderRequest(client_order_id="ord-3", correlation_id="corr-cancel")
        )
        assert receipt.state is OrderState.CANCELLED
        assert receipt.received_at.tzinfo is not None

    def test_conformance_cancel_unknown_order_fails_closed(self) -> None:
        broker = self.make_broker()
        broker.connect()
        with pytest.raises(BrokerPortError) as excinfo:
            broker.cancel_order(
                CancelOrderRequest(client_order_id="missing", correlation_id="corr-x")
            )
        assert excinfo.value.code is BrokerErrorCode.ORDER_NOT_FOUND

    # -- events are append-only and replayable --------------------------------
    def test_conformance_events_are_append_only_and_replayable(self) -> None:
        broker = self.make_broker()
        broker.connect()
        broker.submit_order(self.make_intent("ord-4"))
        broker.cancel_order(CancelOrderRequest(client_order_id="ord-4", correlation_id="c"))
        first_replay = list(broker.order_events())
        second_replay = list(broker.order_events())
        assert first_replay == second_replay
        sequences = [event.sequence for event in first_replay]
        assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
        assert [e.state for e in first_replay if e.client_order_id == "ord-4"] == [
            OrderState.ACKNOWLEDGED,
            OrderState.CANCELLED,
        ]

    # -- health / unknown state ------------------------------------------------
    def test_conformance_unhealthy_broker_blocks_submit(self) -> None:
        broker = self.make_broker(healthy=False)
        with pytest.raises(BrokerPortError) as excinfo:
            broker.connect()
            broker.submit_order(self.make_intent("ord-5"))
        assert excinfo.value.code in (
            BrokerErrorCode.BROKER_UNHEALTHY,
            BrokerErrorCode.NOT_CONNECTED,
        )

    def test_conformance_health_check_reports_canonical_shape(self) -> None:
        broker = self.make_broker()
        broker.connect()
        health = broker.health_check()
        assert isinstance(health.healthy, bool)
        assert health.checked_at.tzinfo is not None

    def test_conformance_unknown_account_fails_closed(self) -> None:
        broker = self.make_broker()
        broker.connect()
        with pytest.raises(BrokerPortError) as excinfo:
            broker.get_account_snapshot("no-such-account")
        assert excinfo.value.code is BrokerErrorCode.ACCOUNT_UNKNOWN

    # -- snapshots + reconciliation ---------------------------------------------
    def test_conformance_account_and_positions_shape(self) -> None:
        broker = self.make_broker()
        broker.connect()
        intent = self.make_intent("ord-6")
        snapshot = broker.get_account_snapshot(intent.account_alias)
        assert snapshot.net_liquidation_value >= 0
        assert snapshot.observed_at.tzinfo is not None
        positions = broker.get_positions(intent.account_alias)
        for position in positions:
            assert isinstance(position.quantity, Decimal)

    def test_conformance_reconcile_returns_full_snapshot(self) -> None:
        broker = self.make_broker()
        broker.connect()
        intent = self.make_intent("ord-7")
        broker.submit_order(intent)
        result = broker.reconcile(intent.account_alias)
        assert result.account_alias == intent.account_alias
        assert any(r.client_order_id == "ord-7" for r in result.open_orders)
        assert result.reconciled_at.tzinfo is not None

    # -- determinism ---------------------------------------------------------------
    def test_conformance_same_state_same_inputs_same_result(self) -> None:
        def run(self_: BrokerPortConformance) -> tuple[object, ...]:
            broker = self_.make_broker()
            broker.connect()
            receipt = broker.submit_order(self_.make_intent("ord-8"))
            cancel = broker.cancel_order(
                CancelOrderRequest(client_order_id="ord-8", correlation_id="c8")
            )
            return (receipt, cancel, tuple(broker.order_events()))

        assert run(self) == run(self)


__all__ = ["BrokerPortConformance"]
