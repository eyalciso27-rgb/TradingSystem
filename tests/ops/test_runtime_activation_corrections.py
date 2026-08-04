"""Operational-runtime corrections found during the real ARM64 activation.

Four defects, all of which passed every previous test because every previous
test used fakes that were kinder than the host:

**Plain `--status` opened a real broker session.** It called a round-trip probe
that connected, sent ``startApi``, requested the current time and disconnected
— while the command was documented as opening no broker session.

**Deep status used two sessions.** The round-trip probe opened one, then the
account-safety adapter opened another, so the "one coherent snapshot" claim
spanned two connections.

**The account identifier leaked.** ``logging.basicConfig(level=INFO)`` raises
the ROOT level, which turns INFO on for every library in the process. ibapi's
``managedAccounts`` callback then printed the complete Paper account id to
stderr. The JSON model was sanitized; the logging path went around it.

**The timer ran the wrong code.** ``/usr/bin/env python3
/opt/trading/scripts/ib_gateway_watchdog.py`` — a stale copy, executed by an
interpreter that cannot import the broker package at all, so the deep probe
could never have worked.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
WATCHDOG_SOURCE = (SCRIPTS / "ib_gateway_watchdog.py").read_text(encoding="utf-8")
WATCHDOG_UNIT = REPO / "systemd" / "ibgateway-watchdog.service"
UNIT_TEXT = WATCHDOG_UNIT.read_text(encoding="utf-8")
REMEDIATE = SCRIPTS / "remediate_owner_host_runtime.sh"

#: A distinctive, obviously fake Paper id. Every containment assertion looks
#: for THIS string, so a leak is unambiguous.
SENTINEL_ACCOUNT = "DU7654321"

BASH_ONLY = pytest.mark.skipif(
    shutil.which("bash") is None, reason="needs a POSIX shell"
)


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


wd = _load("ib_gateway_watchdog_rt", SCRIPTS / "ib_gateway_watchdog.py")
contract = _load("systemd_contract_rt", SCRIPTS / "systemd_contract.py")


# =============================================================================
# WP1 — plain status is broker-connection-free
# =============================================================================


class ExplodingSession:
    """Any use at all is a failure. Records nothing because nothing may happen."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - must not run
        raise AssertionError(f"plain --status touched the broker surface: {name}")


def _status_probes(**over: Any) -> Any:
    defaults: dict[str, Any] = {
        "process": lambda: True,
        "tcp": lambda: True,
        "login": lambda: wd.LoginMarker.LOGGED_IN,
        "restart_inhibited": lambda: False,
    }
    defaults.update(over)
    return wd.StatusProbes(**defaults)


def _plain_report(**over: Any) -> Any:
    return wd.build_status_report(
        _status_probes(**over),
        wd.StateStore(path=Path("unused")),
        policy=wd.Policy(),
        unit="ibgateway-ibc.service",
        now=1000.0,
        observed_at="2026-08-04T00:00:00+00:00",
        deep=False,
    )


def test_status_probes_define_no_round_trip_probe() -> None:
    """Structural: the attribute does not exist, so it cannot be called.

    "We simply stopped calling it" would leave the next edit one attribute
    away from reopening a broker session on a read-only command.
    """
    assert not hasattr(wd.StatusProbes, "current_time")
    fields = {f for f in wd.StatusProbes.__dataclass_fields__}
    assert "current_time" not in fields
    assert fields == {"process", "tcp", "login", "restart_inhibited", "account_safety"}


def test_plain_status_constructs_no_session_and_sends_no_request() -> None:
    """Its account-safety default is inert: no adapter, no transport."""
    report = _plain_report()
    assert report.api_round_trip_ok is None
    assert report.deep_probe is None
    # The fail-closed default must not be a broker call of any kind.
    default = wd.StatusProbes(
        process=lambda: True,
        tcp=lambda: True,
        login=lambda: wd.LoginMarker.NONE,
        restart_inhibited=lambda: False,
    ).account_safety()
    assert default["safety"] == wd.SAFETY_UNKNOWN


def test_plain_status_never_reaches_a_broker_object() -> None:
    exploding = ExplodingSession()
    report = wd.build_status_report(
        _status_probes(account_safety=lambda: exploding.anything),
        wd.StateStore(path=Path("unused")),
        policy=wd.Policy(),
        unit="ibgateway-ibc.service",
        now=1000.0,
        observed_at="2026-08-04T00:00:00+00:00",
        deep=False,  # not deep => account_safety is never invoked
    )
    assert report.account_safety == wd.SAFETY_UNKNOWN


