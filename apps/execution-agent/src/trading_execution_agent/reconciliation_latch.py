"""Persistent entry-reconciliation latch at the FINAL execution boundary.

Authority: Integration_Specification_v0.6 section 9.2 ("no new orders after
disconnect until successful reconciliation"); Decision_Contract 12.5 (UNKNOWN
is resolved only by reconciliation, never guessed).

**Why this exists.** ``FinalPreSubmitCheck`` refuses while
``reconciliation_required`` is true — but its output (a ``PreSubmitCheckResult``
and a ``SingleUseAuthorization``) is FROZEN when it is minted. If the Gateway
crashes and systemd recovers it, a request authorized BEFORE the crash could
reach the ExecutionAgent AFTER recovery, pass a now-healthy ``health_check()``
and submit — with no fresh reconciliation. This latch is the second, final
defence: the ExecutionAgent consults it immediately before consuming the
authorization, so a stale pre-crash authorization can never submit into a
freshly recovered broker session.

**How.** Entry is permitted for exactly one thing: the CURRENT recovered broker
session, identified by a session token the launcher rewrites on every Gateway
(re)start (``/run/ibgateway/gateway-session``). The latch stores the session for
which we last obtained a CLEAN account-safety proof. Entry is permitted only
while ``current_session == cleared_session``. A restart changes the token, so a
pre-crash CLEAN can never satisfy a post-crash session, and any disconnect the
watchdog observes calls :meth:`require`, dropping the cleared session so entry
blocks again until a fresh CLEAN.

**State survives the Gateway process restart** because it lives in the runtime
directory (``/run/ibgateway``, owned by tmpfiles.d), not in Gateway memory.

**ENTRY-ONLY.** This gates NEW submissions. Cancels and protective /
exposure-reducing paths are never routed through it. Only a CLEAN proof clears
it; UNKNOWN and NOT_CLEAN never do; there is no automatic order resend.

Pure stdlib on purpose: the watchdog imports this on a bare host with no
application packages, so nothing here imports the broker or account-safety code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

RUN_DIR = Path("/run/ibgateway")
DEFAULT_SESSION_PATH = RUN_DIR / "gateway-session"
DEFAULT_LATCH_PATH = RUN_DIR / "reconciliation-required.json"

#: The one string a proof must carry to clear the latch. Kept as a literal so
#: this module never has to import ``AccountSafety`` (and its broker deps).
CLEAN = "CLEAN"

#: Canonical refusal code surfaced by the ExecutionAgent when entry is latched.
ENTRY_BLOCKED_CODE = "RECONCILIATION_REQUIRED"


@runtime_checkable
class EntryGate(Protocol):
    """The narrow surface the ExecutionAgent checks before a new submission."""

    def entry_blocked(self) -> tuple[bool, str]:
        """Return ``(blocked, reason)``; ``blocked`` must fail closed."""
        ...


class _AlwaysPermit:
    """Explicit no-latch gate for contexts with no runtime latch wired."""

    def entry_blocked(self) -> tuple[bool, str]:
        return (False, "")


#: A visible, explicit opt-out — never a hidden default hole. Production wires a
#: real :class:`ReconciliationLatch`; unit contexts that are not testing the
#: latch pass this on purpose.
ALWAYS_PERMIT: EntryGate = _AlwaysPermit()


@dataclass(frozen=True, slots=True)
class LatchRecord:
    """The persisted latch state: the session we last cleared, and why."""

    cleared_session: str | None
    reason: str


class ReconciliationLatch:
    """File-backed entry latch keyed on the current broker-session token."""

    def __init__(
        self,
        *,
        session_path: Path | str = DEFAULT_SESSION_PATH,
        latch_path: Path | str = DEFAULT_LATCH_PATH,
    ) -> None:
        self._session_path = Path(session_path)
        self._latch_path = Path(latch_path)

    # -- the current broker session ------------------------------------------
    def current_session(self) -> str | None:
        """The live session token, or None if it cannot be read (fail closed)."""
        try:
            token = self._session_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    # -- persisted latch record ----------------------------------------------
    def _read(self) -> LatchRecord:
        try:
            data = json.loads(self._latch_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return LatchRecord(cleared_session=None, reason="no latch record")
        cleared = data.get("cleared_session")
        return LatchRecord(
            cleared_session=cleared if isinstance(cleared, str) and cleared else None,
            reason=str(data.get("reason", "")),
        )

    def _write(self, record: LatchRecord) -> None:
        self._latch_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._latch_path.with_name(self._latch_path.name + ".tmp")
        tmp.write_text(
            json.dumps(
                {"cleared_session": record.cleared_session, "reason": record.reason},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._latch_path)

    # -- mutations ------------------------------------------------------------
    def require(self, reason: str) -> None:
        """Block entry: a disconnect / recovery happened. Drops the cleared
        session so nothing is permitted until a fresh CLEAN for the current one."""
        self._write(LatchRecord(cleared_session=None, reason=reason))

    def try_clear(self, safety: str, proof_session: str | None) -> bool:
        """Clear ONLY on a CLEAN proof for the CURRENT session.

        Returns whether the latch was cleared. A pre-crash proof carries an old
        ``proof_session`` that no longer equals ``current_session``, so it can
        never clear a post-crash latch; UNKNOWN / NOT_CLEAN never clear.
        """
        current = self.current_session()
        if safety != CLEAN or current is None or proof_session != current:
            return False
        self._write(
            LatchRecord(cleared_session=current, reason="CLEAN for current session")
        )
        return True

    def maintain(self, safety: str, proof_session: str | None) -> None:
        """One supervision step: clear on a current-session CLEAN, otherwise
        require. This is what the watchdog calls so a native Auto Restart / API
        disconnect (no systemd restart) still invalidates a prior proof."""
        if not self.try_clear(safety, proof_session):
            self.require(f"account safety not CLEAN for current session: {safety}")

    # -- the gate -------------------------------------------------------------
    def entry_blocked(self) -> tuple[bool, str]:
        current = self.current_session()
        if current is None:
            return (True, f"{ENTRY_BLOCKED_CODE}: gateway session unknown")
        if self._read().cleared_session == current:
            return (False, "")
        return (True, f"{ENTRY_BLOCKED_CODE}: no CLEAN proof for current gateway session")


__all__ = [
    "ALWAYS_PERMIT",
    "CLEAN",
    "DEFAULT_LATCH_PATH",
    "DEFAULT_SESSION_PATH",
    "ENTRY_BLOCKED_CODE",
    "RUN_DIR",
    "EntryGate",
    "LatchRecord",
    "ReconciliationLatch",
]
