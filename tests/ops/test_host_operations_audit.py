"""WP5/WP6: the read-only host audit and the operational assets (no host change).

The audit's value depends entirely on it being trustworthy about two things:
it must never change the host, and it must never claim a fact it did not
establish. Both are asserted here, along with the redaction rules — an
evidence file that leaks an account id is worse than no evidence file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "host_operations_audit", REPO_ROOT / "scripts" / "host_operations_audit.py"
)
assert _SPEC is not None and _SPEC.loader is not None
audit_mod = importlib.util.module_from_spec(_SPEC)
sys.modules["host_operations_audit"] = audit_mod
_SPEC.loader.exec_module(audit_mod)

AUDIT_SOURCE = (REPO_ROOT / "scripts" / "host_operations_audit.py").read_text(
    encoding="utf-8"
)
INSTALLER = REPO_ROOT / "scripts" / "install_ops_assets.sh"
TMPFILES = REPO_ROOT / "systemd" / "ibgateway-tmpfiles.conf"
LOGROTATE = REPO_ROOT / "systemd" / "ibgateway-logrotate.conf"
IBC_SERVICE = REPO_ROOT / "systemd" / "ibgateway-ibc.service"
WATCHDOG_SERVICE = REPO_ROOT / "systemd" / "ibgateway-watchdog.service"


# -- the audit never mutates ---------------------------------------------------


def test_the_audit_runs_only_read_only_verbs() -> None:
    """Every subprocess verb in the source must be an observation."""
    mutating = (
        "systemctl start", "systemctl stop", "systemctl restart", "systemctl enable",
        "systemctl disable", "firewall-cmd --add", "firewall-cmd --remove",
        "nft add", "nft delete", "nft flush", "iptables", "reboot", "kill",
    )
    for verb in mutating:
        assert verb not in AUDIT_SOURCE, verb


def test_the_audit_never_reads_credentials_or_ibc_config() -> None:
    # It must never READ any of these.
    for fragment in ("config.ini", "getpass", "os.environ"):
        assert fragment not in AUDIT_SOURCE, fragment
    # Credential NAMES may appear, but only inside detection/redaction
    # patterns — never as something the audit fetches or prints.
    for line in AUDIT_SOURCE.splitlines():
        if "IbPassword" in line or "IbLoginId" in line:
            assert "re.search" in line or "re.compile" in line, (
                f"credential name outside a detection pattern: {line.strip()}"
            )


def test_the_audit_uses_argument_vectors_not_shell_strings() -> None:
    assert "shell=True" not in AUDIT_SOURCE


def test_a_full_collection_changes_nothing_and_produces_checks(tmp_path: Path) -> None:
    result = audit_mod.collect(root=tmp_path)
    assert result.checks, "the audit must produce checks"
    sections = {check.section for check in result.checks}
    for section in ("platform", "systemd", "network", "firewall", "time",
                    "permissions", "logging", "readiness"):
        assert section in sections, section
    # tmp_path is untouched by a read-only audit.
    assert list(tmp_path.iterdir()) == []


# -- redaction -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("account DU1234567 ready", "DU1234567"),
        ("account U7654321 ready", "U7654321"),
        ("password=hunter2", "hunter2"),
        ("TOKEN: abc123def", "abc123def"),
        ("/home/opc/ibc/logs", "/home/opc"),
        ("User=opc", "User=opc"),
    ],
)
def test_redaction_removes_identifying_values(raw: str, must_not_contain: str) -> None:
    assert must_not_contain not in audit_mod.redact(raw)


def test_redaction_bounds_long_values() -> None:
    assert len(audit_mod.redact("x" * 5000)) <= audit_mod.MAX_VALUE_LENGTH


def test_every_check_value_passes_through_redaction() -> None:
    audit = audit_mod.Audit()
    audit.add("s", "n", audit_mod.Level.INFO, "account DU1234567", "at /home/opc")
    check = audit.checks[0]
    assert "DU1234567" not in check.value
    assert "/home/opc" not in check.detail


# -- listener scope classification --------------------------------------------


def test_localhost_binding_is_recognised() -> None:
    output = "LISTEN 0 50 127.0.0.1:4002 0.0.0.0:*\n"
    scope, binds = audit_mod._listener_scope(output, 4002)
    assert scope == "localhost"
    assert binds == ["127.0.0.1"]


def test_wildcard_binding_is_flagged_as_all_interfaces() -> None:
    output = "LISTEN 0 50 0.0.0.0:4002 0.0.0.0:*\n"
    scope, _ = audit_mod._listener_scope(output, 4002)
    assert scope == "all-interfaces"


def test_a_port_that_is_not_listening_is_none() -> None:
    scope, binds = audit_mod._listener_scope("LISTEN 0 50 127.0.0.1:22 0.0.0.0:*\n", 4002)
    assert scope == "none"
    assert binds == []


def test_a_mixed_binding_is_all_interfaces() -> None:
    output = "LISTEN 0 50 127.0.0.1:5901 0.0.0.0:*\nLISTEN 0 50 10.0.0.5:5901 0.0.0.0:*\n"
    scope, _ = audit_mod._listener_scope(output, 5901)
    assert scope == "all-interfaces"


# -- permissions ---------------------------------------------------------------


def test_a_world_writable_runtime_dir_fails(tmp_path: Path) -> None:
    runtime = tmp_path / "run" / "ibgateway"
    runtime.mkdir(parents=True)
    runtime.chmod(0o777)
    audit = audit_mod.Audit()
    audit_mod.audit_permissions(audit, root=tmp_path)
    runtime_check = next(c for c in audit.checks if c.name == "runtime_dir")
    assert runtime_check.level is audit_mod.Level.FAIL


def test_an_absent_path_is_info_not_a_failure(tmp_path: Path) -> None:
    audit = audit_mod.Audit()
    audit_mod.audit_permissions(audit, root=tmp_path)
    assert all(c.level is audit_mod.Level.INFO for c in audit.checks)


# -- OCI honesty ---------------------------------------------------------------


def test_oci_network_security_is_always_unknown() -> None:
    audit = audit_mod.Audit()
    audit_mod.audit_firewall(audit)
    oci = next(c for c in audit.checks if c.name == "oci_network_security")
    assert oci.level is audit_mod.Level.UNKNOWN
    assert "OCI console" in oci.detail


def test_the_docs_state_oci_cannot_be_verified_from_the_host() -> None:
    """The claim must be explicitly disclaimed, not merely absent."""
    port_doc = (REPO_ROOT / "docs/runbooks/HOST_PORT_SECURITY.md").read_text(
        encoding="utf-8"
    ).lower()
    assert "never claim oci network security is verified" in port_doc
    assert "cannot" in port_doc and "oci console" in port_doc
    hardening = (REPO_ROOT / "docs/runbooks/OPERATIONAL_HARDENING.md").read_text(
        encoding="utf-8"
    ).lower()
    assert "cannot be verified from the host" in hardening
    # And no affirmative claim anywhere.
    for text in (port_doc, hardening):
        for claim in (
            "oci network security is confirmed",
            "oci is verified",
            "oci security lists are verified",
        ):
            assert claim not in text, claim


# -- rendering + exit codes ----------------------------------------------------


def test_json_output_is_deterministic_and_complete(tmp_path: Path) -> None:
    audit = audit_mod.collect(root=tmp_path)
    payload = json.loads(audit_mod.render_json(audit, observed_at="2026-08-03T12:00:00+00:00"))
    assert payload["read_only"] is True
    assert payload["summary"]["total"] == len(audit.checks)
    assert all({"section", "name", "level", "value", "detail"} <= set(c) for c in payload["checks"])


def test_markdown_states_that_nothing_changed(tmp_path: Path) -> None:
    audit = audit_mod.collect(root=tmp_path)
    markdown = audit_mod.render_markdown(audit, observed_at="t")
    assert "changed nothing" in markdown
    assert "UNKNOWN" in markdown


def test_a_failing_check_makes_the_command_exit_non_zero(
    monkeypatch: Any, tmp_path: Path, capsys: Any
) -> None:
    def failing(**_kwargs: Any) -> Any:
        audit = audit_mod.Audit()
        audit.add("network", "port_4002_listener", audit_mod.Level.FAIL, "all-interfaces")
        return audit

    monkeypatch.setattr(audit_mod, "collect", failing)
    assert audit_mod.main(["--json"]) == 1
    capsys.readouterr()


def test_a_clean_audit_exits_zero(monkeypatch: Any, capsys: Any) -> None:
    def clean(**_kwargs: Any) -> Any:
        audit = audit_mod.Audit()
        audit.add("platform", "os", audit_mod.Level.INFO, "linux")
        return audit

    monkeypatch.setattr(audit_mod, "collect", clean)
    assert audit_mod.main([]) == 0
    capsys.readouterr()


def test_unknown_alone_does_not_fail_the_audit(monkeypatch: Any, capsys: Any) -> None:
    """UNKNOWN is reported loudly but is not itself a security failure."""

    def unknowns(**_kwargs: Any) -> Any:
        audit = audit_mod.Audit()
        audit.add("firewall", "oci", audit_mod.Level.UNKNOWN, "not determinable")
        return audit

    monkeypatch.setattr(audit_mod, "collect", unknowns)
    assert audit_mod.main([]) == 0
    capsys.readouterr()


# -- the installer -------------------------------------------------------------


def test_the_installer_is_strict_bash_and_defaults_to_check() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    assert "APPLY=0" in text, "the installer must default to CHECK/DRY-RUN"
    assert "--apply" in text


def test_the_installer_never_touches_network_or_remote_access() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    for forbidden in (
        "firewall-cmd --add", "firewall-cmd --remove", "nft add", "nft flush",
        "iptables", "sshd_config", "vncserver", "oci ",
    ):
        assert forbidden not in text, forbidden


def test_the_installer_never_starts_or_enables_anything() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    for forbidden in (
        "systemctl start", "systemctl restart", "systemctl enable --now ibgateway-ibc",
    ):
        assert forbidden not in text, forbidden
    # daemon-reload is inert and allowed; enabling is documented, not executed.
    assert "systemctl daemon-reload" in text


# -- tmpfiles / logrotate assets ----------------------------------------------


def test_the_runtime_directory_is_restrictive() -> None:
    text = TMPFILES.read_text(encoding="utf-8")
    assert "/run/ibgateway" in text
    assert "0700" in text


def test_no_unit_declares_a_runtime_directory() -> None:
    """tmpfiles.d owns /run/ibgateway. A unit must never claim it too.

    RuntimeDirectory= on the Type=oneshot watchdog would delete the inhibit
    marker and the restart budget at the end of every cycle. See
    tests/ops/test_runtime_directory_ownership.py for the full model.
    """
    for unit in (IBC_SERVICE, WATCHDOG_SERVICE):
        text = unit.read_text(encoding="utf-8")
        for line in text.splitlines():
            assert not line.strip().startswith("RuntimeDirectory="), unit.name
            assert not line.strip().startswith("RuntimeDirectoryMode="), unit.name


def test_logrotate_is_configured_restrictively() -> None:
    text = LOGROTATE.read_text(encoding="utf-8")
    assert "copytruncate" in text
    assert "create 0600" in text
    assert "rotate" in text


def test_the_gateway_service_uses_bounded_fail_closed_recovery() -> None:
    """Recovery is bounded and fail-closed, not blind: on-failure with a burst
    cap and the fail-closed sentinel exempt. Never Restart=no (no recovery) or
    Restart=always (blind/unbounded)."""
    text = IBC_SERVICE.read_text(encoding="utf-8")
    assert "Restart=on-failure" in text
    assert "StartLimitBurst=3" in text
    assert "RestartPreventExitStatus=78" in text
    for forbidden in ("Restart=always", "Restart=no"):
        assert forbidden not in text


def test_the_watchdog_unit_tolerates_the_gate_refusal_exit_codes() -> None:
    text = WATCHDOG_SERVICE.read_text(encoding="utf-8")
    success = next(
        line for line in text.splitlines() if line.startswith("SuccessExitStatus=")
    )
    for code in ("42", "43"):
        assert code in success, f"exit {code} (gate refusal) must not flap the unit"


def test_service_hardening_preserves_local_api_access() -> None:
    """Hardening must not break the very API the read-only probe needs."""
    text = WATCHDOG_SERVICE.read_text(encoding="utf-8")
    assert "AF_INET" in text
    for forbidden in ("PrivateNetwork=true", "IPAddressDeny=any"):
        assert forbidden not in text, forbidden


def test_no_credential_appears_in_any_unit() -> None:
    for unit in (IBC_SERVICE, WATCHDOG_SERVICE, TMPFILES, LOGROTATE):
        text = unit.read_text(encoding="utf-8")
        for fragment in ("IbPassword=", "IbLoginId=", "--pw=", "password="):
            assert fragment not in text, f"{unit.name}: {fragment}"