def test_plain_status_proof_level_is_at_most_tcp_open() -> None:
    ceiling = [wd.ProofLevel.NONE, wd.ProofLevel.PROCESS_ONLY, wd.ProofLevel.TCP_OPEN]
    for process in (True, False):
        for tcp in (True, False):
            report = _plain_report(process=lambda p=process: p, tcp=lambda t=tcp: t)
            assert wd.ProofLevel(report.proof_level) in ceiling, report.proof_level


def test_plain_status_cannot_report_api_ready_from_tcp_alone() -> None:
    report = _plain_report()  # process up, port open, logged in
    assert report.state is None
    assert report.observed_state == wd.ObservedState.API_PORT_OPEN_UNVERIFIED.value
    assert "API_READY" not in str(report.as_dict())


def test_the_observation_vocabulary_cannot_spell_api_ready() -> None:
    """Honest vocabulary, not careful wording."""
    values = {member.value for member in wd.ObservedState}
    assert "API_READY" not in values
    assert wd.ObservedState.API_PORT_OPEN_UNVERIFIED.value in values


@pytest.mark.parametrize(
    ("process", "tcp", "marker", "expected"),
    [
        (False, False, wd.LoginMarker.NONE, wd.ObservedState.PROCESS_DOWN),
        (True, True, wd.LoginMarker.TWO_FA_PROMPT, wd.ObservedState.TWO_FA_PENDING),
        (True, True, wd.LoginMarker.LOGIN_FAILED, wd.ObservedState.LOGIN_REQUIRED),
        (True, False, wd.LoginMarker.LOGGED_IN, wd.ObservedState.API_PORT_CLOSED),
        (True, True, wd.LoginMarker.LOGGED_IN, wd.ObservedState.API_PORT_OPEN_UNVERIFIED),
    ],
)
def test_observation_classification(
    process: bool, tcp: bool, marker: Any, expected: Any
) -> None:
    assert (
        wd.classify_observation(
            process_running=process, api_tcp_open=tcp, login_marker=marker
        )
        is expected
    )


def test_the_status_path_builds_no_round_trip_probe() -> None:
    """The CLI must not even construct the session-opening probe for status."""
    status_block = WATCHDOG_SOURCE.split("if args.status:", 1)[1].split(
        "    probes = Probes(", 1
    )[0]
    assert "_build_default_current_time_probe" not in status_block
    assert "StatusProbes(" in status_block
    # The adapter builder appears only under --deep-probe.
    assert "if args.deep_probe" in status_block


# =============================================================================
# WP2 — deep status uses exactly one session
# =============================================================================


