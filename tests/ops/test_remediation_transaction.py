"""Remediation as a TRANSACTION: phases, zero-mutation prechecks, rollback.

The previous version validated as it went, so by the time it discovered a
problem it had already moved files. It also "verified" against a baseline it
computed itself in the same run — comparing a value to itself, which proves
nothing — and its idempotency test invoked default CHECK twice, which exercises
no mutation at all and cannot show that a second APPLY is a no-op.

Every test here drives the real script against a complete sysroot fixture.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
REMEDIATE = SCRIPTS / "remediate_owner_host_runtime.sh"
SOURCE = REMEDIATE.read_text(encoding="utf-8")

#: Where the fixture pretends the reviewed repository lives.
FIXTURE_REPO = "home/opc/projects/TradingSystem"
DROPIN_DIR = "etc/systemd/system/ibgateway-ibc.service.d"
STALE = "opt/trading/scripts/ib_gateway_watchdog.py"
STATE_DIR = "var/lib/ibgateway-remediation"
BASELINE = f"{STATE_DIR}/gateway-baseline.env"

CONTRADICTING = "[Service]\nType=oneshot\nRemainAfterExit=yes\nKillMode=process\n"
ALLOWED = "[Service]\nEnvironment=DISPLAY=:1\nEnvironment=XAUTHORITY=/home/opc/.Xauthority\n"

BASH_ONLY = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs a POSIX shell"
)


def _venv_python() -> Path:
    """This interpreter, as an executable the fixture's shim can exec."""
    return Path(sys.executable)


def build_sysroot(
    tmp_path: Path,
    *,
    dropins: dict[str, str] | None = None,
    stale: bool = True,
    runtime: bool = True,
) -> Path:
    """A complete fake host: repo, pinned interpreter, drop-ins, stale copy."""
    sysroot = tmp_path / "sysroot"
    (sysroot / DROPIN_DIR).mkdir(parents=True, exist_ok=True)

    if runtime:
        repo = sysroot / FIXTURE_REPO
        (repo / ".venv/bin").mkdir(parents=True, exist_ok=True)
        (repo / "scripts").mkdir(parents=True, exist_ok=True)
        for name in (
            "systemd_contract.py",
            "ib_gateway_watchdog.py",
            "install_ops_assets.sh",
        ):
            shutil.copy2(SCRIPTS / name, repo / "scripts" / name)
        shim = repo / ".venv/bin/python"
        shim.write_text(
            f'#!/bin/sh\nexec "{_venv_python().as_posix()}" "$@"\n', encoding="utf-8"
        )
        shim.chmod(0o755)

    for name, text in (dropins or {"zz-forking.conf": CONTRADICTING}).items():
        (sysroot / DROPIN_DIR / name).write_text(text, encoding="utf-8")

    if stale:
        (sysroot / STALE).parent.mkdir(parents=True, exist_ok=True)
        (sysroot / STALE).write_text("stale copy\n", encoding="utf-8")

    # A stable, synthetic Gateway identity.
    (sysroot / "gateway.pid").write_text("4242\n", encoding="utf-8")
    (sysroot / "gateway.startticks").write_text("998877\n", encoding="utf-8")
    (sysroot / "gateway.cgroup").write_text(
        "/user.slice/user-1000.slice/session-2.scope\n", encoding="utf-8"
    )
    return sysroot


