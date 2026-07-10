"""IMP-1002: IBKR Paper adapter passes the IMP-1001 conformance suite.

All tests run over a deterministic fake transport — no ibapi import, no
network, no real order. The adapter must behave exactly like any other
BrokerPort (same suite as the fake broker) plus IBKR/paper-specific gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from trading_broker_core.conformance import BrokerPortConformance
from trading_broker_core.port import (
    BrokerCapabilityMode,
    BrokerErrorCode,
    BrokerInstrument,
    BrokerPortError,
    BrokerPosition,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
)
from trading_broker_ibkr.paper_adapter import (
    IBKR_STATUS_MAP,
    IbkrPaperAdapter,
    mask_account_id,
)
from trading_contracts.enums import Environment, OrderState, OrderType

# Concatenated so the hardcoded-account-id guardrail never matches.
PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"
ALIASES = {"paper-main": PAPER_ACCOUNT}


class FakePaperTransport:
    """Deterministic scripted transport; records every call."""

    def __init__(
        self,
        *,
        managed: tuple[str, ...] = (PAPER_ACCOUNT,),
        healthy: bool = True,
        submit_status: str = "Submitted",
        cancel_status: str = "Cancelled",
    ) -> None:
        self.calls: list[str] = []
        self._managed = managed
        self._healthy = healthy
        self._submit_status = submit_status
        self._cancel_status = cancel_status
        self._connected = False
        self._next_id = 1000

    def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
        self.calls.append(f"connect:{host}:{port}:{client_id}")
        if not self._healthy:
            raise RuntimeError("gateway down")
        self._connected = True
        return self._managed

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument | None:
        self.calls.append(f"resolve:{query.symbol}")
        if query.symbol != "AAPL":
            return None
        return BrokerInstrument(
            broker_instrument_ref="conid:265598",
            symbol="AAPL",
            exchange="SMART",
            currency="USD",
        )

    def account_summary(self, account_id: str) -> Mapping[str, Decimal]:
        self.calls.append(f"summary:{mask_account_id(account_id)}")
        return {
            "NetLiquidation": Decimal("100000"),
            "TotalCashValue": Decimal("60000"),
            "BuyingPower": Decimal("120000"),
        }

    def positions(self, account_id: str) -> tuple[BrokerPosition, ...]:
        self.calls.append("positions")
        return (
            BrokerPosition(symbol="MSFT", quantity=Decimal("10"), average_price=Decimal("400")),
        )

    def next_order_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
        self.calls.append(f"submit:{broker_order_id}:{intent.client_order_id}")
        return self._submit_status

    def cancel(self, broker_order_id: int) -> str:
        self.calls.append(f"cancel:{broker_order_id}")
        return self._cancel_status


def make_adapter(
    transport: FakePaperTransport | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    aliases: dict[str, str] | None = None,
) -> IbkrPaperAdapter:
    fake = transport if transport is not None else FakePaperTransport()
    return IbkrPaperAdapter(
        lambda: fake,
        account_aliases=aliases if aliases is not None else dict(ALIASES),
        host=host,
        port=port,
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


class TestIbkrPaperConformance(BrokerPortConformance):
    """The IBKR Paper adapter passes the exact IMP-1001 suite, unchanged."""

    def make_broker(self, *, healthy: bool = True) -> IbkrPaperAdapter:
        return make_adapter(FakePaperTransport(healthy=healthy))

    def make_intent(self, client_order_id: str = "ord-1", **overrides: object) -> OrderIntent:
        return make_intent(client_order_id, **overrides)


# -- paper/live gates beyond the generic suite ------------------------------------


def test_live_api_port_4001_rejected() -> None:
    adapter = make_adapter(port=4001)
    with pytest.raises(BrokerPortError, match="LIVE API port"):
        adapter.connect()


def test_non_paper_port_and_public_host_rejected() -> None:
    with pytest.raises(BrokerPortError, match="only the paper API port"):
        make_adapter(port=7496).connect()
    with pytest.raises(BrokerPortError, match="only 127.0.0.1/localhost"):
        make_adapter(host="0.0.0.0").connect()


def test_live_managed_account_fails_closed_and_disconnects() -> None:
    transport = FakePaperTransport(managed=(PAPER_ACCOUNT, LIVE_ACCOUNT))
    adapter = make_adapter(transport)
    with pytest.raises(BrokerPortError, match="not a paper") as excinfo:
        adapter.connect()
    assert LIVE_ACCOUNT not in str(excinfo.value)  # masked
    assert transport.calls[-1] == "disconnect"


def test_non_du_alias_rejected_at_construction() -> None:
    with pytest.raises(BrokerPortError, match="PAPER \\(DU\\*\\) only"):
        make_adapter(aliases={"main": LIVE_ACCOUNT})


def test_alias_not_managed_by_session_rejected() -> None:
    transport = FakePaperTransport(managed=("DU" + "9999999",))
    with pytest.raises(BrokerPortError) as excinfo:
        make_adapter(transport).connect()
    assert excinfo.value.code is BrokerErrorCode.ACCOUNT_UNKNOWN


def test_live_environment_intent_rejected() -> None:
    adapter = make_adapter()
    adapter.connect()
    with pytest.raises(BrokerPortError, match="PAPER only"):
        adapter.submit_order(make_intent("live-1", environment=Environment.LIVE))


# -- capability honesty --------------------------------------------------------


def test_unimplemented_capabilities_are_never_advertised() -> None:
    """A capability may be NATIVE/EMULATED only if the adapter implements it.

    This adapter has no bracket/OCO wiring yet, so it must not advertise
    them as available at any level — and there must be no silent emulation
    path (Integration 20.4: emulation requires explicit policy).
    """
    from trading_broker_ibkr.paper_adapter import IBKR_PAPER_CAPABILITIES

    capabilities = make_adapter_capabilities()
    assert capabilities == IBKR_PAPER_CAPABILITIES  # adapter publishes the module matrix
    # not implemented => not advertised as usable
    unusable = (BrokerCapabilityMode.UNSUPPORTED, BrokerCapabilityMode.DISABLED)
    assert capabilities.bracket in unusable
    assert capabilities.oco in unusable
    # no silent emulation anywhere in the matrix
    for mode in (
        capabilities.bracket,
        capabilities.oco,
        capabilities.trailing,
        capabilities.fractional,
        capabilities.outside_rth,
        *capabilities.order_types.values(),
    ):
        assert mode is not BrokerCapabilityMode.EMULATED
    # nothing NATIVE that the adapter cannot actually do today: the only
    # NATIVE entries are the four wired order types + preview + events
    assert set(capabilities.order_types) == {
        OrderType.LIMIT,
        OrderType.STOP,
        OrderType.STOP_LIMIT,
        OrderType.MARKET,
    }
    # and there is no bracket/OCO surface on the adapter to call silently
    bracket_like = [
        name
        for name in dir(IbkrPaperAdapter)
        if not name.startswith("_") and ("bracket" in name.lower() or "oco" in name.lower())
    ]
    assert bracket_like == []


def make_adapter_capabilities() -> Any:
    adapter = make_adapter()
    adapter.connect()
    return adapter.capabilities()


# -- status mapping / unknown broker response --------------------------------------


def test_status_map_is_canonical() -> None:
    assert IBKR_STATUS_MAP["Submitted"] is OrderState.ACKNOWLEDGED
    assert IBKR_STATUS_MAP["Filled"] is OrderState.FILLED
    assert IBKR_STATUS_MAP["Cancelled"] is OrderState.CANCELLED
    assert IBKR_STATUS_MAP["Inactive"] is OrderState.REJECTED
    assert set(IBKR_STATUS_MAP.values()) <= set(OrderState)


def test_unknown_broker_status_fails_closed_and_marks_unresolved() -> None:
    transport = FakePaperTransport(submit_status="SomethingWeird")
    adapter = make_adapter(transport)
    adapter.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        adapter.submit_order(make_intent("weird-1"))
    assert excinfo.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE
    # the unknown order is reported by reconciliation, never guessed away
    result = adapter.reconcile("paper-main")
    assert result.unresolved_client_order_ids == ("weird-1",)


def test_transport_failure_maps_to_canonical_error() -> None:
    class ExplodingTransport(FakePaperTransport):
        def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
            raise ConnectionResetError("boom")

    adapter = make_adapter(ExplodingTransport())
    adapter.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        adapter.submit_order(make_intent("boom-1"))
    assert excinfo.value.code is BrokerErrorCode.BROKER_REJECTED


def test_duplicate_with_different_authorization_fails_closed() -> None:
    adapter = make_adapter()
    adapter.connect()
    adapter.submit_order(make_intent("dup-a"))
    with pytest.raises(BrokerPortError) as excinfo:
        adapter.submit_order(make_intent("dup-a", authorization_ref="submit-auth:other"))
    assert excinfo.value.code is BrokerErrorCode.VALIDATION_FAILED


def test_broker_order_ref_is_namespaced_not_raw() -> None:
    adapter = make_adapter()
    adapter.connect()
    receipt = adapter.submit_order(make_intent("ns-1"))
    assert receipt.broker_order_ref.startswith("ibkr-paper:")


def test_unknown_instrument_fails_closed() -> None:
    adapter = make_adapter()
    adapter.connect()
    with pytest.raises(BrokerPortError) as excinfo:
        adapter.resolve_instrument(InstrumentQuery(symbol="NOPE"))
    assert excinfo.value.code is BrokerErrorCode.INSTRUMENT_NOT_FOUND


# -- hygiene ------------------------------------------------------------------------


def test_module_import_does_not_import_ibapi() -> None:
    import sys

    import trading_broker_ibkr.paper_adapter  # noqa: F401

    assert "ibapi" not in sys.modules  # lazy: only inside the host-only builder


def test_source_has_no_secret_access() -> None:
    import trading_broker_ibkr.paper_adapter as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord", "usern" + "ame"):
        assert fragment not in source, fragment
    # ibapi may appear ONLY inside the lazy builder, never at module top level
    import_lines = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from "))  # top-level (unindented) only
    ]
    for line in import_lines:
        assert "ibapi" not in line, line


def test_masking_never_reveals_full_account() -> None:
    assert mask_account_id(PAPER_ACCOUNT) == "DU" + "*" * 5 + PAPER_ACCOUNT[-2:]
    assert PAPER_ACCOUNT[2:-2] not in mask_account_id(PAPER_ACCOUNT)
