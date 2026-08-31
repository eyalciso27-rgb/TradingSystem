"""Repeated account observations on ONE long-lived IBKR connection.

The owner-host defect this pins: readiness failed with

    [FAIL] account_mode_safety: session environment or account alias is unknown

on a run whose own evidence line said ``[OK] account: alias paper-poi
environment PAPER``. ``AccountSnapshot.environment`` is hard-coded ``PAPER`` by
the adapter, so an "unknown environment" can only mean the snapshot RAISED.

The cause is lifecycle, not wiring. ``reqAccountSummary`` is a SUBSCRIPTION and
``cancelAccountSummary`` is asynchronous and unacknowledged, while ONE execution
decision observes the account six times (evidence, risk, readiness, and
readiness again at the submission boundary). The old shape issued six
subscribe/cancel cycles for the same group within a second, each racing the
previous cancel; a gateway may legitimately refuse the duplicate.

These tests drive the REAL ``_IbApiPaperTransport``, the real ``_PaperApp``
callbacks and the real adapter over a stub SDK. Nothing connects to IBKR.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from trading_broker_core.port import (
    BrokerPortError,
    SessionPnlObservation,
    SessionPnlStatus,
)
from trading_broker_ibkr.paper_adapter import IbkrPaperAdapter

ACCOUNT = "DU" + "7654321"
ALIASES = {"paper-poi": ACCOUNT}
TAGS = (
    ("NetLiquidation", "100000"),
    ("TotalCashValue", "60000"),
    ("BuyingPower", "120000"),
)


class _StubEClient:
    """Models reqAccountSummary/cancelAccountSummary faithfully enough to test
    the lifecycle: a subscription, an asynchronous cancel, and a gateway that
    may refuse a duplicate while one is still active."""

    def __init__(self, wrapper: object) -> None:
        self.req_summary_ids: list[int] = []
        self.cancel_summary_ids: list[int] = []
        self.req_pnl_ids: list[int] = []
        self.cancel_pnl_ids: list[int] = []
        #: refuse a second reqAccountSummary while one is still active
        self.refuse_duplicate = False
        #: model an asynchronous cancel that has not landed yet
        self.slow_cancel = False
        #: values served on the next summary (so a refresh can differ)
        self.tags: tuple[tuple[str, str], ...] = TAGS
        self.active: set[int] = set()

    # -- connection handshake -------------------------------------------------
    def connect(self, host: str, port: int, client_id: int) -> None:
        self.managedAccounts(ACCOUNT)  # type: ignore[attr-defined]
        self.nextValidId(1)  # type: ignore[attr-defined]

    def run(self) -> None:
        return None

    def isConnected(self) -> bool:  # noqa: N802
        return True

    def disconnect(self) -> None:
        return None

    # -- account summary ------------------------------------------------------
    def reqAccountSummary(self, req_id: int, group: str, tags: str) -> None:  # noqa: N802
        self.req_summary_ids.append(req_id)
        if self.refuse_duplicate and self.active:
            self.error(req_id, 321, "Duplicate account summary request", "")  # type: ignore[attr-defined]
            return
        self.active.add(req_id)
        for tag, value in self.tags:
            self.accountSummary(req_id, ACCOUNT, tag, value, "USD")  # type: ignore[attr-defined]
        self.accountSummaryEnd(req_id)  # type: ignore[attr-defined]

    def cancelAccountSummary(self, req_id: int) -> None:  # noqa: N802
        self.cancel_summary_ids.append(req_id)
        if not self.slow_cancel:
            self.active.discard(req_id)

    # -- persistent P&L -------------------------------------------------------
    def reqPnL(self, req_id: int, account: str, model_code: str) -> None:  # noqa: N802
        self.req_pnl_ids.append(req_id)
        self.pnl(req_id, 0.0, 0.0, 0.0)  # type: ignore[attr-defined]

    def cancelPnL(self, req_id: int) -> None:  # noqa: N802
        self.cancel_pnl_ids.append(req_id)


def _install_stub_ibapi(monkeypatch: pytest.MonkeyPatch) -> None:
    package = types.ModuleType("ibapi")
    package.__path__ = []
    modules = {
        "ibapi": package,
        "ibapi.client": types.ModuleType("ibapi.client"),
        "ibapi.contract": types.ModuleType("ibapi.contract"),
        "ibapi.order": types.ModuleType("ibapi.order"),
        "ibapi.order_cancel": types.ModuleType("ibapi.order_cancel"),
        "ibapi.wrapper": types.ModuleType("ibapi.wrapper"),
    }
    modules["ibapi.client"].EClient = _StubEClient  # type: ignore[attr-defined]
    modules["ibapi.contract"].Contract = type("Contract", (), {})  # type: ignore[attr-defined]
    modules["ibapi.order"].Order = type("Order", (), {})  # type: ignore[attr-defined]
    modules["ibapi.order_cancel"].OrderCancel = type("OrderCancel", (), {})  # type: ignore[attr-defined]
    modules["ibapi.wrapper"].EWrapper = type("EWrapper", (), {})  # type: ignore[attr-defined]
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> Any:
    _install_stub_ibapi(monkeypatch)
    from trading_broker_ibkr.paper_adapter import _build_ibapi_paper_transport

    def _factory() -> Any:
        return _build_ibapi_paper_transport(
            timeout_seconds=0.3,
            session_pnl_timeout_seconds=0.3,
            account_summary_freshness_seconds=5.0,
            monotonic_clock=clock,
        )

    return _factory


@pytest.fixture
def transport(build: Any) -> Iterator[Any]:
    yield build()


# == repeated observations on one connection =================================


def test_repeated_account_summaries_make_one_gateway_round_trip(transport: Any) -> None:
    """Six port-level observations, one subscribe/cancel cycle."""
    for _ in range(6):
        values = transport.account_summary(ACCOUNT)
        assert values["NetLiquidation"] == Decimal("100000")

    assert len(transport._app.req_summary_ids) == 1, transport._app.req_summary_ids
    assert len(transport._app.cancel_summary_ids) == 1


def test_a_gateway_refusing_a_duplicate_no_longer_breaks_a_decision(
    transport: Any,
) -> None:
    """THE owner-host regression, reproduced and fixed.

    With an asynchronous cancel that has not landed, the OLD shape's second
    request was refused and raised -- which readiness reported as an "unknown
    environment". One shared observation removes the second request entirely.
    """
    transport._app.refuse_duplicate = True
    transport._app.slow_cancel = True

    for _ in range(6):
        values = transport.account_summary(ACCOUNT)
        assert values["BuyingPower"] == Decimal("120000")


# == a later read is a genuinely fresh read ==================================


def test_an_observation_older_than_the_window_is_re_requested(
    transport: Any, clock: _Clock
) -> None:
    """Not stale reuse: past the window a real request is issued again."""
    first = transport.account_summary(ACCOUNT)
    assert first["NetLiquidation"] == Decimal("100000")

    clock.advance(6.0)  # beyond the 5s window
    transport._app.tags = (
        ("NetLiquidation", "90000"),
        ("TotalCashValue", "50000"),
        ("BuyingPower", "110000"),
    )
    second = transport.account_summary(ACCOUNT)

    assert second["NetLiquidation"] == Decimal("90000"), "a later read must be fresh"
    assert len(transport._app.req_summary_ids) == 2


def test_the_observation_is_never_shared_across_accounts(transport: Any) -> None:
    transport.account_summary(ACCOUNT)
    other = transport.account_summary("DU" + "0000001")

    # A different account forces its own request, and this stub only reports
    # rows for ACCOUNT, so the other account legitimately gets nothing.
    assert len(transport._app.req_summary_ids) == 2
    assert other == {}


def test_a_failed_observation_is_not_committed_for_the_next_gate(
    transport: Any,
) -> None:
    """A refusal must not leave a half-built observation behind."""
    transport._app.refuse_duplicate = True
    transport._app.slow_cancel = True
    transport._app.active.add(999)  # poison the FIRST request

    with pytest.raises(BrokerPortError):
        transport.account_summary(ACCOUNT)

    # Nothing cached: the next gate issues a real request rather than reusing
    # a failure.
    transport._app.active.clear()
    transport._app.slow_cancel = False
    values = transport.account_summary(ACCOUNT)
    assert values["NetLiquidation"] == Decimal("100000")


# == the adapter, over the real transport ====================================


def _adapter(factory: Any) -> IbkrPaperAdapter:
    return IbkrPaperAdapter(
        factory,
        account_aliases=dict(ALIASES),
        host="127.0.0.1",
        port=4002,
        clock=lambda: datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
    )


def test_the_adapter_can_observe_the_account_repeatedly_on_one_connection(
    build: Any,
) -> None:
    """The execution sequence: evidence, risk and readiness all observe."""
    transports: list[Any] = []

    def factory() -> Any:
        made = build()
        transports.append(made)
        return made

    adapter = _adapter(factory)
    adapter.connect()

    snapshots = [adapter.get_account_snapshot("paper-poi") for _ in range(6)]

    assert len(snapshots) == 6
    for snapshot in snapshots:
        assert snapshot.environment.value == "PAPER"
        assert snapshot.account_alias == "paper-poi"
        assert snapshot.net_liquidation_value == Decimal("100000")
    # ONE gateway round trip served all six gates.
    assert len(transports[0]._app.req_summary_ids) == 1


def test_a_duplicate_refusing_gateway_no_longer_blocks_the_adapter(build: Any) -> None:
    transports: list[Any] = []

    def factory() -> Any:
        made = build()
        made._app.refuse_duplicate = True
        made._app.slow_cancel = True
        transports.append(made)
        return made

    adapter = _adapter(factory)
    adapter.connect()

    for _ in range(6):
        snapshot = adapter.get_account_snapshot("paper-poi")
        assert snapshot.environment.value == "PAPER"


# == the P&L subscription is untouched =======================================


def test_the_persistent_pnl_subscription_is_unaffected(transport: Any) -> None:
    """No regression: still exactly ONE reqPnL, never cancelled per snapshot."""
    for _ in range(6):
        transport.account_summary(ACCOUNT)
        transport.session_pnl(ACCOUNT)

    assert len(transport._app.req_pnl_ids) == 1, transport._app.req_pnl_ids
    assert transport._app.cancel_pnl_ids == []


# == connection lifecycle ====================================================


def test_disconnect_detaches_the_shared_observation(transport: Any) -> None:
    transport.account_summary(ACCOUNT)
    assert transport._summary_values is not None

    transport.disconnect()

    assert transport._summary_values is None
    assert transport._summary_account is None
    assert transport._summary_observed_monotonic is None


def test_a_reconnect_starts_with_clean_observation_state(build: Any) -> None:
    """A reconnect is a NEW transport, so nothing can carry over."""
    transports: list[Any] = []

    def factory() -> Any:
        made = build()
        transports.append(made)
        return made

    adapter = _adapter(factory)
    adapter.connect()
    adapter.get_account_snapshot("paper-poi")
    adapter.disconnect()
    adapter.connect()
    adapter.get_account_snapshot("paper-poi")

    assert len(transports) == 2, "reconnect must build a new transport"
    assert transports[0] is not transports[1]
    # The new connection made its OWN observation; nothing was inherited.
    assert len(transports[1]._app.req_summary_ids) == 1
    assert transports[1]._summary_values is not None


def test_a_late_callback_for_an_earlier_request_cannot_serve_a_later_one(
    transport: Any, clock: _Clock
) -> None:
    """A retired request's callback is archived, never reused as current."""
    transport.account_summary(ACCOUNT)
    first_req = transport._app.req_summary_ids[0]

    # A stale callback for the FIRST (already retired) request arrives late.
    transport._app.accountSummary(first_req, ACCOUNT, "NetLiquidation", "1", "USD")

    clock.advance(6.0)  # force a genuinely fresh request
    transport._app.tags = (("NetLiquidation", "77000"),)
    values = transport.account_summary(ACCOUNT)

    assert values["NetLiquidation"] == Decimal("77000"), "a stale callback leaked in"


