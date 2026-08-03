"""Read-only owner-host operations audit (B-03 / B-04 evidence collector).

Collects the sanitized facts an owner-host readiness review needs, and changes
**nothing**. It does not start, stop, restart or relogin anything; it does not
touch a firewall, nftables, firewalld, OCI or SSH/VNC configuration; it never
reads the IBC config file; it never prints an environment secret.

Everything it runs is an observation: ``uname``, ``systemctl show``/``cat``,
``ss``, ``pgrep``, ``firewall-cmd --list-all``, ``nft list ruleset``,
``chronyc tracking``, ``stat``. Each is invoked with an explicit argument
vector (never a shell string), each failure degrades to ``UNKNOWN`` rather
than to a guess, and the exit code is non-zero when a **security** or
**readiness** check fails.

Output is both human-readable Markdown and deterministic JSON, so one run can
be pasted into a PR and archived as evidence.

Usage (owner host, read-only):

    uv run --no-sync python scripts/host_operations_audit.py
    uv run --no-sync python scripts/host_operations_audit.py --json
    uv run --no-sync python scripts/host_operations_audit.py \\
        --markdown-out /tmp/host-audit.md --json-out /tmp/host-audit.json
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

AUDIT_VERSION = "host-operations-audit-1.0.0"

#: The canonical Gateway service. There must be exactly one.
CANONICAL_GATEWAY_UNIT = "ibgateway-ibc.service"
WATCHDOG_UNIT = "ibgateway-watchdog.service"
WATCHDOG_TIMER = "ibgateway-watchdog.timer"

PAPER_API_PORT = 4002
LIVE_API_PORT = 4001
VNC_PORT = 5901

#: Anything matching these is replaced before it can reach the report.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(DU|U)\d{4,}\b"), r"\1*****"),
    (re.compile(r"(?i)\b(password|passwd|pw|token|secret|apikey|api_key)\s*[=:]\s*\S+"),
     r"\1=<redacted>"),
    (re.compile(r"/home/([^/\s]+)"), "/home/<user>"),
    (re.compile(r"\bUser=(\S+)"), "User=<user>"),
)

MAX_VALUE_LENGTH = 400


class Level(StrEnum):
    """Outcome of one check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    INFO = "INFO"


#: Levels that make the audit exit non-zero.
FAILING_LEVELS: frozenset[Level] = frozenset({Level.FAIL})


def redact(text: object) -> str:
    """Scrub then bound any value before it enters the report."""
    value = " ".join(str(text).split())
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    if len(value) > MAX_VALUE_LENGTH:
        value = value[: MAX_VALUE_LENGTH - 1] + "…"
    return value


@dataclass(frozen=True, slots=True)
class Check:
    """One audited fact."""

    section: str
    name: str
    level: Level
    value: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "section": self.section,
            "name": self.name,
            "level": self.level.value,
            "value": self.value,
            "detail": self.detail,
        }


@dataclass
class Audit:
    """Accumulates checks; never mutates the host."""

    checks: list[Check] = field(default_factory=list)

    def add(
        self, section: str, name: str, level: Level, value: object, detail: str = ""
    ) -> None:
        self.checks.append(
            Check(section, name, level, redact(value), redact(detail) if detail else "")
        )

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.level in FAILING_LEVELS]

    @property
    def unknown(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.UNKNOWN]


# -- read-only command runner -------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandResult:
    ok: bool
    stdout: str
    detail: str


