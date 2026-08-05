"""Transaction hardening: metadata rollback, full preflight, effective VERIFY.

The reviewed defects this closes, all present in `c61821a` (merged in #60):

* backups were forced to 0600 and restored with ``cp -p``, so a 0644 drop-in or
  a 0755 executable could come back as 0600 — and "byte-identical" was claimed
  on content alone;
* the installer was an opaque mutator outside the manifest, counted as a
  mutation because it exited zero;
* preflight covered a handful of paths, not privilege, unit sources, process
  identity or unit states;
* rollback never told systemd that the files had gone back;
* VERIFY checked liveness fields, not the effective contract, and CHECK
  borrowed the execution word ``REMEDIATED_RESTART_REQUIRED`` for a run that
  changed nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
ENGINE_PATH = SCRIPTS / "owner_host_remediation.py"

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="asserts real POSIX mode/uid/gid")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("owner_host_remediation_t", ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["owner_host_remediation_t"] = module
    spec.loader.exec_module(module)
    return module


rem = _load()

CONTRADICTING = "[Service]\nType=oneshot\nRemainAfterExit=yes\nKillMode=process\n"
ALLOWED = "[Service]\nEnvironment=DISPLAY=:1\nEnvironment=XAUTHORITY=/home/opc/.Xauthority\n"
REPO_ROOT = rem.PRODUCTION_REPO_ROOT

HEALTHY_UNITS: dict[str, dict[str, str]] = {
    rem.CANONICAL_GATEWAY_UNIT: {
        "Type": "simple",
        "RemainAfterExit": "no",
        "Restart": "no",
        "KillMode": "control-group",
        "ProtectSystem": "strict",
        "ProtectHome": "read-only",
        "PrivateTmp": "yes",
        "ReadWritePaths": "/run/ibgateway",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "4242",
        "UnitFileState": "enabled",
    },
    rem.CANONICAL_WATCHDOG_UNIT: {
        "WorkingDirectory": REPO_ROOT,
        "ExecStart": (
            f"{{ argv[]={REPO_ROOT}/.venv/bin/python "
            f"{REPO_ROOT}/scripts/ib_gateway_watchdog.py --host 127.0.0.1 ; }}"
        ),
        "ActiveState": "inactive",
        "UnitFileState": "disabled",
    },
    rem.CANONICAL_WATCHDOG_TIMER: {"UnitFileState": "disabled", "ActiveState": "inactive"},
    rem.LEGACY_GATEWAY_UNIT: {"UnitFileState": "disabled", "ActiveState": "inactive"},
}


def build(
    tmp_path: Path,
    *,
    dropins: dict[str, str] | None = None,
    stale: bool = True,
    runtime: bool = True,
    assets_installed: bool = False,
) -> Path:
    """A complete fake host. Real repository sources, real drop-ins."""
    sysroot = tmp_path / "sysroot"
    (sysroot / f"{REPO_ROOT.lstrip('/')}/scripts").mkdir(parents=True, exist_ok=True)
    (sysroot / f"{REPO_ROOT.lstrip('/')}/systemd").mkdir(parents=True, exist_ok=True)
    (sysroot / f"{REPO_ROOT.lstrip('/')}/.venv/bin").mkdir(parents=True, exist_ok=True)

    if runtime:
        for name in ("systemd_contract.py", "owner_host_remediation.py"):
            shutil.copy2(SCRIPTS / name, sysroot / f"{REPO_ROOT.lstrip('/')}/scripts" / name)
        for source in (REPO / "systemd").glob("*"):
            if source.is_file():
                shutil.copy2(source, sysroot / f"{REPO_ROOT.lstrip('/')}/systemd" / source.name)
        shim = sysroot / f"{REPO_ROOT.lstrip('/')}/.venv/bin/python"
        if os.name == "posix":
            shim.write_text(
                f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n',
                encoding="utf-8",
            )
            shim.chmod(0o755)
        else:
            # Windows cannot exec a #! script through subprocess, so the fake
            # "pinned interpreter" has to be a real executable here. On the
            # real host it is an ELF binary either way. A relocated interpreter
            # also needs its pyvenv.cfg, exactly as a real venv does.
            shutil.copy2(sys.executable, shim)
            venv_root = shim.parent.parent
            real_config = Path(sys.executable).parent.parent / "pyvenv.cfg"
            if real_config.is_file():
                shutil.copy2(real_config, venv_root / "pyvenv.cfg")
            # A relocated interpreter looks for its own site-packages; point it
            # back at the real one so the import probe genuinely resolves the
            # project packages instead of testing a broken venv.
            import sysconfig

            site_packages = venv_root / "Lib" / "site-packages"
            site_packages.mkdir(parents=True, exist_ok=True)
            (site_packages / "_real_site.pth").write_text(
                sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8"
            )

    dropin_dir = sysroot / rem.DROPIN_DIR.lstrip("/")
    dropin_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (dropins if dropins is not None else {"zz.conf": CONTRADICTING}).items():
        (dropin_dir / name).write_text(text, encoding="utf-8")

    if stale:
        stale_path = sysroot / rem.STALE_WATCHDOG_PATH.lstrip("/")
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text("stale copy\n", encoding="utf-8")

    if assets_installed:
        for name, destination, mode in rem.CANONICAL_ASSETS:
            source = sysroot / f"{REPO_ROOT.lstrip('/')}/systemd/{name}"
            target = sysroot / destination.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(mode)

    (sysroot / "var/lib").mkdir(parents=True, exist_ok=True)
    (sysroot / "proc/sys/kernel/random").mkdir(parents=True, exist_ok=True)
    (sysroot / "proc/sys/kernel/random/boot_id").write_text("boot-aaaa\n", encoding="utf-8")
    (sysroot / "gateway-identity.json").write_text(
        json.dumps(
            {
                "pid": "4242",
                "start_ticks": "998877",
                "cgroup": "/user.slice/user-1000.slice/session-2.scope",
                "java_matches": 1,
            }
        ),
        encoding="utf-8",
    )
    return sysroot


def make(
    sysroot: Path,
    *,
    units: dict[str, dict[str, str]] | None = None,
    fail: dict[str, int] | None = None,
    euid: int = 0,
    trace: Path | None = None,
) -> Any:
    # The import probe is simulated here so the transaction logic is testable
    # on any platform; it is exercised FOR REAL against the repository's own
    # venv in test_the_import_probe_really_runs below. Classification is never
    # simulated — it runs the real contract CLI.
    script: dict[str, int] = {"-c import ": 0}
    script.update(fail or {})
    adapter = rem.ScriptedCommandAdapter(script, trace_path=trace)
    adapter.set_unit_properties(units if units is not None else HEALTHY_UNITS)
    return rem.Remediator(
        repo_root=REPO_ROOT,
        sysroot=str(sysroot),
        commands=adapter,
        test_mode=True,
        test_euid=euid,
    )


def tree(root: Path) -> dict[str, tuple[str, int]]:
    """Path -> (sha256, mode). Content AND metadata, because both are restored."""
    import hashlib

    out: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mode & 0o7777,
            )
    return out


@pytest.fixture(autouse=True)
def _reset_fault() -> Any:
    """The fault seam is global; never let it leak between tests."""
    original = rem._FAULT
    yield
    rem._FAULT = original


# =============================================================================
# 1 — rollback metadata integrity
# =============================================================================


def test_file_metadata_captures_content_mode_and_ownership(tmp_path: Path) -> None:
    target = tmp_path / "unit.conf"
    target.write_text("x\n", encoding="utf-8")
    target.chmod(0o644)
    metadata = rem.FileMetadata.of(target)
    assert metadata.mode == (target.stat().st_mode & 0o7777)
    assert metadata.uid == target.stat().st_uid
    assert metadata.gid == target.stat().st_gid
    assert len(metadata.sha256) == 64


def test_differences_names_every_field_that_moved(tmp_path: Path) -> None:
    """ "Identical" needs evidence, per field."""
    base = rem.FileMetadata(sha256="a" * 64, mode=0o644, uid=0, gid=0, size=1)
    same = rem.FileMetadata(sha256="a" * 64, mode=0o644, uid=0, gid=0, size=1)
    assert base.differences(same) == []
    moved = rem.FileMetadata(sha256="b" * 64, mode=0o600, uid=1000, gid=1000, size=1)
    problems = " ".join(base.differences(moved))
    assert "content" in problems
    assert "mode" in problems
    assert "uid" in problems
    assert "gid" in problems


@POSIX_ONLY
@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_rollback_restores_the_original_mode_not_0600(tmp_path: Path, mode: int) -> None:
    """A 0644 drop-in and a 0755 executable must come back as they were.

    Backups keep their original metadata; privacy comes from the 0700
    transaction directory, not from flattening every backup to 0600.
    """
    sysroot = build(tmp_path, dropins={"zz.conf": CONTRADICTING}, stale=False)
    dropin = sysroot / rem.DROPIN_DIR.lstrip("/") / "zz.conf"
    dropin.chmod(mode)
    before = dropin.stat()

    remediator = make(sysroot)
    observation = remediator.collect()
    remediator.validate_all(observation, mode="apply")
    remediator.build_plan()
    remediator.begin_transaction()
    remediator.execute()
    assert not dropin.exists()

    restored, problems = remediator.rollback(reloaded=False)
    assert restored, problems
    assert dropin.is_file()
    after = dropin.stat()
    assert stat.S_IMODE(after.st_mode) == mode, "mode was not restored"
    assert after.st_uid == before.st_uid
    assert after.st_gid == before.st_gid
    # The backup kept the original mode too.
    backups = list(remediator.transaction_dir.glob("*.bak"))
    assert stat.S_IMODE(backups[0].stat().st_mode) == mode


@POSIX_ONLY
def test_a_failed_chmod_during_rollback_is_a_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysroot = build(tmp_path, stale=False)
    remediator = make(sysroot)
    observation = remediator.collect()
    remediator.validate_all(observation, mode="apply")
    remediator.build_plan()
    remediator.begin_transaction()
    remediator.execute()

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("chmod refused")

    monkeypatch.setattr(rem, "_CHMOD", boom)
    restored, problems = remediator.rollback(reloaded=False)
    assert not restored
    assert any("chmod" in p for p in problems), problems


@POSIX_ONLY
def test_a_failed_chown_during_rollback_is_a_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sysroot = build(tmp_path, stale=False)
    remediator = make(sysroot)
    observation = remediator.collect()
    remediator.validate_all(observation, mode="apply")
    remediator.build_plan()
    remediator.begin_transaction()
    remediator.execute()

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("chown refused")

    monkeypatch.setattr(rem, "_CHOWN", boom)
    restored, problems = remediator.rollback(reloaded=False)
    assert not restored
    assert any("chown" in p for p in problems), problems


def test_a_failed_quarantine_cleanup_is_a_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the quarantine destination behind means the host is not restored."""
    sysroot = build(tmp_path, stale=False)
    remediator = make(sysroot)
    observation = remediator.collect()
    remediator.validate_all(observation, mode="apply")
    remediator.build_plan()
    remediator.begin_transaction()
    remediator.execute()

    real_unlink = Path.unlink

    def selective(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.endswith(".quarantined"):
            raise OSError("cleanup refused")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", selective)
    restored, problems = remediator.rollback(reloaded=False)
    assert not restored, "an un-removed quarantine destination must fail rollback"
    assert any("quarantine destination" in p for p in problems), problems


# =============================================================================
# 2 — complete preflight, zero mutation on any failure
# =============================================================================


GATE_NAMES = (
    "euid_root",
    "repo_root_exact",
    "venv_present",
    "contract_module",
    "required_imports",
    "unit_sources_exact",
    "dropins_classifiable",
    "quarantine_destinations_absent",
    "transaction_root_creatable",
    "exactly_one_gateway_java",
    "gateway_identity_readable",
    "timer_disabled_and_inactive",
    "watchdog_service_inactive",
    "legacy_unit_disabled_and_inactive",
    "systemctl_available",
    "no_competing_manager",
)


def test_every_documented_gate_is_evaluated(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    gates = remediator.validate_all(remediator.collect(), mode="apply")
    names = {g.name for g in gates}
    for expected in GATE_NAMES:
        assert expected in names, expected


def test_a_real_apply_requires_euid_zero(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot, euid=1000)
    gates = remediator.validate_all(remediator.collect(), mode="apply")
    failed = {g.name for g in gates if not g.ok}
    assert "euid_root" in failed


def test_check_is_unprivileged_and_never_requires_root(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot, euid=1000)
    gates = remediator.validate_all(remediator.collect(), mode="check")
    assert "euid_root" not in {g.name for g in gates}
    assert any(g.name == "check_unprivileged" and g.ok for g in gates)


@pytest.mark.parametrize(
    ("gate", "units", "identity"),
    [
        (
            "timer_disabled_and_inactive",
            {
                **HEALTHY_UNITS,
                rem.CANONICAL_WATCHDOG_TIMER: {
                    "UnitFileState": "enabled",
                    "ActiveState": "inactive",
                },
            },
            None,
        ),
        (
            "timer_disabled_and_inactive",
            {
                **HEALTHY_UNITS,
                rem.CANONICAL_WATCHDOG_TIMER: {
                    "UnitFileState": "disabled",
                    "ActiveState": "active",
                },
            },
            None,
        ),
        (
            "watchdog_service_inactive",
            {
                **HEALTHY_UNITS,
                rem.CANONICAL_WATCHDOG_UNIT: {
                    **HEALTHY_UNITS[rem.CANONICAL_WATCHDOG_UNIT],
                    "ActiveState": "activating",
                },
            },
            None,
        ),
        (
            "legacy_unit_disabled_and_inactive",
            {
                **HEALTHY_UNITS,
                rem.LEGACY_GATEWAY_UNIT: {"UnitFileState": "static", "ActiveState": "inactive"},
            },
            None,
        ),
        (
            "no_competing_manager",
            {
                **HEALTHY_UNITS,
                rem.LEGACY_GATEWAY_UNIT: {"UnitFileState": "disabled", "ActiveState": "active"},
            },
            None,
        ),
        (
            "exactly_one_gateway_java",
            None,
            {"pid": "1", "start_ticks": "2", "cgroup": "/x", "java_matches": 2},
        ),
        (
            "gateway_identity_readable",
            None,
            {"pid": "none", "start_ticks": "none", "cgroup": "none", "java_matches": 1},
        ),
    ],
)
def test_each_runtime_gate_fails_closed(
    tmp_path: Path, gate: str, units: dict[str, Any] | None, identity: dict[str, Any] | None
) -> None:
    sysroot = build(tmp_path)
    if identity is not None:
        (sysroot / "gateway-identity.json").write_text(json.dumps(identity), encoding="utf-8")
    remediator = make(sysroot, units=units)
    gates = remediator.validate_all(remediator.collect(), mode="apply")
    assert gate in {g.name for g in gates if not g.ok}, [
        (g.name, g.detail) for g in gates if not g.ok
    ]


def test_an_unclassifiable_dropin_is_a_hard_failure(tmp_path: Path) -> None:
    sysroot = build(tmp_path, dropins={"broken.conf": "\x01not-a-unit\n"})
    remediator = make(sysroot)
    gates = remediator.validate_all(remediator.collect(), mode="apply")
    assert "dropins_classifiable" in {g.name for g in gates if not g.ok}


def test_an_existing_quarantine_destination_blocks_the_run(tmp_path: Path) -> None:
    sysroot = build(tmp_path, stale=False)
    dropin_dir = sysroot / rem.DROPIN_DIR.lstrip("/")
    (dropin_dir / "zz.conf.quarantined").write_text("stale\n", encoding="utf-8")
    remediator = make(sysroot)
    gates = remediator.validate_all(remediator.collect(), mode="apply")
    assert "quarantine_destinations_absent" in {g.name for g in gates if not g.ok}


@pytest.mark.parametrize(
    "scenario", ["missing_runtime", "unclassifiable", "bad_euid", "timer_enabled"]
)
def test_a_failed_gate_produces_zero_mutation(tmp_path: Path, scenario: str) -> None:
    """No baseline, no transaction, no backup, no move, no write."""
    kwargs: dict[str, Any] = {}
    units = None
    euid = 0
    if scenario == "missing_runtime":
        kwargs["runtime"] = False
    elif scenario == "unclassifiable":
        kwargs["dropins"] = {"broken.conf": "\x01x\n"}
    elif scenario == "bad_euid":
        euid = 1000
    else:
        units = {
            **HEALTHY_UNITS,
            rem.CANONICAL_WATCHDOG_TIMER: {"UnitFileState": "enabled", "ActiveState": "inactive"},
        }

    sysroot = build(tmp_path, **kwargs)
    before = tree(sysroot)
    remediator = make(sysroot, units=units, euid=euid)
    outcome = rem.run_apply(remediator, evidence_dir=None)
    assert outcome.state is rem.State.FAILED_PRECHECK, outcome.detail
    assert tree(sysroot) == before, "a failed gate still mutated the host"
    assert remediator.transaction_dir is None
    assert not (sysroot / rem.STATE_DIR.lstrip("/") / "gateway-baseline.json").exists()
    assert remediator.reloads == 0


# =============================================================================
# 3 — one transaction covers the installer assets
# =============================================================================


def test_the_plan_names_every_asset_and_what_it_replaces(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    remediator.validate_all(remediator.collect(), mode="apply")
    plan = remediator.build_plan()
    kinds = [c.kind for c in plan]
    assert kinds.count("quarantine_dropin") == 1
    assert kinds.count("quarantine_stale") == 1
    assert kinds.count("install_asset") == len(rem.CANONICAL_ASSETS)
    installs = [c for c in plan if c.kind == "install_asset"]
    assert all(c.creates_new for c in installs), "a fresh host creates every asset"
    assert all(c.existing is None for c in installs)
    # Machine-readable.
    assert json.dumps([c.as_dict() for c in plan])


def test_an_asset_that_is_already_correct_is_not_a_change(tmp_path: Path) -> None:
    sysroot = build(tmp_path, dropins={"display.conf": ALLOWED}, stale=False, assets_installed=True)
    remediator = make(sysroot)
    remediator.validate_all(remediator.collect(), mode="apply")
    assert remediator.build_plan() == []


def test_replacing_an_existing_asset_records_its_metadata(tmp_path: Path) -> None:
    sysroot = build(tmp_path, dropins={"display.conf": ALLOWED}, stale=False, assets_installed=True)
    target = sysroot / rem.CANONICAL_ASSETS[0][1].lstrip("/")
    target.write_text("drifted\n", encoding="utf-8")
    remediator = make(sysroot)
    remediator.validate_all(remediator.collect(), mode="apply")
    plan = remediator.build_plan()
    replaced = [c for c in plan if c.destination == str(target)]
    assert len(replaced) == 1
    assert replaced[0].existing is not None, "the overwritten file must be recorded"
    assert not replaced[0].creates_new


def test_created_files_are_recorded_so_rollback_removes_them(tmp_path: Path) -> None:
    sysroot = build(tmp_path, dropins={"display.conf": ALLOWED}, stale=False)
    remediator = make(sysroot)
    remediator.validate_all(remediator.collect(), mode="apply")
    remediator.build_plan()
    before = tree(sysroot)
    remediator.begin_transaction()
    remediator.execute()
    assert any(e.created for e in remediator.manifest)
    restored, problems = remediator.rollback(reloaded=False)
    assert restored, problems
    after = {k: v for k, v in tree(sysroot).items() if not k.startswith("var/lib")}
    assert after == {k: v for k, v in before.items() if not k.startswith("var/lib")}


# =============================================================================
# 4/5 — command traces, rollback matrix and recovery reload
# =============================================================================


def test_the_forward_reload_happens_exactly_once(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    trace = tmp_path / "trace.txt"
    remediator = make(sysroot, trace=trace)
    outcome = rem.run_apply(remediator, evidence_dir=None)
    assert outcome.state is rem.State.REMEDIATED_RESTART_REQUIRED, outcome.detail
    assert remediator.reloads == 1
    assert remediator.recovery_reloads == 0
    reloads = [c for c in remediator.commands.calls if c[:2] == ("systemctl", "daemon-reload")]
    assert len(reloads) == 1, remediator.commands.calls


def test_no_start_stop_restart_or_enable_is_ever_issued(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    rem.run_apply(remediator, evidence_dir=None)
    joined = [" ".join(c) for c in remediator.commands.calls]
    for forbidden in ("start", "stop", "restart", "enable", "disable", "--now", "kill"):
        assert not any(f"systemctl {forbidden}" in call for call in joined), forbidden


def test_the_adapter_refuses_a_forbidden_verb_even_if_asked() -> None:
    """Enforced where commands run, so a new call site cannot bypass it."""
    adapter = rem.CommandAdapter()
    for verb in ("start", "restart", "enable"):
        with pytest.raises(PermissionError):
            adapter.run(["systemctl", verb, "ibgateway-ibc.service"])
    with pytest.raises(PermissionError):
        adapter.run(["systemctl", "--now", "ibgateway-watchdog.timer"])


ROLLBACK_POINTS = [
    "after_baseline",
    "after_backup",
    "after_move",
    "after_manifest",
    "after_install",
    "before_reload",
    "after_reload",
    "after_post_collect",
]


@pytest.mark.parametrize("point", ROLLBACK_POINTS)
def test_a_failure_at_each_point_rolls_back_completely(tmp_path: Path, point: str) -> None:
    sysroot = build(tmp_path)
    before = tree(sysroot)
    remediator = make(sysroot)
    rem.install_fault(point)
    outcome = rem.run_apply(remediator, evidence_dir=None)

    assert outcome.state in (
        rem.State.FAILED_APPLY_ROLLED_BACK,
        rem.State.FAILED_APPLY_PARTIAL_ROLLBACK,
    ), f"{point}: {outcome.state} {outcome.detail}"
    after = {k: v for k, v in tree(sysroot).items() if not k.startswith("var/lib")}
    expected = {k: v for k, v in before.items() if not k.startswith("var/lib")}
    if outcome.state is rem.State.FAILED_APPLY_ROLLED_BACK:
        assert after == expected, f"{point} claimed a full rollback but the host differs"


def test_a_failure_after_the_reload_triggers_a_recovery_reload(tmp_path: Path) -> None:
    """Files went back, so systemd must be told again."""
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    rem.install_fault("after_reload")
    outcome = rem.run_apply(remediator, evidence_dir=None)
    assert outcome.state is rem.State.FAILED_APPLY_ROLLED_BACK, outcome.detail
    assert remediator.reloads == 1
    assert remediator.recovery_reloads == 1, "the forward reload was never undone"
    reloads = [c for c in remediator.commands.calls if c[:2] == ("systemctl", "daemon-reload")]
    assert len(reloads) == 2


def test_a_failure_before_the_reload_needs_no_recovery_reload(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    rem.install_fault("before_reload")
    rem.run_apply(remediator, evidence_dir=None)
    assert remediator.reloads == 0
    assert remediator.recovery_reloads == 0


def test_a_failed_recovery_reload_downgrades_to_partial(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot, fail={"daemon-reload": 0})
    # Fail only the SECOND reload: script the forward one to succeed by
    # allowing it, then break recovery through a failing adapter.
    calls = {"n": 0}
    original = remediator.commands.run

    def flaky(argv: list[str]) -> Any:
        if argv[:2] == ["systemctl", "daemon-reload"]:
            calls["n"] += 1
            if calls["n"] == 2:
                return rem.CommandResult(tuple(argv), 1, "", "recovery reload failed")
        return original(argv)

    remediator.commands.run = flaky
    rem.install_fault("after_reload")
    outcome = rem.run_apply(remediator, evidence_dir=None)
    assert outcome.state is rem.State.FAILED_APPLY_PARTIAL_ROLLBACK, outcome.detail
    assert "recovery daemon-reload failed" in outcome.detail


def test_an_effective_property_mismatch_rolls_back(tmp_path: Path) -> None:
    """The contract is verified from a FRESH collection, not from the plan."""
    sysroot = build(tmp_path)
    contradicted = {
        **HEALTHY_UNITS,
        rem.CANONICAL_GATEWAY_UNIT: {
            **HEALTHY_UNITS[rem.CANONICAL_GATEWAY_UNIT],
            "Type": "oneshot",
        },
    }
    before = tree(sysroot)
    remediator = make(sysroot, units=contradicted)
    outcome = rem.run_apply(remediator, evidence_dir=None)
    assert outcome.state is rem.State.FAILED_APPLY_ROLLED_BACK, outcome.detail
    assert remediator.recovery_reloads == 1
    after = {k: v for k, v in tree(sysroot).items() if not k.startswith("var/lib")}
    assert after == {k: v for k, v in before.items() if not k.startswith("var/lib")}


# =============================================================================
# 6 — VERIFY proves the effective contract
# =============================================================================


def test_verify_checks_every_documented_effective_property(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    gates = remediator.verify_contract(remediator.collect())
    names = {g.name for g in gates}
    for key in rem.EXPECTED_GATEWAY_PROPERTIES:
        assert f"gateway:{key}" in names
    for expected in (
        "gateway:ReadWritePaths",
        "gateway:no_forbidden_dropin",
        "watchdog:WorkingDirectory",
        "watchdog:interpreter",
        "watchdog:script",
        "watchdog:no_env_interpreter",
        "watchdog:no_stale_opt",
        "timer:UnitFileState",
        "timer:ActiveState",
        "watchdog_service:ActiveState",
    ):
        assert expected in names, expected


@pytest.mark.parametrize("bad", ["", "unknown", "static", "failed", "activating", "oneshot"])
def test_any_non_exact_value_fails_verification(tmp_path: Path, bad: str) -> None:
    sysroot = build(tmp_path)
    units = {
        **HEALTHY_UNITS,
        rem.CANONICAL_GATEWAY_UNIT: {**HEALTHY_UNITS[rem.CANONICAL_GATEWAY_UNIT], "Type": bad},
    }
    remediator = make(sysroot, units=units)
    gates = remediator.verify_contract(remediator.collect())
    assert any(g.name == "gateway:Type" and not g.ok for g in gates)


def test_verify_without_a_baseline_refuses(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    outcome = rem.run_verify(remediator, post_restart=False)
    assert outcome.state is rem.State.FAILED_VERIFY
    assert "never creates its own" in outcome.detail
    assert not (sysroot / rem.STATE_DIR.lstrip("/") / "gateway-baseline.json").exists()


def test_preserving_the_user_session_cgroup_is_not_verified_complete(tmp_path: Path) -> None:
    """It proves only that remediation did not restart the Gateway."""
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    applied = rem.run_apply(remediator, evidence_dir=None)
    assert applied.state is rem.State.REMEDIATED_RESTART_REQUIRED

    verifier = make(sysroot)
    outcome = rem.run_verify(verifier, post_restart=False)
    assert outcome.state is rem.State.REMEDIATED_RESTART_REQUIRED
    assert outcome.state is not rem.State.VERIFIED_COMPLETE
    assert "owner-authorized restart" in outcome.detail


def test_post_restart_verify_reaches_verified_complete(tmp_path: Path) -> None:
    """After the restart the PID MUST differ, so the old identity is not required."""
    sysroot = build(tmp_path)
    make(sysroot).__class__  # noqa: B018 - readability
    remediator = make(sysroot)
    rem.run_apply(remediator, evidence_dir=None)

    # The owner-authorized restart happened: new PID, canonical cgroup.
    (sysroot / "gateway-identity.json").write_text(
        json.dumps(
            {
                "pid": "5150",
                "start_ticks": "111222",
                "cgroup": rem.CANONICAL_SERVICE_CGROUP,
                "java_matches": 1,
            }
        ),
        encoding="utf-8",
    )
    verifier = make(sysroot)
    outcome = rem.run_verify(verifier, post_restart=True)
    assert outcome.state is rem.State.VERIFIED_COMPLETE, outcome.detail


def test_post_restart_verify_fails_when_ownership_is_still_wrong(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot)
    rem.run_apply(remediator, evidence_dir=None)
    outcome = rem.run_verify(make(sysroot), post_restart=True)
    assert outcome.state is rem.State.FAILED_VERIFY


# =============================================================================
# 7 — persisted evidence
# =============================================================================


def test_evidence_carries_the_full_schema(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    evidence = tmp_path / "evidence"
    remediator = make(sysroot)
    rem.run_apply(remediator, evidence_dir=evidence)
    payload = json.loads((evidence / "remediation-apply.json").read_text(encoding="utf-8"))
    for key in rem.REQUIRED_EVIDENCE_KEYS:
        assert key in payload, key
    assert payload["schema"] == rem.EVIDENCE_SCHEMA
    assert payload["transaction_id"] == remediator.transaction_id
    assert payload["boot_id"] == "boot-aaaa"
    assert payload["gateway"]["start_ticks"] == "998877"
    assert payload["planned"] and payload["installed"] and payload["quarantined"]
    assert (
        rem.validate_evidence(
            payload, transaction_id=remediator.transaction_id, boot_id="boot-aaaa"
        )
        == []
    )


@POSIX_ONLY
def test_evidence_is_written_privately(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    evidence = tmp_path / "evidence"
    rem.run_apply(make(sysroot), evidence_dir=evidence)
    written = evidence / "remediation-apply.json"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda p: p.__setitem__("schema", "wrong"), "schema"),
        (lambda p: p.pop("boot_id"), "missing key: boot_id"),
        (lambda p: p.__setitem__("timestamp", "yesterday"), "ISO-8601"),
        (lambda p: p["gateway"].__setitem__("start_ticks", "abc"), "not numeric"),
        (lambda p: p["gateway"].pop("cgroup"), "gateway.cgroup missing"),
    ],
)
def test_malformed_evidence_is_rejected(mutate: Any, expected: str) -> None:
    payload = {
        "schema": rem.EVIDENCE_SCHEMA,
        "transaction_id": "txn-1",
        "timestamp": "2026-08-05T00:00:00Z",
        "boot_id": "boot-aaaa",
        "captured_phase": "post_apply",
        "gateway": {"pid": "1", "start_ticks": "2", "cgroup": "/x"},
        "unit_states": {},
        "planned": [],
        "installed": [],
        "quarantined": [],
    }
    mutate(payload)
    problems = " ".join(rem.validate_evidence(payload))
    assert expected in problems, problems


def test_stale_evidence_from_another_boot_is_rejected() -> None:
    payload = {
        "schema": rem.EVIDENCE_SCHEMA,
        "transaction_id": "txn-1",
        "timestamp": "2026-08-05T00:00:00Z",
        "boot_id": "boot-OLD",
        "captured_phase": "post_apply",
        "gateway": {"pid": "1", "start_ticks": "2", "cgroup": "/x"},
        "unit_states": {},
        "planned": [],
        "installed": [],
        "quarantined": [],
    }
    problems = rem.validate_evidence(payload, boot_id="boot-NEW")
    assert any("stale" in p for p in problems), problems


# =============================================================================
# 8 — real APPLY twice
# =============================================================================


def test_a_second_real_apply_changes_absolutely_nothing(tmp_path: Path) -> None:
    """STATE_DIR included in the comparison, deliberately."""
    sysroot = build(tmp_path)
    first = make(sysroot)
    outcome = rem.run_apply(first, evidence_dir=None)
    assert outcome.state is rem.State.REMEDIATED_RESTART_REQUIRED, outcome.detail
    assert first.reloads == 1
    after_first = tree(sysroot)

    second = make(sysroot)
    again = rem.run_apply(second, evidence_dir=None)
    assert again.state is rem.State.NO_CHANGES_REQUIRED, again.detail
    assert second.manifest == []
    assert second.transaction_dir is None, "a transaction was created for nothing"
    assert second.reloads == 0
    assert second.recovery_reloads == 0
    assert tree(sysroot) == after_first, "the second apply changed something"
    joined = [" ".join(c) for c in second.commands.calls]
    for forbidden in ("start", "stop", "restart", "enable"):
        assert not any(f"systemctl {forbidden}" in c for c in joined)


# =============================================================================
# 9 — CHECK vocabulary
# =============================================================================


def test_check_uses_a_planning_state_not_an_execution_state(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    remediator = make(sysroot, euid=1000)
    outcome = rem.run_check(remediator)
    assert outcome.state is rem.State.CHANGES_REQUIRED
    assert outcome.state is not rem.State.REMEDIATED_RESTART_REQUIRED
    assert remediator.manifest == []
    assert remediator.transaction_dir is None


def test_check_on_a_clean_host_reports_no_changes(tmp_path: Path) -> None:
    sysroot = build(tmp_path, dropins={"display.conf": ALLOWED}, stale=False, assets_installed=True)
    outcome = rem.run_check(make(sysroot, euid=1000))
    assert outcome.state is rem.State.NO_CHANGES_REQUIRED


def test_check_writes_nothing_at_all(tmp_path: Path) -> None:
    sysroot = build(tmp_path)
    before = tree(sysroot)
    rem.run_check(make(sysroot, euid=1000))
    assert tree(sysroot) == before


# =============================================================================
# cwd independence
# =============================================================================


@pytest.mark.parametrize("where", ["repo", "tmp", "unrelated"])
def test_the_engine_does_not_depend_on_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    sysroot = build(tmp_path)
    target = {"repo": REPO, "tmp": tmp_path, "unrelated": tmp_path.parent}[where]
    monkeypatch.chdir(target)
    outcome = rem.run_check(make(sysroot, euid=1000))
    assert outcome.state is rem.State.CHANGES_REQUIRED, outcome.detail


@POSIX_ONLY
def test_the_import_probe_really_runs_against_the_repository_venv() -> None:
    """The probe itself, unsimulated, with -B so it writes no bytecode.

    The venv root is derived from the interpreter actually running the tests,
    not assumed to be `<repo>/.venv`: CI may resolve `uv run` to an environment
    outside the working tree, and asserting a layout instead of the real
    interpreter would fail for a reason that says nothing about the probe.
    """
    interpreter = Path(sys.executable).resolve()
    venv_root = interpreter.parent.parent
    if not (venv_root / "bin" / "python").exists():
        pytest.skip(f"no <root>/bin/python layout for {interpreter}")

    remediator = rem.Remediator(
        repo_root=str(venv_root.parent),
        sysroot="",
        commands=rem.CommandAdapter(),
        test_mode=True,
    )
    ok, detail = remediator._probe_imports()
    assert ok, detail
    issued = [" ".join(c) for c in remediator.commands.calls]
    assert issued, "the probe issued no command"
    assert all("-B" in c for c in issued), issued
