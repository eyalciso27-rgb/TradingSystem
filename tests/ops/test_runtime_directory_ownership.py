"""Defect 1: ONE owner for /run/ibgateway, and a oneshot that cannot wipe it.

The watchdog unit is ``Type=oneshot``. A oneshot goes inactive the moment its
ExecStart returns, and ``RuntimeDirectory=`` is torn down when a unit goes
inactive unless ``RuntimeDirectoryPreserve=`` says otherwise. Both units used
to declare ``RuntimeDirectory=ibgateway``, so every watchdog cycle would have
deleted:

    /run/ibgateway/order-lifecycle.active   the inhibit marker
    /run/ibgateway/watchdog-state.json      the restart budget
    /run/ibgateway/watchdog.lock            the concurrency lock

A supervisor that erases its own inhibit marker every minute, and resets the
budget that rate-limits it, is worse than no supervisor: it would look healthy
while quietly removing the two things that stop it acting.

These tests parse the units rather than grepping them, so a directive that is
re-added under any spelling or in any section is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SYSTEMD = REPO / "systemd"
IBC_SERVICE = SYSTEMD / "ibgateway-ibc.service"
WATCHDOG_SERVICE = SYSTEMD / "ibgateway-watchdog.service"
WATCHDOG_TIMER = SYSTEMD / "ibgateway-watchdog.timer"
TMPFILES = SYSTEMD / "ibgateway-tmpfiles.conf"

UNITS = (IBC_SERVICE, WATCHDOG_SERVICE, WATCHDOG_TIMER)

#: Every file whose loss would change supervision behaviour.
SAFETY_STATE = (
    "/run/ibgateway/order-lifecycle.active",
    "/run/ibgateway/watchdog-state.json",
    "/run/ibgateway/watchdog.lock",
)


def parse_unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Parse a systemd unit into ``section -> [(key, value), ...]``.

    Keeps duplicates: systemd allows a key to repeat, and a naive dict would
    hide a second ``RuntimeDirectory=`` line. Handles the trailing-backslash
    line continuations used by ExecStart.
    """
    sections: dict[str, list[tuple[str, str]]] = {}
    section = ""
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if pending:
            pending = (pending + " " + line).strip()
            if pending.endswith("\\"):
                pending = pending[:-1].strip()
                continue
            line, pending = pending, ""
        elif not line or line.startswith(("#", ";")):
            continue
        elif line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            sections.setdefault(section, [])
            continue
        elif line.endswith("\\"):
            pending = line[:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        sections.setdefault(section, []).append((key.strip(), value.strip()))
    return sections


def directives(path: Path, name: str) -> list[str]:
    """Every value assigned to ``name`` anywhere in the unit."""
    parsed = parse_unit(path)
    return [v for entries in parsed.values() for k, v in entries if k == name]


# -- the ownership model ------------------------------------------------------


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_no_unit_owns_the_runtime_directory(unit: Path) -> None:
    """Parsed, not grepped: no unit may declare RuntimeDirectory at all."""
    assert directives(unit, "RuntimeDirectory") == []
    assert directives(unit, "RuntimeDirectoryMode") == []


def test_there_is_exactly_one_canonical_owner() -> None:
    """tmpfiles.d declares the directory; nothing else does."""
    tmpfiles = TMPFILES.read_text(encoding="utf-8")
    declarations = [
        line
        for line in tmpfiles.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(declarations) == 1, f"exactly one tmpfiles rule, got {declarations}"
    fields = declarations[0].split()
    assert fields[0] == "d", "must be a directory rule"
    assert fields[1] == "/run/ibgateway"
    assert fields[2] == "0700", "the inhibit marker lives here; 0700 is the floor"
    assert fields[3] == fields[4] == "opc", "owned by the service user"


def test_the_oneshot_cannot_remove_the_shared_safety_directory() -> None:
    """The watchdog is a oneshot, so it must own no directory lifecycle.

    This is the specific defect: Type=oneshot + RuntimeDirectory + the default
    RuntimeDirectoryPreserve=no deletes the directory on every cycle.
    """
    parsed = parse_unit(WATCHDOG_SERVICE)
    service = dict(parsed["Service"])
    assert service["Type"] == "oneshot", "if this changes, re-derive the analysis"
    assert directives(WATCHDOG_SERVICE, "RuntimeDirectory") == []
    # And nothing else in the unit may remove state either.
    text = WATCHDOG_SERVICE.read_text(encoding="utf-8")
    for forbidden in ("ExecStopPost=", "ExecStop="):
        assert forbidden not in text, f"{forbidden} could delete shared state"
    body = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )
    for destructive in ("rm ", "rm -", "unlink ", "shred "):
        assert destructive not in body, f"{destructive!r} in the watchdog unit"


def test_the_lifecycle_marker_survives_watchdog_completion_semantics() -> None:
    """Nothing tied to the oneshot's lifetime may hold the marker.

    The marker's entire purpose is to inhibit action while a lifecycle run is
    in flight — a run that outlives any single watchdog cycle.
    """
    marker = "/run/ibgateway/order-lifecycle.active"
    text = WATCHDOG_SERVICE.read_text(encoding="utf-8")
    assert marker in text, "the watchdog must still be told where the marker is"
    # It is passed as an argument to read, never declared as unit-owned state.
    assert directives(WATCHDOG_SERVICE, "RuntimeDirectory") == []
    for key in ("StateDirectory", "CacheDirectory", "LogsDirectory"):
        assert directives(WATCHDOG_SERVICE, key) == [], (
            f"{key}= is also lifetime-managed by systemd and would recreate the bug"
        )


@pytest.mark.parametrize("unit", (IBC_SERVICE, WATCHDOG_SERVICE), ids=lambda p: p.name)
def test_units_are_ordered_after_the_directory_is_created(unit: Path) -> None:
    """Reboot-time recreation is deterministic, not a race."""
    after = " ".join(directives(unit, "After"))
    assert "systemd-tmpfiles-setup.service" in after


@pytest.mark.parametrize("unit", (IBC_SERVICE, WATCHDOG_SERVICE), ids=lambda p: p.name)
def test_units_can_write_the_directory_but_fail_closed_without_it(unit: Path) -> None:
    """No leading '-': a missing directory must fail the unit, not be ignored.

    Running the watchdog without its budget, lock and marker is the dangerous
    case, so it must refuse to start rather than silently proceed.
    """
    paths = " ".join(directives(unit, "ReadWritePaths"))
    assert "/run/ibgateway" in paths
    for entry in paths.split():
        assert not entry.startswith("-"), f"'-' would make {entry} non-fatal when missing"


def test_the_directory_is_never_world_writable_anywhere() -> None:
    """No permissive fallback in any asset."""
    for path in (*UNITS, TMPFILES):
        text = path.read_text(encoding="utf-8")
        body = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        for loose in ("0777", "0666", "0755", "0644 opc"):
            assert loose not in body, f"{loose} in {path.name}"


def test_still_exactly_one_gateway_service() -> None:
    """The fix must not have introduced a second manager."""
    services = sorted(p.name for p in SYSTEMD.glob("*.service"))
    assert services == ["ibgateway-ibc.service", "ibgateway-watchdog.service"]
    assert directives(WATCHDOG_SERVICE, "Restart") == []
    # Bounded, fail-closed recovery — one manager still, just not Restart=no.
    assert directives(IBC_SERVICE, "Restart") == ["on-failure"]


# -- the code side of the same guarantee --------------------------------------


def test_the_audit_flags_a_unit_that_claims_the_directory() -> None:
    """The host audit's check is INVERTED relative to its first version.

    It used to PASS when a unit declared RuntimeDirectory and WARN when none
    did — i.e. it would have blessed the dangerous configuration and warned
    about the correct one.
    """
    source = (REPO / "scripts" / "host_operations_audit.py").read_text(encoding="utf-8")
    assert "no_runtime_directory_ownership" in source
    assert "Level.FAIL if declared else Level.PASS" in source
    assert "Level.PASS if props.get(\"RuntimeDirectory\")" not in source


def test_the_watchdog_creates_the_directory_restrictively() -> None:
    """The last-resort mkdir must be 0700, never umask-dependent."""
    source = (REPO / "scripts" / "ib_gateway_watchdog.py").read_text(encoding="utf-8")
    assert "RUNTIME_DIR_MODE = 0o700" in source
    assert "RUNTIME_FILE_MODE = 0o600" in source
    assert "def ensure_runtime_dir" in source
    # mkdir(mode=...) is masked by the umask, so an explicit chmod must follow.
    assert "parent.chmod(RUNTIME_DIR_MODE)" in source
