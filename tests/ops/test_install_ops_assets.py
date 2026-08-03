"""Defect 4: the installer must not report success for a failed tmpfiles run.

``systemd-tmpfiles --create ... || true`` followed by an unconditional
``echo "tmpfiles applied"`` meant a failure printed success and the script went
on to suggest enabling the timer. That is the worst possible failure mode for
this particular step: /etc/tmpfiles.d/ibgateway.conf is the SINGLE canonical
owner of /run/ibgateway, so if it did not apply, the directory holding the
inhibit marker, the restart budget and the lock may be missing or wrongly
permissioned — and the operator was just told to go ahead.

The functional tests run the real script against a scratch dest-root with
stubbed systemctl/systemd-tmpfiles. They are POSIX-only; on Windows only the
static assertions run (CI runs Linux, which is where this ships).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_ops_assets.sh"

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="runs the installer for real; POSIX + bash only (CI is Linux)",
)


# -- static -------------------------------------------------------------------


def test_the_tmpfiles_failure_is_not_swallowed() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    tmpfiles_lines = [
        line
        for line in text.splitlines()
        if "systemd-tmpfiles" in line and not line.strip().startswith("#")
    ]
    assert tmpfiles_lines, "the installer must still create the runtime directory"
    for line in tmpfiles_lines:
        assert "|| true" not in line, "a false-success path on a security step"
        assert "|| :" not in line


def test_the_installer_exits_non_zero_when_tmpfiles_fails() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "if ! systemd-tmpfiles --create" in text
    assert "exit 1" in text.split("if ! systemd-tmpfiles --create")[1][:600]


def test_the_installer_defaults_to_check_mode() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "APPLY=0" in text
    assert "--apply) APPLY=1" in text


def test_the_installer_never_enables_or_starts_anything() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    body = "\n".join(
        line
        for line in text.splitlines()
        if not line.strip().startswith("#") and "NEXT STEPS" not in line
    )
    # daemon-reload is inert. enable/start/restart are owner decisions.
    for forbidden in (
        "systemctl enable",
        "systemctl start",
        "systemctl restart",
        "systemctl stop",
    ):
        # They may appear only inside the printed NEXT STEPS guidance block.
        guidance = body.split("cat <<'NEXT'")[0]
        assert forbidden not in guidance, forbidden


def test_the_installer_never_touches_network_or_remote_access() -> None:
    """Executable lines only — the comments SAY 'oci' precisely to disclaim it."""
    text = INSTALLER.read_text(encoding="utf-8").lower()
    code = "\n".join(
        line
        for line in text.split("cat <<'next'")[0].splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    for forbidden in ("firewall-cmd", "nft ", "iptables", "oci ", "sshd_config", "vncserver"):
        assert forbidden not in code, forbidden


# -- functional ---------------------------------------------------------------


def _stub_bin(tmp_path: Path, *, tmpfiles_exit: int) -> Path:
    """systemctl / systemd-tmpfiles stubs that record how they were called."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "calls.log"
    for name, exit_code in (("systemctl", 0), ("systemd-tmpfiles", tmpfiles_exit)):
        script = stub_dir / name
        script.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {exit_code}\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
    return stub_dir


def _run(tmp_path: Path, *args: str, tmpfiles_exit: int = 0) -> subprocess.CompletedProcess[str]:
    stub_dir = _stub_bin(tmp_path, tmpfiles_exit=tmpfiles_exit)
    dest_root = tmp_path / "root"
    dest_root.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ.get('PATH', '')}")
    return subprocess.run(  # noqa: S603
        ["bash", str(INSTALLER), f"--dest-root={dest_root}", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@POSIX_ONLY
def test_check_mode_writes_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "CHECK / DRY-RUN" in result.stdout
    installed = list((tmp_path / "root").rglob("*"))
    assert installed == [], f"check mode wrote {installed}"
    assert not (tmp_path / "calls.log").exists(), "check mode invoked a system command"


@POSIX_ONLY
def test_a_successful_apply_installs_and_reports_success(tmp_path: Path) -> None:
    result = _run(tmp_path, "--apply")
    assert result.returncode == 0, result.stderr
    assert "tmpfiles applied" in result.stdout
    root = tmp_path / "root"
    assert (root / "etc/systemd/system/ibgateway-ibc.service").is_file()
    assert (root / "etc/systemd/system/ibgateway-watchdog.service").is_file()
    assert (root / "etc/tmpfiles.d/ibgateway.conf").is_file()
    assert (root / "etc/logrotate.d/ibgateway").is_file()
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "systemctl daemon-reload" in calls
    # Never enabled, never started — even on the success path.
    for forbidden in ("enable", "start", "restart"):
        assert f"systemctl {forbidden}" not in calls


@POSIX_ONLY
def test_a_failed_tmpfiles_run_fails_the_install(tmp_path: Path) -> None:
    result = _run(tmp_path, "--apply", tmpfiles_exit=1)
    assert result.returncode != 0, "a failed tmpfiles must fail the install"
    assert "tmpfiles applied" not in result.stdout
    assert "FAILED" in result.stderr
    assert "INCOMPLETE" in result.stderr


@POSIX_ONLY
def test_a_failed_tmpfiles_run_does_not_suggest_enabling_the_timer(tmp_path: Path) -> None:
    """The operator must not be told to proceed on a half-installed host."""
    result = _run(tmp_path, "--apply", tmpfiles_exit=1)
    combined = result.stdout + result.stderr
    assert "systemctl enable --now ibgateway-watchdog.timer" not in result.stdout
    assert "Do NOT enable the timer" in combined


@POSIX_ONLY
def test_a_failed_tmpfiles_run_never_enables_anything(tmp_path: Path) -> None:
    _run(tmp_path, "--apply", tmpfiles_exit=1)
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    for forbidden in ("enable", "start", "restart"):
        assert f"systemctl {forbidden}" not in calls
