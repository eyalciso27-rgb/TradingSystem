"""POI market-data session drills (fake client; no IBKR contact).

These prove the session invariants — read-only allowlist surface,
localhost + paper-port gate, DU account gate with immediate disconnect,
fail-closed guarding — and that the merged read-only harness script drives
this new session end to end through a fake client.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from trading_broker_ibkr.poi_market_data_client import ContractResolution, MarketDataQuote
from trading_broker_ibkr.poi_market_data_session import (
    IbkrMarketDataSession,
    RawMarketDataClient,
    create_session,
)
from trading_broker_ibkr.poi_readonly_session import PoiSessionError, ReadOnlyAccountSummary

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"


def make_quote(**overrides: Any) -> MarketDataQuote:
    values: dict[str, Any] = {
        "symbol": "AAPL",
        "requested_for": "symbol:AAPL",
        "market_data_type": 1,
        "bid": "191.20",
        "ask": "191.24",
        "last": "191.22",
        "bid_size": "300",
        "ask_size": "500",
        "last_size": "100",
        "exchange_timestamp": "2026-07-14T18:00:00+00:00",
        "received_at": "2026-07-14T18:30:00+00:00",
        "entitlement_errors": (),
        "missing_fields": (),
        "freshness": "REALTIME",
    }
    values.update(overrides)
    return MarketDataQuote(**values)


class FakeClient:
    """Scripted RawMarketDataClient: records calls, returns fixtures."""

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        resolution: ContractResolution | None = None,
        quote: MarketDataQuote | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._account_id = account_id
        self._resolution = resolution or ContractResolution("AAPL", True, True, "ok")
        self._quote = quote or make_quote()

    def open(self, host: str, port: int) -> None:
        self.calls.append(f"open:{host}:{port}")

    def current_time(self) -> str:
        self.calls.append("current_time")
        return "2026-07-14T18:30:00+00:00"

    def account_summary(self) -> ReadOnlyAccountSummary:
        self.calls.append("account_summary")
        return ReadOnlyAccountSummary(
            account_id=self._account_id,
            net_liquidation="100000",
            total_cash="60000",
            buying_power="120000",
        )

    def resolve_contract(self, symbol: str) -> ContractResolution:
        self.calls.append(f"resolve_contract:{symbol}")
        return self._resolution

    def market_data(
        self, symbol: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote:
        self.calls.append(f"market_data:{symbol}:{requested_market_data_type}")
        return self._quote

    def market_data_for_instrument(
        self, broker_instrument_ref: str, requested_market_data_type: int = 1
    ) -> MarketDataQuote:
        self.calls.append(
            f"market_data_for_instrument:{broker_instrument_ref}"
            f":{requested_market_data_type}"
        )
        return self._quote

    def close(self) -> None:
        self.calls.append("close")


def session(client: FakeClient) -> IbkrMarketDataSession:
    return create_session(client)


# -- create_session + happy path ---------------------------------------------------


def test_create_session_returns_market_data_session() -> None:
    assert isinstance(session(FakeClient()), IbkrMarketDataSession)


def test_connect_current_time_resolve_and_market_data() -> None:
    client = FakeClient()
    sess = session(client)
    sess.connect("127.0.0.1", 4002)
    assert sess.req_current_time() == "2026-07-14T18:30:00+00:00"
    assert sess.req_account_summary().account_id == PAPER_ACCOUNT
    resolution = sess.resolve_contract("AAPL")
    assert resolution.resolved is True
    quote = sess.req_market_data("AAPL")
    assert quote.market_data_type == 1
    sess.disconnect()
    assert client.calls[0] == "open:127.0.0.1:4002"
    assert client.calls[-1] == "close"


# -- endpoint + paper-account gates (reused from the readonly session) -------------


def test_non_localhost_and_live_port_rejected() -> None:
    with pytest.raises(PoiSessionError, match="only 127.0.0.1/localhost"):
        session(FakeClient()).connect("192.168.0.5", 4002)
    with pytest.raises(PoiSessionError, match="LIVE API port"):
        session(FakeClient()).connect("127.0.0.1", 4001)


def test_non_paper_account_fails_closed_and_disconnects() -> None:
    client = FakeClient(account_id=LIVE_ACCOUNT)
    sess = session(client)
    sess.connect("127.0.0.1", 4002)
    with pytest.raises(PoiSessionError, match="not a paper account"):
        sess.req_account_summary()
    assert client.calls[-1] == "close"  # immediate disconnect
    assert LIVE_ACCOUNT not in str(client.calls)  # id never echoed by the session


def test_requests_before_connect_fail_closed() -> None:
    sess = session(FakeClient())
    for call in (
        sess.req_current_time,
        sess.req_account_summary,
        lambda: sess.resolve_contract("AAPL"),
        lambda: sess.req_market_data("AAPL"),
    ):
        with pytest.raises(PoiSessionError, match="not connected"):
            call()


def test_client_exception_is_wrapped_fail_closed() -> None:
    class Boom(FakeClient):
        def market_data(
            self, symbol: str, requested_market_data_type: int = 1
        ) -> MarketDataQuote:
            raise RuntimeError("socket dropped")

    sess = session(Boom())
    sess.connect("127.0.0.1", 4002)
    with pytest.raises(PoiSessionError, match="failing closed"):
        sess.req_market_data("AAPL")


def test_req_market_data_forwards_requested_type_to_client() -> None:
    client = FakeClient()
    sess = session(client)
    sess.connect("127.0.0.1", 4002)
    sess.req_market_data("AAPL", 3)  # delayed
    assert "market_data:AAPL:3" in client.calls
    sess.req_market_data("AAPL")  # default realtime
    assert "market_data:AAPL:1" in client.calls


# -- read-only surface -------------------------------------------------------------


def test_session_has_no_order_surface() -> None:
    sess = session(FakeClient())
    for surface in ("place_order", "cancel_order", "reqGlobalCancel", "submit", "flatten"):
        assert not hasattr(sess, surface)


def test_no_order_methods_or_imports_in_session_source() -> None:
    import trading_broker_ibkr.poi_market_data_session as module

    assert module.__file__ is not None
    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    for fragment in (
        "place" + "Order",
        "cancel" + "Order",
        "reqGlobal" + "Cancel",
        "OrderIntent",
        "SubmitAuthorization",
    ):
        assert fragment not in source, fragment
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord"):
        assert fragment not in source, fragment
    top_level = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]
    for line in top_level:
        assert "ib" + "api" not in line, line


# -- integration: the merged harness script drives the new session -----------------


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location(
        "poi_ibkr_market_data_readonly_it",
        REPO_ROOT / "scripts" / "poi_ibkr_market_data_readonly.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_existing_harness_runs_against_new_fake_session_and_passes() -> None:
    harness = _load_harness()
    client = FakeClient()
    report = harness.run_market_data_poi(lambda: session(client), symbol="AAPL")
    assert report.passed
    rendered = report.render()
    assert "RESULT: PASS" in rendered
    assert "REALTIME" in rendered
    assert PAPER_ACCOUNT not in rendered  # masked by the harness
    assert harness.mask_account_id(PAPER_ACCOUNT) in rendered


def test_harness_fails_when_session_returns_delayed_when_realtime_expected() -> None:
    harness = _load_harness()
    client = FakeClient(quote=make_quote(market_data_type=3, freshness="DELAYED"))
    report = harness.run_market_data_poi(lambda: session(client), symbol="AAPL")
    assert not report.passed
    assert "expected REALTIME but classified DELAYED" in report.render()


def test_harness_fails_on_entitlement_error_from_session() -> None:
    harness = _load_harness()
    client = FakeClient(
        quote=make_quote(entitlement_errors=("354: not subscribed",))
    )
    report = harness.run_market_data_poi(lambda: session(client), symbol="AAPL")
    assert not report.passed
    assert "entitlement/permission error" in report.render()


def test_protocol_is_satisfied_by_fake_client() -> None:
    # Structural: FakeClient satisfies the RawMarketDataClient protocol.
    checked: RawMarketDataClient = FakeClient()
    assert checked.current_time() == "2026-07-14T18:30:00+00:00"
