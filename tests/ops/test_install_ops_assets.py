"""Installer safety: isolated rehearsal, and a mandatory runtime-directory step.

Two defects fixed here.

**A — `--dest-root` was not isolated.** It presented itself as a scratch
rehearsal but still ran `systemctl daemon-reload` and
`systemd-tmpfiles --create <scratch>/etc/tmpfiles.d/ibgateway.conf` against the
real host. Staging the config elsewhere isolates nothing: the rule names the
ABSOLUTE path `/run/ibgateway`, so tmpfiles would have created and chowned the
REAL directory. A rehearsal that mutates the thing it is rehearsing is not a
rehearsal. Isolation is now **structural** — rehearsal invokes neither command
at all — and these tests assert that both structurally and by inspecting the
exact argv of every command the script attempts.

**B — tmpfiles was skipped when nothing changed.** It was nested under
`RELOAD == 1 && changed > 0`, so an apply where every asset already matched, or
an apply with `--no-reload`, reported success while `/run/ibgateway` could be
missing entirely. `/run` is volatile: the directory vanishes on reboot while
the config on disk still looks perfectly installed. Runtime-directory creation
and verification is now a mandatory apply phase.

Functional tests are POSIX-only (they assert real mode bits and use `stat -c`);
CI runs Linux, which is the deployment target. The static tests run everywhere.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_ops_assets.sh"
SOURCE = INSTALLER.read_text(encoding="utf-8")

#: Commands that touch real systemd or the real /run.
PRIVILEGED = ("systemctl", "systemd-tmpfiles")

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="asserts a real 0700 directory, which needs POSIX mode bits (CI is Linux)",
)

#: Most of these checks only need a shell — not settable POSIX mode bits — so
#: they run everywhere. Gating them on POSIX would leave the behaviour they
#: protect unguarded on any developer machine that is not Linux, which is
#: exactly how a regression reaches CI unnoticed.
BASH_ONLY = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs a POSIX shell to run the installer"
)


def sh(path: Path | str) -> str:
    """Path in a form a POSIX shell will accept.

    Backslashes in a Windows path are escape characters to bash, so a raw
    `str(path)` interpolated into a command silently becomes a different
    (usually non-existent) path.
    """
    return Path(path).as_posix()


def _strip_heredocs(text: str) -> list[str]:
    """Drop heredoc bodies: printed guidance is prose, not executed code."""
    lines: list[str] = []
    terminator: str | None = None
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        match = re.search(r"<<'([A-Z]+)'", line)
        if match:
            terminator = match.group(1)
            continue
        lines.append(line)
    return lines


def code_lines(text: str | None = None) -> list[str]:
    """Executable lines only — comments discuss the commands they forbid."""
    return [
        line
        for line in _strip_heredocs(SOURCE if text is None else text)
        if line.strip() and not line.strip().startswith("#")
    ]


def command_lines(text: str | None = None) -> list[str]:
    """Lines that INVOKE something, with string literals blanked out.

    A command name inside a quoted string is data, not a call:
    `echo "systemd-tmpfiles NOT invoked"` and
    `fail "systemd-tmpfiles --create failed"` both mention the command
    precisely because they are not running it. Matching those would invert
    the very tests that are supposed to prove it is never invoked, so only
    text OUTSIDE quotes counts as a call.
    """
    stripped: list[str] = []
    for line in code_lines(text):
        if re.match(r"^\s*(echo|printf|cat)\b", line):
            continue
        without_literals = re.sub(r'"[^"]*"', '""', line)
        without_literals = re.sub(r"'[^']*'", "''", without_literals)
        stripped.append(without_literals)
    return stripped


# -- static structure ---------------------------------------------------------


def test_the_tmpfiles_failure_is_not_swallowed() -> None:
    for line in command_lines():
        if "systemd-tmpfiles" in line:
            assert "|| true" not in line, "a false-success path on a security step"
            assert "|| :" not in line


def test_the_installer_defaults_to_check_mode() -> None:
    assert "APPLY=0" in SOURCE
    assert "--apply) APPLY=1" in SOURCE


def test_the_installer_never_enables_or_starts_anything() -> None:
    executable = "\n".join(command_lines())
    for forbidden in ("systemctl enable", "systemctl start", "systemctl restart", "systemctl stop"):
        assert forbidden not in executable, forbidden


def test_the_installer_never_touches_network_or_remote_access() -> None:
    """Invocations only — comments and printed guidance SAY 'oci' to disclaim it."""
    executable = "\n".join(command_lines()).lower()
    for forbidden in ("firewall-cmd", "nft ", "iptables", "oci ", "sshd_config", "vncserver"):
        assert forbidden not in executable, forbidden


def _rehearsal_guarded_blocks() -> list[str]:
    """The `then` branches of every `if [[ "${REHEARSAL}" -eq 1 ]]`."""
    blocks: list[str] = []
    collecting = False
    depth = 0
    current: list[str] = []
    for line in SOURCE.splitlines():
        stripped = line.strip()
        if not collecting:
            if stripped.startswith('if [[ "${REHEARSAL}" -eq 1 ]]'):
                collecting, depth, current = True, 0, []
            continue
        if stripped.startswith(("if ", "for ", "while ", "case ")):
            depth += 1
        elif stripped in ("fi", "done", "esac"):
            if depth == 0:
                blocks.append("\n".join(current))
                collecting = False
                continue
            depth -= 1
        elif stripped in ("else", "elif") and depth == 0:
            blocks.append("\n".join(current))
            collecting = False
            continue
        current.append(line)
    return blocks


def test_the_rehearsal_branches_contain_no_privileged_command() -> None:
    """Structural isolation, not stub-dependent.

    Every `REHEARSAL -eq 1` branch is parsed out of the script and checked. A
    PATH stub proves only that the test environment intercepted a call; this
    proves the call is not written.
    """
    blocks = _rehearsal_guarded_blocks()
    assert blocks, "expected at least one rehearsal-guarded block"
    for block in blocks:
        executable = "\n".join(command_lines(block))
        for command in PRIVILEGED:
            assert command not in executable, f"{command} inside a rehearsal branch:\n{block}"


def test_privileged_commands_appear_only_in_their_dedicated_call_sites() -> None:
    """One `systemctl daemon-reload`, one `systemd-tmpfiles`, both guarded."""
    executable = command_lines()
    reloads = [line for line in executable if "systemctl daemon-reload" in line]
    tmpfiles = [line for line in executable if "systemd-tmpfiles" in line]
    assert len(reloads) == 1, reloads
    assert len(tmpfiles) == 1, tmpfiles
    # The only systemd-tmpfiles call lives in apply_tmpfiles_real, which is
    # invoked exactly once — from the non-rehearsal branch.
    assert "apply_tmpfiles_real() {" in SOURCE
    calls = [line for line in executable if line.strip().startswith("apply_tmpfiles_real ")]
    assert len(calls) == 1, calls


def test_the_runtime_directory_step_is_not_conditional_on_changes() -> None:
    """Defect B, structurally: the phase must be unconditional and unskippable.

    Checking only for the old literal guard was too weak — ANY new early exit
    or `changed`/`RELOAD` condition ahead of the verification reintroduces the
    defect.

    The region deliberately starts at the END OF PHASE 2, not at the phase-3
    anchor: a guard inserted in the gap between them would otherwise sit just
    outside the scanned window and skip the whole phase unnoticed.
    """
    assert 'RELOAD}" -eq 1 && "${changed}" -gt 0' not in SOURCE
    assert "verify_runtime_dir() {" in SOURCE

    # Raw code lines here: command_lines() blanks string literals, which would
    # erase the very anchors that delimit the phase.
    body = "\n".join(code_lines())
    start = body.index('STATE_RELOAD="DONE')
    end = body.index("verify_runtime_dir ", start)
    phase = body[start:end]
    assert '[[ -f "${TMPFILES_CONF}" ]]' in phase, "region must span all of phase 3"

    assert "exit 0" not in phase, "an early exit would skip the runtime directory"
    assert "${changed}" not in phase, "the runtime directory must not depend on changes"
    assert "${RELOAD}" not in phase, "--no-reload must not suppress the runtime directory"

    calls = [
        line for line in command_lines() if line.strip().startswith("verify_runtime_dir ")
    ]
    assert len(calls) == 1, "one unconditional verification call on the apply path"


def test_the_runtime_directory_identity_comes_from_the_tmpfiles_config() -> None:
    """tmpfiles.d stays the single source of truth for path, mode and owner."""
    assert "read_tmpfiles_rule() {" in SOURCE
    assert "read -r rule_path rule_mode rule_user rule_group" in SOURCE


# -- functional helpers -------------------------------------------------------


def _stub_bin(tmp_path: Path, *, tmpfiles_exit: int = 0, systemctl_exit: int = 0) -> Path:
    """Stubs that record the FULL argv of every privileged call."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for name, exit_code in (("systemctl", systemctl_exit), ("systemd-tmpfiles", tmpfiles_exit)):
        script = stub_dir / name
        script.write_text(
            f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {exit_code}\n', encoding="utf-8"
        )
        script.chmod(0o755)
    return stub_dir


