"""POI-IBKR market-data read-only harness drills (fake session; no IBKR).

These prove the harness invariants — read-only allowlist (no order/cancel
API reachable), paper-only port + DU account gate, localhost-only,
masking, entitlement detection, explicit canonical freshness
classification, fail-closed unknowns, no credential/ENV access. They do
NOT mark POI-MD-001 PASS: docs/poi/evidence/POI-MD-001.md stays NOT RUN
until the owner runs the real Gateway probe.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "poi_ibkr_market_data_readonly",
    REPO_ROOT / "scripts" / "poi_ibkr_market_data_readonly.py",
)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules["poi_ibkr_market_data_readonly"] = harness
_SPEC.loader.exec_module(harness)

# Built by concatenation so the guardrail for quoted hardcoded account ids
# never matches this fixture.
PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"


def make_quote(
    *,
    market_data_type: int | None = 1,
    bid: str | None = "191.20",
    ask: str | None = "191.24",
    last: str | None = "191.22",
    bid_size: str | None = "300",
    ask_size: str | None = "500",
    last_size: str | None = "100",
    received_at: str | None = "2026-07-14T18:30:00+00:00",
    entitlement_errors: tuple[str, ...] = (),
) -> Any:
    return harness.MarketDataQuote(
        symbol="AAPL",
        market_data_type=market_data_type,
        bid=bid,
        ask=ask,
        last=last,
        bid_size=bid_size,
        ask_size=ask_size,
        last_size=last_size,
        received_at=received_at,
        entitlement_errors=entitlement_errors,
    )


class FakeSession:
    """Scripted market-data session: records calls, returns fixture data."""

    def __init__(
        self,
        *,
        account_id: str = PAPER_ACCOUNT,
        quote: Any = None,
        resolved: bool = True,
        con_id_present: bool = True,
        fail_time: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.requested_types: list[int] = []
        self._account_id = account_id
        self._quote = quote if quote is not None else make_quote()
        self._resolved = resolved
        self._con_id_present = con_id_present
        self._fail_time = fail_time

    def connect(self, host: str, port: int) -> None:
        self.calls.append(f"connect:{host}:{port}")

    def req_current_time(self) -> str:
        self.calls.append("req_current_time")
        if self._fail_time:
            raise RuntimeError("socket dropped")
        return "2026-07-14T18:30:00+00:00"

    def req_account_summary(self) -> Any:
        self.calls.append("req_account_summary")
        return harness.AccountSummary(account_id=self._account_id)

    def resolve_contract(self, symbol: str) -> Any:
        self.calls.append(f"resolve_contract:{symbol}")
        return harness.ContractResolution(
            symbol=symbol,
            resolved=self._resolved,
            con_id_present=self._con_id_present,
            detail="" if self._resolved else "no security definition",
        )

    def req_market_data(self, symbol: str, requested_market_data_type: int = 1) -> Any:
        self.calls.append(f"req_market_data:{symbol}")
        self.requested_types.append(requested_market_data_type)
        return self._quote

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    # Mutating methods that MUST never be reachable through the guard.
    def place_order(self) -> None:  # pragma: no cover - must not run
        raise AssertionError("order method was reachable")

    def cancel_order(self) -> None:  # pragma: no cover - must not run
        raise AssertionError("cancel method was reachable")


def run(session: FakeSession, **kwargs: Any) -> Any:
    return harness.run_market_data_poi(lambda: session, **kwargs)


# -- happy path -------------------------------------------------------------------


def test_realtime_run_passes_with_masked_account_and_public_quote() -> None:
    session = FakeSession()
    report = run(session)
    assert report.passed
    rendered = report.render()
    assert "RESULT: PASS" in rendered
    assert PAPER_ACCOUNT not in rendered  # full account id never printed
    assert harness.mask_account_id(PAPER_ACCOUNT) in rendered
    assert "REALTIME" in rendered
    assert "191.22" in rendered  # public market quote may be shown
    assert session.calls[0] == "connect:127.0.0.1:4002"
    assert session.calls[-1] == "disconnect"


def test_same_inputs_same_report() -> None:
    first = run(FakeSession()).render()
    second = run(FakeSession()).render()
    assert first == second


# -- endpoint / paper-account gates ------------------------------------------------


def test_live_api_port_rejected() -> None:
    with pytest.raises(harness.PoiFailure, match="port 4001 refused"):
        harness.validate_endpoint("127.0.0.1", 4001)
    with pytest.raises(harness.PoiFailure, match="refused"):
        harness.validate_endpoint("127.0.0.1", 7496)


def test_public_host_rejected_no_override_exists() -> None:
    for bad_host in ("0.0.0.0", "192.168.1.5", "gateway.example.com"):
        with pytest.raises(harness.PoiFailure, match="only 127.0.0.1/localhost"):
            harness.validate_endpoint(bad_host, harness.PAPER_API_PORT)
    assert "--allow-remote" not in Path(harness.__file__ or "").read_text(encoding="utf-8")


def test_non_paper_account_fails_closed_and_disconnects() -> None:
    session = FakeSession(account_id=LIVE_ACCOUNT)
    report = run(session)
    assert not report.passed
    rendered = report.render()
    assert "RESULT: FAIL" in rendered
    assert "not a paper account" in rendered
    assert LIVE_ACCOUNT not in rendered  # masked even on failure
    assert session.calls[-1] == "disconnect"
    assert "req_market_data:AAPL" not in session.calls  # nothing after the gate


# -- read-only allowlist -----------------------------------------------------------


def test_guard_blocks_order_and_cancel_and_everything_outside_allowlist() -> None:
    guard = harness.ReadOnlyGuard(FakeSession())
    for forbidden in (
        "place_order",
        "cancel_order",
        "reqGlobalCancel",
        "placeOrder",
        "cancelOrder",
        "modify",
        "submit",
    ):
        with pytest.raises(harness.ReadOnlyViolation):
            getattr(guard, forbidden)
    with pytest.raises(harness.ReadOnlyViolation):
        guard.anything = "nope"
    assert harness.ALLOWED_METHODS == {
        "connect",
        "req_current_time",
        "req_account_summary",
        "resolve_contract",
        "req_market_data",
        "disconnect",
    }


def test_static_no_order_api_and_no_secret_access_in_source() -> None:
    assert harness.__file__ is not None
    source = Path(harness.__file__).read_text(encoding="utf-8")
    for fragment in (
        "place" + "Order",
        "cancel" + "Order",
        "reqGlobal" + "Cancel",
        "exercise" + "Options",
        "modify" + "Order",
        "transmit",
    ):
        assert fragment not in source, fragment
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord"):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for forbidden in ("ib" + "api", "trading_broker_ibkr", "httpx", "requests", "socket"):
            assert forbidden not in line, line


# -- contract resolution -----------------------------------------------------------


def test_unresolved_contract_fails_closed() -> None:
    session = FakeSession(resolved=False, con_id_present=False)
    report = run(session)
    assert not report.passed
    assert "did not resolve" in report.render()
    assert session.calls[-1] == "disconnect"
    assert "req_market_data:AAPL" not in session.calls


# -- market data type + freshness classification -----------------------------------


def test_freshness_classification_mapping_is_explicit() -> None:
    assert harness.classify_freshness(1) == "REALTIME"
    assert harness.classify_freshness(2) == "STALE"  # frozen
    assert harness.classify_freshness(3) == "DELAYED"
    assert harness.classify_freshness(4) == "STALE"  # delayed-frozen
    assert harness.classify_freshness(99) == "UNKNOWN"
    assert harness.classify_freshness(None) == "UNKNOWN"
    assert harness.market_data_type_label(1) == "REALTIME"
    assert harness.market_data_type_label(4) == "DELAYED_FROZEN"
    assert harness.market_data_type_label(None) == "UNKNOWN"


def test_delayed_when_realtime_expected_fails() -> None:
    session = FakeSession(quote=make_quote(market_data_type=3))
    report = run(session)
    assert not report.passed
    rendered = report.render()
    assert "expected REALTIME but classified DELAYED" in rendered
    # the class is still recorded explicitly
    assert "canonical freshness=DELAYED" in rendered


def test_unknown_market_data_type_fails_closed() -> None:
    session = FakeSession(quote=make_quote(market_data_type=99))
    report = run(session)
    assert not report.passed
    rendered = report.render()
    assert "UNKNOWN" in rendered


def test_off_hours_mode_records_frozen_without_failing() -> None:
    session = FakeSession(quote=make_quote(market_data_type=2))
    report = run(session, require_realtime=False)
    assert report.passed  # frozen accepted in observe mode
    rendered = report.render()
    assert "canonical freshness=STALE" in rendered
    assert "off-hours observe mode" in rendered


# -- missing fields / timestamp / entitlement --------------------------------------


def test_missing_bid_ask_last_fails() -> None:
    session = FakeSession(quote=make_quote(bid=None, last="  "))
    report = run(session)
    assert not report.passed
    assert "missing field(s)" in report.render()


def test_missing_timestamp_fails() -> None:
    session = FakeSession(quote=make_quote(received_at=None))
    report = run(session)
    assert not report.passed
    assert "received_at=None" in report.render()


def test_entitlement_error_fails() -> None:
    session = FakeSession(
        quote=make_quote(
            entitlement_errors=("354: requested market data is not subscribed",)
        )
    )
    report = run(session)
    assert not report.passed
    rendered = report.render()
    assert "entitlement/permission error" in rendered
    assert "not subscribed" in rendered


# -- fail-closed behavior ----------------------------------------------------------


def test_unknown_error_fails_closed_with_disconnect() -> None:
    session = FakeSession(fail_time=True)
    report = run(session)
    assert not report.passed
    assert "failing closed" in report.render()
    assert session.calls[-1] == "disconnect"


def test_session_module_must_expose_factory() -> None:
    with pytest.raises(harness.PoiFailure, match="create_session"):
        harness._load_session_factory("json")  # stdlib module without the hook


def test_masking() -> None:
    assert harness.mask_account_id(PAPER_ACCOUNT) == "DU" + "*" * 5 + PAPER_ACCOUNT[-2:]
    assert PAPER_ACCOUNT[2:-2] not in harness.mask_account_id(PAPER_ACCOUNT)
    assert harness.mask_account_id("DU12") == "DU**"


# -- market-data mode: realtime vs. explicit delayed -------------------------------


def test_realtime_mode_requests_type_1() -> None:
    session = FakeSession()
    report = run(session, mode="realtime")
    assert report.passed
    assert session.requested_types == [1]  # reqMarketDataType(1)
    assert "MODE: realtime" in report.render()


def test_delayed_mode_requests_type_3() -> None:
    session = FakeSession(quote=make_quote(market_data_type=3))
    run(session, mode="delayed")
    assert session.requested_types == [3]  # reqMarketDataType(3)


def test_delayed_mode_passes_development_only_never_poi_md_pass() -> None:
    session = FakeSession(quote=make_quote(market_data_type=3))
    report = run(session, mode="delayed")
    assert report.passed
    assert report.result_label == "PASS_DEVELOPMENT_DELAYED"
    rendered = report.render()
    assert "RESULT: PASS_DEVELOPMENT_DELAYED" in rendered
    assert "RESULT: PASS\n" not in rendered  # never a plain POI-MD-001 PASS
    assert "DELAYED DATA - RESEARCH/DEVELOPMENT ONLY" in rendered
    assert "NOT a POI-MD-001 PASS" in rendered


def test_delayed_mode_is_never_execution_eligible() -> None:
    session = FakeSession(quote=make_quote(market_data_type=4))  # delayed-frozen
    report = run(session, mode="delayed")
    assert report.execution_eligible is False
    rendered = report.render()
    assert "EXECUTION_ELIGIBLE: false" in rendered
    assert "delayed-mode class=DELAYED_FROZEN" in rendered


def test_delayed_quote_with_bid_ask_last_accepted_as_development_only() -> None:
    session = FakeSession(
        quote=make_quote(market_data_type=3, bid="191.10", ask="191.14", last="191.12")
    )
    report = run(session, mode="delayed")
    assert report.passed
    rendered = report.render()
    assert "bid=191.10 ask=191.14 last=191.12" in rendered
    assert report.result_label == "PASS_DEVELOPMENT_DELAYED"


def test_no_silent_fallback_realtime_requested_delayed_received_fails() -> None:
    session = FakeSession(quote=make_quote(market_data_type=3))
    # default realtime, require_realtime True
    report = run(session)
    assert not report.passed
    assert "expected REALTIME but classified DELAYED" in report.render()


def test_off_hours_still_fails_on_delayed_downgrade() -> None:
    session = FakeSession(quote=make_quote(market_data_type=3))
    report = run(session, require_realtime=False)  # off-hours, realtime mode
    assert not report.passed
    assert "no silent fallback to delayed" in report.render()


def test_delayed_requested_but_realtime_received_reported_explicitly() -> None:
    session = FakeSession(quote=make_quote(market_data_type=1))
    report = run(session, mode="delayed")
    assert not report.passed  # type 1 is not an accepted delayed class
    rendered = report.render()
    assert "reported explicitly, not reclassified" in rendered
    assert "marketDataType=1" in rendered


def test_realtime_mode_clean_data_is_execution_eligible_as_data_freshness() -> None:
    session = FakeSession()  # realtime type 1, clean
    report = run(session)
    assert report.execution_eligible is True
    assert "EXECUTION_ELIGIBLE: true" in report.render()


def test_entitlement_error_still_preserved_in_realtime_mode() -> None:
    session = FakeSession(
        quote=make_quote(entitlement_errors=("354: not subscribed",))
    )
    report = run(session)
    assert not report.passed
    assert report.execution_eligible is False
    assert "entitlement/permission error" in report.render()


def test_unknown_mode_fails_closed() -> None:
    report = run(FakeSession(), mode="live")
    assert not report.passed
    assert "unknown mode" in report.render()