class CountingSafetyProbe:
    """Stands in for the whole adapter: one call == one broker session."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.calls = 0
        self._report = report

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return dict(self._report)


DEEP_CLEAN = {
    "safety": "CLEAN",
    "reasons": ["ALL_CHECKS_PASSED"],
    "connect_ok": True,
    "api_round_trip_ok": True,
    "disconnect_ok": True,
    "working_orders": 0,
    "non_zero_positions": 0,
    "unresolved_states": 0,
    "reconciled": True,
    "blocking_findings": 0,
}


def _deep_report(probe: Any) -> Any:
    return wd.build_status_report(
        _status_probes(account_safety=probe),
        wd.StateStore(path=Path("unused")),
        policy=wd.Policy(),
        unit="ibgateway-ibc.service",
        now=1000.0,
        observed_at="2026-08-04T00:00:00+00:00",
        deep=True,
    )


def test_deep_status_opens_exactly_one_session() -> None:
    probe = CountingSafetyProbe(DEEP_CLEAN)
    report = _deep_report(probe)
    assert probe.calls == 1, "deep status must not open a second broker session"
    assert report.account_safety == "CLEAN"


def test_deep_status_derives_the_round_trip_from_that_same_session() -> None:
    """Not from a separate reqCurrentTime connection."""
    probe = CountingSafetyProbe(DEEP_CLEAN)
    report = _deep_report(probe)
    assert probe.calls == 1
    assert report.api_round_trip_ok is True
    assert report.state == wd.WatchdogState.API_READY.value
    assert report.proof_level == wd.ProofLevel.FULL_ACCOUNT_SNAPSHOT.value


def test_deep_status_reports_one_connect_and_one_disconnect() -> None:
    report = _deep_report(CountingSafetyProbe(DEEP_CLEAN))
    assert report.deep_probe is not None
    assert report.deep_probe["connect_ok"] is True
    assert report.deep_probe["disconnect_ok"] is True


def test_a_failed_deep_session_is_unknown_not_a_second_attempt() -> None:
    probe = CountingSafetyProbe(
        {"safety": "UNKNOWN", "reasons": ["CONNECT_FAILED"], "api_round_trip_ok": False}
    )
    report = _deep_report(probe)
    assert probe.calls == 1, "a failure must not trigger a retry session"
    assert report.account_safety == wd.SAFETY_UNKNOWN
    assert report.action_permitted is False


def test_deep_status_exposes_no_order_surface() -> None:
    from trading_execution_agent.account_safety import (
        FORBIDDEN_BROKER_VERBS,
        ReadOnlyBrokerFacade,
    )

    for verb in FORBIDDEN_BROKER_VERBS:
        assert not hasattr(ReadOnlyBrokerFacade, verb), verb
    for verb in ("submit_order", "cancel_order", "modify_order", "flatten"):
        assert verb not in WATCHDOG_SOURCE, verb


# =============================================================================
# WP3 — account-id containment
# =============================================================================


def test_the_watchdog_never_raises_the_root_log_level() -> None:
    """`basicConfig(level=INFO)` is what turned on ibapi INFO.

    Executable lines only: the replacement documents the removed call by
    name, and matching that comment would invert the test.
    """
    code = "\n".join(
        line
        for line in WATCHDOG_SOURCE.splitlines()
        if line.strip() and not line.strip().startswith(("#", '"', "*"))
    )
    # The docstring names the removed call to explain WHY it is gone; strip
    # that mention so only a real call site could match.
    code = code.replace("``logging.basicConfig(level=logging.INFO)``", "")
    assert "logging.basicConfig" not in code
    assert "configure_logging" in WATCHDOG_SOURCE


def test_configure_logging_pins_dependency_levels() -> None:
    wd.configure_logging()
    assert logging.getLogger().level == logging.WARNING
    for name in wd._DEPENDENCY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.parametrize(
    "text",
    [
        SENTINEL_ACCOUNT,
        f"managedAccounts: {SENTINEL_ACCOUNT}",
        f"account={SENTINEL_ACCOUNT} port=4002",
        "U1234567",
        "DU12345",
    ],
)
def test_account_shaped_tokens_are_redacted(text: str) -> None:
    scrubbed = wd.redact_account_ids(text)
    assert SENTINEL_ACCOUNT not in scrubbed
    assert "U1234567" not in scrubbed
    assert "DU12345" not in scrubbed
    assert wd.ACCOUNT_ID_PLACEHOLDER in scrubbed


def _capture_logging(emit: Any) -> str:
    """Run `emit` with the watchdog's own logging configuration installed."""
    stream = io.StringIO()
    wd.configure_logging()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.addFilter(wd.AccountIdRedactingFilter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)  # worst case: everything is emitted
    try:
        emit()
    finally:
        root.removeHandler(handler)
    return stream.getvalue()


def test_dependency_info_logging_cannot_expose_a_callback_payload() -> None:
    """Two independent barriers, tested separately.

    1. The level pin means an ibapi INFO record is never emitted at all.
    2. If a record DOES get through — at a level above the pin, or because a
       future edit lowers it — the filter still scrubs the payload.
    """
    dropped = _capture_logging(
        lambda: logging.getLogger("ibapi.wrapper").info(
            "managedAccounts: %s", SENTINEL_ACCOUNT
        )
    )
    assert SENTINEL_ACCOUNT not in dropped

    emitted = _capture_logging(
        lambda: logging.getLogger("ibapi.wrapper").error(
            "managedAccounts: %s", SENTINEL_ACCOUNT
        )
    )
    assert SENTINEL_ACCOUNT not in emitted
    assert wd.ACCOUNT_ID_PLACEHOLDER in emitted, "the record was emitted unscrubbed"


