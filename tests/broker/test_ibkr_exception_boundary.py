"""Pre-merge audit of PR #53: the adapter's public exception boundary.

The lifecycle harness normalizes ``BrokerPortError`` only (``_probe`` catches
exactly that). Anything else escaping a public adapter method lands in
``run_lifecycle``'s generic handler and collapses the whole run into
"unknown error, failing closed" — the D-CLOCK-02 symptom that hid the account
state from the owner.

``health_check`` used to be exactly that hole: it calls
``transport.is_connected()``, which reaches into ``EClient.isConnected``, and
nothing guarded it — on the FIRST probe of every preflight.

These tests assert the invariant directly: **no public method of
``IbkrPaperAdapter`` may raise anything but ``BrokerPortError``**, whatever the
transport does. Fakes only; no ibapi, no network, no order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal

import pytest
from trading_broker_core.port import (
    BrokerInstrument,
    BrokerPortError,
    BrokerPosition,
    InstrumentQuery,
    OrderIntent,
)
from trading_broker_ibkr.paper_adapter import IbkrPaperAdapter, RawOpenOrder

from tests.broker.test_ibkr_paper_adapter import (
    ALIASES,
    PAPER_ACCOUNT,
    FakePaperTransport,
    deterministic_clock,
)


class Boom(Exception):
    """An arbitrary, non-canonical failure — e.g. anything ibapi may raise."""


class ExplodingTransport:
    """A transport whose every read raises an ARBITRARY exception."""

    def __init__(self, *, connect_ok: bool = True) -> None:
        self._connect_ok = connect_ok
        self.disconnected = False

    def session_pnl(self, account_id: str) -> Decimal | None:
        raise RuntimeError("arbitrary SDK explosion")

    def connect(self, host: str, port: int, client_id: int) -> tuple[str, ...]:
        if not self._connect_ok:
            raise Boom("connect exploded")
        return (PAPER_ACCOUNT,)

    def disconnect(self) -> None:
        self.disconnected = True
        raise Boom("disconnect exploded")

    def is_connected(self) -> bool:
        raise Boom("isConnected exploded")

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument | None:
        raise Boom("contract details exploded")

    def account_summary(self, account_id: str) -> Mapping[str, Decimal]:
        raise Boom("account summary exploded")

    def positions(self, account_id: str) -> tuple[BrokerPosition, ...]:
        raise Boom("positions exploded")

    def open_order_snapshot(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        raise Boom("reqAllOpenOrders exploded")

    def same_client_open_orders(self, account_id: str) -> tuple[RawOpenOrder, ...]:
        raise Boom("reqOpenOrders exploded")

    def next_order_id(self) -> int:
        raise Boom("nextValidId exploded")

    def submit(self, broker_order_id: int, account_id: str, intent: OrderIntent) -> str:
        raise Boom("placeOrder exploded")

    def cancel(self, broker_order_id: int) -> str:
        raise Boom("cancelOrder exploded")


def exploding_adapter() -> IbkrPaperAdapter:
    adapter = IbkrPaperAdapter(
        ExplodingTransport,
        account_aliases=dict(ALIASES),
        clock=deterministic_clock(),
    )
    adapter.connect()  # connect() itself succeeds; every later read explodes
    return adapter


#: Every public READ the harness's preflight / verify-clean paths can reach.
READ_METHODS: tuple[tuple[str, Callable[[IbkrPaperAdapter], object]], ...] = (
    ("get_account_snapshot", lambda a: a.get_account_snapshot("paper-main")),
    ("resolve_instrument", lambda a: a.resolve_instrument(InstrumentQuery(symbol="AAPL"))),
    ("get_open_orders", lambda a: a.get_open_orders("paper-main")),
    ("get_positions", lambda a: a.get_positions("paper-main")),
    ("recover_cancellable_orders", lambda a: a.recover_cancellable_orders("paper-main")),
    ("recover_working_orders", lambda a: a.recover_working_orders("paper-main")),
    ("reconcile", lambda a: a.reconcile("paper-main")),
)


@pytest.mark.parametrize(("name", "call"), READ_METHODS, ids=[n for n, _ in READ_METHODS])
def test_arbitrary_transport_failures_normalize_to_broker_port_error(
    name: str, call: Callable[[IbkrPaperAdapter], object]
) -> None:
    adapter = exploding_adapter()
    with pytest.raises(BrokerPortError) as error:
        call(adapter)
    # Fail-closed and typed: the harness can normalize this.
    assert error.value.code is not None
    # The arbitrary cause is chained (diagnosable) but never leaks as-is.
    assert isinstance(error.value.__cause__, Boom)
    # The TYPE reaches the message; the third-party text does not.
    assert "Boom" in str(error.value)
    assert "exploded" not in str(error.value)


def test_health_check_never_raises_and_reports_unhealthy() -> None:
    """The hole this audit found: is_connected() reaches into the SDK.

    An arbitrary failure must be answered as UNHEALTHY, not raised — that keeps
    the Execution Agent's tested BROKER_UNHEALTHY / NOT_SENT pre-submit refusal
    on its normal path instead of rerouting a safety gate through an exception.
    """
    adapter = exploding_adapter()

    health = adapter.health_check()  # must not raise

    assert health.healthy is False
    assert "Boom" in health.detail
    assert health.checked_at.tzinfo is not None
    # Not swallowed silently: the owner can see a probe failed.
    assert "unhealthy" in health.detail


def test_health_check_reports_not_connected_before_connect() -> None:
    adapter = IbkrPaperAdapter(
        FakePaperTransport, account_aliases=dict(ALIASES), clock=deterministic_clock()
    )
    health = adapter.health_check()
    assert health.healthy is False
    assert health.detail == "not connected"


def test_disconnect_normalizes_and_still_drops_the_transport() -> None:
    adapter = exploding_adapter()

    with pytest.raises(BrokerPortError) as error:
        adapter.disconnect()

    assert isinstance(error.value.__cause__, Boom)
    # The adapter must never be left holding a half-dead connection.
    assert adapter._transport is None
    # And a second disconnect is a harmless no-op.
    adapter.disconnect()


def test_connect_failure_is_normalized() -> None:
    adapter = IbkrPaperAdapter(
        lambda: ExplodingTransport(connect_ok=False),
        account_aliases=dict(ALIASES),
        clock=deterministic_clock(),
    )
    with pytest.raises(BrokerPortError) as error:
        adapter.connect()
    assert isinstance(error.value.__cause__, Boom)
    assert "Boom" in str(error.value)


def test_capabilities_and_order_events_cannot_fail() -> None:
    """Both are pure reads of in-process state — no transport, no I/O."""
    adapter = exploding_adapter()
    assert adapter.capabilities() is not None
    assert list(adapter.order_events()) == []


def test_submit_and_cancel_failures_are_also_normalized() -> None:
    """Not a read path, but the same boundary rule must hold."""
    from tests.broker.test_ibkr_paper_adapter import make_intent  # noqa: PLC0415

    adapter = exploding_adapter()
    with pytest.raises(BrokerPortError):
        adapter.submit_order(make_intent("ord-boom"))


def test_every_public_adapter_method_is_covered_by_this_audit() -> None:
    """If a new public method appears, this test forces it to be audited."""
    public = {
        name
        for name in dir(IbkrPaperAdapter)
        if not name.startswith("_") and callable(getattr(IbkrPaperAdapter, name))
    }
    audited = {
        "connect",
        "disconnect",
        "health_check",
        "capabilities",
        "order_events",
        "preview_order",
        "submit_order",
        "cancel_order",
        *(name for name, _ in READ_METHODS),
    }
    assert public == audited, (
        "a public adapter method is not covered by the exception-boundary audit:"
        f" {sorted(public ^ audited)}"
    )


def test_preview_order_does_not_touch_the_transport() -> None:
    """preview_order is a what-if: it must never reach the broker."""
    from tests.broker.test_ibkr_paper_adapter import make_intent  # noqa: PLC0415

    adapter = exploding_adapter()
    preview = adapter.preview_order(make_intent("ord-preview"))
    assert preview.client_order_id == "ord-preview"
