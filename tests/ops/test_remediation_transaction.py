"""The remediation ENTRY POINT contract.

The transaction engine moved from shell to Python
(`scripts/owner_host_remediation.py`) once the requirements became "record and
restore ownership and mode", "produce a machine-readable plan", "inject a
command adapter" and "prove the effective systemd contract" — none of which
shell can express in a way a test can hold.

Behavioural coverage therefore lives in **test_remediation_hardening.py**:
phase ordering, the full preflight matrix, metadata rollback, the
recovery daemon-reload, the persisted evidence schema, real APPLY-twice and the
effective VERIFY matrix. What remains here is the shell wrapper's own
contract, which is small and worth pinning.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WRAPPER = REPO / "scripts" / "remediate_owner_host_runtime.sh"
ENGINE = REPO / "scripts" / "owner_host_remediation.py"
SOURCE = WRAPPER.read_text(encoding="utf-8")

BASH_ONLY = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs a POSIX shell"
)


def code_lines() -> list[str]:
    return [
        line
        for line in SOURCE.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_engine_exists_and_the_wrapper_delegates_to_it() -> None:
    assert ENGINE.is_file()
    body = "\n".join(code_lines())
    assert "owner_host_remediation.py" in body
    assert ".venv/bin/python" in body, "must use the PINNED interpreter"


def test_the_wrapper_never_writes_bytecode() -> None:
    """A read-only CHECK must leave no __pycache__ on the host."""
    body = "\n".join(code_lines())
    assert "PYTHONDONTWRITEBYTECODE=1" in body
    assert " -B " in body


def test_the_wrapper_fails_closed_without_the_pinned_runtime() -> None:
    body = "\n".join(code_lines())
    assert 'if [[ ! -x "${VENV_PYTHON}" ]]' in body
    assert 'if [[ ! -r "${ENGINE}" ]]' in body
    assert body.count("FAILED_PRECHECK") >= 2


def test_the_wrapper_issues_no_process_action() -> None:
    body = "\n".join(code_lines())
    for forbidden in (
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "systemctl enable",
        "--now",
        "pkill",
        "kill -",
    ):
        assert forbidden not in body, forbidden


def test_the_wrapper_declares_the_full_prohibition_list() -> None:
    for promise in (
        "never starts, stops, restarts or relogins",
        "never enables the watchdog timer",
        "never touches firewall",
        "never touches SSH or VNC",
        "never prints Java command-line arguments",
    ):
        assert promise in SOURCE, promise


def test_the_documented_modes_are_all_offered() -> None:
    for mode in ("--check", "--apply", "--verify", "--verify-post-restart"):
        assert mode in SOURCE, mode


@BASH_ONLY
def test_a_missing_pinned_interpreter_fails_closed(tmp_path: Path) -> None:
    """No engine invocation, no mutation, an explicit FAILED_PRECHECK."""
    result = subprocess.run(  # noqa: S603
        ["bash", str(WRAPPER), f"--repo-root={tmp_path.as_posix()}", "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "FAILED_PRECHECK" in result.stderr
    assert "pinned interpreter missing" in result.stderr
    assert list(tmp_path.iterdir()) == [], "the wrapper wrote something"


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bit")
def test_the_wrapper_is_executable() -> None:
    assert os.access(WRAPPER, os.X_OK)