def test_exception_text_is_sanitized() -> None:
    def emit() -> None:
        try:
            raise RuntimeError(f"connect failed for {SENTINEL_ACCOUNT}")
        except RuntimeError:
            logging.getLogger("ibapi").exception("probe failed")

    output = _capture_logging(emit)
    assert SENTINEL_ACCOUNT not in output


def test_timeout_and_disconnect_failure_paths_stay_sanitized() -> None:
    for message in (
        f"timeout waiting for {SENTINEL_ACCOUNT}",
        f"disconnect failed: {SENTINEL_ACCOUNT} still attached",
        f"callback shape error: managedAccounts={SENTINEL_ACCOUNT},",
    ):
        output = _capture_logging(
            lambda m=message: logging.getLogger("ibapi.client").error(m)
        )
        assert SENTINEL_ACCOUNT not in output, message


def test_status_output_is_scrubbed_before_printing(capsys: Any, tmp_path: Path) -> None:
    """stdout is the last place an id could reach a terminal or evidence file."""
    wd.main(
        [
            "--status",
            "--json",
            "--process-pattern", "definitely-not-running-xyz",
            "--state-file", str(tmp_path / "s.json"),
            "--lock-file", str(tmp_path / "s.lock"),
            "--log-path", str(tmp_path / "absent.log"),
            "--reconciliation-marker", str(tmp_path / "absent.marker"),
        ]
    )
    captured = capsys.readouterr()
    assert SENTINEL_ACCOUNT not in captured.out
    assert SENTINEL_ACCOUNT not in captured.err
    assert "redact_account_ids(rendered)" in WATCHDOG_SOURCE


def test_the_report_model_carries_no_account_field() -> None:
    fields = set(wd.StatusReport.__dataclass_fields__)
    for forbidden in ("account_id", "account", "managed_accounts"):
        assert forbidden not in fields


# =============================================================================
# WP4 — the canonical watchdog runtime
# =============================================================================


def test_unit_working_directory_is_exact() -> None:
    assert f"\nWorkingDirectory={contract.REPO_ROOT}\n" in UNIT_TEXT


def test_unit_execstart_interpreter_is_exact() -> None:
    assert contract.VENV_PYTHON in UNIT_TEXT
    exec_line = next(
        line for line in UNIT_TEXT.splitlines() if line.startswith("ExecStart=")
    )
    assert exec_line == f"ExecStart={contract.VENV_PYTHON} \\"


def test_unit_execstart_script_is_exact() -> None:
    assert contract.WATCHDOG_SCRIPT in UNIT_TEXT


def test_no_unit_references_the_stale_opt_watchdog() -> None:
    for unit in (REPO / "systemd").glob("*.service"):
        text = unit.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        assert contract.STALE_WATCHDOG_PATH not in code, unit.name


def test_no_unit_resolves_python_through_path() -> None:
    for unit in (REPO / "systemd").glob("*.service"):
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart=") and "python" in line:
                assert "/usr/bin/env" not in line, unit.name


# =============================================================================
# WP6 — drop-in detection, by content and not by filename
# =============================================================================


def test_a_forbidden_override_is_detected_whatever_the_file_is_called() -> None:
    for name in ("zz-forking.conf", "aaa.conf", "harmless-sounding.conf"):
        dropin = contract.parse_dropin(
            f"/etc/systemd/system/x.d/{name}",
            "[Service]\nType=oneshot\nRemainAfterExit=yes\nKillMode=process\n",
        )
        findings = contract.classify_dropin(dropin)
        failed = {f.check.split(":")[-1] for f in findings if f.severity is contract.Severity.FAIL}
        assert {"Type", "RemainAfterExit", "KillMode"} <= failed, name


def test_the_observed_bringup_dropin_is_rejected() -> None:
    dropin = contract.parse_dropin(
        "/etc/systemd/system/x.d/zz-bringup.conf",
        "[Service]\nPrivateTmp=false\nProtectSystem=false\nProtectHome=false\nReadWritePaths=\n",
    )
    assert contract.contradicting_dropins([dropin]) == [dropin]


def test_a_display_only_dropin_is_allowed() -> None:
    dropin = contract.parse_dropin(
        "/etc/systemd/system/x.d/display.conf",
        "[Service]\nEnvironment=DISPLAY=:1\nEnvironment=XAUTHORITY=/home/opc/.Xauthority\n",
    )
    findings = contract.classify_dropin(dropin)
    assert all(f.severity is contract.Severity.OK for f in findings), findings
    assert contract.contradicting_dropins([dropin]) == []