# == P&L still fails closed in every non-OK form =============================


@pytest.mark.parametrize(
    "status",
    [
        SessionPnlStatus.TIMEOUT,
        SessionPnlStatus.REQUEST_ERROR,
        SessionPnlStatus.UNSET,
        SessionPnlStatus.STALE,
    ],
)
def test_every_non_ok_pnl_status_leaves_the_snapshot_value_absent(
    status: SessionPnlStatus,
) -> None:
    """Sharing ONE account observation must not make P&L look present.

    The account summary and the daily P&L are separate observations; only the
    summary is shared. A non-OK P&L still yields ``session_pnl=None``, which is
    what fails the daily-halt decision closed. UNKNOWN is never zero.
    """
    from tests.broker.test_ibkr_paper_adapter import FakePaperTransport, make_adapter

    class _PnlTransport(FakePaperTransport):
        def session_pnl(self, account_id: str) -> SessionPnlObservation:
            return SessionPnlObservation(status, detail="fake")

    adapter = make_adapter(_PnlTransport())
    adapter.connect()

    snapshot = adapter.get_account_snapshot("paper-main")

    assert snapshot.session_pnl is None, "a non-OK P&L must never carry a value"
    assert snapshot.session_pnl_status is status
    assert snapshot.session_pnl != 0


def test_an_ok_pnl_still_reaches_the_snapshot() -> None:
    """The control, so the test above cannot pass vacuously."""
    from tests.broker.test_ibkr_paper_adapter import FakePaperTransport, make_adapter

    adapter = make_adapter(FakePaperTransport(daily_pnl=Decimal("0")))
    adapter.connect()

    snapshot = adapter.get_account_snapshot("paper-main")

    assert snapshot.session_pnl == Decimal("0")
    assert snapshot.session_pnl_status is SessionPnlStatus.OK
