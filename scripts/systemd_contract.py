"""The canonical systemd contract for the Gateway and watchdog units.

**Why this exists.** On the owner host the installed base unit matched the
repository byte-for-byte — and the *effective* unit did not. Three local
drop-ins silently rewrote the contract::

    zz-forking.conf : Type=oneshot  RemainAfterExit=yes  KillMode=process
    zz-bringup.conf : PrivateTmp=false ProtectSystem=false ProtectHome=false
                      ReadWritePaths=
    display.conf    : DISPLAY / XAUTHORITY

The consequence was not cosmetic. ``Type=oneshot`` + ``RemainAfterExit=yes``
makes systemd report the service ``active (exited)`` with ``MainPID=0`` while
the real Java process lives in a *user* session scope
(``/user.slice/.../session-2.scope``), launched systemd -> xterm -> bash ->
java. So systemd claimed the Gateway was fine while owning nothing, and every
hardening directive the unit declared had been switched off.

Comparing installed FILES therefore proves nothing. Only the EFFECTIVE
properties and the drop-in set can. This module is the single source of truth
for that comparison, shared by the host audit and the remediation tool.

Nothing here executes a privileged action; callers pass in already-collected
text, so the whole contract is testable without systemd.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

CANONICAL_GATEWAY_UNIT = "ibgateway-ibc.service"
CANONICAL_WATCHDOG_UNIT = "ibgateway-watchdog.service"
CANONICAL_WATCHDOG_TIMER = "ibgateway-watchdog.timer"
LEGACY_GATEWAY_UNIT = "ibgateway.service"

#: The reviewed repository, its pinned interpreter and the canonical script.
#: Absolute on purpose: a PATH-resolved interpreter means the timer runs
#: whatever python happens to be first today.
HOME_ROOT = "/home/opc"
REPO_ROOT = f"{HOME_ROOT}/projects/TradingSystem"
VENV_PYTHON = f"{REPO_ROOT}/.venv/bin/python"
WATCHDOG_SCRIPT = f"{REPO_ROOT}/scripts/ib_gateway_watchdog.py"

#: The stale copy the unit used to execute. It had drifted from the reviewed
#: source, and the interpreter that ran it could not import the broker package.
STALE_WATCHDOG_PATH = "/opt/trading/scripts/ib_gateway_watchdog.py"

#: Imports the deep probe genuinely needs. Checked with the venv interpreter.
REQUIRED_IMPORTS = ("trading_broker_ibkr", "trading_execution_agent.account_safety")

#: Properties whose override changes the SAFETY or OWNERSHIP contract. An
#: override of any of these is a finding, whatever file it came from.
SAFETY_CRITICAL_PROPERTIES: frozenset[str] = frozenset(
    {
        "Type",
        "RemainAfterExit",
        "KillMode",
        "Restart",
        "RestartSec",
        "RestartPreventExitStatus",
        "StartLimitBurst",
        "StartLimitIntervalSec",
        "ExecStart",
        "User",
        "Group",
        "WorkingDirectory",
        "PrivateTmp",
        "ProtectSystem",
        "ProtectHome",
        "ReadWritePaths",
        "RuntimeDirectory",
    }
)

#: The ONLY settings a host-specific drop-in may carry. Both are non-secret
#: X11 addressing that genuinely varies per host; neither changes process
#: ownership, lifetime or confinement. Everything else must be refused —
#: including other settings inside an otherwise-allowed file.
ALLOWED_DROPIN_SETTINGS: frozenset[str] = frozenset({"DISPLAY", "XAUTHORITY"})

#: What the canonical Gateway unit must effectively be.
#:
#: ``Restart=on-failure`` with ``StartLimitBurst=3`` is BOUNDED, FAIL-CLOSED
#: recovery — not blind restart. ``Restart=always`` (unbounded) is a contract
#: violation, as is ``Restart=no`` (no crash recovery at all). The fail-closed
#: sentinel that must never be restarted is checked separately by
#: :func:`check_restart_prevent_exit_status`.
CANONICAL_GATEWAY_CONTRACT: dict[str, str] = {
    "Type": "simple",
    "Restart": "on-failure",
    "RemainAfterExit": "no",
    "StartLimitBurst": "3",
}

#: The launcher's fail-closed exit code. It MUST appear in the unit's effective
#: ``RestartPreventExitStatus`` so a known fail-closed condition (invalid
#: config, non-paper, missing Java/files, 2FA timeout, auth failure) can never
#: drive an automatic restart loop.
FAIL_CLOSED_EXIT_CODE = "78"

#: The exact bounded-recovery timings the effective unit must carry. Checked
#: tolerantly (systemd renders these as ``RestartUSec`` / ``StartLimitIntervalUSec``
#: in microseconds, or as ``30s`` / ``15min``).
EXPECTED_RESTART_SECONDS = 30.0
EXPECTED_START_LIMIT_INTERVAL_SECONDS = 900.0

_DURATION_UNITS = {
    "us": 1e-6, "usec": 1e-6,
    "ms": 1e-3, "msec": 1e-3,
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
}

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def duration_seconds(value: str) -> float | None:
    """Parse a systemd duration to seconds, or None if unparseable.

    Accepts the two forms ``systemctl show`` can emit: a bare integer of
    MICROSECONDS (the ``*USec`` convention, e.g. ``30000000``) and a composite
    human form (``30s``, ``15min``, ``1min30s``).
    """
    text = value.strip().lower()
    if not text:
        return None
    if text.isdigit():  # bare integer => microseconds
        return int(text) / 1_000_000
    total = 0.0
    matched = False
    for number, unit in _DURATION_TOKEN.findall(text):
        factor = _DURATION_UNITS.get(unit)
        if factor is None:
            return None
        total += float(number) * factor
        matched = True
    return total if matched else None


def check_bounded_recovery(properties: dict[str, str]) -> list[Finding]:
    """Verify the WHOLE bound: RestartSec≈30s and StartLimitInterval≈900s.

    ``Restart=on-failure`` and ``StartLimitBurst=3`` are checked exactly by the
    main contract; this covers the two duration values (rendered as ``*USec``),
    so a drop-in cannot silently stretch the settle delay or the burst window.
    """
    findings: list[Finding] = []
    for key, want in (
        ("RestartUSec", EXPECTED_RESTART_SECONDS),
        ("StartLimitIntervalUSec", EXPECTED_START_LIMIT_INTERVAL_SECONDS),
    ):
        if key not in properties:
            findings.append(
                Finding(check=f"effective:{key}", severity=Severity.UNKNOWN,
                        detail=f"{key} not reported")
            )
            continue
        raw = properties.get(key, "")
        seconds = duration_seconds(raw)
        if seconds is None:
            findings.append(
                Finding(check=f"effective:{key}", severity=Severity.FAIL,
                        detail=f"unparseable {key}={raw or '(blank)'}")
            )
        elif abs(seconds - want) > 0.5:
            findings.append(
                Finding(
                    check=f"effective:{key}",
                    severity=Severity.FAIL,
                    detail=f"{key}={raw} ({seconds:g}s), want {want:g}s",
                    remedy=f"set the unit's {key[:-4]}Sec to {want:g} seconds",
                )
            )
        else:
            findings.append(
                Finding(check=f"effective:{key}", severity=Severity.OK, detail=raw)
            )
    return findings

#: The IB Gateway installation and settings tree. IBC's ``ibcstart.sh`` uses
#: ``~/Jts`` as the TWS/Gateway path and writes there during startup, and the
#: Java the service executes lives under it.
GATEWAY_INSTALL_ROOT = f"{HOME_ROOT}/Jts"

#: Absolute paths the canonical Gateway service MUST be able to write.
#:
#: ``ProtectHome=read-only`` and ``ProtectSystem=strict`` are not negotiable,
#: so every writable tree has to be named here explicitly. Omitting one does
#: not produce a permission error in the journal: the launch fails and the
#: unit reports ``inactive (dead)`` / ``Deactivated successfully.`` — which
#: reads like a clean shutdown. That is exactly how the missing
#: ``/home/opc/Jts`` entry presented on the owner host.
CANONICAL_GATEWAY_WRITABLE_PATHS: tuple[str, ...] = (
    GATEWAY_INSTALL_ROOT,
    f"{HOME_ROOT}/ibc",
    f"{HOME_ROOT}/.config/ibc",
    "/run/ibgateway",
)


class Severity(StrEnum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class Finding:
    """One contract observation. ``detail`` is always sanitized."""

    check: str
    severity: Severity
    detail: str
    remedy: str = ""


@dataclass(frozen=True, slots=True)
class DropIn:
    """One drop-in file and the settings it assigns."""

    path: str
    settings: dict[str, list[str]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def parse_dropin(path: str, text: str) -> DropIn:
    """Parse a drop-in into ``Key -> [values]``, keeping duplicates."""
    settings: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        settings.setdefault(key.strip(), []).append(value.strip())
    return DropIn(path=path, settings=settings)


class DropInParseError(Exception):
    """A drop-in could not be parsed, so it cannot be judged."""


def parse_dropin_strict(path: str, text: str) -> DropIn:
    """Parse, refusing anything that is not recognisable unit syntax.

    The lenient parser skips lines it does not understand, which means a
    corrupt or unexpected drop-in classifies as "no overrides" — i.e. as
    harmless. For a tool that is about to move files on a host, "I could not
    read this" must never be reported as "this is fine".
    """
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" not in line:
            raise DropInParseError(f"{path}:{number}: not a key=value assignment")
        key, _, _ = line.partition("=")
        if not key.strip() or any(c in key for c in "\x00\r"):
            raise DropInParseError(f"{path}:{number}: unusable key")
    return parse_dropin(path, text)


def _environment_names(values: list[str]) -> set[str]:
    """The variable NAMES a set of Environment= assignments defines."""
    names: set[str] = set()
    for value in values:
        for assignment in value.split():
            name, _, _ = assignment.partition("=")
            if name:
                names.add(name.strip('"'))
    return names


def classify_dropin(dropin: DropIn) -> list[Finding]:
    """Judge one drop-in against the contract.

    Filename-independent on purpose: the three observed names are examples,
    not the rule. A drop-in called anything at all is judged by what it
    ASSIGNS.
    """
    findings: list[Finding] = []
    for key, values in sorted(dropin.settings.items()):
        if key == "Environment":
            names = _environment_names(values)
            disallowed = sorted(names - ALLOWED_DROPIN_SETTINGS)
            if disallowed:
                findings.append(
                    Finding(
                        check=f"dropin:{dropin.name}:Environment",
                        severity=Severity.FAIL,
                        detail=(
                            "drop-in sets environment variables outside the allowed"
                            f" set: {', '.join(disallowed)}"
                        ),
                        remedy="remove the disallowed assignments",
                    )
                )
            else:
                findings.append(
                    Finding(
                        check=f"dropin:{dropin.name}:Environment",
                        severity=Severity.OK,
                        detail=(
                            "allowed host-specific X11 addressing only:"
                            f" {', '.join(sorted(names)) or '(none)'}"
                        ),
                    )
                )
            continue
        if key in SAFETY_CRITICAL_PROPERTIES:
            findings.append(
                Finding(
                    check=f"dropin:{dropin.name}:{key}",
                    severity=Severity.FAIL,
                    detail=f"drop-in overrides safety-critical property {key}",
                    remedy=f"remove {key} from {dropin.name}",
                )
            )
        else:
            findings.append(
                Finding(
                    check=f"dropin:{dropin.name}:{key}",
                    severity=Severity.WARN,
                    detail=f"drop-in sets non-canonical property {key}",
                    remedy="review whether this override is intended",
                )
            )
    return findings


def contradicting_dropins(dropins: list[DropIn]) -> list[DropIn]:
    """Drop-ins that must be quarantined before the contract can hold."""
    return [
        dropin
        for dropin in dropins
        if any(f.severity is Severity.FAIL for f in classify_dropin(dropin))
    ]


def parse_show_properties(text: str) -> dict[str, str]:
    """Parse ``systemctl show`` ``Key=Value`` output."""
    properties: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        properties[key.strip()] = value.strip()
    return properties


def check_effective_gateway(properties: dict[str, str]) -> list[Finding]:
    """Compare the EFFECTIVE Gateway properties against the contract."""
    findings: list[Finding] = []
    for key, expected in CANONICAL_GATEWAY_CONTRACT.items():
        actual = properties.get(key, "")
        if not actual:
            findings.append(
                Finding(
                    check=f"effective:{key}",
                    severity=Severity.UNKNOWN,
                    detail=f"{key} not reported",
                )
            )
        elif actual.lower() != expected.lower():
            findings.append(
                Finding(
                    check=f"effective:{key}",
                    severity=Severity.FAIL,
                    detail=f"{key}={actual}, canonical contract is {expected}",
                    remedy="remove the contradicting drop-in",
                )
            )
        else:
            findings.append(
                Finding(check=f"effective:{key}", severity=Severity.OK, detail=actual)
            )
    return findings


def check_restart_prevent_exit_status(properties: dict[str, str]) -> Finding:
    """Is the launcher's fail-closed sentinel (78) exempt from auto-restart?

    Bounded crash recovery is only safe if KNOWN fail-closed conditions never
    restart. The launcher exits 78 for every such condition, so the effective
    ``RestartPreventExitStatus`` MUST contain 78. A blank/missing value is a
    FAIL, not UNKNOWN: without it a 2FA-timeout or auth-failure would be
    eligible for the bounded restart burst, i.e. a (short) 2FA loop.
    """
    if "RestartPreventExitStatus" not in properties:
        return Finding(
            check="effective:RestartPreventExitStatus",
            severity=Severity.UNKNOWN,
            detail="RestartPreventExitStatus not reported",
        )
    raw = properties.get("RestartPreventExitStatus", "")
    tokens = raw.replace(",", " ").split()
    if FAIL_CLOSED_EXIT_CODE in tokens:
        return Finding(
            check="effective:RestartPreventExitStatus",
            severity=Severity.OK,
            detail=raw,
        )
    return Finding(
        check="effective:RestartPreventExitStatus",
        severity=Severity.FAIL,
        detail=(
            f"fail-closed exit code {FAIL_CLOSED_EXIT_CODE} not exempt from restart"
            f" (effective: {raw or '(blank)'})"
        ),
        remedy=(
            f"set RestartPreventExitStatus={FAIL_CLOSED_EXIT_CODE} so known"
            " fail-closed conditions (2FA/auth/config) never auto-restart"
        ),
    )


def read_write_paths(value: str) -> list[str]:
    """The paths a ``ReadWritePaths=`` value actually grants.

    systemd allows a leading ``-`` (tolerate a missing path) and ``+`` (root
    relative). Both still grant the write, so both are stripped here. The unit
    file separately forbids the ``-`` form, because a Gateway that starts
    without its runtime directory is the failure mode we fail closed on.
    """
    granted: list[str] = []
    for entry in value.split():
        path = entry.lstrip("-+")
        if path:
            granted.append(path)
    return granted


def missing_writable_paths(value: str) -> list[str]:
    """Which required writable paths a ``ReadWritePaths=`` value omits."""
    granted = set(read_write_paths(value))
    return [path for path in CANONICAL_GATEWAY_WRITABLE_PATHS if path not in granted]


def check_gateway_writable_paths(properties: dict[str, str]) -> Finding:
    """Does the effective sandbox leave the Gateway's own tree writable?

    A blank value is a FAIL, not UNKNOWN: ``ReadWritePaths=`` with nothing
    after it is precisely how the observed ``zz-bringup.conf`` drop-in wiped
    the grants, and an empty sandbox exception is a confinement that the
    Gateway cannot start under.
    """
    raw = properties.get("ReadWritePaths", "")
    if "ReadWritePaths" not in properties:
        return Finding(
            check="effective:ReadWritePaths",
            severity=Severity.UNKNOWN,
            detail="ReadWritePaths not reported",
        )
    missing = missing_writable_paths(raw)
    if missing:
        return Finding(
            check="effective:ReadWritePaths",
            severity=Severity.FAIL,
            detail=(
                f"sandbox makes required Gateway path(s) read-only: {' '.join(missing)}"
                f" (effective: {raw or '(blank)'})"
            ),
            remedy=(
                "reinstall the canonical unit so ReadWritePaths grants "
                f"{' '.join(CANONICAL_GATEWAY_WRITABLE_PATHS)}, and remove any "
                "drop-in that overrides ReadWritePaths"
            ),
        )
    return Finding(
        check="effective:ReadWritePaths",
        severity=Severity.OK,
        detail=raw,
    )


def check_process_ownership(
    properties: dict[str, str], process_cgroup: str | None
) -> Finding:
    """Does the canonical service actually OWN the running Gateway?

    ``ActiveState=active`` with ``SubState=exited`` and ``MainPID=0`` means
    systemd is holding a tombstone, not a process. Treating that as ownership
    is how a Gateway ends up supervised by nothing while the dashboard is
    green. Real ownership requires the live process to sit in the service's
    own cgroup.
    """
    active = properties.get("ActiveState", "")
    sub = properties.get("SubState", "")
    main_pid = properties.get("MainPID", "0").strip() or "0"

    if main_pid == "0":
        detail = f"ActiveState={active} SubState={sub} MainPID=0"
        if process_cgroup and CANONICAL_GATEWAY_UNIT in process_cgroup:
            return Finding(
                check="gateway_process_ownership",
                severity=Severity.WARN,
                detail=f"{detail}; process cgroup does match the unit",
            )
        return Finding(
            check="gateway_process_ownership",
            severity=Severity.FAIL,
            detail=(
                f"{detail} — the unit owns no process."
                + (
                    f" The live Gateway is in {process_cgroup}"
                    if process_cgroup
                    else " No Gateway process cgroup was observed."
                )
            ),
            remedy=(
                "remove the Type/RemainAfterExit drop-in so the canonical"
                " Type=simple unit tracks the process; restarting the Gateway"
                " is an owner-authorized action and is NOT done automatically"
            ),
        )
    if process_cgroup and CANONICAL_GATEWAY_UNIT not in process_cgroup:
        return Finding(
            check="gateway_process_ownership",
            severity=Severity.FAIL,
            detail=f"MainPID={main_pid} but the live process is in {process_cgroup}",
            remedy="the running Gateway was not started by the canonical unit",
        )
    return Finding(
        check="gateway_process_ownership",
        severity=Severity.OK,
        detail=f"MainPID={main_pid} in the canonical cgroup",
    )


_EXEC_ARGV = re.compile(r"argv\[\]=([^;]+);")


def exec_start_argv(exec_start: str) -> list[str]:
    """The argv of a ``systemctl show -p ExecStart`` value (or a raw line)."""
    match = _EXEC_ARGV.search(exec_start)
    raw = match.group(1) if match else exec_start
    return raw.split()


def check_watchdog_entrypoint(properties: dict[str, str]) -> list[Finding]:
    """The timer must execute the reviewed code with the pinned interpreter."""
    findings: list[Finding] = []

    working_dir = properties.get("WorkingDirectory", "")
    findings.append(
        Finding(
            check="watchdog_working_directory",
            severity=Severity.OK if working_dir == REPO_ROOT else Severity.FAIL,
            detail=working_dir or "(unset)",
            remedy="" if working_dir == REPO_ROOT else f"must be exactly {REPO_ROOT}",
        )
    )

    argv = exec_start_argv(properties.get("ExecStart", ""))
    interpreter = argv[0] if argv else ""
    script = next((a for a in argv[1:] if a.endswith(".py")), "")

    findings.append(
        Finding(
            check="watchdog_interpreter",
            severity=Severity.OK if interpreter == VENV_PYTHON else Severity.FAIL,
            detail=interpreter or "(none)",
            remedy="" if interpreter == VENV_PYTHON else f"must be exactly {VENV_PYTHON}",
        )
    )
    findings.append(
        Finding(
            check="watchdog_script",
            severity=Severity.OK if script == WATCHDOG_SCRIPT else Severity.FAIL,
            detail=script or "(none)",
            remedy="" if script == WATCHDOG_SCRIPT else f"must be exactly {WATCHDOG_SCRIPT}",
        )
    )
    findings.append(
        Finding(
            check="watchdog_not_stale_copy",
            severity=(
                Severity.FAIL if STALE_WATCHDOG_PATH in " ".join(argv) else Severity.OK
            ),
            detail=(
                f"unit still references {STALE_WATCHDOG_PATH}"
                if STALE_WATCHDOG_PATH in " ".join(argv)
                else "no stale /opt reference"
            ),
            remedy="point ExecStart at the reviewed repository script",
        )
    )
    findings.append(
        Finding(
            check="watchdog_no_env_interpreter",
            severity=Severity.FAIL if "/usr/bin/env" in argv else Severity.OK,
            detail=(
                "/usr/bin/env resolves the interpreter through PATH"
                if "/usr/bin/env" in argv
                else "absolute interpreter"
            ),
            remedy="use the absolute venv interpreter",
        )
    )
    return findings


def worst(findings: list[Finding]) -> Severity:
    """The most severe outcome present. UNKNOWN never masks a FAIL."""
    for severity in (Severity.FAIL, Severity.UNKNOWN, Severity.WARN):
        if any(f.severity is severity for f in findings):
            return severity
    return Severity.OK


#: Exit codes for the ``classify`` CLI below. The caller MUST distinguish
#: "classified, and it contradicts" from "could not classify at all": the
#: second is a failure to know, and a remediation tool that cannot classify a
#: drop-in must refuse to touch anything rather than assume it is harmless.
EXIT_CLEAN = 0
EXIT_CONTRADICTS = 1
EXIT_CANNOT_CLASSIFY = 2


def main(argv: list[str] | None = None) -> int:
    """``systemd_contract.py classify <path>`` — deterministic, importable.

    Exists so the remediation tool can classify a drop-in by invoking this
    file at an ABSOLUTE path with the pinned interpreter, instead of piping a
    heredoc into whatever python is nearby. A heredoc has no stable identity:
    it cannot be reviewed, hashed or version-pinned, and any import problem
    inside it degrades to text on stdout.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "classify":
        print("usage: systemd_contract.py classify <dropin-path>", file=sys.stderr)
        return EXIT_CANNOT_CLASSIFY
    path = Path(args[1])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"cannot read drop-in: {error}", file=sys.stderr)
        return EXIT_CANNOT_CLASSIFY
    try:
        findings = classify_dropin(parse_dropin_strict(str(path), text))
    except Exception as error:  # noqa: BLE001 - any failure => cannot classify
        print(f"cannot classify drop-in: {type(error).__name__}", file=sys.stderr)
        return EXIT_CANNOT_CLASSIFY

    failed = [f for f in findings if f.severity is Severity.FAIL]
    if failed:
        print("; ".join(f.detail for f in failed))
        return EXIT_CONTRADICTS
    print("; ".join(f.detail for f in findings) or "no overrides")
    return EXIT_CLEAN