def run_readonly(argv: Sequence[str], *, timeout: float = 10.0) -> CommandResult:
    """Run ONE read-only command with an explicit argv (never a shell string).

    Any failure — missing binary, non-zero exit, timeout — degrades to
    ``ok=False`` so the caller records UNKNOWN instead of inventing a fact.
    """
    if shutil.which(argv[0]) is None:
        return CommandResult(False, "", f"{argv[0]} not available")
    try:
        completed = subprocess.run(  # noqa: S603 - explicit argv, read-only verbs
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CommandResult(False, "", type(error).__name__)
    if completed.returncode != 0:
        return CommandResult(False, completed.stdout, f"exit {completed.returncode}")
    return CommandResult(True, completed.stdout, "")


# -- sections -----------------------------------------------------------------


def audit_platform(audit: Audit) -> None:
    audit.add("platform", "os", Level.INFO, platform.platform())
    machine = platform.machine()
    audit.add(
        "platform",
        "architecture",
        Level.PASS if machine in {"aarch64", "arm64"} else Level.WARN,
        machine,
        "" if machine in {"aarch64", "arm64"} else "expected ARM64 for the owner host",
    )
    audit.add("platform", "python", Level.INFO, platform.python_version())
    uv = shutil.which("uv")
    audit.add(
        "platform",
        "uv_available",
        Level.PASS if uv else Level.FAIL,
        bool(uv),
        "" if uv else "uv is required for `uv run --no-sync`",
    )


def audit_units(audit: Audit) -> None:
    """Unit definitions, ExecStart paths and the one-service invariant."""
    for unit in (CANONICAL_GATEWAY_UNIT, WATCHDOG_UNIT, WATCHDOG_TIMER):
        shown = run_readonly(["systemctl", "show", unit, "--no-pager"])
        if not shown.ok:
            audit.add("systemd", f"{unit}:defined", Level.UNKNOWN, "unreadable", shown.detail)
            continue
        props = dict(
            line.split("=", 1)
            for line in shown.stdout.splitlines()
            if "=" in line
        )
        load_state = props.get("LoadState", "unknown")
        audit.add(
            "systemd",
            f"{unit}:load_state",
            Level.PASS if load_state == "loaded" else Level.WARN,
            load_state,
        )
        audit.add(
            "systemd", f"{unit}:active_state", Level.INFO, props.get("ActiveState", "unknown")
        )
        if unit.endswith(".service"):
            audit.add(
                "systemd", f"{unit}:exec_start", Level.INFO, props.get("ExecStart", "unknown")
            )
            restart = props.get("Restart", "unknown")
            expected_no_restart = unit == CANONICAL_GATEWAY_UNIT
            audit.add(
                "systemd",
                f"{unit}:restart_policy",
                Level.PASS
                if (restart == "no" or not expected_no_restart)
                else Level.FAIL,
                restart,
                ""
                if not expected_no_restart
                else "the Gateway must NOT auto-restart: recovery is owner-authorized",
            )
            audit.add(
                "systemd",
                f"{unit}:runtime_directory",
                Level.PASS if props.get("RuntimeDirectory") else Level.WARN,
                props.get("RuntimeDirectory", "(none)"),
            )
            # A credential must never appear in a unit's Environment.
            environment = props.get("Environment", "")
            leaked = bool(
                re.search(r"(?i)(password|passwd|IbLoginId|IbPassword|token)", environment)
            )
            audit.add(
                "systemd",
                f"{unit}:no_credential_in_environment",
                Level.FAIL if leaked else Level.PASS,
                "credential-shaped Environment entry" if leaked else "clean",
            )

    # One canonical Gateway service — no competing manager.
    listed = run_readonly(
        ["systemctl", "list-unit-files", "--no-pager", "--no-legend", "--type=service"]
    )
    if not listed.ok:
        audit.add("systemd", "single_gateway_service", Level.UNKNOWN, "unreadable", listed.detail)
        return
    gateway_units = sorted(
        {
            line.split()[0]
            for line in listed.stdout.splitlines()
            if line.strip() and re.search(r"(?i)(ibgateway|ibc|tws)", line.split()[0])
            and not line.split()[0].startswith(WATCHDOG_UNIT.split(".")[0])
        }
    )
    extra = [u for u in gateway_units if u != CANONICAL_GATEWAY_UNIT]
    audit.add(
        "systemd",
        "single_gateway_service",
        Level.PASS if not extra else Level.FAIL,
        gateway_units or "(none found)",
        "" if not extra else f"competing Gateway/restart manager(s): {extra}",
    )


def audit_processes(audit: Audit) -> None:
    result = run_readonly(["pgrep", "-fc", "ibgateway"])
    if not result.ok:
        # pgrep exits 1 when nothing matches — that is a real answer, not an error.
        audit.add("process", "gateway_process_count", Level.INFO, "0")
        return
    count = result.stdout.strip() or "0"
    audit.add(
        "process",
        "gateway_process_count",
        Level.PASS if count in {"0", "1"} else Level.FAIL,
        count,
        "" if count in {"0", "1"} else "more than one Gateway process is running",
    )


#: Addresses that mean "this host only".
_LOCAL_BINDS: frozenset[str] = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})


def _listener_scope(output: str, port: int) -> tuple[str, list[str]]:
    """('localhost' | 'all-interfaces' | 'none'), plus the observed binds.

    Parses `ss -lntH` columns positionally
    (``State Recv-Q Send-Q Local:Port Peer:Port``) rather than by regex, so an
    IPv6 bind such as ``[::]:4002`` is split on the LAST colon and classified
    correctly. Getting this wrong in the safe direction would hide a real
    exposure, so the unknown case is treated as all-interfaces.
    """
    binds: list[str] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        address, separator, listen_port = fields[3].rpartition(":")
        if not separator or listen_port != str(port):
            continue
        binds.append(address)
    if not binds:
        return "none", []
    if all(bind in _LOCAL_BINDS for bind in binds):
        return "localhost", binds
    return "all-interfaces", binds


