"""Tests for the reconciliation driver over the REAL file-backed latch (A3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading_execution_agent.account_safety import (
    AccountSafety,
    AccountSafetyReport,
    SafetyReason,
    unknown_report,
)
from trading_execution_agent.reconciliation_latch import ReconciliationLatch
from trading_execution_agent.runtime.reconciliation_driver import (
    LatchReconciliationDriver,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _latch(tmp_path: Path, session: str | None) -> ReconciliationLatch:
    session_path = tmp_path / "gateway-session"
    latch_path = tmp_path / "reconciliation-required.json"
    if session is not None:
        session_path.write_text(session, encoding="utf-8")
    return ReconciliationLatch(session_path=session_path, latch_path=latch_path)


def _clean() -> AccountSafetyReport:
    return AccountSafetyReport(
        safety=AccountSafety.CLEAN,
        reasons=(SafetyReason.ALL_CHECKS_PASSED,),
        checked_at=NOW,
        working_orders=0,
        non_zero_positions=0,
        reconciled=True,
        blocking_findings=0,
        connect_ok=True,
        api_round_trip_ok=True,
        disconnect_ok=True,
    )


def _not_clean_positions(count: int) -> AccountSafetyReport:
    return AccountSafetyReport(
        safety=AccountSafety.NOT_CLEAN,
        reasons=(SafetyReason.NON_ZERO_POSITION_PRESENT,),
        checked_at=NOW,
        working_orders=0,
        non_zero_positions=count,
        reconciled=True,
        blocking_findings=0,
        connect_ok=True,
        api_round_trip_ok=True,
        disconnect_ok=True,
    )


class _Probe:
    def __init__(self, report: AccountSafetyReport) -> None:
        self._report = report
        self.calls = 0

    def probe(self, now: datetime) -> AccountSafetyReport:
        self.calls += 1
        return self._report


def test_clean_for_current_session_clears_the_latch(tmp_path: Path) -> None:
    latch = _latch(tmp_path, "session-1")
    driver = LatchReconciliationDriver(probe=_Probe(_clean()), latch=latch, now=lambda: NOW)
    state = driver.run_startup()
    assert state.clean is True
    assert state.entry_blocked is False
    assert state.entry_permitted is True
    assert state.unknown_positions == 0
    assert state.unknown_orders == 0
    # The latch is genuinely cleared for the current session.
    assert latch.entry_blocked() == (False, "")


def test_unknown_never_clears_the_latch(tmp_path: Path) -> None:
    latch = _latch(tmp_path, "session-1")
    report = unknown_report(SafetyReason.CONNECT_FAILED, now=NOW)
    driver = LatchReconciliationDriver(probe=_Probe(report), latch=latch, now=lambda: NOW)
    state = driver.run_startup()
    assert state.clean is False
    assert state.entry_blocked is True
    assert state.entry_permitted is False
    blocked, _ = latch.entry_blocked()
    assert blocked is True


def test_not_clean_positions_block_and_surface_the_count(tmp_path: Path) -> None:
    latch = _latch(tmp_path, "session-1")
    driver = LatchReconciliationDriver(
        probe=_Probe(_not_clean_positions(2)), latch=latch, now=lambda: NOW
    )
    state = driver.run_startup()
    assert state.entry_permitted is False
    assert state.unknown_positions == 2


def test_no_current_session_blocks(tmp_path: Path) -> None:
    latch = _latch(tmp_path, None)  # no session token at all
    driver = LatchReconciliationDriver(probe=_Probe(_clean()), latch=latch, now=lambda: NOW)
    state = driver.run_startup()
    assert state.clean is False
    assert state.entry_permitted is False


def test_session_change_during_probe_blocks(tmp_path: Path) -> None:
    session_path = tmp_path / "gateway-session"
    latch_path = tmp_path / "reconciliation-required.json"
    session_path.write_text("session-1", encoding="utf-8")
    latch = ReconciliationLatch(session_path=session_path, latch_path=latch_path)

    class _RestartingProbe:
        def probe(self, now: datetime) -> AccountSafetyReport:
            # A Gateway restart mid-probe: the session token changes underneath.
            session_path.write_text("session-2", encoding="utf-8")
            return _clean()

    driver = LatchReconciliationDriver(probe=_RestartingProbe(), latch=latch, now=lambda: NOW)
    state = driver.run_startup()
    # The proof described session-1, but the live session is now session-2, so
    # nothing may clear: fail closed.
    assert state.clean is False
    assert state.entry_permitted is False


def test_probe_exception_is_treated_as_unknown(tmp_path: Path) -> None:
    latch = _latch(tmp_path, "session-1")

    class _BoomProbe:
        def probe(self, now: datetime) -> AccountSafetyReport:
            raise RuntimeError("gateway timeout")

    driver = LatchReconciliationDriver(probe=_BoomProbe(), latch=latch, now=lambda: NOW)
    state = driver.run_startup()
    assert state.clean is False
    assert state.entry_permitted is False


def test_disconnect_after_clean_reblocks_via_require(tmp_path: Path) -> None:
    # A CLEAN proof clears the latch; a subsequent require() (what a disconnect
    # triggers) re-blocks until the next CLEAN.
    latch = _latch(tmp_path, "session-1")
    driver = LatchReconciliationDriver(probe=_Probe(_clean()), latch=latch, now=lambda: NOW)
    assert driver.run_startup().entry_permitted is True
    latch.require("disconnect observed")
    assert latch.entry_blocked()[0] is True
    # And a fresh CLEAN clears it again.
    assert driver.run_periodic().entry_permitted is True
