"""Entry-reconciliation latch: a frozen pre-crash proof/authorization can never
permit a new entry into a freshly recovered broker session (Integration 9.2)."""

from __future__ import annotations

from pathlib import Path

from trading_execution_agent.reconciliation_latch import (
    ALWAYS_PERMIT,
    CLEAN,
    ENTRY_BLOCKED_CODE,
    ReconciliationLatch,
)


def make_latch(tmp_path: Path) -> ReconciliationLatch:
    return ReconciliationLatch(
        session_path=tmp_path / "gateway-session",
        latch_path=tmp_path / "reconciliation-required.json",
    )


def set_session(tmp_path: Path, token: str) -> None:
    (tmp_path / "gateway-session").write_text(token, encoding="utf-8")


# -- entry_blocked semantics ---------------------------------------------------------


def test_no_session_token_blocks_fail_closed(tmp_path: Path) -> None:
    latch = make_latch(tmp_path)  # no session file at all
    blocked, reason = latch.entry_blocked()
    assert blocked
    assert ENTRY_BLOCKED_CODE in reason


def test_require_blocks_entry(tmp_path: Path) -> None:
    set_session(tmp_path, "gw-1")
    latch = make_latch(tmp_path)
    latch.require("disconnect observed")
    blocked, _ = latch.entry_blocked()
    assert blocked


def test_clean_for_current_session_clears_and_permits(tmp_path: Path) -> None:  # C
    set_session(tmp_path, "gw-1")
    latch = make_latch(tmp_path)
    latch.require("startup")
    assert latch.entry_blocked()[0]
    assert latch.try_clear(CLEAN, "gw-1") is True
    assert latch.entry_blocked() == (False, "")


def test_stale_pre_crash_clean_cannot_clear_a_post_crash_latch(tmp_path: Path) -> None:  # D
    set_session(tmp_path, "gw-1")
    latch = make_latch(tmp_path)
    assert latch.try_clear(CLEAN, "gw-1") is True  # CLEAN for the pre-crash session
    assert latch.entry_blocked() == (False, "")
    # Gateway crashes and systemd recovers -> the launcher wrote a NEW token.
    set_session(tmp_path, "gw-2")
    assert latch.entry_blocked()[0] is True  # blocked again automatically
    # A CLEAN captured under the OLD session must not clear the new one.
    assert latch.try_clear(CLEAN, "gw-1") is False
    assert latch.entry_blocked()[0] is True


def test_unknown_and_not_clean_never_clear(tmp_path: Path) -> None:  # E
    set_session(tmp_path, "gw-1")
    latch = make_latch(tmp_path)
    latch.require("startup")
    assert latch.try_clear("UNKNOWN", "gw-1") is False
    assert latch.try_clear("NOT_CLEAN", "gw-1") is False
    assert latch.entry_blocked()[0] is True


def test_session_change_invalidates_prior_proof(tmp_path: Path) -> None:  # F
    set_session(tmp_path, "gw-1")
    latch = make_latch(tmp_path)
    latch.try_clear(CLEAN, "gw-1")
    assert latch.entry_blocked() == (False, "")
    set_session(tmp_path, "gw-2")  # disconnect/reconnect -> new generation
    assert latch.entry_blocked()[0] is True


def test_maintain_clears_on_current_clean_and_requires_otherwise(tmp_path: Path) -> None:
    set_session(tmp_path, "gw-1")
    latch = make_latch(tmp_path)
    latch.maintain("UNKNOWN", "gw-1")  # native disconnect -> require
    assert latch.entry_blocked()[0] is True
    latch.maintain(CLEAN, "gw-1")  # proven clean for current session -> clear
    assert latch.entry_blocked() == (False, "")
    latch.maintain("NOT_CLEAN", "gw-1")  # something outstanding -> re-block
    assert latch.entry_blocked()[0] is True


def test_clean_cannot_clear_when_session_unknown(tmp_path: Path) -> None:
    latch = make_latch(tmp_path)  # no session file
    assert latch.try_clear(CLEAN, "gw-1") is False
    assert latch.entry_blocked()[0] is True


def test_always_permit_never_blocks() -> None:
    assert ALWAYS_PERMIT.entry_blocked() == (False, "")