def audit_listeners(audit: Audit) -> None:
    result = run_readonly(["ss", "-lntH"])
    if not result.ok:
        for port in (PAPER_API_PORT, VNC_PORT, LIVE_API_PORT):
            audit.add("network", f"port_{port}", Level.UNKNOWN, "unreadable", result.detail)
        return

    for port, label in ((PAPER_API_PORT, "IBKR Paper API"), (VNC_PORT, "VNC")):
        scope, binds = _listener_scope(result.stdout, port)
        if scope == "none":
            audit.add("network", f"port_{port}_listener", Level.INFO, f"{label}: not listening")
            continue
        audit.add(
            "network",
            f"port_{port}_listener",
            Level.PASS if scope == "localhost" else Level.FAIL,
            f"{label}: {scope} ({', '.join(binds)})",
            ""
            if scope == "localhost"
            else f"{label} is reachable beyond localhost; restrict it and use an SSH tunnel",
        )

    scope, binds = _listener_scope(result.stdout, LIVE_API_PORT)
    audit.add(
        "network",
        f"port_{LIVE_API_PORT}_live_api",
        Level.PASS if scope == "none" else Level.FAIL,
        scope,
        "" if scope == "none" else "the LIVE API port must never be listening",
    )


def audit_firewall(audit: Audit) -> None:
    """Read-only local firewall observation. NEVER changes any rule."""
    firewalld = run_readonly(["firewall-cmd", "--state"])
    if firewalld.ok:
        listing = run_readonly(["firewall-cmd", "--list-all"])
        audit.add(
            "firewall",
            "firewalld",
            Level.INFO if listing.ok else Level.UNKNOWN,
            firewalld.stdout.strip() or "running",
            listing.stdout if listing.ok else listing.detail,
        )
    else:
        audit.add("firewall", "firewalld", Level.INFO, "not running/unavailable")

    nft = run_readonly(["nft", "list", "ruleset"])
    if nft.ok:
        rules = nft.stdout.strip()
        audit.add(
            "firewall",
            "nftables_ruleset",
            Level.INFO if rules else Level.WARN,
            f"{len(rules.splitlines())} line(s)" if rules else "empty ruleset",
            "" if rules else "no local packet filter is configured",
        )
    else:
        audit.add("firewall", "nftables_ruleset", Level.UNKNOWN, "unreadable", nft.detail)

    # The honest limit of host-local evidence.
    audit.add(
        "firewall",
        "oci_network_security",
        Level.UNKNOWN,
        "not determinable from the host",
        "OCI NSG / Security List state cannot be verified from repository or"
        " host-local firewall evidence; confirm it in the OCI console",
    )


