"""Startup / periodic reconciliation driver (A3).

Wires the EXISTING primitives -- the read-only account-safety probe (which itself
runs the reconciliation engine over one broker snapshot) and the file-backed
``ReconciliationLatch`` -- into the runtime's boot and tick. It owns no new
safety logic; it sequences the ones that exist and translates their verdict into
a ``ReconciliationState`` the runtime can gate on.

The rule the driver exists to enforce: **the latch is cleared only because the
current broker session was proven CLEAN, never because startup finished.** Each
run reads the live session token, takes an account-safety proof, and calls
``latch.maintain(safety, session)`` -- which clears ONLY on a CLEAN proof whose
session equals the current one, and otherwise re-requires. A session that
changes underneath the probe (a Gateway restart mid-run) is treated as a lost
proof and blocks. UNKNOWN and NOT_CLEAN never clear.

Attribution in Phase A is deliberately conservative: any working order or
non-zero position the probe reports is 'unknown' (unattributed), because no
strategy/portfolio authority exists yet to claim it. A non-zero count therefore
blocks new entries, and a count the probe could not even read stays ``None`` --
also blocking. An unknown position is never silently treated as flat.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from trading_execution_agent.account_safety import AccountSafety, AccountSafetyReport
from trading_execution_agent.runtime.reconciliation_state import ReconciliationState

LOGGER = logging.getLogger("trading_execution_agent.runtime.reconciliation")


@runtime_checkable
class SafetyProbe(Protocol):
    """Takes ONE read-only account-safety proof against the current session.

    The production probe runs ``probe_account_safety`` over the runtime's owned
    broker view; a test supplies a canned report. Either way it must be
    fail-closed: connectivity failure is UNKNOWN, never CLEAN.
    """

    def probe(self, now: datetime) -> AccountSafetyReport: ...


@runtime_checkable
class EntryLatch(Protocol):
    """The subset of ``ReconciliationLatch`` the driver drives."""

    def current_session(self) -> str | None: ...

    def maintain(self, safety: str, proof_session: str | None) -> None: ...

    def require(self, reason: str) -> None: ...

    def entry_blocked(self) -> tuple[bool, str]: ...


class LatchReconciliationDriver:
    """Sequences account-safety proof + latch into a ``ReconciliationState``."""

    def __init__(
        self,
        *,
        probe: SafetyProbe,
        latch: EntryLatch,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._probe = probe
        self._latch = latch
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(tz=UTC))

    def run_startup(self) -> ReconciliationState:
        return self._run("STARTUP")

    def run_periodic(self) -> ReconciliationState:
        return self._run("PERIODIC")

    def _run(self, trigger: str) -> ReconciliationState:
        now = self._now()
        session_before = self._latch.current_session()
        report = self._safe_probe(now)
        session_after = self._latch.current_session()

        if session_after is None:
            # No live session token -> nothing to prove CLEAN for. Fail closed.
            self._latch.require("no current broker session token")
            return self._blocked_state(
                now, session_after, "no current broker session token"
            )
        if session_before != session_after:
            # The Gateway session changed while we were probing: the proof we
            # just took may describe a session that no longer exists. Block and
            # let the next run prove the new session.
            self._latch.require("broker session changed during reconciliation")
            return self._blocked_state(
                now,
                session_after,
                "broker session changed during reconciliation",
            )

        # Clear ONLY on a CLEAN proof for THIS session; otherwise re-require.
        self._latch.maintain(report.safety.value, session_after)
        blocked, latch_reason = self._latch.entry_blocked()
        clean = report.safety is AccountSafety.CLEAN and not blocked

        state = ReconciliationState(
            clean=clean,
            entry_blocked=blocked,
            # On a fresh account these are 0 when CLEAN; a non-zero count is an
            # unattributed exposure that blocks; None (unreadable) also blocks.
            unknown_positions=report.non_zero_positions,
            unknown_orders=report.working_orders,
            session_token=session_after,
            checked_at=now,
            reason=(
                f"{trigger}: safety={report.safety.value}"
                + (f"; {latch_reason}" if latch_reason else "")
            ),
        )
        LOGGER.info(
            "reconciliation run",
            extra={
                "trigger": trigger,
                "safety": report.safety.value,
                "entry_blocked": blocked,
                "clean": clean,
                "unknown_positions": report.non_zero_positions,
                "unknown_orders": report.working_orders,
            },
        )
        return state

    def _safe_probe(self, now: datetime) -> AccountSafetyReport:
        try:
            return self._probe.probe(now)
        except Exception as error:  # noqa: BLE001 - a failed proof is UNKNOWN
            LOGGER.error("account-safety probe failed; treating as UNKNOWN", exc_info=True)
            from trading_execution_agent.account_safety import (
                SafetyReason,
                unknown_report,
            )

            return unknown_report(
                SafetyReason.PROBE_EXCEPTION,
                now=now,
                detail=type(error).__name__,
            )

    def _blocked_state(
        self, now: datetime, session: str | None, reason: str
    ) -> ReconciliationState:
        blocked, latch_reason = self._latch.entry_blocked()
        return ReconciliationState(
            clean=False,
            entry_blocked=blocked,
            unknown_positions=None,
            unknown_orders=None,
            session_token=session,
            checked_at=now,
            reason=f"{reason}" + (f"; {latch_reason}" if latch_reason else ""),
        )


__all__ = ["EntryLatch", "LatchReconciliationDriver", "SafetyProbe"]
