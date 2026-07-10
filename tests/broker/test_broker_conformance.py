"""FakeBroker passes the reusable BrokerPort conformance suite (IMP-1001)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from trading_broker_core.conformance import BrokerPortConformance
from trading_broker_core.fake import DEFAULT_CAPABILITIES, FAKE_ORDER_REF_PREFIX, FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilityMode,
    BrokerErrorCode,
    BrokerInstrument,
    BrokerOrderReceipt,
    BrokerPortError,
    BrokerPosition,
    CancelReceipt,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderType

OBSERVED_AT = datetime(2026, 7, 9, 11, 0, tzinfo=UTC)

ACCOUNT = AccountSnapshot(
    account_alias="paper-main",
    environment=Environment.PAPER,
    net_liquidation_value=Decimal("100000"),
    cash=Decimal("60000"),
    buying_power=Decimal("120000"),
    observed_at=OBSERVED_AT,
)
POSITIONS = (
    BrokerPosition(symbol="MSFT", quantity=Decimal("10"), average_price=Decimal("400")),
)
INSTRUMENTS = {
    "AAPL": BrokerInstrument(
        broker_instrument_ref="FAKE-INSTR-AAPL",
        symbol="AAPL",
        exchange="SMART",
        currency="USD",
    )
}


def make_fake(*, healthy: bool = True) -> FakeBroker:
    return FakeBroker(
        accounts={"paper-main": ACCOUNT},
        positions={"paper-main": POSITIONS},
        instruments=INSTRUMENTS,
        healthy=healthy,
    )


def make_intent(client_order_id: str = "ord-1", **overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "client_order_id": client_order_id,
        "authorization_ref": f"submit-auth:{client_order_id}",
        "correlation_id": f"corr-{client_order_id}",
        "environment": Environment.PAPER,
        "account_alias": "paper-main",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": Decimal("100"),
        "limit_price": Decimal("10"),
    }
    fields.update(overrides)
    return OrderIntent(**fields)


class TestFakeBrokerConformance(BrokerPortConformance):
    """The fake broker must pass the same suite every future adapter will."""

    def make_broker(self, *, healthy: bool = True) -> FakeBroker:
        return make_fake(healthy=healthy)

    def make_intent(self, client_order_id: str = "ord-1", **overrides: object) -> OrderIntent:
        return make_intent(client_order_id, **overrides)


# -- fake-specific behavior beyond the generic suite ------------------------------


def test_fake_order_refs_are_clearly_fake() -> None:
    broker = make_fake()
    broker.connect()
    receipt = broker.submit_order(make_intent("ord-ref"))
    assert receipt.broker_order_ref.startswith(FAKE_ORDER_REF_PREFIX)


def test_preview_requires_authorization_and_places_nothing() -> None:
    broker = make_fake()
    broker.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        broker.preview_order(make_intent("prev-1", authorization_ref=" "))
    assert excinfo.value.code is BrokerErrorCode.AUTHORIZATION_MISSING
    preview = broker.preview_order(make_intent("prev-1"))
    assert preview.estimated_notional == Decimal("1000")
    assert broker.get_open_orders("paper-main") == ()  # preview placed nothing
    assert list(broker.order_events()) == []


def test_duplicate_id_with_different_authorization_fails_closed() -> None:
    broker = make_fake()
    broker.connect()
    broker.submit_order(make_intent("dup-auth"))
    with pytest.raises(BrokerPortError) as excinfo:
        broker.submit_order(make_intent("dup-auth", authorization_ref="submit-auth:other"))
    assert excinfo.value.code is BrokerErrorCode.VALIDATION_FAILED


def test_unsupported_capability_fails_closed_no_silent_emulation() -> None:
    limited = FakeBroker(
        accounts={"paper-main": ACCOUNT},
        instruments=INSTRUMENTS,
        capabilities=DEFAULT_CAPABILITIES.__class__(
            order_types={OrderType.LIMIT: BrokerCapabilityMode.NATIVE},
            bracket=BrokerCapabilityMode.UNSUPPORTED,
            oco=BrokerCapabilityMode.UNSUPPORTED,
            trailing=BrokerCapabilityMode.UNSUPPORTED,
            fractional=BrokerCapabilityMode.DISABLED,
            outside_rth=BrokerCapabilityMode.DISABLED,
            what_if=BrokerCapabilityMode.NATIVE,
            streaming_events=BrokerCapabilityMode.NATIVE,
        ),
    )
    limited.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        limited.submit_order(
            make_intent("mk-1", order_type=OrderType.MARKET, limit_price=None)
        )
    assert excinfo.value.code is BrokerErrorCode.CAPABILITY_UNSUPPORTED


def test_fractional_quantity_rejected_mvp() -> None:
    broker = make_fake()
    broker.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        broker.submit_order(make_intent("frac-1", quantity=Decimal("1.5")))
    assert excinfo.value.code is BrokerErrorCode.VALIDATION_FAILED


def test_unknown_instrument_fails_closed() -> None:
    broker = make_fake()
    broker.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        broker.resolve_instrument(InstrumentQuery(symbol="NOPE"))
    assert excinfo.value.code is BrokerErrorCode.INSTRUMENT_NOT_FOUND


def test_intent_validation_fails_closed() -> None:
    with pytest.raises(ValueError, match="client_order_id"):
        make_intent(" ")
    with pytest.raises(ValueError, match="quantity"):
        make_intent("q0", quantity=Decimal("0"))
    with pytest.raises(ValueError, match="limit_price"):
        make_intent("lp", limit_price=None)
    with pytest.raises(ValueError, match="stop_price"):
        make_intent("sp", order_type=OrderType.STOP, limit_price=None, stop_price=None)


def test_naive_timestamps_rejected_in_port_types() -> None:
    naive = datetime(2026, 7, 9, 11, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        AccountSnapshot(
            account_alias="a",
            environment=Environment.PAPER,
            net_liquidation_value=Decimal("1"),
            cash=Decimal("1"),
            buying_power=Decimal("1"),
            observed_at=naive,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        BrokerOrderReceipt(
            client_order_id="x",
            broker_order_ref="FAKE-x",
            state=next(iter(broker_states())),
            authorization_ref="auth",
            correlation_id="c",
            received_at=naive,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        CancelReceipt(
            client_order_id="x",
            broker_order_ref="FAKE-x",
            state=next(iter(broker_states())),
            correlation_id="c",
            received_at=naive,
        )
    from trading_broker_core.fake import _deterministic_clock

    with pytest.raises(ValueError, match="timezone-aware"):
        _deterministic_clock(naive)


def broker_states() -> tuple[Any, ...]:
    from trading_contracts.enums import OrderState

    return (OrderState.ACKNOWLEDGED,)


def test_error_taxonomy_is_canonical_and_covered() -> None:
    codes = {code.value for code in BrokerErrorCode}
    assert codes == {
        "NOT_CONNECTED",
        "BROKER_UNHEALTHY",
        "CAPABILITY_UNSUPPORTED",
        "INSTRUMENT_NOT_FOUND",
        "ACCOUNT_UNKNOWN",
        "ORDER_NOT_FOUND",
        "AUTHORIZATION_MISSING",
        "VALIDATION_FAILED",
        "BROKER_REJECTED",
        "UNKNOWN_BROKER_RESPONSE",
    }
    error = BrokerPortError(BrokerErrorCode.BROKER_REJECTED, "msg", correlation_id="c-1")
    assert error.code is BrokerErrorCode.BROKER_REJECTED
    assert error.correlation_id == "c-1"
    assert "BROKER_REJECTED" in str(error)


def test_no_ibkr_network_or_secret_imports() -> None:
    import trading_broker_core.conformance as conformance_module
    import trading_broker_core.fake as fake_module
    import trading_broker_core.port as port_module

    for module in (port_module, fake_module, conformance_module):
        assert module.__file__ is not None
        source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
        import_lines = [
            line for line in source.splitlines() if line.strip().startswith(("import", "from"))
        ]
        for line in import_lines:
            for forbidden in (
                "ibapi", "trading_broker_ibkr", "httpx", "requests", "socket",
                "urllib", "subprocess", "os.environ", "trading_config",
            ):
                assert forbidden not in line, f"{module.__name__}: {line}"
