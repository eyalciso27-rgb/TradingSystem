"""Owner-host runtime remediation, as an auditable transaction.

The previous implementation was a shell script. That was the wrong tool once
the requirements became "record and restore ownership and mode", "produce a
machine-readable plan", "inject a command adapter" and "prove the effective
systemd contract": those need structured data and a seam a test can hold, and
in shell they degrade into string handling that cannot be verified.

WHAT THIS NEVER DOES, IN ANY MODE
    * never starts, stops, restarts or relogins IB Gateway;
    * never enables the watchdog timer;
    * never runs any `--now` unit operation;
    * never touches firewall / firewalld / nftables / OCI security lists;
    * never touches SSH or VNC configuration;
    * never reads or prints an IBC config file, a credential or an account id;
    * never prints Java command-line arguments.

PHASE ORDER IS THE SAFETY PROPERTY

    COLLECT -> VALIDATE_ALL -> BUILD_PLAN -> [EXECUTE] -> POST_COLLECT -> STATE

Nothing is created, copied, moved or written until the first three complete.
A tool that validates as it goes has already changed the host by the time it
discovers it should not have started.

WHY REMEDIATION DOES NOT FINISH THE JOB

Correcting the Gateway's process ownership requires a restart, and a restart is
an owner-authorized action under docs/runbooks/CHAOS_RECOVERY_DRILLS.md. So a
successful apply returns REMEDIATED_RESTART_REQUIRED, never VERIFIED_COMPLETE:
preserving the current user-session cgroup proves only that we did not restart
anything. VERIFIED_COMPLETE is reachable only from the separate post-restart
verification, once the Gateway actually sits under
/system.slice/ibgateway-ibc.service.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
EVIDENCE_SCHEMA = "owner-host-remediation/2"

CANONICAL_GATEWAY_UNIT = "ibgateway-ibc.service"
CANONICAL_WATCHDOG_UNIT = "ibgateway-watchdog.service"
CANONICAL_WATCHDOG_TIMER = "ibgateway-watchdog.timer"
LEGACY_GATEWAY_UNIT = "ibgateway.service"

PRODUCTION_REPO_ROOT = "/home/opc/projects/TradingSystem"
STALE_WATCHDOG_PATH = "/opt/trading/scripts/ib_gateway_watchdog.py"
#: The Java the canonical Gateway runs. Matching on this absolute path rather
#: than on the string "java" is what makes "exactly one Gateway process" mean
#: something on a host that also runs other JVMs.
CANONICAL_JAVA_PATH = "/opt/ibc/jre/bin/java"
CANONICAL_SERVICE_CGROUP = f"/system.slice/{CANONICAL_GATEWAY_UNIT}"

STATE_DIR = "/var/lib/ibgateway-remediation"
DROPIN_DIR = f"/etc/systemd/system/{CANONICAL_GATEWAY_UNIT}.d"

#: source name -> absolute destination and the mode the destination must have.
CANONICAL_ASSETS: tuple[tuple[str, str, int], ...] = (
    ("ibgateway-ibc.service", f"/etc/systemd/system/{CANONICAL_GATEWAY_UNIT}", 0o644),
    ("ibgateway-watchdog.service", f"/etc/systemd/system/{CANONICAL_WATCHDOG_UNIT}", 0o644),
    ("ibgateway-watchdog.timer", f"/etc/systemd/system/{CANONICAL_WATCHDOG_TIMER}", 0o644),
    ("ibgateway-tmpfiles.conf", "/etc/tmpfiles.d/ibgateway.conf", 0o644),
    ("ibgateway-logrotate.conf", "/etc/logrotate.d/ibgateway", 0o644),
)

#: Effective Gateway properties the contract fixes exactly.
EXPECTED_GATEWAY_PROPERTIES: dict[str, str] = {
    "Type": "simple",
    "RemainAfterExit": "no",
    "Restart": "no",
    "KillMode": "control-group",
    "ProtectSystem": "strict",
    "ProtectHome": "read-only",
    "PrivateTmp": "yes",
}
EXPECTED_GATEWAY_READWRITEPATHS = "/run/ibgateway"


class State(StrEnum):
    """Final states. CHECK and EXECUTE vocabularies are deliberately separate."""

    #: CHECK only. It changed nothing, so it must not borrow an execution word.
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    NO_CHANGES_REQUIRED = "NO_CHANGES_REQUIRED"
    FAILED_PRECHECK = "FAILED_PRECHECK"
    FAILED_APPLY_ROLLED_BACK = "FAILED_APPLY_ROLLED_BACK"
    FAILED_APPLY_PARTIAL_ROLLBACK = "FAILED_APPLY_PARTIAL_ROLLBACK"
    REMEDIATED_RESTART_REQUIRED = "REMEDIATED_RESTART_REQUIRED"
    VERIFIED_COMPLETE = "VERIFIED_COMPLETE"
    FAILED_VERIFY = "FAILED_VERIFY"


SUCCESS_STATES = frozenset(
    {
        State.CHANGES_REQUIRED,
        State.NO_CHANGES_REQUIRED,
        State.REMEDIATED_RESTART_REQUIRED,
        State.VERIFIED_COMPLETE,
    }
)


# =============================================================================
# command adapter
# =============================================================================


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandAdapter:
    """Every external command goes through here, and every call is recorded.

    The trace is the evidence that no start/stop/restart/enable ever happened —
    a claim that cannot be made by reading the source alone, because the source
    is not what runs.
    """

    #: Verbs this tool must never issue, whatever the caller asks for. Enforced
    #: here rather than at call sites: a refusal that lives next to the one
    #: place commands are executed cannot be bypassed by a new call site.
    FORBIDDEN = (
        "start",
        "stop",
        "restart",
        "reload-or-restart",
        "try-restart",
        "enable",
        "disable",
        "kill",
        "isolate",
        "reboot",
        "poweroff",
    )

    def __init__(self, *, trace_path: Path | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._trace_path = trace_path

    def _guard(self, argv: tuple[str, ...]) -> None:
        if not argv or Path(argv[0]).name != "systemctl":
            return
        for word in argv[1:]:
            if word.startswith("-"):
                if word == "--now":
                    raise PermissionError("refusing a --now unit operation")
                continue
            if word in self.FORBIDDEN:
                raise PermissionError(f"refusing systemctl {word}")
            break

    def _record(self, argv: tuple[str, ...]) -> None:
        self.calls.append(argv)
        if self._trace_path is not None:
            with self._trace_path.open("a", encoding="utf-8") as handle:
                handle.write(" ".join(argv) + "\n")

    def run(self, argv: list[str]) -> CommandResult:
        frozen = tuple(argv)
        self._guard(frozen)
        self._record(frozen)
        try:
            completed = subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, check=False, timeout=60
            )
        except (OSError, subprocess.SubprocessError) as error:
            return CommandResult(frozen, 127, "", f"{type(error).__name__}")
        return CommandResult(frozen, completed.returncode, completed.stdout, completed.stderr)


class ScriptedCommandAdapter(CommandAdapter):
    """Test adapter: scripted results, no real process, full trace.

    Used so the installer integration and both daemon-reloads are genuinely
    exercised instead of being skipped because a test root is active.
    """

    def __init__(self, script: dict[str, int], *, trace_path: Path | None = None) -> None:
        super().__init__(trace_path=trace_path)
        self._script = script
        self._properties: dict[str, str] = {}
        self._units: dict[str, dict[str, str]] = {}

    def set_properties(self, properties: dict[str, str]) -> None:
        self._properties = dict(properties)

    def set_unit_properties(self, units: dict[str, dict[str, str]]) -> None:
        self._units = {unit: dict(props) for unit, props in units.items()}

    def run(self, argv: list[str]) -> CommandResult:
        frozen = tuple(argv)
        self._guard(frozen)
        self._record(frozen)
        key = " ".join(argv)
        for pattern, code in self._script.items():
            if pattern in key:
                return CommandResult(frozen, code, "", f"scripted failure: {pattern}")
        if Path(argv[0]).name != "systemctl":
            # Only systemd is simulated. The drop-in classifier and the import
            # probe REALLY run, because their behaviour is the thing under
            # test — scripting them would mean testing the script.
            try:
                completed = subprocess.run(  # noqa: S603
                    argv, capture_output=True, text=True, check=False, timeout=60
                )
            except (OSError, subprocess.SubprocessError) as error:
                return CommandResult(frozen, 127, "", type(error).__name__)
            return CommandResult(frozen, completed.returncode, completed.stdout, completed.stderr)
        if len(argv) >= 3 and argv[1] == "show":
            unit = argv[2]
            properties = self._units.get(unit, self._properties)
            lines = [f"{k}={v}" for k, v in properties.items()]
            return CommandResult(frozen, 0, "\n".join(lines), "")
        return CommandResult(frozen, 0, "", "")


# =============================================================================
# file metadata
# =============================================================================


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """Content AND ownership. Restoring one without the other is not a restore.

    The previous implementation forced every backup to 0600 and restored with
    ``cp -p``, which could hand a 0644 drop-in or a 0755 executable back to the
    host as 0600. The transaction directory being 0700 root-owned is what keeps
    a backup private; destroying the original metadata to achieve that trades a
    real property for an appearance of one.
    """

    sha256: str
    mode: int
    uid: int
    gid: int
    size: int

    @classmethod
    def of(cls, path: Path) -> FileMetadata:
        stat = path.stat()
        return cls(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            mode=stat.st_mode & 0o7777,
            uid=stat.st_uid,
            gid=stat.st_gid,
            size=stat.st_size,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "mode": f"{self.mode:04o}",
            "uid": self.uid,
            "gid": self.gid,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FileMetadata:
        return cls(
            sha256=str(payload["sha256"]),
            mode=int(str(payload["mode"]), 8),
            uid=int(payload["uid"]),
            gid=int(payload["gid"]),
            size=int(payload["size"]),
        )

    def differences(self, other: FileMetadata) -> list[str]:
        """Every field that differs, named. '"identical" needs evidence."""
        out = []
        if self.sha256 != other.sha256:
            out.append("content")
        if self.mode != other.mode:
            out.append(f"mode {other.mode:04o}!={self.mode:04o}")
        if self.uid != other.uid:
            out.append(f"uid {other.uid}!={self.uid}")
        if self.gid != other.gid:
            out.append(f"gid {other.gid}!={self.gid}")
        return out