def test_mixed_allowed_and_forbidden_overrides_fail() -> None:
    """An allowed setting does not launder the file it travels in."""
    dropin = contract.parse_dropin(
        "/etc/systemd/system/x.d/display.conf",
        "[Service]\nEnvironment=DISPLAY=:1\nType=oneshot\n",
    )
    assert contract.contradicting_dropins([dropin]) == [dropin]

    other_env = contract.parse_dropin(
        "/etc/systemd/system/x.d/display.conf",
        "[Service]\nEnvironment=DISPLAY=:1\nEnvironment=IB_ACCOUNT=DU1\n",
    )
    assert contract.contradicting_dropins([other_env]) == [other_env]


@pytest.mark.parametrize("prop", sorted(contract.SAFETY_CRITICAL_PROPERTIES))
def test_every_safety_critical_property_is_detected(prop: str) -> None:
    dropin = contract.parse_dropin("/x.d/any.conf", f"[Service]\n{prop}=whatever\n")
    assert contract.contradicting_dropins([dropin]) == [dropin], prop


# =============================================================================
# WP8 — ownership, entrypoint and exposure facts
# =============================================================================


def test_active_exited_with_no_main_pid_is_not_ownership() -> None:
    """The exact owner-host state: green dashboard, zero processes owned."""
    properties = {
        "Type": "oneshot",
        "RemainAfterExit": "yes",
        "ActiveState": "active",
        "SubState": "exited",
        "MainPID": "0",
    }
    finding = contract.check_process_ownership(
        properties, "/user.slice/user-1000.slice/session-2.scope"
    )
    assert finding.severity is contract.Severity.FAIL
    assert "owns no process" in finding.detail
    assert "session-2.scope" in finding.detail


def test_effective_properties_contradicting_the_contract_fail() -> None:
    findings = contract.check_effective_gateway(
        {"Type": "oneshot", "Restart": "no", "RemainAfterExit": "yes"}
    )
    assert contract.worst(findings) is contract.Severity.FAIL
    failed = {f.check for f in findings if f.severity is contract.Severity.FAIL}
    assert failed == {"effective:Type", "effective:RemainAfterExit"}


def test_a_correctly_owned_gateway_passes() -> None:
    finding = contract.check_process_ownership(
        {"ActiveState": "active", "SubState": "running", "MainPID": "4242"},
        "/system.slice/ibgateway-ibc.service",
    )
    assert finding.severity is contract.Severity.OK


def test_the_stale_entrypoint_is_a_failure() -> None:
    findings = contract.check_watchdog_entrypoint(
        {
            "WorkingDirectory": "",
            "ExecStart": (
                "{ path=/usr/bin/env ; argv[]=/usr/bin/env python3 "
                f"{contract.STALE_WATCHDOG_PATH} --host 127.0.0.1 ; }}"
            ),
        }
    )
    failed = {f.check for f in findings if f.severity is contract.Severity.FAIL}
    assert failed == {
        "watchdog_working_directory",
        "watchdog_interpreter",
        "watchdog_script",
        "watchdog_not_stale_copy",
        "watchdog_no_env_interpreter",
    }


def test_the_canonical_entrypoint_passes() -> None:
    findings = contract.check_watchdog_entrypoint(
        {
            "WorkingDirectory": contract.REPO_ROOT,
            "ExecStart": (
                f"{{ path={contract.VENV_PYTHON} ; argv[]={contract.VENV_PYTHON} "
                f"{contract.WATCHDOG_SCRIPT} --host 127.0.0.1 ; }}"
            ),
        }
    )
    assert contract.worst(findings) is contract.Severity.OK


def test_unknown_never_masks_a_failure() -> None:
    findings = [
        contract.Finding("a", contract.Severity.UNKNOWN, ""),
        contract.Finding("b", contract.Severity.FAIL, ""),
    ]
    assert contract.worst(findings) is contract.Severity.FAIL


def test_the_audit_separates_binding_from_ingress_and_keeps_oci_unknown() -> None:
    source = (SCRIPTS / "host_operations_audit.py").read_text(encoding="utf-8")
    assert "binding_vs_ingress" in source
    assert "three" in source and "separate facts" in source
    assert "oci_network_security" in source
    # nftables: no privilege is not the same fact as no rules.
    assert "not readable without privilege" in source
    assert "NOT absence of rules" in source


