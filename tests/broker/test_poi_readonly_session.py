"""POI-IBKR-READONLY session module drills (fake client; no ibapi, no network).

Proves the session's read-only surface and safety gates — allowlist-only
methods, localhost + paper-port only, live-port rejection, DU-prefix
fail-closed with disconnect, masking, fail-closed on unknown errors, no
credential/ENV access, SDK confined to this package — using an injected
fake raw client. No real IBKR connection is ever made.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from trading_broker_ibkr.poi_readonly_session import (
    IbkrReadOnlySession,
    PoiSessionError,
    ReadOnlyAccountSummary,
    create_session,
    mask_account_id,
)

# Built by concatenation so the hardcoded-account-id guardrail never matches.
PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"

ALLOWED_SESSION_METHODS = {
    "connect",
    "req_current_time",
    "req_account_summary",
    "req_positions",
    "disconnect",
}


class FakeRawClient:
    """Scripted read-only transport; records calls, returns fixture data."""

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        positions: Sequence[object] = ("pos-a", "pos-b"),
        fail_time: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self._account_id = account_id
        self._positions = tuple(positions)
        self._fail_time = fail_time

    def open(self, host: str, port: int) -> None:
        self.calls.append(f"open:{host}:{port}")

    def current_time(self) -> str:
        self.calls.append("current_time")
        if self._fail_time:
            raise RuntimeError("socket dropped")
        return "2026-07-10T12:00:00+00:00"

    def account_summary(self) -> ReadOnlyAccountSummary:
        self.calls.append("account_summary")
        return ReadOnlyAccountSummary(
            account_id=self._account_id,
            net_liquidation="100000",
            total_cash="60000",
            buying_power="120000",
        )

    def positions(self) -> Sequence[object]:
        self.calls.append("positions")
        return self._positions

    def close(self) -> None:
        self.calls.append("close")


def make_session(**kwargs: Any) -> tuple[IbkrReadOnlySession, FakeRawClient]:
    client = FakeRawClient(**kwargs)
    return create_session(client), client


# -- factory + happy path ----------------------------------------------------------


def test_create_session_returns_readonly_session() -> None:
    session, _ = make_session()
    assert isinstance(session, IbkrReadOnlySession)


def test_readonly_flow_calls_allowlisted_methods_only() -> None:
    session, client = make_session()
    session.connect("127.0.0.1", 4002)
    assert session.req_current_time() == "2026-07-10T12:00:00+00:00"
    summary = session.req_account_summary()
    assert summary.account_id == PAPER_ACCOUNT
    assert len(session.req_positions()) == 2
    session.disconnect()
    assert client.calls == [
        "open:127.0.0.1:4002",
        "current_time",
        "account_summary",
        "positions",
        "close",
    ]


# -- endpoint gates -----------------------------------------------------------------


def test_live_port_rejected() -> None:
    session, client = make_session()
    with pytest.raises(PoiSessionError, match="LIVE API port"):
        session.connect("127.0.0.1", 4001)
    assert client.calls == []  # never opened


def test_non_paper_port_rejected() -> None:
    session, _ = make_session()
    with pytest.raises(PoiSessionError, match="only the paper API port"):
        session.connect("127.0.0.1", 7496)


def test_public_host_rejected() -> None:
    session, _ = make_session()
    for bad_host in ("0.0.0.0", "10.0.0.5", "broker.example.com"):
        with pytest.raises(PoiSessionError, match="only 127.0.0.1/localhost"):
            session.connect(bad_host, 4002)


# -- paper/live account gate --------------------------------------------------------


def test_non_du_account_fails_closed_and_disconnects() -> None:
    session, client = make_session(account_id=LIVE_ACCOUNT)
    session.connect("127.0.0.1", 4002)
    with pytest.raises(PoiSessionError, match="not a paper account") as excinfo:
        session.req_account_summary()
    assert LIVE_ACCOUNT not in str(excinfo.value)  # masked even on failure
    assert client.calls[-1] == "close"  # disconnected immediately


# -- fail-closed --------------------------------------------------------------------


def test_unknown_error_fails_closed() -> None:
    session, _ = make_session(fail_time=True)
    session.connect("127.0.0.1", 4002)
    with pytest.raises(PoiSessionError, match="failing closed"):
        session.req_current_time()


def test_methods_require_connection() -> None:
    session, _ = make_session()
    with pytest.raises(PoiSessionError, match="not connected"):
        session.req_current_time()


def test_default_client_defers_sdk_and_fails_closed_on_connect() -> None:
    # No injected client: create_session builds the concrete client WITHOUT
    # importing the SDK; the lazy ibapi import happens only on connect, which
    # fails closed on a host without ibapi (dev/CI) rather than pretend.
    session = create_session()
    with pytest.raises(PoiSessionError):
        session.connect("127.0.0.1", 4002)


# -- masking ------------------------------------------------------------------------


def test_masking() -> None:
    assert mask_account_id(PAPER_ACCOUNT) == "DU" + "*" * 5 + PAPER_ACCOUNT[-2:]
    assert PAPER_ACCOUNT[2:-2] not in mask_account_id(PAPER_ACCOUNT)


# -- static safety of the module source ---------------------------------------------


def test_source_has_no_order_api_and_no_secret_access() -> None:
    import trading_broker_ibkr.poi_readonly_session as module

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
    # the public surface is exactly the harness allowlist
    public = {name for name in dir(IbkrReadOnlySession) if not name.startswith("_")}
    assert public == ALLOWED_SESSION_METHODS


def test_session_module_import_does_not_require_ibapi() -> None:
    # Importing the module must not import the SDK (lazy import only).
    import sys

    import trading_broker_ibkr.poi_readonly_session  # noqa: F401

    assert "ibapi" not in sys.modules