def audit_time_sync(audit: Audit) -> None:
    """Clock sync matters: evidence timestamps are UTC wall-clock (D-CLOCK-01)."""
    tracking = run_readonly(["chronyc", "tracking"])
    if tracking.ok:
        leap = "Normal" in tracking.stdout or "Leap status" in tracking.stdout
        audit.add(
            "time",
            "chrony_tracking",
            Level.PASS if leap else Level.WARN,
            "tracking available",
            tracking.stdout.splitlines()[0] if tracking.stdout.strip() else "",
        )
        return
    timedatectl = run_readonly(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if timedatectl.ok:
        synced = timedatectl.stdout.strip() == "yes"
        audit.add(
            "time",
            "ntp_synchronized",
            Level.PASS if synced else Level.FAIL,
            timedatectl.stdout.strip(),
            "" if synced else "an unsynchronized clock silently corrupts evidence timestamps",
        )
        return
    audit.add("time", "time_sync", Level.UNKNOWN, "unreadable", tracking.detail)


#: Runtime paths whose ownership/mode matter. The lifecycle marker inhibits
#: unattended action, so a world-writable directory would be a control surface.
_GUARDED_PATHS: tuple[tuple[str, str, int], ...] = (
    ("runtime_dir", "/run/ibgateway", 0o700),
    ("watchdog_state", "/run/ibgateway/watchdog-state.json", 0o600),
    ("watchdog_lock", "/run/ibgateway/watchdog.lock", 0o600),
    ("lifecycle_marker", "/run/ibgateway/order-lifecycle.active", 0o600),
)


def audit_permissions(audit: Audit, *, root: Path | None = None) -> None:
    base = root or Path("/")
    for name, raw_path, max_mode in _GUARDED_PATHS:
        path = base / raw_path.lstrip("/")
        if not path.exists():
            audit.add("permissions", name, Level.INFO, "absent")
            continue
        try:
            info = path.stat()
        except OSError as error:
            audit.add("permissions", name, Level.UNKNOWN, "unreadable", type(error).__name__)
            continue
        mode = stat.S_IMODE(info.st_mode)
        too_open = bool(mode & ~max_mode)
        audit.add(
            "permissions",
            name,
            Level.FAIL if too_open else Level.PASS,
            f"mode={mode:04o} uid={info.st_uid} gid={info.st_gid}",
            "" if too_open is False else f"wider than {max_mode:04o}",
        )


def audit_logrotate(audit: Audit, *, root: Path | None = None) -> None:
    base = root or Path("/")
    configured = base / "etc/logrotate.d/ibgateway"
    audit.add(
        "logging",
        "logrotate_config",
        Level.PASS if configured.exists() else Level.WARN,
        str(configured) if configured.exists() else "absent",
        "" if configured.exists() else "IBC logs will grow unbounded without rotation",
    )


def audit_deep_probe_dependency(audit: Audit) -> None:
    """Is the broker package the deep probe needs importable? (B-04)"""
    try:
        import importlib.util

        spec = importlib.util.find_spec("ibapi")
    except (ImportError, ValueError):
        spec = None
    audit.add(
        "readiness",
        "ibapi_importable",
        Level.PASS if spec is not None else Level.WARN,
        spec is not None,
        "" if spec is not None else "the deep probe cannot run without the broker package",
    )
    try:
        import importlib.util as _util

        adapter = _util.find_spec("trading_execution_agent.account_safety")
    except (ImportError, ValueError):
        adapter = None
    audit.add(
        "readiness",
        "account_safety_importable",
        Level.PASS if adapter is not None else Level.FAIL,
        adapter is not None,
        "" if adapter is not None else "the account-safety probe module is not importable",
    )


def collect(*, root: Path | None = None) -> Audit:
    """Run every read-only section. Changes nothing."""
    audit = Audit()
    audit_platform(audit)
    audit_units(audit)
    audit_processes(audit)
    audit_listeners(audit)
    audit_firewall(audit)
    audit_time_sync(audit)
    audit_permissions(audit, root=root)
    audit_logrotate(audit, root=root)
    audit_deep_probe_dependency(audit)
    return audit


# -- rendering ----------------------------------------------------------------


def render_json(audit: Audit, *, observed_at: str) -> str:
    payload = {
        "audit_version": AUDIT_VERSION,
        "observed_at": observed_at,
        "read_only": True,
        "checks": [check.as_dict() for check in audit.checks],
        "summary": {
            "total": len(audit.checks),
            "failed": len(audit.failed),
            "unknown": len(audit.unknown),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def render_markdown(audit: Audit, *, observed_at: str) -> str:
    lines = [
        "# Owner-host operations audit (read-only)",
        "",
        f"- audit version: `{AUDIT_VERSION}`",
        f"- observed at (UTC): `{observed_at}`",
        "- **This audit changed nothing.** No service was started, stopped or",
        "  restarted; no firewall, OCI, SSH or VNC configuration was touched;",
        "  no credential or IBC config content was read.",
        "",
        "| Section | Check | Result | Value | Detail |",
        "|---|---|---|---|---|",
    ]
    for check in audit.checks:
        lines.append(
            f"| {check.section} | {check.name} | **{check.level.value}** |"
            f" {check.value} | {check.detail} |"
        )
    lines += [
        "",
        f"**{len(audit.failed)} failing check(s), {len(audit.unknown)} unknown.**",
        "",
        "> `UNKNOWN` means the fact could not be established, never that it is",
        "> fine. OCI network security in particular cannot be verified from the",
        "> host — confirm it in the OCI console.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only owner-host operations audit. Collects sanitized B-03/B-04"
            " facts and changes nothing: no service action, no firewall change,"
            " no credential read."
        )
    )
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    parser.add_argument("--json-out", default=None, help="also write JSON here")
    parser.add_argument("--markdown-out", default=None, help="also write Markdown here")
    parser.add_argument(
        "--root",
        default=None,
        help="filesystem root for path checks (testing only; defaults to /)",
    )
    args = parser.parse_args(argv)

    observed_at = datetime.now(tz=UTC).isoformat()
    audit = collect(root=Path(args.root) if args.root else None)

    markdown = render_markdown(audit, observed_at=observed_at)
    payload = render_json(audit, observed_at=observed_at)
    print(payload if args.json else markdown)

    if args.json_out:
        Path(args.json_out).write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        Path(args.markdown_out).write_text(markdown + "\n", encoding="utf-8")

    # Non-zero on a security or readiness failure so CI/an owner script notices.
    return 1 if audit.failed else 0


if __name__ == "__main__":
    sys.exit(main())