def _run(
    tmp_path: Path,
    *args: str,
    tmpfiles_exit: int = 0,
    systemctl_exit: int = 0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    stub_dir = _stub_bin(tmp_path, tmpfiles_exit=tmpfiles_exit, systemctl_exit=systemctl_exit)
    env = dict(
        os.environ,
        PATH=f"{sh(stub_dir)}:{os.environ.get('PATH', '')}",
        OPS_INSTALL_TEST_MODE="1",
    )
    return subprocess.run(  # noqa: S603
        ["bash", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        cwd=None if cwd is None else str(cwd),
        check=False,
    )


def _calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _lib(script: str, tmp_path: Path, **stub: int) -> subprocess.CompletedProcess[str]:
    """Call the installer's helpers directly via its documented test seam."""
    stub_dir = _stub_bin(tmp_path, **stub)
    env = dict(
        os.environ,
        PATH=f"{sh(stub_dir)}:{os.environ.get('PATH', '')}",
        OPS_INSTALL_LIB_ONLY="1",
    )
    # as_posix(): a Windows path inside `bash -c` would have its backslashes
    # consumed as escapes, silently sourcing nothing.
    return subprocess.run(  # noqa: S603
        ["bash", "-c", f"source '{INSTALLER.as_posix()}'; {script}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )


def _assert_failed_cleanly(result: subprocess.CompletedProcess[str]) -> None:
    """Non-zero, no success claim, no invitation to enable the timer."""
    assert result.returncode != 0, result.stdout
    combined = result.stdout + result.stderr
    assert "INSTALL INCOMPLETE" in combined
    assert "COMPLETE — NOTHING was enabled" not in result.stdout
    assert "tmpfiles config: APPLIED" not in result.stdout
    assert "systemctl enable --now" not in result.stdout


# -- 1. CHECK mode ------------------------------------------------------------


@POSIX_ONLY
def test_check_mode_writes_nothing_and_runs_no_system_command(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result = _run(tmp_path, f"--dest-root={sh(root)}")
    assert result.returncode == 0, result.stderr
    assert "CHECK / DRY-RUN" in result.stdout
    assert list(root.rglob("*")) == []
    assert _calls(tmp_path) == [], "CHECK mode must invoke no system command"


# -- 2/3. reload decision, and the mandatory runtime directory ----------------


@POSIX_ONLY
def test_changed_files_would_reload_and_the_runtime_directory_is_verified(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a real apply would: RELOAD (3 unit file(s) changed)" in result.stdout
    assert "runtime dir    : VERIFIED" in result.stdout


@POSIX_ONLY
def test_an_unchanged_apply_still_creates_the_vanished_runtime_directory(
    tmp_path: Path,
) -> None:
    """Defect B, stated without depending on settable mode bits.

    Every asset already matches and no reload is needed, yet `/run` has been
    emptied by a reboot. The directory must still come back — the old flow
    skipped the whole phase and reported success.
    """
    root = tmp_path / "root"
    first = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    runtime = root / "run" / "ibgateway"
    assert runtime.is_dir(), first.stdout + first.stderr

    shutil.rmtree(runtime)
    second = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert "unchanged :" in second.stdout, "the fixture must exercise the no-change path"
    assert "a real apply would: SKIP (no unit file changed)" in second.stdout
    assert runtime.is_dir(), "the vanished runtime directory was not recreated"


@POSIX_ONLY
def test_no_reload_still_creates_the_runtime_directory(tmp_path: Path) -> None:
    """--no-reload suppresses the reload ONLY."""
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", "--no-reload", f"--dest-root={sh(root)}")
    assert "a real apply would: SKIP (--no-reload)" in result.stdout
    assert (root / "run" / "ibgateway").is_dir(), "--no-reload suppressed the runtime dir"


@POSIX_ONLY
def test_unchanged_files_do_not_reload_but_still_verify_the_runtime_directory(
    tmp_path: Path,
) -> None:
    """Defect B, the core case: nothing changed, yet the directory is still made."""
    root = tmp_path / "root"
    first = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert first.returncode == 0, first.stderr

    # Simulate the reboot that empties /run while every config file survives.
    shutil.rmtree(root / "run" / "ibgateway")
    assert not (root / "run" / "ibgateway").exists()

    second = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "a real apply would: SKIP (no unit file changed)" in second.stdout
    assert "runtime dir    : VERIFIED" in second.stdout
    assert (root / "run" / "ibgateway").is_dir(), "the vanished directory was not recreated"


@POSIX_ONLY
def test_no_reload_suppresses_only_the_reload(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", "--no-reload", f"--dest-root={sh(root)}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "a real apply would: SKIP (--no-reload)" in result.stdout
    assert "runtime dir    : VERIFIED" in result.stdout
    assert (root / "run" / "ibgateway").is_dir()


# -- 4. rehearsal isolation ---------------------------------------------------


@POSIX_ONLY
def test_rehearsal_invokes_no_privileged_command_at_all(tmp_path: Path) -> None:
    """Exact argv inspection: the stubs recorded nothing, because nothing ran."""
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [], f"rehearsal invoked: {_calls(tmp_path)}"
    assert "systemd-tmpfiles NOT invoked" in result.stdout
    assert "the real systemd is never contacted" in result.stdout


@POSIX_ONLY
def test_rehearsal_does_not_create_or_modify_the_real_runtime_directory(
    tmp_path: Path,
) -> None:
    real = Path("/run/ibgateway")
    existed = real.exists()
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert result.returncode == 0, result.stderr
    assert real.exists() == existed, "the rehearsal touched the REAL /run/ibgateway"


@POSIX_ONLY
def test_rehearsal_stages_everything_inside_the_scratch_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert result.returncode == 0, result.stderr
    staged = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert staged == [
        "etc/logrotate.d/ibgateway",
        "etc/systemd/system/ibgateway-ibc.service",
        "etc/systemd/system/ibgateway-watchdog.service",
        "etc/systemd/system/ibgateway-watchdog.timer",
        "etc/tmpfiles.d/ibgateway.conf",
    ], staged
    runtime = root / "run" / "ibgateway"
    assert runtime.is_dir()
    assert oct(runtime.stat().st_mode & 0o777) == "0o700"


@POSIX_ONLY
def test_rehearsal_never_claims_to_have_established_ownership(tmp_path: Path) -> None:
    """It cannot chown without privilege, and it must not pretend otherwise."""
    root = tmp_path / "root"
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    assert "ownership opc:opc is NOT established by a" in result.stdout
    assert "REHEARSAL COMPLETE — the real host is unchanged" in result.stdout
    assert "systemctl enable --now" not in result.stdout, "no timer advice after a rehearsal"


@BASH_ONLY
def test_a_wrong_mode_fails_verification(tmp_path: Path) -> None:
    target = tmp_path / "ibgateway"
    target.mkdir()
    target.chmod(0o755)
    result = _lib(f'verify_runtime_dir "{sh(target)}" 0700 "$(id -un)" "$(id -gn)"', tmp_path)
    _assert_failed_cleanly(result)
    assert "has mode 755, expected 0700" in result.stderr


@BASH_ONLY
def test_a_wrong_owner_fails_verification(tmp_path: Path) -> None:
    """Pass the directory's ACTUAL mode so the OWNER check is what fails.

    Otherwise this silently degrades into a second mode test on any host whose
    umask or filesystem does not yield 0700.
    """
    target = tmp_path / "ibgateway"
    target.mkdir()
    result = _lib(
        f'verify_runtime_dir "{sh(target)}" "$(stat -c \'%a\' "{sh(target)}")"'
        " nosuchuser nosuchgroup",
        tmp_path,
    )
    _assert_failed_cleanly(result)
    assert "is owned by" in result.stderr


@BASH_ONLY
def test_a_wrong_group_fails_verification(tmp_path: Path) -> None:
    target = tmp_path / "ibgateway"
    target.mkdir()
    result = _lib(
        f'verify_runtime_dir "{sh(target)}" "$(stat -c \'%a\' "{sh(target)}")"'
        f' "$(stat -c \'%U\' "{sh(target)}")" nosuchgroup',
        tmp_path,
    )
    _assert_failed_cleanly(result)
    assert "has group" in result.stderr


@BASH_ONLY
def test_a_missing_or_non_directory_runtime_path_fails(tmp_path: Path) -> None:
    missing = _lib(f'verify_runtime_dir "{sh(tmp_path)}/absent" 0700 x y', tmp_path)
    _assert_failed_cleanly(missing)
    assert "does not exist" in missing.stderr

    regular = tmp_path / "not-a-dir"
    regular.write_text("", encoding="utf-8")
    result = _lib(f'verify_runtime_dir "{sh(regular)}" 0700 x y', tmp_path)
    _assert_failed_cleanly(result)
    assert "not a directory" in result.stderr


@BASH_ONLY
def test_a_loosened_tmpfiles_rule_is_rejected(tmp_path: Path) -> None:
    """0700 and opc:opc are the contract, wherever the config claims otherwise."""
    loose = _lib("validate_tmpfiles_rule /run/ibgateway 0755 opc opc", tmp_path)
    _assert_failed_cleanly(loose)
    assert "must be 0700" in loose.stderr

    wrong_owner = _lib("validate_tmpfiles_rule /run/ibgateway 0700 root root", tmp_path)
    _assert_failed_cleanly(wrong_owner)
    assert "must be owned by opc:opc" in wrong_owner.stderr

    wrong_path = _lib("validate_tmpfiles_rule /tmp/elsewhere 0700 opc opc", tmp_path)
    _assert_failed_cleanly(wrong_path)
    assert "unexpected path" in wrong_path.stderr


# -- 5/6. failure semantics on the privileged path ----------------------------


@BASH_ONLY
def test_a_failed_tmpfiles_run_fails_the_install(tmp_path: Path) -> None:
    result = _lib(
        'apply_tmpfiles_real /etc/tmpfiles.d/ibgateway.conf /run/ibgateway',
        tmp_path,
        tmpfiles_exit=1,
    )
    _assert_failed_cleanly(result)
    assert "systemd-tmpfiles --create failed" in result.stderr
    assert "Do NOT enable the timer" in result.stderr


@BASH_ONLY
def test_a_successful_tmpfiles_run_is_silent_about_success(tmp_path: Path) -> None:
    """The success claim belongs to the caller, after verification passes."""
    result = _lib(
        'apply_tmpfiles_real /etc/tmpfiles.d/ibgateway.conf /run/ibgateway', tmp_path
    )
    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls == ["systemd-tmpfiles --create /etc/tmpfiles.d/ibgateway.conf"], calls


@BASH_ONLY
def test_verification_failure_after_tmpfiles_is_still_an_incomplete_install(
    tmp_path: Path,
) -> None:
    """tmpfiles can exit 0 and still leave nothing usable behind."""
    result = _lib(
        'apply_tmpfiles_real /etc/tmpfiles.d/ibgateway.conf /run/ibgateway'
        f' && verify_runtime_dir "{sh(tmp_path)}/never-created" 0700 opc opc',
        tmp_path,
    )
    _assert_failed_cleanly(result)
    assert "does not exist" in result.stderr


# -- rehearsal root containment (arbitrary destination root) ------------------
#
# The rehearsal avoids systemctl and systemd-tmpfiles, but it still runs
# `install -D`, `mkdir -p` and `chmod`. With an unconstrained destination root
# those three alone overwrite the real host: `--dest-root=/` wrote straight
# into /etc/systemd/system and chmod'ed the real /run/ibgateway. Absence of
# privileged COMMANDS is not containment.

SYMLINK_CAPABLE = pytest.mark.skipif(
    os.name != "posix",
    reason="creating symlinks (and to non-existent targets) needs POSIX",
)


def _assert_rejected_without_touching_anything(
    result: subprocess.CompletedProcess[str], tmp_path: Path
) -> None:
    """Non-zero, nothing executed, nothing claimed, no invitation to proceed."""
    assert result.returncode != 0, result.stdout
    assert _calls(tmp_path) == [], f"a rejected run executed: {_calls(tmp_path)}"
    assert "REHEARSAL COMPLETE" not in result.stdout
    assert "install           : COMPLETE" not in result.stdout
    assert "tmpfiles config: APPLIED" not in result.stdout
    assert "systemctl enable --now" not in result.stdout


@BASH_ONLY
def test_dest_root_is_not_a_public_interface(tmp_path: Path) -> None:
    """Owners get --rehearsal; a caller-supplied root is the attack surface."""
    stub_dir = _stub_bin(tmp_path)
    env = dict(os.environ, PATH=f"{sh(stub_dir)}:{os.environ.get('PATH', '')}")
    env.pop("OPS_INSTALL_TEST_MODE", None)
    result = subprocess.run(  # noqa: S603
        ["bash", str(INSTALLER), "--apply", "--dest-root=/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2, result.stdout
    assert "test-only interface" in result.stderr
    assert "--rehearsal" in result.stderr
    assert _calls(tmp_path) == []


@BASH_ONLY
def test_the_root_filesystem_is_rejected(tmp_path: Path) -> None:
    """The headline escape: / would install into the real /etc."""
    result = _run(tmp_path, "--apply", "--dest-root=/")
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "must not be /" in result.stderr


@BASH_ONLY
def test_an_empty_root_is_rejected(tmp_path: Path) -> None:
    """It must not silently upgrade into a private root."""
    result = _run(tmp_path, "--apply", "--dest-root=")
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "must not be empty" in result.stderr


@BASH_ONLY
def test_traversal_resolving_to_the_root_filesystem_is_rejected(
    tmp_path: Path,
) -> None:
    """Relative + cwd: the shell resolves it, so this works on any platform."""
    work = tmp_path / "work"
    work.mkdir()
    result = _run(
        tmp_path, "--apply", "--dest-root=../../../../../../../../../..", cwd=work
    )
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "must not be /" in result.stderr


@BASH_ONLY
def test_traversal_resolving_into_a_system_tree_is_rejected(tmp_path: Path) -> None:
    """Canonicalization is what the containment check is measured against."""
    work = tmp_path / "work"
    work.mkdir()
    result = _run(
        tmp_path, "--apply", "--dest-root=../../../../../../../../../../etc", cwd=work
    )
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "system tree" in result.stderr


@BASH_ONLY
def test_an_existing_directory_without_the_marker_is_rejected(tmp_path: Path) -> None:
    """A mistyped root must never become "install into someone else's directory"."""
    victim = tmp_path / "someone-elses-data"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me", encoding="utf-8")
    result = _run(
        tmp_path, "--apply", "--dest-root=./someone-elses-data", cwd=tmp_path
    )
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert ".ops-rehearsal-root" in result.stderr
    assert (victim / "important.txt").read_text(encoding="utf-8") == "keep me"
    assert not (victim / "etc").exists(), "a rejected run still wrote into the tree"


@SYMLINK_CAPABLE
@BASH_ONLY
def test_a_root_that_is_a_symlink_to_slash_is_rejected(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to("/")
    result = _run(tmp_path, "--apply", f"--dest-root={sh(link)}")
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "symlink" in result.stderr


@SYMLINK_CAPABLE
@BASH_ONLY
def test_a_root_containing_an_etc_symlink_is_rejected(tmp_path: Path) -> None:
    """`etc -> /etc` would make `install -D` write through to the real /etc."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / ".ops-rehearsal-root").write_text("x", encoding="utf-8")
    (root / "etc").symlink_to("/etc")
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "symlink inside the rehearsal root" in result.stderr


@SYMLINK_CAPABLE
@BASH_ONLY
def test_a_root_containing_a_run_symlink_is_rejected(tmp_path: Path) -> None:
    """`run -> /run` would make mkdir/chmod hit the real /run/ibgateway."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / ".ops-rehearsal-root").write_text("x", encoding="utf-8")
    (root / "run").symlink_to("/run")
    result = _run(tmp_path, "--apply", f"--dest-root={sh(root)}")
    _assert_rejected_without_touching_anything(result, tmp_path)
    assert "symlink inside the rehearsal root" in result.stderr


# -- the public --rehearsal mode ----------------------------------------------


@POSIX_ONLY
def test_rehearsal_creates_a_fresh_private_root_and_writes_only_beneath_it(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, "--rehearsal")
    assert result.returncode == 0, result.stdout + result.stderr

    prefix = ">>> REHEARSAL ROOT: "
    line = next(x for x in result.stdout.splitlines() if x.startswith(prefix))
    root = Path(line[len(prefix):].strip())

    assert root.is_dir(), "the rehearsal root must be kept for inspection"
    assert str(root) != "/"
    assert not root.is_symlink()
    assert root.resolve() == root
    assert oct(root.stat().st_mode & 0o777) == "0o700"
    assert (root / ".ops-rehearsal-root").is_file()

    staged = sorted(
        str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()
    )
    assert staged == [
        ".ops-rehearsal-root",
        "etc/logrotate.d/ibgateway",
        "etc/systemd/system/ibgateway-ibc.service",
        "etc/systemd/system/ibgateway-watchdog.service",
        "etc/systemd/system/ibgateway-watchdog.timer",
        "etc/tmpfiles.d/ibgateway.conf",
    ], staged
    assert (root / "run" / "ibgateway").is_dir()
    assert _calls(tmp_path) == [], "rehearsal invoked a privileged command"
    assert "is NOT deleted on exit" in result.stdout
    shutil.rmtree(root, ignore_errors=True)


@POSIX_ONLY
def test_rehearsal_needs_no_destination_argument(tmp_path: Path) -> None:
    """--rehearsal implies apply-into-scratch; there is no root to get wrong."""
    result = _run(tmp_path, "--rehearsal")
    assert result.returncode == 0, result.stderr
    assert "ownership opc:opc is NOT established by a" in result.stdout
    prefix = ">>> REHEARSAL ROOT: "
    line = next(x for x in result.stdout.splitlines() if x.startswith(prefix))
    shutil.rmtree(Path(line[len(prefix):].strip()), ignore_errors=True)


# -- the containment pipeline, asserted structurally --------------------------


def _function_body(name: str) -> str:
    return SOURCE.split(f"{name}() {{", 1)[1].split("\n}", 1)[0]


def test_the_rehearsal_root_validation_pipeline_is_complete() -> None:
    """Every containment step must be present, and structure before ownership.

    Behavioural proof of the symlink cases needs a POSIX host (you cannot
    reliably create a symlink to a non-existent target on Windows), so the
    pipeline itself is pinned here where it can be checked anywhere.
    """
    body = _function_body("validate_rehearsal_root")
    for step in (
        "must not be empty",
        "reject_symlinked_components",
        "canonicalize",
        "must not be /",
        "SYSTEM_TREES",
        "reject_symlinks_inside",
        "REHEARSAL_MARKER",
        "require_private_dir",
    ):
        assert step in body, f"missing containment step: {step}"

    # The supplied path is checked for symlinks BEFORE canonicalization,
    # because realpath would resolve them silently.
    assert body.index("reject_symlinked_components") < body.index("canonicalize")
    # A hostile structure is rejected before ownership/mode, so the rejection
    # does not depend on a filesystem that can represent 0700.
    assert body.index("reject_symlinks_inside") < body.index("require_private_dir")


def test_the_private_rehearsal_root_is_fresh_and_restrictive() -> None:
    body = _function_body("create_private_rehearsal_root")
    assert "mktemp -d" in body
    assert "chmod 0700" in body
    assert "-L" in body, "must reject a symlink returned by mktemp"
    assert 'must not be /' in body or '!= "/"' in body
    assert "ls -A" in body, "a fresh root must be proven empty"
    assert "require_private_dir" in body
    # Never deleted: the owner has to be able to inspect it.
    assert "rm -rf" not in SOURCE
    assert "trap " not in SOURCE, "no cleanup trap may remove the rehearsal root"


def test_every_rehearsal_write_is_containment_checked() -> None:
    """install / mkdir / chmod each get a fresh resolve-and-compare."""
    assert "assert_within_root() {" in SOURCE
    body = _function_body("assert_within_root")
    assert "canonicalize" in body
    assert "REHEARSAL_ROOT" in body
    assert "escapes the rehearsal root" in body

    calls = [
        line
        for line in command_lines()
        if "assert_within_root " in line and "()" not in line
    ]
    assert len(calls) >= 3, f"expected a check before each write, found {calls}"

    # The check precedes the write it guards.
    flat = "\n".join(code_lines())
    assert flat.index("assert_within_root") < flat.index("install -D")
    runtime = flat.split("runtime_dir=", 1)[1]
    assert runtime.index("assert_within_root") < runtime.index("mkdir -p")


def test_dest_root_is_gated_behind_test_mode() -> None:
    assert "OPS_INSTALL_TEST_MODE" in SOURCE
    assert "test-only interface" in SOURCE
    assert "--rehearsal) REHEARSAL=1" in SOURCE


@BASH_ONLY
def test_a_rehearsal_root_that_is_not_private_is_rejected(tmp_path: Path) -> None:
    """0700 is required of the ROOT itself, not only of run/ibgateway.

    Behavioural and portable: a default-umask mkdir gives 0755 on every
    platform, so this guards the check even where 0700 cannot be set.
    """
    loose = tmp_path / "loose"
    loose.mkdir()
    result = _lib(f'require_private_dir "{sh(loose)}"', tmp_path)
    _assert_failed_cleanly(result)
    assert "must be 0700" in result.stderr


def test_the_rehearsal_root_privacy_check_covers_mode_and_owner() -> None:
    """Ownership cannot be faked in a test, so it is pinned structurally."""
    body = _function_body("require_private_dir")
    assert "stat -c '%a'" in body, "mode must be read"
    assert "stat -c '%U'" in body, "owner must be read"
    assert "must be 0700" in body
    assert "must be owned by" in body
    assert "id -un" in body

    calls = [
        line
        for line in command_lines()
        if line.strip().startswith("require_private_dir ")
    ]
    assert len(calls) >= 3, (
        f"every path that establishes a root must check it, found {calls}"
    )


def test_the_rehearsal_completion_notice_points_at_the_manual_apply() -> None:
    """It used to say "re-run without --dest-root", which no longer exists.

    Telling an owner to re-run with a flag that has been removed from the
    public interface is a dead end at exactly the moment they are deciding
    whether to touch the host.
    """
    notice = SOURCE.split("cat <<'REHEARSALNEXT'", 1)[1].split("REHEARSALNEXT", 1)[0]
    assert "--dest-root" not in notice, "the notice must not name a removed flag"
    assert "WITHOUT --rehearsal" in notice
    assert "OPERATIONAL_HARDENING.md" in notice, "point at the documented step"
    assert "Do NOT" in notice and "enable any timer" in notice
    # The manual step stays manual: nothing here starts a privileged action.
    for command in PRIVILEGED:
        assert command not in notice