__all__ = [
    "ALLOWED_DROPIN_SETTINGS",
    "CANONICAL_GATEWAY_CONTRACT",
    "CANONICAL_GATEWAY_UNIT",
    "CANONICAL_GATEWAY_WRITABLE_PATHS",
    "EXPECTED_RESTART_SECONDS",
    "EXPECTED_START_LIMIT_INTERVAL_SECONDS",
    "FAIL_CLOSED_EXIT_CODE",
    "CANONICAL_WATCHDOG_TIMER",
    "CANONICAL_WATCHDOG_UNIT",
    "GATEWAY_INSTALL_ROOT",
    "HOME_ROOT",
    "LEGACY_GATEWAY_UNIT",
    "REPO_ROOT",
    "REQUIRED_IMPORTS",
    "SAFETY_CRITICAL_PROPERTIES",
    "STALE_WATCHDOG_PATH",
    "VENV_PYTHON",
    "WATCHDOG_SCRIPT",
    "DropIn",
    "DropInParseError",
    "Finding",
    "Severity",
    "check_bounded_recovery",
    "check_effective_gateway",
    "check_gateway_writable_paths",
    "check_process_ownership",
    "check_restart_prevent_exit_status",
    "duration_seconds",
    "check_watchdog_entrypoint",
    "classify_dropin",
    "contradicting_dropins",
    "exec_start_argv",
    "missing_writable_paths",
    "parse_dropin",
    "read_write_paths",
    "parse_dropin_strict",
    "parse_show_properties",
    "worst",
    "EXIT_CANNOT_CLASSIFY",
    "EXIT_CLEAN",
    "EXIT_CONTRADICTS",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