def run(
    sysroot: Path, *args: str, fail_on: int | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, OPS_REMEDIATE_TEST_MODE="1")
    if fail_on is not None:
        env["OPS_REMEDIATE_FAIL_ON_MUTATION"] = str(fail_on)
    return subprocess.run(  # noqa: S603
        ["bash", str(REMEDIATE), f"--sysroot={sysroot.as_posix()}", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        check=False,
    )


def final_state(result: subprocess.CompletedProcess[str]) -> str:
    for line in result.stdout.splitlines():
        if line.startswith(">>> FINAL STATE: "):
            return line.split(": ", 1)[1].strip()
    return ""


def counters(result: subprocess.CompletedProcess[str]) -> tuple[int, int]:
    """(mutations, daemon_reloads) as the script reported them."""
    for line in result.stdout.splitlines():
        if line.startswith(">>> mutations performed: "):
            body = line.split(": ", 1)[1]
            mutations, reloads = body.split(";")
            return int(mutations.strip()), int(reloads.split(":")[1].strip())
    return -1, -1


def tree(root: Path) -> dict[str, str]:
    """Path -> sha256, for every regular file. The comparison for 'unchanged'."""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            # as_posix(): the STATE_DIR filters below are written with forward
            # slashes, and on Windows a backslash key would never match them.
            out[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


# =============================================================================
# Phase ordering and zero-mutation prechecks
# =============================================================================


def test_the_phase_order_is_declared_and_execute_comes_last() -> None:
    for phase in ("PHASE 1: COLLECT", "PHASE 2: VALIDATE_ALL", "PHASE 3: BUILD_PLAN"):
        assert phase in SOURCE, phase
    assert SOURCE.index("phase_collect()") < SOURCE.index("phase_validate_all()")
    assert SOURCE.index("phase_validate_all()") < SOURCE.index("phase_build_plan()")
    # The only mutating phase is invoked after the plan is complete.
    body = SOURCE.split("# main\n", 1)[1]
    assert body.index("phase_validate_all") < body.index("phase_build_plan")
    assert body.index("phase_build_plan") < body.index("phase_execute")


@BASH_ONLY
@pytest.mark.parametrize(
    ("what", "kwargs"),
    [
        ("no runtime at all", {"runtime": False}),
        ("unclassifiable drop-in", {"dropins": {"broken.conf": "\x01not-a-unit\n"}}),
    ],
)
def test_a_precheck_failure_causes_zero_mutation(
    tmp_path: Path, what: str, kwargs: dict[str, Any]
) -> None:
    """FAILED_PRECHECK, and the host is byte-for-byte what it was.

    "Cannot classify" is the important one: a tool that cannot judge a drop-in
    must refuse to move one. Unknown is not harmless.
    """
    sysroot = build_sysroot(tmp_path, **kwargs)
    before = tree(sysroot)
    result = run(sysroot, "--apply")
    assert final_state(result) == "FAILED_PRECHECK", result.stdout
    assert result.returncode == 1
    assert counters(result) == (0, 0)
    assert tree(sysroot) == before, f"{what} still mutated the host"
    assert not (sysroot / STATE_DIR).exists(), "a transaction was opened anyway"
    assert not (sysroot / BASELINE).exists(), "a baseline was written anyway"


@BASH_ONLY
def test_an_unclassifiable_dropin_is_never_treated_as_harmless(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path, dropins={"broken.conf": "\x01garbage\n"})
    result = run(sysroot, "--check")
    assert final_state(result) == "FAILED_PRECHECK"
    assert "cannot classify" in result.stdout.lower()


@BASH_ONLY
def test_classification_uses_the_pinned_interpreter_from_the_repo_root() -> None:
    """Not a heredoc piped into whatever python is nearby."""
    assert "${SYSROOT}${CONTRACT_CLI}" in SOURCE
    assert "classify" in SOURCE
    assert "PYTHONDONTWRITEBYTECODE=1" in SOURCE
    assert ' -B \\\n' in SOURCE or " -B " in SOURCE
    assert "<<'PYEOF'" not in SOURCE, "classification must not be an inline heredoc"


# =============================================================================
# CHECK
# =============================================================================


@BASH_ONLY
def test_check_is_read_only_on_a_dirty_host(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path)
    before = tree(sysroot)
    result = run(sysroot, "--check")
    assert result.returncode == 0, result.stdout
    assert final_state(result) == "REMEDIATED_RESTART_REQUIRED"
    assert counters(result) == (0, 0)
    assert tree(sysroot) == before, "CHECK mutated the host"


@BASH_ONLY
def test_check_on_a_clean_host_reports_no_changes_required(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path, dropins={"display.conf": ALLOWED}, stale=False)
    result = run(sysroot, "--check")
    assert final_state(result) == "NO_CHANGES_REQUIRED", result.stdout
    assert counters(result) == (0, 0)


# =============================================================================
# APPLY, idempotency and the daemon-reload matrix
# =============================================================================


@BASH_ONLY
def test_apply_then_apply_again_is_genuinely_idempotent(tmp_path: Path) -> None:
    """A REAL apply-twice test.

    The superseded test called default CHECK twice, which mutates nothing and
    therefore could not show that a second APPLY is a no-op.
    """
    sysroot = build_sysroot(tmp_path)

    first = run(sysroot, "--apply")
    assert final_state(first) == "REMEDIATED_RESTART_REQUIRED", first.stdout
    first_mutations, first_reloads = counters(first)
    assert first_mutations == 2, first.stdout
    assert first_reloads == 1
    after_first = tree(sysroot)

    second = run(sysroot, "--apply")
    assert final_state(second) == "NO_CHANGES_REQUIRED", second.stdout
    assert counters(second) == (0, 0), "the second apply mutated or reloaded"

    # Ignore the new transaction directory the second run would not create.
    changed = {
        path: digest
        for path, digest in tree(sysroot).items()
        if not path.startswith(STATE_DIR)
    }
    baseline_first = {
        path: digest
        for path, digest in after_first.items()
        if not path.startswith(STATE_DIR)
    }
    assert changed == baseline_first, "the second apply changed the host"


@BASH_ONLY
def test_daemon_reload_happens_exactly_once_when_only_a_dropin_changes(
    tmp_path: Path,
) -> None:
    """The base unit can be byte-identical and the EFFECTIVE unit still change.

    Keying the reload on "did the unit file change" would leave systemd serving
    the contradicted contract after a drop-in was quarantined.
    """
    sysroot = build_sysroot(tmp_path, stale=False)
    result = run(sysroot, "--apply")
    mutations, reloads = counters(result)
    assert mutations == 1, "only the drop-in should have moved"
    assert reloads == 1, "a drop-in change must still trigger exactly one reload"
    assert "daemon-reload: 1" in result.stdout


@BASH_ONLY
def test_no_daemon_reload_when_nothing_changed(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path, dropins={"display.conf": ALLOWED}, stale=False)
    result = run(sysroot, "--apply")
    assert counters(result) == (0, 0)
    assert "daemon-reload: 0" in result.stdout


@BASH_ONLY
def test_an_allowed_dropin_is_kept_and_a_contradicting_one_is_quarantined(
    tmp_path: Path,
) -> None:
    sysroot = build_sysroot(
        tmp_path, dropins={"display.conf": ALLOWED, "zz-forking.conf": CONTRADICTING}
    )
    run(sysroot, "--apply")
    names = sorted(p.name for p in (sysroot / DROPIN_DIR).iterdir())
    assert names == ["display.conf", "zz-forking.conf.quarantined"], names


@BASH_ONLY
def test_the_stale_copy_is_quarantined_not_deleted(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path)
    run(sysroot, "--apply")
    assert not (sysroot / STALE).exists()
    assert (sysroot / f"{STALE}.quarantined").is_file(), "the stale copy was DELETED"


# =============================================================================
# Rollback matrix
# =============================================================================


@BASH_ONLY
@pytest.mark.parametrize("fail_on", [1, 2])
def test_a_failure_during_apply_rolls_back_to_a_byte_identical_host(
    tmp_path: Path, fail_on: int
) -> None:
    sysroot = build_sysroot(
        tmp_path, dropins={"aaa.conf": CONTRADICTING, "bbb.conf": CONTRADICTING}
    )
    before = {
        path: digest
        for path, digest in tree(sysroot).items()
        if not path.startswith(STATE_DIR)
    }
    result = run(sysroot, "--apply", fail_on=fail_on)
    assert final_state(result) == "FAILED_APPLY_ROLLED_BACK", result.stdout
    assert result.returncode == 1
    after = {
        path: digest
        for path, digest in tree(sysroot).items()
        if not path.startswith(STATE_DIR)
    }
    assert after == before, f"rollback after mutation {fail_on} left the host changed"


@BASH_ONLY
def test_the_transaction_manifest_and_backup_are_private(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path)
    run(sysroot, "--apply")
    state = sysroot / STATE_DIR
    assert state.is_dir()
    if os.name == "posix":
        assert oct(state.stat().st_mode & 0o777) == "0o700"
    transactions = [p for p in state.iterdir() if p.name.startswith("txn-")]
    assert len(transactions) == 1, transactions
    # Collision-resistant: mktemp-generated, not a predictable name.
    assert len(transactions[0].name) > len("txn-")
    manifest = transactions[0] / "manifest.tsv"
    assert manifest.is_file()
    rows = [r for r in manifest.read_text(encoding="utf-8").splitlines() if r.strip()]
    assert rows, "the manifest recorded nothing"
    for row in rows:
        fields = row.split("\t")
        assert len(fields) == 5, row  # action, original, backup, sha256, destination
        assert len(fields[3]) == 64, "a sha256 digest must be recorded"
    if os.name == "posix":
        assert oct(manifest.stat().st_mode & 0o777) == "0o600"


@BASH_ONLY
def test_the_backup_matches_the_original_by_hash(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path, dropins={"zz.conf": CONTRADICTING})
    original = hashlib.sha256(
        (sysroot / DROPIN_DIR / "zz.conf").read_bytes()
    ).hexdigest()
    run(sysroot, "--apply")
    backups = list((sysroot / STATE_DIR).glob("txn-*/zz.conf.bak"))
    assert len(backups) == 1, backups
    assert hashlib.sha256(backups[0].read_bytes()).hexdigest() == original


# =============================================================================
# Persisted baseline and VERIFY
# =============================================================================


@BASH_ONLY
def test_apply_persists_a_baseline_with_the_documented_schema(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path)
    run(sysroot, "--apply")
    baseline = sysroot / BASELINE
    assert baseline.is_file()
    fields = dict(
        line.split("=", 1)
        for line in baseline.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    assert fields["schema"] == "1"
    assert fields["captured_phase"] == "pre_apply"
    assert fields["gateway_pid"] == "4242"
    assert fields["gateway_start_ticks"] == "998877", "PID alone is not identity"
    assert "session-2.scope" in fields["gateway_cgroup"]
    if os.name == "posix":
        assert oct(baseline.stat().st_mode & 0o777) == "0o600"


@BASH_ONLY
def test_standalone_verify_requires_a_persisted_baseline(tmp_path: Path) -> None:
    """It must never self-baseline: comparing a value to itself proves nothing."""
    sysroot = build_sysroot(tmp_path)
    result = run(sysroot, "--verify")
    assert final_state(result) == "FAILED_PRECHECK", result.stdout
    assert result.returncode == 1
    assert "must never create its own" in result.stdout
    assert not (sysroot / BASELINE).exists(), "VERIFY created its own baseline"


@BASH_ONLY
def test_verify_after_apply_reaches_verified_complete(tmp_path: Path) -> None:
    sysroot = build_sysroot(tmp_path)
    run(sysroot, "--apply")
    result = run(sysroot, "--verify")
    assert final_state(result) == "VERIFIED_COMPLETE", result.stdout
    assert result.returncode == 0
    assert "gateway_untouched" in result.stdout


@BASH_ONLY
def test_verify_detects_a_changed_gateway_identity(tmp_path: Path) -> None:
    """A restart during remediation must be caught, including PID reuse."""
    sysroot = build_sysroot(tmp_path)
    run(sysroot, "--apply")
    # Same PID, different start ticks: exactly what PID reuse looks like.
    (sysroot / "gateway.startticks").write_text("111111\n", encoding="utf-8")
    result = run(sysroot, "--verify")
    assert final_state(result) == "FAILED_PRECHECK"
    assert "identity CHANGED" in result.stdout


@BASH_ONLY
def test_verify_requires_the_timer_disabled_and_inactive_and_service_inactive(
    tmp_path: Path,
) -> None:
    sysroot = build_sysroot(tmp_path)
    run(sysroot, "--apply")
    show = sysroot / "show"
    show.mkdir(exist_ok=True)

    (show / "ibgateway-watchdog.timer.UnitFileState").write_text("enabled")
    assert "timer UnitFileState=enabled" in run(sysroot, "--verify").stdout
    (show / "ibgateway-watchdog.timer.UnitFileState").write_text("disabled")

    (show / "ibgateway-watchdog.timer.ActiveState").write_text("active")
    assert "timer ActiveState=active" in run(sysroot, "--verify").stdout
    (show / "ibgateway-watchdog.timer.ActiveState").write_text("inactive")

    (show / "ibgateway-watchdog.service.ActiveState").write_text("active")
    assert "watchdog service ActiveState=active" in run(sysroot, "--verify").stdout


# =============================================================================
# Final-state vocabulary
# =============================================================================


def test_every_documented_final_state_exists() -> None:
    for state in (
        "FAILED_PRECHECK",
        "FAILED_APPLY_ROLLED_BACK",
        "FAILED_APPLY_PARTIAL_ROLLBACK",
        "REMEDIATED_RESTART_REQUIRED",
        "VERIFIED_COMPLETE",
        "NO_CHANGES_REQUIRED",
    ):
        assert f'"{state}"' in SOURCE, state


def test_success_states_exit_zero_and_failures_do_not() -> None:
    block = SOURCE.split("case \"${FINAL_STATE}\" in", 1)[1].split("esac", 1)[0]
    assert "${STATE_VERIFIED}" in block
    assert "${STATE_NO_CHANGES}" in block
    assert "${STATE_REMEDIATED}" in block
    assert "exit 0" in block
    assert "exit 1" in block


@BASH_ONLY
def test_apply_reports_that_a_restart_is_still_required(tmp_path: Path) -> None:
    """The contract is fixed; the RUNNING process still belongs to its old session."""
    sysroot = build_sysroot(tmp_path)
    result = run(sysroot, "--apply")
    assert final_state(result) == "REMEDIATED_RESTART_REQUIRED"
    assert "owner-authorized restart is required" in result.stdout
