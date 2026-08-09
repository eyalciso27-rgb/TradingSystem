"""The Gateway sandbox must not make the Gateway's own tree read-only.

The owner-host failure this pins:

    systemctl start ibgateway-ibc.service
    Active: inactive (dead)
    ibgateway-ibc.service: Deactivated successfully.

Nothing in that output says "sandbox". The launch script does not background —
its last statement is ``exec "${entry}" ... --gateway`` — so ``Type=simple``
was correct and the process really was the Gateway. What was wrong is that the
unit declared::

    ProtectHome=read-only
    ReadWritePaths=/home/opc/ibc /home/opc/.config/ibc /run/ibgateway

while IBC's ``ibcstart.sh`` uses ``~/Jts`` as the TWS/Gateway path and writes
there during startup, and the Java the service executes lives under it. The
Gateway's own installation and settings tree was therefore read-only, the
launch failed, and systemd reported a clean deactivation.

The class of defect matters more than the instance: a *missing* sandbox
exception fails silently and looks healthy. So these tests assert the required
set positively — every path in the contract must be granted — rather than
spot-checking the one path that happened to be missing. Adding a fifth
required tree later without granting it is caught here too.

They also refuse the tempting wrong fixes: dropping ``ProtectHome``, dropping
``ProtectSystem``, switching ``Restart``, or adding a second manager.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
SYSTEMD = REPO / "systemd"
IBC_SERVICE = SYSTEMD / "ibgateway-ibc.service"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contract = _load("systemd_contract_wp", SCRIPTS / "systemd_contract.py")
rem = _load("owner_host_remediation_wp", SCRIPTS / "owner_host_remediation.py")

#: The Gateway installation and settings tree. Named literally here so the
#: test cannot be satisfied by a constant that drifted with the bug.
JTS = "/home/opc/Jts"


def service_directives(key: str) -> list[str]:
    """Every ``key=`` value in the [Service] section of the canonical unit."""
    values: list[str] = []
    section = ""
    for raw in IBC_SERVICE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if section == "Service" and name.strip() == key:
            values.append(value.strip())
    return values


def granted_paths() -> set[str]:
    return set(contract.read_write_paths(" ".join(service_directives("ReadWritePaths"))))


# =============================================================================
# The unit file
# =============================================================================


@pytest.mark.parametrize("required", contract.CANONICAL_GATEWAY_WRITABLE_PATHS)
def test_the_unit_grants_every_required_writable_path(required: str) -> None:
    assert required in granted_paths(), (
        f"{required} is in the canonical writable contract but the unit does not grant it; "
        "the Gateway will fail to launch and systemd will report a clean deactivation"
    )


def test_the_gateway_installation_tree_is_writable() -> None:
    """The specific tree whose absence stopped the owner host."""
    assert JTS in granted_paths()
    assert JTS in contract.CANONICAL_GATEWAY_WRITABLE_PATHS


def test_the_java_the_service_runs_lives_inside_a_writable_tree() -> None:
    """Structural, not incidental.

    Moving the Gateway installation without moving the write grant would
    reproduce exactly this defect, so the two are tied together here.
    """
    java = PurePosixPath(rem.CANONICAL_JAVA_PATH)
    assert java.is_absolute(), "these are host paths; they are POSIX even on Windows"
    covered = [
        path
        for path in contract.CANONICAL_GATEWAY_WRITABLE_PATHS
        if java.is_relative_to(path)
    ]
    assert covered, (
        f"{rem.CANONICAL_JAVA_PATH} is not inside any tree the sandbox leaves writable: "
        f"{contract.CANONICAL_GATEWAY_WRITABLE_PATHS}"
    )
    assert contract.GATEWAY_INSTALL_ROOT in covered


# =============================================================================
# The sandbox must NOT be weakened to achieve this
# =============================================================================


def test_protecthome_is_still_read_only() -> None:
    assert service_directives("ProtectHome") == ["read-only"]


def test_protectsystem_is_still_strict() -> None:
    assert service_directives("ProtectSystem") == ["strict"]


def test_the_rest_of_home_is_not_writable() -> None:
    """The grant is an exception list, not a relaxation.

    ``/home`` or ``/home/opc`` in ReadWritePaths would re-open the repository,
    the SSH keys and the owner's files to the Gateway — which is what
    ``ProtectHome=read-only`` exists to prevent.
    """
    for wide in ("/home", "/home/opc", "/", "/home/opc/"):
        assert wide not in granted_paths(), f"{wide} defeats ProtectHome"


def test_the_other_hardening_directives_survive() -> None:
    for key, expected in (
        ("Type", "simple"),
        ("Restart", "on-failure"),  # bounded, fail-closed recovery
        ("PrivateTmp", "true"),
        ("NoNewPrivileges", "true"),
        ("User", "opc"),
    ):
        assert service_directives(key) == [expected], key


def test_no_second_manager_was_introduced() -> None:
    services = sorted(p.name for p in SYSTEMD.glob("*.service"))
    assert services == ["ibgateway-ibc.service", "ibgateway-watchdog.service"]
    assert service_directives("RuntimeDirectory") == []


# =============================================================================
# The contract module
# =============================================================================


def test_read_write_paths_strips_the_systemd_prefixes() -> None:
    parsed = contract.read_write_paths("-/a +/b /c")
    assert parsed == ["/a", "/b", "/c"]
    assert contract.read_write_paths("") == []
    assert contract.read_write_paths("-") == []


@pytest.mark.parametrize("dropped", contract.CANONICAL_GATEWAY_WRITABLE_PATHS)
def test_the_contract_rejects_a_sandbox_missing_any_required_path(dropped: str) -> None:
    value = " ".join(p for p in contract.CANONICAL_GATEWAY_WRITABLE_PATHS if p != dropped)
    finding = contract.check_gateway_writable_paths({"ReadWritePaths": value})
    assert finding.severity is contract.Severity.FAIL
    assert dropped in finding.detail
    assert contract.missing_writable_paths(value) == [dropped]


def test_a_blank_readwritepaths_fails_rather_than_reading_as_unknown() -> None:
    """``ReadWritePaths=`` is how the observed zz-bringup.conf wiped the grants."""
    finding = contract.check_gateway_writable_paths({"ReadWritePaths": ""})
    assert finding.severity is contract.Severity.FAIL
    assert JTS in finding.detail


def test_an_unreported_property_is_unknown_not_a_pass() -> None:
    finding = contract.check_gateway_writable_paths({})
    assert finding.severity is contract.Severity.UNKNOWN


def test_a_complete_sandbox_passes() -> None:
    value = " ".join(contract.CANONICAL_GATEWAY_WRITABLE_PATHS)
    assert contract.check_gateway_writable_paths({"ReadWritePaths": value}).severity is (
        contract.Severity.OK
    )
    assert contract.missing_writable_paths(value) == []


def test_extra_paths_do_not_break_the_check() -> None:
    """The contract is a required minimum, not an exact string match."""
    value = " ".join((*contract.CANONICAL_GATEWAY_WRITABLE_PATHS, "/var/tmp/something"))
    assert contract.check_gateway_writable_paths({"ReadWritePaths": value}).severity is (
        contract.Severity.OK
    )


def test_readwritepaths_remains_safety_critical_for_dropins() -> None:
    """A drop-in must not be able to re-impose the read-only tree."""
    dropin = contract.parse_dropin("/x.d/zz.conf", "[Service]\nReadWritePaths=\n")
    assert contract.contradicting_dropins([dropin]) == [dropin]


# =============================================================================
# The remediation engine
# =============================================================================


def test_the_engine_and_the_contract_agree_on_the_writable_set() -> None:
    """Two modules, one contract.

    ``owner_host_remediation`` deliberately carries its own constants so it can
    run against a ``--sysroot`` without importing host modules. That is only
    safe while the two cannot drift.
    """
    assert tuple(rem.EXPECTED_GATEWAY_READWRITEPATHS) == tuple(
        contract.CANONICAL_GATEWAY_WRITABLE_PATHS
    )


def test_the_unit_file_and_the_engine_agree() -> None:
    assert granted_paths() == set(rem.EXPECTED_GATEWAY_READWRITEPATHS)


@pytest.mark.parametrize("dropped", tuple(rem.EXPECTED_GATEWAY_READWRITEPATHS))
def test_verify_fails_when_a_required_writable_path_is_absent(
    tmp_path: Path, dropped: str
) -> None:
    """VERIFIED_COMPLETE must be unreachable with the Gateway tree read-only."""
    hardening = _load(
        "test_remediation_hardening_wp",
        Path(__file__).parent / "test_remediation_hardening.py",
    )
    sysroot = hardening.build(tmp_path, dropins={})
    gateway = dict(hardening.HEALTHY_UNITS[rem.CANONICAL_GATEWAY_UNIT])
    gateway["ReadWritePaths"] = " ".join(
        p for p in rem.EXPECTED_GATEWAY_READWRITEPATHS if p != dropped
    )
    remediator = hardening.make(
        sysroot,
        units={**hardening.HEALTHY_UNITS, rem.CANONICAL_GATEWAY_UNIT: gateway},
    )
    gates = remediator.verify_contract(remediator.collect())
    failed = {g.name: g for g in gates if not g.ok}
    assert "gateway:ReadWritePaths" in failed, f"{dropped} removed but VERIFY passed"
    assert dropped in failed["gateway:ReadWritePaths"].detail