#: Test seams for the two metadata operations that need privilege. Overridden
#: in tests to force a failure; never overridden in production.
_CHMOD = os.chmod
_CHOWN = getattr(os, "chown", None)


def restore_metadata(path: Path, metadata: FileMetadata) -> list[str]:
    """Put mode, uid and gid back. Returns the problems, empty when clean."""
    problems: list[str] = []
    try:
        _CHMOD(path, metadata.mode)
    except OSError as error:
        problems.append(f"chmod: {type(error).__name__}")
    if _CHOWN is not None:
        try:
            _CHOWN(path, metadata.uid, metadata.gid)
        except OSError as error:
            # Unprivileged restore cannot change owner; that is a real
            # limitation and is reported, never silently accepted.
            problems.append(f"chown: {type(error).__name__}")
    return problems


# =============================================================================
# observation
# =============================================================================


@dataclass(slots=True)
class GatewayIdentity:
    """PID alone is not identity: PIDs are reused."""

    pid: str = "none"
    start_ticks: str = "none"
    cgroup: str = "none"

    def as_dict(self) -> dict[str, str]:
        return {"pid": self.pid, "start_ticks": self.start_ticks, "cgroup": self.cgroup}

    def matches(self, other: GatewayIdentity) -> bool:
        return (
            self.pid == other.pid
            and self.start_ticks == other.start_ticks
            and self.cgroup == other.cgroup
        )


