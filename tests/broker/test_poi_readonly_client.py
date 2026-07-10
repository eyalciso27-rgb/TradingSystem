"""POI-IBKR-READONLY concrete client drills (fake transport; no ibapi/network).

Proves the concrete RawReadOnlyClient orchestration end-to-end through the
real session, using an injected fake transport that replays the callback
sequence synchronously. No IBKR SDK is imported and no socket is opened.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from trading_broker_ibkr.poi_readonly_client import (
    IbkrReadOnlyRawClient,
    ReadOnlySink,
    build_readonly_client,
)
from trading_broker_ibkr.poi_readonly_session import (
    PoiSessionError,
    create_session,
)

PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"


class FakeTransport:
    """Replays the read-only callback sequence synchronously via the sink."""

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        symbols: Sequence[str] = ("MSFT", "AAPL"),
        epoch: int = 1_780_000_000,
        fatal: tuple[int, str] | None = None,
    ) -> None:
        self._account_id = account_id
        self._symbols = tuple(symbols)
        self._epoch = epoch
        self._fatal = fatal
        self._sink: ReadOnlySink | None = None
        self.started: tuple[str, int, int] | None = None
        self.stopped = False
        self.cancelled_summary: int | None = None
        self.cancelled_positions = False

    def start(self, host: str, port: int, client_id: int, sink: ReadOnlySink) -> None:
        self.started = (host, port, client_id)
        self._sink = sink
        if self._fatal is not None:
            sink.on_error(*self._fatal)
        else:
            sink.on_ready()

    def stop(self) -> None:
        self.stopped = True

    def request_current_time(self) -> None:
        assert self._sink is not None
        self._sink.on_current_time(self._epoch)

    def request_account_summary(self, req_id: int, tags: str) -> None:
        assert self._sink is not None
        self._sink.on_account_value(self._account_id, "NetLiquidation", "100000")
        self._sink.on_account_value(self._account_id, "TotalCashValue", "60000")
        self._sink.on_account_value(self._account_id, "BuyingPower", "120000")
        self._sink.on_account_end()

    def cancel_account_summary(self, req_id: int) -> None:
        self.cancelled_summary = req_id

    def request_positions(self) -> None:
        assert self._sink is not None
        for symbol in self._symbols:
            self._sink.on_position(symbol, 10)
        self._sink.on_positions_end()

    def cancel_positions(self) -> None:
        self.cancelled_positions = True


def make_client(transport: FakeTransport) -> IbkrReadOnlyRawClient:
    return IbkrReadOnlyRawClient(lambda: transport, client_id=0, timeout_seconds=2.0)


# -- concrete client through the real session -----------------------------------


def test_session_over_concrete_client_happy_path() -> None:
    transport = FakeTransport()
    session = create_session(make_client(transport))
    session.connect("127.0.0.1", 4002)
    assert transport.started == ("127.0.0.1", 4002, 0)
    assert session.req_current_time().startswith("2026-")  # iso, from epoch
    summary = session.req_account_summary()
    assert summary.account_id == PAPER_ACCOUNT
    assert summary.net_liquidation == "100000"
    assert transport.cancelled_summary == 9001  # read-only subscription torn down
    positions = session.req_positions()
    assert len(positions) == 2
    assert transport.cancelled_positions
    session.disconnect()
    assert transport.stopped


def test_concrete_client_rejects_live_port_directly() -> None:
    client = make_client(FakeTransport())
    with pytest.raises(PoiSessionError, match="LIVE API port"):
        client.open("127.0.0.1", 4001)


def test_concrete_client_rejects_public_host_directly() -> None:
    client = make_client(FakeTransport())
    with pytest.raises(PoiSessionError, match="only 127.0.0.1/localhost"):
        client.open("0.0.0.0", 4002)


def test_non_du_account_fails_closed_and_disconnects_via_client() -> None:
    transport = FakeTransport(account_id=LIVE_ACCOUNT)
    session = create_session(make_client(transport))
    session.connect("127.0.0.1", 4002)
    with pytest.raises(PoiSessionError, match="not a paper account") as excinfo:
        session.req_account_summary()
    assert LIVE_ACCOUNT not in str(excinfo.value)  # masked
    assert transport.stopped  # session.disconnect -> client.close -> transport.stop


def test_fatal_ibkr_error_fails_closed_on_connect() -> None:
    transport = FakeTransport(fatal=(502, "Couldn't connect to TWS"))
    session = create_session(make_client(transport))
    with pytest.raises(PoiSessionError, match="IBKR error 502"):
        session.connect("127.0.0.1", 4002)


def test_benign_info_message_is_not_fatal() -> None:
    # A >= 2000 info code (e.g. market-data farm status) must not fail closed.
    sink = ReadOnlySink()
    sink.on_error(2104, "Market data farm connection is OK")
    assert sink.fatal_error is None


def test_request_before_open_fails_closed() -> None:
    client = make_client(FakeTransport())
    with pytest.raises(PoiSessionError, match="not open"):
        client.current_time()


def test_build_readonly_client_returns_client_without_importing_sdk() -> None:
    import sys

    client = build_readonly_client()
    assert isinstance(client, IbkrReadOnlyRawClient)
    assert "ibapi" not in sys.modules  # lazy: only imported at connect time


# -- static safety of the client source -----------------------------------------


def test_client_source_has_no_order_api_and_no_secret_access() -> None:
    import trading_broker_ibkr.poi_readonly_client as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in (
        "place" + "Order",
        "cancel" + "Order",
        "reqGlobal" + "Cancel",
        "exercise" + "Options",
        "modify" + "Order",
        "replace" + "Order",
        "transmit",
        "submit",
    ):
        assert fragment not in source, fragment
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord", "usern" + "ame"):
        assert fragment not in source, fragment
