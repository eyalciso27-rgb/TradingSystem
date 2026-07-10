"""POI-IBKR-READONLY harness drills (fake session; no IBKR contact).

These prove the harness invariants — read-only allowlist, paper-only port
and DU-prefix account gate, localhost-only, masking, fail-closed unknowns,
no credential/ENV access — so the only thing left for the real experiment
is the owner's manual Gateway install + login. They do NOT mark the PoI
PASS: docs/poi/evidence/POI-IBKR-READONLY.md stays NOT RUN until then.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "poi_ibkr_readonly", REPO_ROOT / "scripts" / "poi_ibkr_readonly.py"
)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules["poi_ibkr_readonly"] = harness
_SPEC.loader.exec_module(harness)

# Built by concatenation so the architecture guardrail for quoted hardcoded
# account ids never matches test fixtures.
PAPER_ACCOUNT = "DU" + "1234567"
LIVE_ACCOUNT = "U" + "7654321"


class FakeSession:
    """Scripted session: records calls, returns fixture data."""

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

    def connect(self, host: str, port: int) -> None:
        self.calls.append(f"connect:{host}:{port}")

    def req_current_time(self) -> str:
        self.calls.append("req_current_time")
        if self._fail_time:
            raise RuntimeError("socket dropped")
        return "2026-07-10T12:00:00+00:00"

    def req_account_summary(self) -> Any:
        self.calls.append("req_account_summary")
        return harness.AccountSummary(
            account_id=self._account_id,
            net_liquidation="100000",
            total_cash="60000",
            buying_power="120000",
        )

    def req_positions(self) -> Sequence[object]:
        self.calls.append("req_positions")
        return self._positions

    def disconnect(self) -> None:
        self.calls.append("disconnect")

    # A mutating method that MUST never be reachable through the guard.
    def dangerous_mutation(self) -> None:  # pragma: no cover - must not run
        raise AssertionError("mutating method was reachable")


def run(session: FakeSession, **kwargs: Any) -> Any:
    return harness.run_poi(lambda: session, **kwargs)


# -- happy path -------------------------------------------------------------------


def test_paper_readonly_run_passes_with_masked_output() -> None:
    session = FakeSession()
    report = run(session)
    assert report.passed
    rendered = report.render()
    assert "RESULT: PASS" in rendered
    assert PAPER_ACCOUNT not in rendered  # full id never printed
    assert harness.mask_account_id(PAPER_ACCOUNT) in rendered
    assert "100000" not in rendered  # snapshot values withheld
    assert "2 position(s)" in rendered  # count only
    assert session.calls[0] == "connect:127.0.0.1:4002"
    assert session.calls[-1] == "disconnect"


def test_same_inputs_same_report() -> None:
    first = run(FakeSession()).render()
    second = run(FakeSession()).render()
    assert first == second


# -- paper/live + endpoint gates -----------------------------------------------


def test_live_api_port_rejected() -> None:
    with pytest.raises(harness.PoiFailure, match="port 4001 refused"):
        harness.validate_endpoint("127.0.0.1", 4001)
    with pytest.raises(harness.PoiFailure, match="refused"):
        harness.validate_endpoint("127.0.0.1", 7496)


def test_public_host_rejected_no_override_exists() -> None:
    for bad_host in ("0.0.0.0", "192.168.1.5", "gateway.example.com"):
        with pytest.raises(harness.PoiFailure, match="only 127.0.0.1/localhost"):
            harness.validate_endpoint(bad_host, harness.PAPER_API_PORT)
    # and there is no override flag at all in the CLI
    assert "--allow-remote" not in Path(harness.__file__ or "").read_text(encoding="utf-8")


def test_non_paper_account_fails_closed_and_disconnects() -> None:
    session = FakeSession(account_id=LIVE_ACCOUNT)
    report = run(session)
    assert not report.passed
    rendered = report.render()
    assert "RESULT: FAIL" in rendered
    assert "not a paper account" in rendered
    assert LIVE_ACCOUNT not in rendered  # masked even on failure
    assert session.calls[-1] == "disconnect"  # immediate disconnect
    assert "req_positions" not in session.calls  # nothing after the gate


# -- read-only allowlist -----------------------------------------------------------


def test_guard_blocks_everything_outside_allowlist() -> None:
    guard = harness.ReadOnlyGuard(FakeSession())
    for forbidden in ("dangerous_mutation", "place", "cancel", "modify", "submit"):
        with pytest.raises(harness.ReadOnlyViolation):
            getattr(guard, forbidden)
    with pytest.raises(harness.ReadOnlyViolation):
        guard.anything = "nope"
    assert harness.ALLOWED_METHODS == {
        "connect",
        "req_current_time",
        "req_account_summary",
        "req_positions",
        "disconnect",
    }


def test_static_no_order_api_and_no_secret_access_in_source() -> None:
    assert harness.__file__ is not None
    source = Path(harness.__file__).read_text(encoding="utf-8")
    # order/mutation API names must not appear at all (built by concat here
    # so this test never matches itself when scanning the repo)
    for fragment in (
        "place" + "Order",
        "cancel" + "Order",
        "reqGlobal" + "Cancel",
        "exercise" + "Options",
        "modify" + "Order",
        "transmit",
    ):
        assert fragment not in source, fragment
    # no credential / env / prompt access
    for fragment in ("os.environ", "getenv", "getpass", "input(", "passw" + "ord"):
        assert fragment not in source, fragment
    # no SDK import (allowed only inside the adapter package)
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for forbidden in ("ib" + "api", "trading_broker_ibkr", "httpx", "requests", "socket"):
            assert forbidden not in line, line


# -- fail-closed behavior -----------------------------------------------------------


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
