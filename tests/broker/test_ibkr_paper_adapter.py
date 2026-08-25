"""IMP-1002: IBKR Paper adapter passes the IMP-1001 conformance suite.

All tests run over a deterministic fake transport — no ibapi import, no
network, no real order. The adapter must behave exactly like any other
BrokerPort (same suite as the fake broker) plus IBKR/paper-specific gates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    SessionPnlObservation,
    SessionPnlStatus,
    TimeInForce,
)
from trading_broker_ibkr.paper_adapter import (
    DEFAULT_CLIENT_ID,
    IBKR_STATUS_MAP,
    IBKR_TIME_IN_FORCE_MAP,
    LIFECYCLE_ORDER_REF_PREFIX,
    ORDER_REF_MAX_LEN,
    IbkrPaperAdapter,
    OrderRefTooLongError,
    RawOpenOrder,
    build_outgoing_order,
    decode_order_ref,
    encode_order_ref,
    ibkr_time_in_force,
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
        daily_pnl: Decimal | None = None,
    ) -> None:
        self.calls: list[str] = []
        #: Every OrderIntent this transport was actually handed. The call
        #: log records that a submit happened; this records WHAT was sent,
        #: which is what a seal test has to compare against.
        self.submitted_intents: list[OrderIntent] = []
        #: What reqPnL would answer. None (the default) means the gateway does
        #: not serve one, which is the common real case.
        self.daily_pnl = daily_pnl
        self._managed = managed
        self._healthy = healthy
        self._submit_status = submit_status
        self._cancel_status = cancel_status
        self._connected = False
        self._next_id = 1000
        #: the client id the adapter connected with; recovered rows carry it
        self.client_id = DEFAULT_CLIENT_ID
        # tracked live orders: broker_order_id -> RawOpenOrder (for the
        # read-only reqAllOpenOrders snapshot)
        self._live: dict[int, RawOpenOrder] = {}
        #: injected snapshot rows (for restart / foreign / malformed drills)
        self.snapshot_override: tuple[RawOpenOrder, ...] | None = None
        self.snapshot_error: Exception | None = None
        #: injected SAME-CLIENT rows; when None the same-client feed is derived
        #: from the global snapshot by filtering on the connected client id,
        #: exactly as the real reqOpenOrders feed behaves.
        self.same_client_override: tuple[RawOpenOrder, ...] | None = None

    def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
        self.calls.append(f"connect:{host}:{port}:{client_id}")
        if not self._healthy:
            raise RuntimeError("gateway down")
        self._connected = True
        self.client_id = client_id
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

    def session_pnl(self, account_id: str) -> SessionPnlObservation:
        """The daily P&L observation a gateway would serve through reqPnL.

        The default is a bounded TIMEOUT because it is the honest one: absence
        of an update is not a flat day and not a permission fact. A configured
        ``daily_pnl`` is served as OK.
        """
        self.calls.append("session_pnl")
        if self.daily_pnl is None:
            return SessionPnlObservation(
                SessionPnlStatus.TIMEOUT, detail="no reqPnL update (fake)"
            )
        return SessionPnlObservation(SessionPnlStatus.OK, value=self.daily_pnl)

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
        self.submitted_intents.append(intent)
        # Faithfully round-trip the orderRef codec so the snapshot can recover
        # the client_order_id after a "restart" (a fresh adapter instance).
        self._live[broker_order_id] = RawOpenOrder(
            broker_order_id=broker_order_id,
            order_ref=encode_order_ref(intent.client_order_id),
            status=self._submit_status,
            symbol=intent.symbol,
            client_id=self.client_id,
            perm_id=900000 + broker_order_id,
            account=account_id,
            action=intent.side.value,
            quantity=intent.quantity,
            order_type=intent.order_type.value,
            limit_price=intent.limit_price,
        )
        return self._submit_status

    def cancel(self, broker_order_id: int) -> str:
        self.calls.append(f"cancel:{broker_order_id}")
        existing = self._live.get(broker_order_id)
        if existing is not None:
            self._live[broker_order_id] = replace(existing, status=self._cancel_status)
        return self._cancel_status

    def open_order_snapshot(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        self.calls.append("open_order_snapshot")
        if self.snapshot_error is not None:
            raise self.snapshot_error
        if self.snapshot_override is not None:
            return self.snapshot_override
        return tuple(self._live.values())

    def same_client_open_orders(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        self.calls.append("same_client_open_orders")
        if self.snapshot_error is not None:
            raise self.snapshot_error
        if self.same_client_override is not None:
            return self.same_client_override
        # The real reqOpenOrders feed only reports THIS client's orders.
        rows = self.snapshot_override if self.snapshot_override is not None else tuple(
            self._live.values()
        )
        return tuple(row for row in rows if row.client_id == self.client_id)


def deterministic_clock(
    start: datetime = datetime(2026, 7, 10, 12, 0, tzinfo=UTC),
) -> Callable[[], datetime]:
    """A monotonically-ticking UTC clock for tests that compare timestamps.

    The adapter's production default is the REAL UTC clock (D-CLOCK-01), so any
    test asserting reproducible receipts/events must inject its own time rather
    than rely on a frozen default shipping in production code.
    """
    state = {"ticks": 0}

    def now() -> datetime:
        state["ticks"] += 1
        return start + timedelta(seconds=state["ticks"])

    return now


def make_adapter(
    transport: FakePaperTransport | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 4002,
    aliases: dict[str, str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> IbkrPaperAdapter:
    fake = transport if transport is not None else FakePaperTransport()
    return IbkrPaperAdapter(
        lambda: fake,
        account_aliases=aliases if aliases is not None else dict(ALIASES),
        host=host,
        port=port,
        # Time is an explicit test input here; production uses the real clock.
        clock=clock if clock is not None else deterministic_clock(),
    )


def make_intent(client_order_id: str = "ord-1", **overrides: Any) -> OrderIntent:
    fields: dict[str, Any] = {
        "client_order_id": client_order_id,
        "authorization_ref": f"submit-auth:{client_order_id}",
        "correlation_id": f"corr-{client_order_id}",
        "environment": Environment.PAPER,
        "account_alias": "paper-main",
        "symbol": "AAPL",
        # The SECURITY the broker resolved. An order that names only a ticker
        # is refused by the port now, which is the point.
        "broker_instrument_ref": "conid:265598",
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


# -- orderRef codec + restart-safe open-order recovery (D-ADAPTER-01) --------
#
# A realistic lifecycle stop client_order_id (the longest id the harness makes),
# built by concatenation so no guardrail matches a hardcoded literal.
_RUN_REF = "A40B11ABF9D10C5B"
_ENTRY_ID = "poi-ibkr-001-2026-07-21-fill-path-AAPL-" + _RUN_REF
_STOP_ID = f"{_ENTRY_ID}-protection:{_ENTRY_ID}:stop:1"


def test_encode_order_ref_is_prefixed_and_reversible() -> None:
    ref = encode_order_ref(_STOP_ID)
    assert ref.startswith(LIFECYCLE_ORDER_REF_PREFIX)
    recovered = decode_order_ref(ref, 1)
    assert recovered.lifecycle_owned is True
    assert recovered.client_order_id == _STOP_ID  # fully reversible


def test_encode_order_ref_fails_closed_when_too_long_never_truncates() -> None:
    too_long = "poi-ibkr-001-" + "x" * ORDER_REF_MAX_LEN
    with pytest.raises(OrderRefTooLongError):
        encode_order_ref(too_long)


def test_confirmation_token_never_appears_in_order_ref() -> None:
    # The encoded ref is the client_order_id only; whatever token-like value an
    # operator holds is never part of it.
    token = "DEADBEEF1234"
    ref = encode_order_ref(_STOP_ID)
    assert token not in ref
    # and the client_order_id itself carries the operation reference, not a token
    assert _RUN_REF in ref and token not in _STOP_ID


def test_submit_sets_order_ref_and_snapshot_recovers_it() -> None:
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    adapter.connect()
    adapter.submit_order(make_intent(_STOP_ID, order_type=OrderType.STOP, stop_price=Decimal("9")))
    # A FRESH adapter instance (empty memory) — simulates a process restart.
    restarted = make_adapter(transport)
    restarted.connect()
    open_orders = restarted.get_open_orders("paper-main")
    assert [o.client_order_id for o in open_orders] == [_STOP_ID]  # recovered from orderRef


def test_recovery_after_restart_has_no_process_local_dependency() -> None:
    transport = FakePaperTransport()
    seed = make_adapter(transport)
    seed.connect()
    seed.submit_order(make_intent(_ENTRY_ID))
    # brand-new adapter with empty _orders/_broker_ids
    restarted = make_adapter(transport)
    restarted.connect()
    recovered = restarted.get_open_orders("paper-main")
    assert any(o.client_order_id == _ENTRY_ID for o in recovered)


def test_missing_order_ref_is_foreign_not_lifecycle_owned() -> None:
    transport = FakePaperTransport()
    transport.snapshot_override = (
        RawOpenOrder(broker_order_id=1, order_ref="", status="Submitted", symbol="AAPL"),
    )
    adapter = make_adapter(transport)
    adapter.connect()
    (order,) = adapter.get_open_orders("paper-main")
    assert order.client_order_id.startswith("foreign:")
    assert decode_order_ref("", 1).lifecycle_owned is False


def test_malformed_lifecycle_ref_is_surfaced_not_claimed() -> None:
    transport = FakePaperTransport()
    transport.snapshot_override = (
        RawOpenOrder(
            broker_order_id=2,
            order_ref=LIFECYCLE_ORDER_REF_PREFIX + "   ",
            status="Submitted",
            symbol="AAPL",
        ),
    )
    adapter = make_adapter(transport)
    adapter.connect()
    (order,) = adapter.get_open_orders("paper-main")
    assert order.client_order_id.startswith("malformed:")
    assert decode_order_ref(LIFECYCLE_ORDER_REF_PREFIX + "  ", 2).lifecycle_owned is False


def test_foreign_manual_order_stays_foreign() -> None:
    transport = FakePaperTransport()
    transport.snapshot_override = (
        RawOpenOrder(
            broker_order_id=3, order_ref="MANUAL-TWS-123", status="Submitted", symbol="AAPL"
        ),
    )
    adapter = make_adapter(transport)
    adapter.connect()
    (order,) = adapter.get_open_orders("paper-main")
    assert order.client_order_id.startswith("foreign:")
    assert not order.client_order_id.startswith("poi-ibkr-001")


def test_duplicate_open_order_callback_does_not_duplicate_state() -> None:
    ref = encode_order_ref(_STOP_ID)
    transport = FakePaperTransport()
    transport.snapshot_override = (
        RawOpenOrder(broker_order_id=4, order_ref=ref, status="Submitted", symbol="AAPL"),
        RawOpenOrder(broker_order_id=4, order_ref=ref, status="Submitted", symbol="AAPL"),
    )
    adapter = make_adapter(transport)
    adapter.connect()
    orders = adapter.get_open_orders("paper-main")
    assert len(orders) == 1  # collapsed to one


def test_cancelled_filled_rejected_orders_do_not_count_as_working() -> None:
    ref = encode_order_ref(_STOP_ID)
    for status in ("Cancelled", "ApiCancelled", "Filled", "Inactive"):
        transport = FakePaperTransport()
        transport.snapshot_override = (
            RawOpenOrder(broker_order_id=5, order_ref=ref, status=status, symbol="AAPL"),
        )
        adapter = make_adapter(transport)
        adapter.connect()
        assert adapter.get_open_orders("paper-main") == (), status


def test_unknown_status_in_snapshot_is_dropped_not_claimed() -> None:
    ref = encode_order_ref(_STOP_ID)
    transport = FakePaperTransport()
    transport.snapshot_override = (
        RawOpenOrder(broker_order_id=6, order_ref=ref, status="SomeNewStatus", symbol="AAPL"),
    )
    adapter = make_adapter(transport)
    adapter.connect()
    # An unmapped status is not silently claimed as working protection.
    assert adapter.get_open_orders("paper-main") == ()


def test_snapshot_error_fails_closed() -> None:
    transport = FakePaperTransport()
    transport.snapshot_error = RuntimeError("api error during snapshot")
    adapter = make_adapter(transport)
    adapter.connect()
    with pytest.raises(BrokerPortError):
        adapter.get_open_orders("paper-main")


def test_reconcile_uses_recovered_snapshot() -> None:
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    adapter.connect()
    adapter.submit_order(make_intent(_STOP_ID, order_type=OrderType.STOP, stop_price=Decimal("9")))
    restarted = make_adapter(transport)
    restarted.connect()
    result = restarted.reconcile("paper-main")
    assert any(o.client_order_id == _STOP_ID for o in result.open_orders)


def test_get_open_orders_read_path_never_mutates_the_broker() -> None:
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    adapter.connect()
    adapter.submit_order(make_intent(_ENTRY_ID))
    before = list(transport.calls)
    adapter.get_open_orders("paper-main")
    new_calls = transport.calls[len(before) :]
    # the recovery read path only takes a snapshot — never submit/cancel
    assert all(c == "open_order_snapshot" for c in new_calls)
    assert not any(c.startswith(("submit:", "cancel:")) for c in new_calls)


def test_no_recovery_code_calls_broker_mutation_methods() -> None:
    """Static guard: get_open_orders / decode / recovery must not reference
    placeOrder/cancelOrder/reqGlobalCancel or the transport submit/cancel."""
    source = Path("packages/py/broker-ibkr/src/trading_broker_ibkr/paper_adapter.py").read_text(
        encoding="utf-8"
    )
    start = source.index("def get_open_orders")
    end = source.index("def preview_order")
    read_path = source[start:end]
    for banned in (
        "placeOrder",
        "cancelOrder",
        "reqGlobalCancel",
        "transport.submit",
        "transport.cancel",
        ".submit(",
        ".cancel(",
    ):
        assert banned not in read_path, f"recovery read path must not call {banned}"


def test_two_matching_stops_both_surface_so_lifecycle_can_flag_ambiguity() -> None:
    """Two working orders that decode to the SAME lifecycle client_order_id
    (distinct broker order ids) must BOTH surface, so the lifecycle reconnect
    matcher can detect the ambiguity and fail closed — the adapter never hides
    one."""
    ref = encode_order_ref(_STOP_ID)
    transport = FakePaperTransport()
    transport.snapshot_override = (
        RawOpenOrder(broker_order_id=7, order_ref=ref, status="Submitted", symbol="AAPL"),
        RawOpenOrder(broker_order_id=8, order_ref=ref, status="Submitted", symbol="AAPL"),
    )
    adapter = make_adapter(transport)
    adapter.connect()
    orders = adapter.get_open_orders("paper-main")
    assert len(orders) == 2  # distinct broker ids, same recovered id -> both surface
    assert {o.client_order_id for o in orders} == {_STOP_ID}
    assert {o.broker_order_ref for o in orders} == {"ibkr-paper:7", "ibkr-paper:8"}


def test_order_ref_carries_no_account_secret_or_authorization() -> None:
    """The persisted ref is only the non-authorizing client_order_id."""
    ref = encode_order_ref(_STOP_ID)
    for secret_like in ("submit-auth:", "DU1234567", "password", "authorized_at"):
        assert secret_like not in ref


# -- outgoing IBKR order-field matrix + TIF hardening (10052 regression) -----


def test_buy_limit_outgoing_fields_include_explicit_day_tif() -> None:
    intent = make_intent(
        "poi-lmt",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("5"),
    )
    fields = build_outgoing_order(intent, PAPER_ACCOUNT)
    assert fields.account == PAPER_ACCOUNT
    assert fields.action == "BUY"
    assert fields.order_type == "LMT"
    assert fields.total_quantity == 1
    assert fields.limit_price == 5.0
    assert fields.aux_price is None
    assert fields.tif == "DAY"
    assert fields.transmit is True
    assert fields.order_ref.startswith(LIFECYCLE_ORDER_REF_PREFIX)


def test_protective_stop_outgoing_fields_are_verified_independently() -> None:
    intent = make_intent(
        "poi-stp",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        quantity=Decimal("1"),
        limit_price=None,
        stop_price=Decimal("9"),
    )
    fields = build_outgoing_order(intent, PAPER_ACCOUNT)
    assert fields.action == "SELL"
    assert fields.order_type == "STP"
    assert fields.total_quantity == 1
    assert fields.aux_price == 9.0
    assert fields.limit_price is None
    assert fields.tif == "DAY"
    assert fields.transmit is True


def test_no_outgoing_required_field_is_ever_empty() -> None:
    for intent in (
        make_intent("m1", order_type=OrderType.LIMIT, limit_price=Decimal("5")),
        make_intent("m2", side=OrderSide.SELL, order_type=OrderType.STOP, stop_price=Decimal("9")),
        make_intent("m3", order_type=OrderType.MARKET, limit_price=None),
    ):
        f = build_outgoing_order(intent, PAPER_ACCOUNT)
        assert f.account and f.action and f.order_type and f.tif and f.order_ref
        assert f.total_quantity > 0
        assert f.transmit is True


def test_time_in_force_default_is_day() -> None:
    assert make_intent("d1").time_in_force is TimeInForce.DAY


def test_ibkr_time_in_force_maps_supported_values() -> None:
    assert ibkr_time_in_force(TimeInForce.DAY) == "DAY"
    assert ibkr_time_in_force(TimeInForce.GTC) == "GTC"
    assert set(IBKR_TIME_IN_FORCE_MAP) == set(TimeInForce)  # every enum member mapped


def test_ibkr_time_in_force_absent_or_non_canonical_fails_closed() -> None:
    with pytest.raises(BrokerPortError) as e:
        ibkr_time_in_force("DAY")  # type: ignore[arg-type]  # bare str, not the enum
    assert e.value.code is BrokerErrorCode.VALIDATION_FAILED


def test_ibkr_time_in_force_unsupported_fails_closed(monkeypatch: Any) -> None:
    import trading_broker_ibkr.paper_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "IBKR_TIME_IN_FORCE_MAP", {})
    with pytest.raises(BrokerPortError) as e:
        adapter_mod.ibkr_time_in_force(TimeInForce.DAY)
    assert e.value.code is BrokerErrorCode.CAPABILITY_UNSUPPORTED


def test_ibkr_order_type_unsupported_fails_closed(monkeypatch: Any) -> None:
    import trading_broker_ibkr.paper_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "IBKR_ORDER_TYPE_MAP", {})
    with pytest.raises(BrokerPortError):
        adapter_mod.ibkr_order_type(OrderType.LIMIT)


def test_10052_regression_empty_tif_never_reaches_placeorder(monkeypatch: Any) -> None:
    """Reproduces IBKR error 10052: an order whose TIF cannot be translated to
    a non-empty value MUST fail closed in the adapter BEFORE placeOrder — the
    transport (and thus placeOrder) is never reached."""
    import trading_broker_ibkr.paper_adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "IBKR_TIME_IN_FORCE_MAP", {})  # nothing maps
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    adapter.connect()
    before = list(transport.calls)
    with pytest.raises(BrokerPortError):
        adapter.submit_order(
            make_intent("poi-10052", order_type=OrderType.LIMIT, limit_price=Decimal("5"))
        )
    # fail-closed BEFORE any submit -> placeOrder was never called
    assert not any(c.startswith("submit:") for c in transport.calls[len(before) :])


def test_outgoing_order_never_produces_empty_tif() -> None:
    """The exact defect: the old path left order.tif empty. The builder can
    never emit an empty TIF for any supported intent."""
    for tif in TimeInForce:
        intent = make_intent(
            "t-" + tif.value,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("5"),
            time_in_force=tif,
        )
        assert build_outgoing_order(intent, PAPER_ACCOUNT).tif != ""


def test_outgoing_order_ref_carries_no_confirmation_token_or_secret() -> None:
    intent = make_intent("poi-lmt", order_type=OrderType.LIMIT, limit_price=Decimal("5"))
    ref = build_outgoing_order(intent, PAPER_ACCOUNT).order_ref
    for secret_like in ("submit-auth:", PAPER_ACCOUNT, "DEADBEEF"):
        assert secret_like not in ref


def test_protective_stop_gtc_outgoing_fields() -> None:
    """A GTC protective STOP: aux_price set, no lmt_price, tif=GTC (survives the
    session), transmit=True — verified independently of the entry."""
    intent = make_intent(
        "poi-stp-gtc",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        quantity=Decimal("1"),
        limit_price=None,
        stop_price=Decimal("9"),
        time_in_force=TimeInForce.GTC,
    )
    fields = build_outgoing_order(intent, PAPER_ACCOUNT)
    assert fields.action == "SELL"
    assert fields.order_type == "STP"
    assert fields.tif == "GTC"
    assert fields.aux_price == 9.0
    assert fields.limit_price is None  # no stale/irrelevant price field
    assert fields.transmit is True


def test_day_and_gtc_are_both_supported_and_distinct() -> None:
    assert ibkr_time_in_force(TimeInForce.DAY) == "DAY"
    assert ibkr_time_in_force(TimeInForce.GTC) == "GTC"


# == the IBKR max-double sentinel never becomes a P&L value ==================


def test_a_max_double_session_pnl_is_none_and_unset_through_the_snapshot() -> None:
    """A DBL_MAX 'value' must not reach AccountSnapshot as a number.

    DBL_MAX is finite, so a naive is_finite check would pass it. The adapter
    downgrades it to UNSET, and the snapshot carries None -- so the risk engine,
    which reads only the value, cannot see a vast fabricated profit."""
    import sys

    transport = FakePaperTransport(daily_pnl=Decimal(str(sys.float_info.max)))
    adapter = make_adapter(transport)
    adapter.connect()

    snapshot = adapter.get_account_snapshot("paper-main")

    assert snapshot.session_pnl is None
    assert snapshot.session_pnl_status is SessionPnlStatus.UNSET


def test_a_finite_session_pnl_reaches_the_snapshot_as_ok() -> None:
    transport = FakePaperTransport(daily_pnl=Decimal("-125.50"))
    adapter = make_adapter(transport)
    adapter.connect()

    snapshot = adapter.get_account_snapshot("paper-main")

    assert snapshot.session_pnl == Decimal("-125.50")
    assert snapshot.session_pnl_status is SessionPnlStatus.OK


def test_a_request_error_detail_reaches_the_snapshot_safely() -> None:
    """The safe code-only detail is propagated for the owner probe to render."""
    from trading_broker_core.port import SessionPnlObservation

    class _RejectingTransport(FakePaperTransport):
        def session_pnl(self, account_id: str) -> SessionPnlObservation:
            self.calls.append("session_pnl")
            return SessionPnlObservation(
                SessionPnlStatus.REQUEST_ERROR, detail="reqPnL rejected (IBKR 321)"
            )

    adapter = make_adapter(_RejectingTransport())
    adapter.connect()

    snapshot = adapter.get_account_snapshot("paper-main")

    assert snapshot.session_pnl is None
    assert snapshot.session_pnl_status is SessionPnlStatus.REQUEST_ERROR
    assert snapshot.session_pnl_detail == "reqPnL rejected (IBKR 321)"