def test_the_audit_never_prints_the_process_command_line() -> None:
    source = (SCRIPTS / "host_operations_audit.py").read_text(encoding="utf-8")
    assert "_gateway_process_cgroup" in source
    assert "never returns or logs the process command line" in source.lower()
    for forbidden in ("ps -o args", "ps aux", "/proc/{pid}/cmdline", "cmdline"):
        assert forbidden not in source, forbidden


# =============================================================================
# WP7 — the remediation tool
# =============================================================================


def _remediate(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    sysroot = tmp_path / "sysroot"
    (sysroot / "etc/systemd/system/ibgateway-ibc.service.d").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OPS_REMEDIATE_TEST_MODE="1")
    return subprocess.run(  # noqa: S603
        ["bash", str(REMEDIATE), f"--sysroot={sysroot.as_posix()}", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )


@BASH_ONLY
def test_remediation_defaults_to_check_and_changes_nothing(tmp_path: Path) -> None:
    sysroot = tmp_path / "sysroot"
    result = _remediate(tmp_path)
    assert "MODE: CHECK" in result.stdout.upper()
    assert "CHECK ONLY — nothing was changed" in result.stdout
    # The scratch tree is untouched apart from the dirs the fixture created.
    created = sorted(p.name for p in (sysroot / "etc/systemd/system").rglob("*"))
    assert created == ["ibgateway-ibc.service.d"]


@BASH_ONLY
def test_remediation_never_invokes_a_process_action(tmp_path: Path) -> None:
    """No start/stop/restart/relogin/enable exists anywhere in the tool."""
    source = REMEDIATE.read_text(encoding="utf-8")
    code = "\n".join(
        line
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    for forbidden in (
        "systemctl start",
        "systemctl stop",
        "systemctl restart",
        "systemctl enable",
        "systemctl kill",
        "--now",
        "pkill",
        "kill -",
    ):
        assert forbidden not in code, forbidden


@BASH_ONLY
def test_remediation_check_is_read_only(tmp_path: Path) -> None:
    result = _remediate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "NEVER started, stopped, restarted or relogged in" in result.stdout


@BASH_ONLY
def test_remediation_is_idempotent(tmp_path: Path) -> None:
    first = _remediate(tmp_path)
    second = _remediate(tmp_path)
    assert first.returncode == second.returncode
    # Same findings, same verdict, on an unchanged tree.
    assert _verdict(first.stdout) == _verdict(second.stdout)


def _verdict(text: str) -> str:
    for line in text.splitlines():
        if "worst finding:" in line:
            return line.strip()
    return ""


@BASH_ONLY
def test_remediation_preserves_the_gateway_identity(tmp_path: Path) -> None:
    sysroot = tmp_path / "sysroot"
    sysroot.mkdir(parents=True, exist_ok=True)
    (sysroot / "gateway.pid").write_text("4242\n", encoding="utf-8")
    (sysroot / "gateway.start").write_text("Mon Aug 4 00:00:00 2026\n", encoding="utf-8")
    result = _remediate(tmp_path, "--verify")
    assert "gateway_untouched" in result.stdout
    assert "PID and start time unchanged" in result.stdout


@BASH_ONLY
def test_remediation_sysroot_is_test_only(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("OPS_REMEDIATE_TEST_MODE", None)
    result = subprocess.run(  # noqa: S603
        ["bash", str(REMEDIATE), "--sysroot=/tmp/x"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "test-only" in result.stderr


def test_remediation_never_prints_sensitive_operational_data() -> None:
    source = REMEDIATE.read_text(encoding="utf-8")
    code = "\n".join(
        line
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    # No java argv, no IBC config, no account id, no credential.
    for forbidden in ("config.ini", "IbLoginId", "IbPassword", "ps -o args", "cmdline"):
        assert forbidden not in code, forbidden
    assert "never prints Java command-line arguments" in source


def test_remediation_declares_the_full_prohibition_list() -> None:
    source = REMEDIATE.read_text(encoding="utf-8")
    for promise in (
        "never starts, stops, restarts or relogins",
        "never enables the watchdog timer",
        "never touches firewall",
        "never touches SSH or VNC",
    ):
        assert promise in source, promise