@dataclass(slots=True)
class Observation:
    identity: GatewayIdentity = field(default_factory=GatewayIdentity)
    java_matches: int = 0
    dropins: list[Path] = field(default_factory=list)
    unit_properties: dict[str, dict[str, str]] = field(default_factory=dict)
    boot_id: str = "unknown"


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class PlannedChange:
    """One intended mutation, with what it will overwrite."""

    kind: str  # install_asset | quarantine_dropin | quarantine_stale
    source: str
    destination: str
    mode: int | None = None
    existing: FileMetadata | None = None
    creates_new: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "destination": self.destination,
            "mode": None if self.mode is None else f"{self.mode:04o}",
            "creates_new": self.creates_new,
            "existing": None if self.existing is None else self.existing.as_dict(),
        }


@dataclass(slots=True)
class ManifestEntry:
    action: str
    path: str
    backup: str | None
    metadata: FileMetadata | None
    created: bool
    destination: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "backup": self.backup,
            "created": self.created,
            "destination": self.destination,
            "metadata": None if self.metadata is None else self.metadata.as_dict(),
        }


class Remediator:
    """COLLECT -> VALIDATE_ALL -> BUILD_PLAN -> [EXECUTE] -> POST_COLLECT."""

    def __init__(
        self,
        *,
        repo_root: str,
        sysroot: str = "",
        commands: CommandAdapter | None = None,
        test_mode: bool = False,
        test_euid: int | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.sysroot = sysroot
        self.commands = commands or CommandAdapter()
        self.test_mode = test_mode
        self._test_euid = test_euid
        self.gates: list[Gate] = []
        self.plan: list[PlannedChange] = []
        self.manifest: list[ManifestEntry] = []
        self.transaction_dir: Path | None = None
        self.transaction_id = ""
        self.reloads = 0
        self.recovery_reloads = 0
        self.notes: list[str] = []
        self._contradicting: list[Path] = []

    # -- paths ---------------------------------------------------------------

    def host(self, absolute: str) -> Path:
        return Path(f"{self.sysroot}{absolute}")

    @property
    def venv_python(self) -> Path:
        return self.host(f"{self.repo_root}/.venv/bin/python")

    @property
    def contract_cli(self) -> Path:
        return self.host(f"{self.repo_root}/scripts/systemd_contract.py")

    @property
    def state_dir(self) -> Path:
        return self.host(STATE_DIR)

    @property
    def dropin_dir(self) -> Path:
        return self.host(DROPIN_DIR)

    @property
    def euid(self) -> int:
        if self._test_euid is not None:
            return self._test_euid
        return os.geteuid() if hasattr(os, "geteuid") else 0

    # -- PHASE 1: COLLECT ----------------------------------------------------

    def collect(self) -> Observation:
        observation = Observation()
        observation.boot_id = self._boot_id()
        observation.identity, observation.java_matches = self._gateway_identity()
        try:
            observation.dropins = sorted(self.dropin_dir.glob("*.conf"))
        except OSError as error:
            self.notes.append(f"dropin listing failed: {type(error).__name__}")
            observation.dropins = []
        for unit in (
            CANONICAL_GATEWAY_UNIT,
            CANONICAL_WATCHDOG_UNIT,
            CANONICAL_WATCHDOG_TIMER,
            LEGACY_GATEWAY_UNIT,
        ):
            observation.unit_properties[unit] = self._show(unit)
        return observation

    def _boot_id(self) -> str:
        path = self.host("/proc/sys/kernel/random/boot_id")
        with contextlib.suppress(OSError):
            return path.read_text(encoding="utf-8").strip()
        return "unknown"

    def _gateway_identity(self) -> tuple[GatewayIdentity, int]:
        fixture = self.host("/gateway-identity.json")
        if self.sysroot and fixture.is_file():
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            return (
                GatewayIdentity(
                    pid=str(payload.get("pid", "none")),
                    start_ticks=str(payload.get("start_ticks", "none")),
                    cgroup=str(payload.get("cgroup", "none")),
                ),
                int(payload.get("java_matches", 0)),
            )
        found = self.commands.run(["pgrep", "-f", CANONICAL_JAVA_PATH])
        pids = [p for p in found.stdout.split() if p.isdigit()]
        if len(pids) != 1:
            return GatewayIdentity(), len(pids)
        pid = pids[0]
        identity = GatewayIdentity(pid=pid)
        with contextlib.suppress(OSError, IndexError):
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            identity.start_ticks = fields[21]
        with contextlib.suppress(OSError):
            raw = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
            for line in raw.splitlines():
                if "::" in line:
                    identity.cgroup = line.split("::", 1)[1].strip()
                    break
        return identity, 1

    def _show(self, unit: str) -> dict[str, str]:
        properties = [
            "Type",
            "RemainAfterExit",
            "Restart",
            "KillMode",
            "ProtectSystem",
            "ProtectHome",
            "PrivateTmp",
            "ReadWritePaths",
            "WorkingDirectory",
            "ExecStart",
            "UnitFileState",
            "ActiveState",
            "SubState",
            "MainPID",
        ]
        argv = ["systemctl", "show", unit]
        for name in properties:
            argv += ["-p", name]
        result = self.commands.run(argv)
        if not result.ok:
            return {}
        out: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
        return out

    # -- PHASE 2: VALIDATE_ALL ----------------------------------------------

    def validate_all(self, observation: Observation, *, mode: str) -> list[Gate]:
        gates: list[Gate] = []
        add = gates.append

        # A real apply that is not root cannot chown a restored file, so it
        # cannot honour its own rollback contract.
        if mode == "apply":
            add(Gate("euid_root", self.euid == 0, f"euid={self.euid}"))
        else:
            add(Gate("check_unprivileged", True, f"euid={self.euid}; read-only"))

        add(
            Gate(
                "repo_root_exact",
                self.repo_root == PRODUCTION_REPO_ROOT or self.test_mode,
                self.repo_root,
            )
        )
        add(Gate("venv_present", self.venv_python.is_file(), str(self.venv_python)))
        add(Gate("contract_module", self.contract_cli.is_file(), str(self.contract_cli)))

        imports_ok, imports_detail = self._probe_imports()
        add(Gate("required_imports", imports_ok, imports_detail))

        source_ok, source_detail = self._validate_unit_sources()
        add(Gate("unit_sources_exact", source_ok, source_detail))

        dropins_ok, dropins_detail = self._classify_dropins(observation)
        add(Gate("dropins_classifiable", dropins_ok, dropins_detail))

        present = [str(p) for p in self._quarantine_destinations() if p.exists()]
        add(
            Gate(
                "quarantine_destinations_absent",
                not present,
                "clear" if not present else f"already present: {present}",
            )
        )
        add(
            Gate(
                "transaction_root_creatable",
                self.state_dir.parent.is_dir() or self.state_dir.is_dir(),
                str(self.state_dir),
            )
        )
        add(
            Gate(
                "exactly_one_gateway_java",
                observation.java_matches == 1,
                f"{observation.java_matches} matching {CANONICAL_JAVA_PATH}",
            )
        )
        identity = observation.identity
        add(
            Gate(
                "gateway_identity_readable",
                "none" not in (identity.pid, identity.start_ticks, identity.cgroup),
                f"pid={identity.pid} ticks={identity.start_ticks}",
            )
        )

        timer = observation.unit_properties.get(CANONICAL_WATCHDOG_TIMER, {})
        add(
            Gate(
                "timer_disabled_and_inactive",
                timer.get("UnitFileState") == "disabled" and timer.get("ActiveState") == "inactive",
                f"UnitFileState={timer.get('UnitFileState', '')} "
                f"ActiveState={timer.get('ActiveState', '')}",
            )
        )
        watchdog = observation.unit_properties.get(CANONICAL_WATCHDOG_UNIT, {})
        add(
            Gate(
                "watchdog_service_inactive",
                watchdog.get("ActiveState") == "inactive",
                f"ActiveState={watchdog.get('ActiveState', '')}",
            )
        )
        legacy = observation.unit_properties.get(LEGACY_GATEWAY_UNIT, {})
        add(
            Gate(
                "legacy_unit_disabled_and_inactive",
                legacy.get("UnitFileState") == "disabled"
                and legacy.get("ActiveState") == "inactive",
                f"UnitFileState={legacy.get('UnitFileState', '')} "
                f"ActiveState={legacy.get('ActiveState', '')}",
            )
        )
        add(
            Gate(
                "systemctl_available",
                bool(observation.unit_properties.get(CANONICAL_GATEWAY_UNIT)),
                "systemctl show answered",
            )
        )
        competing = "" if legacy.get("ActiveState") != "active" else f"{LEGACY_GATEWAY_UNIT} active"
        add(Gate("no_competing_manager", not competing, competing or "none"))

        self.gates = gates
        return gates

    def _probe_imports(self) -> tuple[bool, str]:
        if not self.venv_python.is_file():
            return False, "interpreter missing"
        for module in ("trading_broker_ibkr", "trading_execution_agent.account_safety"):
            result = self.commands.run([str(self.venv_python), "-B", "-c", f"import {module}"])
            if not result.ok:
                return False, f"cannot import {module}"
        return True, "imports satisfied with -B (no bytecode written)"

    def _validate_unit_sources(self) -> tuple[bool, str]:
        source = self.host(f"{self.repo_root}/systemd/ibgateway-watchdog.service")
        if not source.is_file():
            return False, f"missing {source}"
        text = source.read_text(encoding="utf-8")
        if f"\nWorkingDirectory={PRODUCTION_REPO_ROOT}\n" not in text:
            return False, "watchdog unit WorkingDirectory is not exact"
        if f"{PRODUCTION_REPO_ROOT}/.venv/bin/python" not in text:
            return False, "watchdog unit does not use the pinned interpreter"
        if f"{PRODUCTION_REPO_ROOT}/scripts/ib_gateway_watchdog.py" not in text:
            return False, "watchdog unit does not execute the canonical script"
        body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        if "/usr/bin/env" in body:
            return False, "watchdog unit resolves its interpreter through PATH"
        if STALE_WATCHDOG_PATH in body:
            return False, "watchdog unit references the stale /opt copy"
        return True, "WorkingDirectory, interpreter and script are exact"

    def _classify_dropins(self, observation: Observation) -> tuple[bool, str]:
        self._contradicting = []
        if not self.venv_python.is_file() or not self.contract_cli.is_file():
            return False, "classification runtime unavailable"
        for dropin in observation.dropins:
            try:
                dropin.read_bytes()
            except OSError:
                return False, f"unreadable drop-in: {dropin.name}"
            result = self.commands.run(
                [
                    str(self.venv_python),
                    "-B",
                    str(self.contract_cli),
                    "classify",
                    str(dropin),
                ]
            )
            if result.returncode == 1:
                self._contradicting.append(dropin)
            elif result.returncode != 0:
                # "Cannot classify" is never "harmless".
                return False, f"cannot classify {dropin.name}"
        return True, f"{len(self._contradicting)} contradicting of {len(observation.dropins)}"

    def _quarantine_destinations(self) -> list[Path]:
        out = [Path(f"{p}.quarantined") for p in self._contradicting]
        stale = self.host(STALE_WATCHDOG_PATH)
        if stale.exists():
            out.append(Path(f"{stale}.quarantined"))
        return out

    @property
    def failed_gates(self) -> list[Gate]:
        return [g for g in self.gates if not g.ok]

    # -- PHASE 3: BUILD_PLAN -------------------------------------------------

    def build_plan(self) -> list[PlannedChange]:
        """Every intended mutation, including what it will overwrite.

        The installer is NOT delegated to as an opaque mutator: each asset it
        would place appears here, so every destination is backed up with its
        metadata and every newly created file is recorded for removal.
        """
        plan: list[PlannedChange] = []
        for dropin in self._contradicting:
            plan.append(
                PlannedChange(
                    kind="quarantine_dropin",
                    source=str(dropin),
                    destination=f"{dropin}.quarantined",
                    existing=FileMetadata.of(dropin),
                )
            )
        stale = self.host(STALE_WATCHDOG_PATH)
        if stale.exists():
            plan.append(
                PlannedChange(
                    kind="quarantine_stale",
                    source=str(stale),
                    destination=f"{stale}.quarantined",
                    existing=FileMetadata.of(stale),
                )
            )
        for name, destination, mode in CANONICAL_ASSETS:
            source = self.host(f"{self.repo_root}/systemd/{name}")
            target = self.host(destination)
            if not source.is_file():
                continue
            existing = FileMetadata.of(target) if target.is_file() else None
            if existing is not None and existing.sha256 == FileMetadata.of(source).sha256:
                continue  # already correct: not a change
            plan.append(
                PlannedChange(
                    kind="install_asset",
                    source=str(source),
                    destination=str(target),
                    mode=mode,
                    existing=existing,
                    creates_new=existing is None,
                )
            )
        self.plan = plan
        return plan

    @property
    def requires_reload(self) -> bool:
        """A quarantined drop-in changes the EFFECTIVE unit even when every
        unit FILE is byte-identical, so the reload cannot be keyed on files."""
        return bool(self.plan)

    # -- PHASE 4: EXECUTE ----------------------------------------------------

    def begin_transaction(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            _CHMOD(self.state_dir, 0o700)
        directory = Path(tempfile.mkdtemp(prefix="txn-", dir=str(self.state_dir)))
        with contextlib.suppress(OSError):
            _CHMOD(directory, 0o700)
        self.transaction_dir = directory
        self.transaction_id = directory.name
        return directory

    def _backup(self, path: Path) -> tuple[Path, FileMetadata]:
        """Copy content AND record metadata.

        The backup keeps its ORIGINAL mode. Privacy comes from the 0700
        transaction directory; forcing every backup to 0600 would destroy the
        very metadata rollback has to restore.
        """
        assert self.transaction_dir is not None
        metadata = FileMetadata.of(path)
        backup = self.transaction_dir / f"{len(self.manifest):03d}-{path.name}.bak"
        shutil.copy2(path, backup)
        if FileMetadata.of(backup).sha256 != metadata.sha256:
            raise OSError(f"backup hash mismatch for {path}")
        return backup, metadata

    def execute(self) -> tuple[bool, str]:
        """Apply the plan. Returns (ok, detail); rollback is the caller's."""
        for change in self.plan:
            source = Path(change.source)
            destination = Path(change.destination)
            if change.kind in ("quarantine_dropin", "quarantine_stale"):
                backup, metadata = self._backup(source)
                self.manifest.append(
                    ManifestEntry(
                        "move", str(source), str(backup), metadata, False, str(destination)
                    )
                )
                _FAULT("after_backup", str(source))
                source.rename(destination)
                _FAULT("after_move", str(source))
            else:
                if change.existing is not None:
                    backup, metadata = self._backup(destination)
                    self.manifest.append(
                        ManifestEntry("replace", str(destination), str(backup), metadata, False)
                    )
                else:
                    self.manifest.append(
                        ManifestEntry("create", str(destination), None, None, True)
                    )
                _FAULT("after_manifest", str(destination))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                if change.mode is not None:
                    with contextlib.suppress(OSError):
                        _CHMOD(destination, change.mode)
                _FAULT("after_install", str(destination))
        return True, f"{len(self.manifest)} change(s) applied"

    def daemon_reload(self, *, recovery: bool = False) -> bool:
        result = self.commands.run(["systemctl", "daemon-reload"])
        if result.ok:
            if recovery:
                self.recovery_reloads += 1
            else:
                self.reloads += 1
        return result.ok

    def rollback(self, *, reloaded: bool) -> tuple[bool, list[str]]:
        """Undo everything, metadata included, newest first.

        Returns (fully_restored, problems). A failure to remove a quarantine
        destination is a rollback FAILURE, not a warning: leaving it behind
        means the host is not what we found.
        """
        problems: list[str] = []
        for entry in reversed(self.manifest):
            if entry.created:
                try:
                    Path(entry.path).unlink()
                except OSError as error:
                    problems.append(f"cannot remove created {entry.path}: {type(error).__name__}")
                continue
            if entry.backup is None or entry.metadata is None:
                problems.append(f"no backup recorded for {entry.path}")
                continue
            target = Path(entry.path)
            try:
                shutil.copyfile(entry.backup, target)
            except OSError as error:
                problems.append(f"cannot restore {entry.path}: {type(error).__name__}")
                continue
            problems.extend(f"{entry.path}: {p}" for p in restore_metadata(target, entry.metadata))
            actual = FileMetadata.of(target)
            differences = entry.metadata.differences(actual)
            if differences:
                problems.append(f"{entry.path} not restored: {', '.join(differences)}")
            if entry.destination:
                destination = Path(entry.destination)
                if destination.exists():
                    try:
                        destination.unlink()
                    except OSError as error:
                        problems.append(
                            f"cannot remove quarantine destination {destination}:"
                            f" {type(error).__name__}"
                        )
        if reloaded:
            # The forward reload already told systemd about the changed files.
            # Putting the files back without telling it again would leave the
            # effective contract describing state that no longer exists.
            if not self.daemon_reload(recovery=True):
                problems.append("recovery daemon-reload failed")
        return (not problems), problems

    # -- PHASE 5: VERIFY -----------------------------------------------------

    def verify_contract(self, observation: Observation) -> list[Gate]:
        """The EFFECTIVE contract. Blank/unknown/static/failed are failures."""
        gates: list[Gate] = []
        gateway = observation.unit_properties.get(CANONICAL_GATEWAY_UNIT, {})
        for key, expected in EXPECTED_GATEWAY_PROPERTIES.items():
            actual = gateway.get(key, "")
            gates.append(
                Gate(
                    f"gateway:{key}", actual == expected, f"{actual or '(blank)'} (want {expected})"
                )
            )
        rwp = gateway.get("ReadWritePaths", "")
        gates.append(
            Gate(
                "gateway:ReadWritePaths",
                EXPECTED_GATEWAY_READWRITEPATHS in rwp,
                rwp or "(blank)",
            )
        )
        gates.append(
            Gate(
                "gateway:no_forbidden_dropin",
                not self._contradicting,
                f"{len(self._contradicting)} contradicting drop-in(s)",
            )
        )

        watchdog = observation.unit_properties.get(CANONICAL_WATCHDOG_UNIT, {})
        working = watchdog.get("WorkingDirectory", "")
        gates.append(
            Gate("watchdog:WorkingDirectory", working == PRODUCTION_REPO_ROOT, working or "(blank)")
        )
        exec_start = watchdog.get("ExecStart", "")
        gates.append(
            Gate(
                "watchdog:interpreter",
                f"{PRODUCTION_REPO_ROOT}/.venv/bin/python" in exec_start,
                "absolute venv interpreter"
                if f"{PRODUCTION_REPO_ROOT}/.venv/bin/python" in exec_start
                else exec_start or "(blank)",
            )
        )
        gates.append(
            Gate(
                "watchdog:script",
                f"{PRODUCTION_REPO_ROOT}/scripts/ib_gateway_watchdog.py" in exec_start,
                "canonical repository script"
                if f"{PRODUCTION_REPO_ROOT}/scripts/ib_gateway_watchdog.py" in exec_start
                else exec_start or "(blank)",
            )
        )
        gates.append(
            Gate("watchdog:no_env_interpreter", "/usr/bin/env" not in exec_start, "absolute")
        )
        gates.append(
            Gate(
                "watchdog:no_stale_opt", STALE_WATCHDOG_PATH not in exec_start, "no /opt reference"
            )
        )

        timer = observation.unit_properties.get(CANONICAL_WATCHDOG_TIMER, {})
        gates.append(
            Gate(
                "timer:UnitFileState",
                timer.get("UnitFileState") == "disabled",
                timer.get("UnitFileState") or "(blank)",
            )
        )
        gates.append(
            Gate(
                "timer:ActiveState",
                timer.get("ActiveState") == "inactive",
                timer.get("ActiveState") or "(blank)",
            )
        )
        gates.append(
            Gate(
                "watchdog_service:ActiveState",
                watchdog.get("ActiveState") == "inactive",
                watchdog.get("ActiveState") or "(blank)",
            )
        )
        return gates

    def gateway_is_canonically_owned(self, observation: Observation) -> bool:
        gateway = observation.unit_properties.get(CANONICAL_GATEWAY_UNIT, {})
        main_pid = gateway.get("MainPID", "0").strip() or "0"
        return main_pid != "0" and CANONICAL_SERVICE_CGROUP in observation.identity.cgroup

    # -- evidence ------------------------------------------------------------

    def write_evidence(
        self,
        path: Path,
        *,
        phase: str,
        state: State,
        observation: Observation,
        baseline: dict[str, Any] | None = None,
    ) -> Path:
        payload: dict[str, Any] = {
            "schema": EVIDENCE_SCHEMA,
            "transaction_id": self.transaction_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "boot_id": observation.boot_id,
            "captured_phase": phase,
            "final_state": str(state),
            "gateway": observation.identity.as_dict(),
            "unit_states": {
                unit: {
                    "UnitFileState": props.get("UnitFileState", ""),
                    "ActiveState": props.get("ActiveState", ""),
                    "SubState": props.get("SubState", ""),
                }
                for unit, props in observation.unit_properties.items()
            },
            "planned": [c.as_dict() for c in self.plan],
            "installed": [e.as_dict() for e in self.manifest if e.action in ("create", "replace")],
            "quarantined": [e.as_dict() for e in self.manifest if e.action == "move"],
            "daemon_reloads": self.reloads,
            "recovery_reloads": self.recovery_reloads,
        }
        if baseline is not None:
            payload["baseline"] = baseline
        _write_private_json(path, payload)
        return path


#: Fault-injection seam. Production leaves it inert; tests replace it to force
#: a failure at a named point so the rollback matrix can be exercised.
def _no_fault(point: str, detail: str) -> None:  # noqa: ARG001
    return None


#: Reassigned by install_fault(); typed loosely because that is the point.
_FAULT: Callable[[str, str], None] = _no_fault


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic, 0600. A half-written evidence file is worse than none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    temporary_path = Path(temporary)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _CHMOD(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary_path.unlink()
        raise


REQUIRED_EVIDENCE_KEYS = (
    "schema",
    "transaction_id",
    "timestamp",
    "boot_id",
    "captured_phase",
    "gateway",
    "unit_states",
    "planned",
    "installed",
    "quarantined",
)


def validate_evidence(
    payload: dict[str, Any], *, transaction_id: str | None = None, boot_id: str | None = None
) -> list[str]:
    """Every way an evidence file can be untrustworthy, named."""
    problems: list[str] = []
    if payload.get("schema") != EVIDENCE_SCHEMA:
        problems.append(f"schema is {payload.get('schema')!r}, expected {EVIDENCE_SCHEMA!r}")
    for key in REQUIRED_EVIDENCE_KEYS:
        if key not in payload:
            problems.append(f"missing key: {key}")
    gateway = payload.get("gateway", {})
    if isinstance(gateway, dict):
        for key in ("pid", "start_ticks", "cgroup"):
            if key not in gateway:
                problems.append(f"gateway.{key} missing")
        ticks = str(gateway.get("start_ticks", ""))
        if ticks and ticks != "none" and not ticks.isdigit():
            problems.append("gateway.start_ticks is not numeric")
    timestamp = str(payload.get("timestamp", ""))
    if timestamp and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp):
        problems.append("timestamp is not an ISO-8601 UTC instant")
    if transaction_id is not None and payload.get("transaction_id") != transaction_id:
        problems.append("transaction_id does not match this transaction")
    if boot_id is not None and payload.get("boot_id") != boot_id:
        problems.append("boot_id does not match this boot: the evidence is stale")
    return problems


# =============================================================================
# CLI
# =============================================================================


@dataclass(slots=True)
class Outcome:
    state: State
    gates: list[Gate] = field(default_factory=list)
    detail: str = ""
    mutations: int = 0
    reloads: int = 0
    recovery_reloads: int = 0


def _report(outcome: Outcome, stream: Any) -> None:
    for gate in outcome.gates:
        mark = "OK  " if gate.ok else "FAIL"
        print(f"  {mark} {gate.name:<38} {gate.detail}", file=stream)
    if outcome.detail:
        print(f">>> {outcome.detail}", file=stream)
    print(f">>> FINAL STATE: {outcome.state}", file=stream)
    print(
        f">>> mutations: {outcome.mutations}"
        f"; daemon-reloads: {outcome.reloads}"
        f"; recovery-reloads: {outcome.recovery_reloads}",
        file=stream,
    )


def run_check(remediator: Remediator) -> Outcome:
    observation = remediator.collect()
    gates = remediator.validate_all(observation, mode="check")
    if remediator.failed_gates:
        return Outcome(State.FAILED_PRECHECK, gates, "preflight failed; nothing was changed")
    plan = remediator.build_plan()
    if not plan:
        return Outcome(State.NO_CHANGES_REQUIRED, gates, "the contract is already in place")
    # CHECK changed nothing, so it must not borrow an execution word.
    return Outcome(State.CHANGES_REQUIRED, gates, f"{len(plan)} change(s) would be applied")


def run_apply(remediator: Remediator, *, evidence_dir: Path | None) -> Outcome:
    observation = remediator.collect()
    gates = remediator.validate_all(observation, mode="apply")
    if remediator.failed_gates:
        # ZERO mutation: no baseline, no transaction, no backup, no write.
        return Outcome(State.FAILED_PRECHECK, gates, "preflight failed; nothing was changed")

    plan = remediator.build_plan()
    if not plan:
        return Outcome(State.NO_CHANGES_REQUIRED, gates, "the contract is already in place")

    baseline = {
        "schema": SCHEMA_VERSION,
        "captured_phase": "pre_apply",
        "boot_id": observation.boot_id,
        **observation.identity.as_dict(),
    }
    remediator.begin_transaction()
    assert remediator.transaction_dir is not None
    _write_private_json(remediator.state_dir / "gateway-baseline.json", baseline)

    reloaded = False
    try:
        _FAULT("after_baseline", str(remediator.state_dir))
        remediator.execute()
        _FAULT("before_reload", "")
        if not remediator.daemon_reload():
            raise OSError("daemon-reload failed")
        reloaded = True
        _FAULT("after_reload", "")
        post = remediator.collect()
        _FAULT("after_post_collect", "")
        # Re-classify from the FRESH collection: the pre-apply verdict named
        # drop-ins that have since been quarantined, and verifying against a
        # stale list would report a contract that no longer describes the host.
        remediator._classify_dropins(post)
        contract = remediator.verify_contract(post)
        if any(not g.ok for g in contract):
            raise OSError("effective properties do not match the contract")
    except Exception as error:  # noqa: BLE001 - every failure rolls back
        restored, problems = remediator.rollback(reloaded=reloaded)
        state = State.FAILED_APPLY_ROLLED_BACK if restored else State.FAILED_APPLY_PARTIAL_ROLLBACK
        detail = f"{type(error).__name__}: {error}"
        if problems:
            detail += " | " + "; ".join(problems[:4])
        outcome = Outcome(
            state,
            gates,
            detail,
            len(remediator.manifest),
            remediator.reloads,
            remediator.recovery_reloads,
        )
        if evidence_dir is not None:
            remediator.write_evidence(
                evidence_dir / "remediation-apply.json",
                phase="post_apply_failed",
                state=state,
                observation=observation,
                baseline=baseline,
            )
        return outcome

    if evidence_dir is not None:
        remediator.write_evidence(
            evidence_dir / "remediation-apply.json",
            phase="post_apply",
            state=State.REMEDIATED_RESTART_REQUIRED,
            observation=post,
            baseline=baseline,
        )
    return Outcome(
        State.REMEDIATED_RESTART_REQUIRED,
        gates + contract,
        "the contract is in place; the RUNNING Gateway still belongs to its old"
        " session, so an owner-authorized restart is still required",
        len(remediator.manifest),
        remediator.reloads,
        remediator.recovery_reloads,
    )


def run_verify(remediator: Remediator, *, post_restart: bool) -> Outcome:
    observation = remediator.collect()
    remediator.validate_all(observation, mode="verify")
    remediator._classify_dropins(observation)
    contract = remediator.verify_contract(observation)

    baseline_path = remediator.state_dir / "gateway-baseline.json"
    identity_gates: list[Gate] = []
    if post_restart:
        # After the owner-authorized restart the PID MUST differ, so requiring
        # the pre-restart identity here would fail the very outcome we want.
        owned = remediator.gateway_is_canonically_owned(observation)
        identity_gates.append(
            Gate(
                "gateway_canonically_owned",
                owned,
                observation.identity.cgroup or "(none)",
            )
        )
    else:
        if not baseline_path.is_file():
            return Outcome(
                State.FAILED_VERIFY,
                [Gate("baseline_present", False, f"missing {baseline_path}")],
                "VERIFY requires a baseline written by --apply; it never creates its own",
            )
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        same = (
            baseline.get("pid") == observation.identity.pid
            and baseline.get("start_ticks") == observation.identity.start_ticks
            and baseline.get("cgroup") == observation.identity.cgroup
        )
        identity_gates.append(Gate("gateway_untouched", same, observation.identity.pid))
        identity_gates.append(
            Gate(
                "boot_unchanged",
                baseline.get("boot_id") == observation.boot_id,
                observation.boot_id,
            )
        )

    gates = contract + identity_gates
    if any(not g.ok for g in gates):
        return Outcome(State.FAILED_VERIFY, gates, "the effective contract is not satisfied")

    if post_restart:
        return Outcome(State.VERIFIED_COMPLETE, gates, "the Gateway is owned by the canonical unit")
    # Preserving the user-session cgroup proves only that we did not restart.
    return Outcome(
        State.REMEDIATED_RESTART_REQUIRED,
        gates,
        "contract satisfied, but the Gateway still belongs to its pre-remediation"
        " session; VERIFIED_COMPLETE requires the owner-authorized restart",
    )


def _build_adapter(fixture_path: str | None, trace: Path | None) -> CommandAdapter:
    """Real adapter in production; scripted adapter when a fixture is supplied.

    The scripted adapter is what lets a test drive the REAL execute/rollback
    path — including both daemon-reloads — instead of the code skipping them
    because a test root happens to be active. Skipping them would leave the
    most dangerous branch untested.
    """
    if not fixture_path:
        return CommandAdapter(trace_path=trace)
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    scripted = ScriptedCommandAdapter(
        {str(k): int(v) for k, v in fixture.get("fail", {}).items()}, trace_path=trace
    )
    scripted.set_unit_properties(
        {unit: dict(props) for unit, props in fixture.get("units", {}).items()}
    )
    point = fixture.get("fault_point")
    if point:
        install_fault(str(point))
    return scripted


def install_fault(point: str) -> None:
    """Arm the documented fault-injection seam at a named execution point."""
    global _FAULT  # noqa: PLW0603 - a single, documented test seam

    def _injected(name: str, detail: str) -> None:
        if name == point:
            raise OSError(f"injected failure at {name}: {detail}")

    _FAULT = _injected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--verify-post-restart",
        action="store_true",
        help="verify AFTER the owner-authorized restart: proves canonical"
        " ownership without demanding the old pre-restart PID",
    )
    parser.add_argument("--evidence-dir", default=None)
    parser.add_argument("--repo-root", default=None, help="TEST ONLY")
    parser.add_argument("--sysroot", default=None, help="TEST ONLY")
    parser.add_argument("--test-euid", type=int, default=None, help="TEST ONLY")
    parser.add_argument("--command-trace", default=None, help="TEST ONLY")
    parser.add_argument(
        "--command-fixture",
        default=None,
        help="TEST ONLY: JSON of unit properties, scripted command failures and"
        " a fault point, so the installer path and BOTH daemon-reloads are"
        " genuinely exercised instead of skipped because a test root is active",
    )
    args = parser.parse_args(argv)

    test_mode = os.environ.get("OPS_REMEDIATE_TEST_MODE") == "1"
    for flag, value in (
        ("--repo-root", args.repo_root),
        ("--sysroot", args.sysroot),
        ("--test-euid", args.test_euid),
        ("--command-trace", args.command_trace),
        ("--command-fixture", args.command_fixture),
    ):
        if value is not None and not test_mode:
            print(f"{flag} is a test-only interface", file=sys.stderr)
            return 2

    mode = "check"
    if args.apply:
        mode = "apply"
    elif args.verify_post_restart:
        mode = "verify_post_restart"
    elif args.verify:
        mode = "verify"

    trace = Path(args.command_trace) if args.command_trace else None
    adapter = _build_adapter(args.command_fixture, trace)
    remediator = Remediator(
        repo_root=args.repo_root or PRODUCTION_REPO_ROOT,
        sysroot=args.sysroot or "",
        commands=adapter,
        test_mode=test_mode,
        test_euid=args.test_euid,
    )
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else None

    print(f">>> owner-host runtime remediation — mode: {mode.upper()}")
    print(">>> the running Gateway is NEVER started, stopped, restarted or relogged in.")
    if mode == "apply":
        outcome = run_apply(remediator, evidence_dir=evidence_dir)
    elif mode.startswith("verify"):
        outcome = run_verify(remediator, post_restart=mode.endswith("post_restart"))
    else:
        outcome = run_check(remediator)
    outcome.mutations = outcome.mutations or len(remediator.manifest)
    outcome.reloads = remediator.reloads
    outcome.recovery_reloads = remediator.recovery_reloads
    _report(outcome, sys.stdout)
    return 0 if outcome.state in SUCCESS_STATES else 1


if __name__ == "__main__":
    raise SystemExit(main())
