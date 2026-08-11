"""Behavioural regression for the Gateway launcher's bounded, fail-closed
recovery decision (scripts/start_ibc_gateway.sh).

These source the launcher in a POSIX shell and exercise two pure functions
without ever launching IBC:

    recovery_exit_code <stopped> <child_rc>   -> the exit code systemd sees
    classify_ibc_log                          -> FAILCLOSED | UNKNOWN

Proven outcomes (the owner's four):
  * native Auto Restart is transparent (in-process; never reaches the wrapper)
  * clean unrequested logoff/exit (rc=0)      -> recoverable (bounded restart)
  * unexpected crash (rc!=0)                  -> recoverable (bounded restart)
  * explicit owner stop                       -> 0 (no restart)
  * current-invocation 2FA/auth/config        -> 78 (no restart)
And that classification is INVOCATION-SCOPED: a stale marker from a previous
run never classifies the current termination.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SH = (REPO_ROOT / "scripts" / "start_ibc_gateway.sh").as_posix()

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="sources the launcher in a POSIX shell",
)


def _bash(log_dir: Path, body: str) -> str:
    """Source the launcher (main() is guarded, so it does not run) and execute
    ``body``; return the last non-empty stdout line."""
    script = (
        "set -uo pipefail\n"
        f'export IBC_LOG_PATH="{log_dir.as_posix()}"\n'
        f'source "{START_SH}"\n'
        f'LOG_PATH="{log_dir.as_posix()}"\n'
        f"{body}\n"
    )
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", script],  # noqa: S607
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    lines = [ln for ln in result.stdout.replace("\x00", "").splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def _decide(log_dir: Path, stopped: int, child_rc: int, setup: str = "") -> str:
    return _bash(log_dir, f"{setup}\nrecovery_exit_code {stopped} {child_rc}")


TWO_FA = "Second Factor Authentication has timed out"
AUTH_FAIL = "Login failed: invalid username or password"


# -- item 1: the four recovery outcomes ---------------------------------------------


@POSIX_ONLY
def test_explicit_owner_stop_exits_zero_no_restart(tmp_path: Path) -> None:
    assert _decide(tmp_path, stopped=1, child_rc=0) == "0"


@POSIX_ONLY
def test_clean_unrequested_exit_is_recoverable(tmp_path: Path) -> None:
    # rc=0 but NOT an operator stop (Auto Logoff / clean unrequested exit) ->
    # a recoverable non-zero so systemd's bounded on-failure recovery fires.
    code = _decide(tmp_path, stopped=0, child_rc=0)
    assert code not in ("0", "78")
    assert code == "1"


@POSIX_ONLY
def test_unexpected_crash_is_recoverable(tmp_path: Path) -> None:
    code = _decide(tmp_path, stopped=0, child_rc=134)  # e.g. SIGABRT
    assert code == "134"  # non-zero, not the fail-closed sentinel


@POSIX_ONLY
def test_current_fail_closed_is_sentinel_78(tmp_path: Path) -> None:
    setup = f'capture_log_baseline\nprintf "%s\\n" "{TWO_FA}" > "{tmp_path.as_posix()}/ibc.log"'
    assert _decide(tmp_path, stopped=0, child_rc=1, setup=setup) == "78"


@POSIX_ONLY
def test_a_crash_never_surfaces_the_fail_closed_sentinel(tmp_path: Path) -> None:
    # If IBC itself happened to exit 78 without a fail-closed marker, it must be
    # remapped to a recoverable code, or systemd would wrongly suppress restart.
    assert _decide(tmp_path, stopped=0, child_rc=78) == "1"


# -- item 2: invocation-scoped classification ----------------------------------------


@POSIX_ONLY
def test_stale_2fa_marker_plus_current_crash_is_not_78(tmp_path: Path) -> None:
    log = tmp_path / "ibc.log"
    setup = (
        f'printf "%s\\n" "{TWO_FA}" > "{log.as_posix()}"\n'  # stale, pre-baseline
        "capture_log_baseline"  # baseline AFTER the stale marker
    )
    # current termination is a crash with NO new marker -> recoverable, not 78
    assert _decide(tmp_path, stopped=0, child_rc=139, setup=setup) == "139"


@POSIX_ONLY
def test_stale_auth_marker_plus_current_clean_exit_is_recoverable(tmp_path: Path) -> None:
    log = tmp_path / "ibc.log"
    setup = f'printf "%s\\n" "{AUTH_FAIL}" > "{log.as_posix()}"\ncapture_log_baseline'
    assert _decide(tmp_path, stopped=0, child_rc=0, setup=setup) == "1"


@POSIX_ONLY
def test_current_2fa_timeout_is_exactly_78(tmp_path: Path) -> None:
    log = tmp_path / "ibc.log"
    setup = (
        f'printf "old boot line\\n" > "{log.as_posix()}"\n'
        "capture_log_baseline\n"
        f'printf "%s\\n" "{TWO_FA}" >> "{log.as_posix()}"'  # appended THIS run
    )
    assert _classify(tmp_path, setup) == "FAILCLOSED"
    assert _decide(tmp_path, stopped=0, child_rc=1, setup=setup) == "78"


@POSIX_ONLY
def test_current_auth_failure_is_exactly_78(tmp_path: Path) -> None:
    log = tmp_path / "ibc.log"
    setup = f'capture_log_baseline\nprintf "%s\\n" "{AUTH_FAIL}" >> "{log.as_posix()}"'
    assert _decide(tmp_path, stopped=0, child_rc=1, setup=setup) == "78"


@POSIX_ONLY
def test_rotated_new_current_log_with_2fa_is_78(tmp_path: Path) -> None:
    # baseline taken with the old log; a NEW rotated file carries the current 2FA
    old = tmp_path / "ibc.log"
    new = tmp_path / "ibc.1.log"
    setup = (
        f'printf "startup\\n" > "{old.as_posix()}"\n'
        "capture_log_baseline\n"
        f'printf "%s\\n" "{TWO_FA}" > "{new.as_posix()}"'
    )
    assert _decide(tmp_path, stopped=0, child_rc=1, setup=setup) == "78"


@POSIX_ONLY
def test_no_current_marker_nonzero_failure_is_recoverable(tmp_path: Path) -> None:
    log = tmp_path / "ibc.log"
    setup = f'printf "%s\\n" "{TWO_FA}" > "{log.as_posix()}"\ncapture_log_baseline'
    # stale 2FA present but NOT current; a nonzero crash must recover, not be 78
    assert _decide(tmp_path, stopped=0, child_rc=7, setup=setup) == "7"


def _classify(log_dir: Path, setup: str) -> str:
    return _bash(log_dir, f"{setup}\nclassify_ibc_log")
