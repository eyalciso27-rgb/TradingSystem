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
from dataclasses import dataclass, field
from enum import StrEnum

CANONICAL_GATEWAY_UNIT = "ibgateway-ibc.service"
CANONICAL_WATCHDOG_UNIT = "ibgateway-watchdog.service"
CANONICAL_WATCHDOG_TIMER = "ibgateway-watchdog.timer"
LEGACY_GATEWAY_UNIT = "ibgateway.service"

#: The reviewed repository, its pinned interpreter and the canonical script.
#: Absolute on purpose: a PATH-resolved interpreter means the timer runs
#: whatever python happens to be first today.
REPO_ROOT = "/home/opc/projects/TradingSystem"
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
CANONICAL_GATEWAY_CONTRACT: dict[str, str] = {
    "Type": "simple",
    "Restart": "no",
    "RemainAfterExit": "no",
}


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


__all__ = [
    "ALLOWED_DROPIN_SETTINGS",
    "CANONICAL_GATEWAY_CONTRACT",
    "CANONICAL_GATEWAY_UNIT",
    "CANONICAL_WATCHDOG_TIMER",
    "CANONICAL_WATCHDOG_UNIT",
    "LEGACY_GATEWAY_UNIT",
    "REPO_ROOT",
    "REQUIRED_IMPORTS",
    "SAFETY_CRITICAL_PROPERTIES",
    "STALE_WATCHDOG_PATH",
    "VENV_PYTHON",
    "WATCHDOG_SCRIPT",
    "DropIn",
    "Finding",
    "Severity",
    "check_effective_gateway",
    "check_process_ownership",
    "check_watchdog_entrypoint",
    "classify_dropin",
    "contradicting_dropins",
    "exec_start_argv",
    "parse_dropin",
    "parse_show_properties",
    "worst",
]
