"""WP2/WP3: the account-safety gate and the read-only CLI (fakes only).

These are the tests that matter most in this bundle. The watchdog is the only
component in the repository that can act on the Gateway without a human, so
every one of these asserts a refusal: what must NOT happen, and under exactly
which conditions.

Nothing here starts a process, opens a broker socket, reads a credential or
runs systemctl.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "ib_gateway_watchdog_gate", REPO_ROOT / "scripts" / "ib_gateway_watchdog.py"
)
assert _SPEC is not None and _SPEC.loader is not None
wd = importlib.util.module_from_spec(_SPEC)
sys.modules["ib_gateway_watchdog_gate"] = wd
_SPEC.loader.exec_module(wd)

WATCHDOG_SOURCE = (REPO_ROOT / "scripts" / "ib_gateway_watchdog.py").read_text(
    encoding="utf-8"
)


class ExplodingLauncher:
    """Any call is a test failure: these paths must never reach a launcher."""

    def start(self) -> None:
        raise AssertionError("launcher.start() must not be called")

    def restart(self) -> None:
        raise AssertionError("launcher.restart() must not be called")

    def relogin(self) -> None:
        raise AssertionError("launcher.relogin() must not be called")


class RecordingLauncher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def restart(self) -> None:
        self.calls.append("restart")

    def relogin(self) -> None:
        self.calls.append("relogin")


def safety(value: str, **extra: Any) -> dict[str, Any]:
    return {"safety": value, "reasons": ["TEST"], **extra}


def probes(
    *,
    process: bool = True,
    tcp: bool = True,
    current_time: bool = True,
    login: Any = None,
    inhibited: bool = False,
    account: Any = None,
) -> Any:
    marker = login if login is not None else wd.LoginMarker.LOGGED_IN
    account_probe = account if account is not None else (lambda: safety(wd.SAFETY_CLEAN))
    return wd.Probes(
        process=lambda: process,
        tcp=lambda: tcp,
        current_time=lambda: current_time,
        login=lambda: marker,
        restart_inhibited=lambda: inhibited,
        account_safety=account_probe,
    )


def store(tmp_path: Path, **kwargs: Any) -> Any:
    s = wd.StateStore(path=tmp_path / "state.json")
    s.restart_times = list(kwargs.get("restart_times", []))
    s.relogin_times = list(kwargs.get("relogin_times", []))
    return s


# -- the gate itself (pure) ---------------------------------------------------


@pytest.mark.parametrize("action", [wd.Action.START, wd.Action.RESTART, wd.Action.RELOGIN])
def test_unknown_never_permits_a_process_action(action: Any) -> None:
    plan = wd.ActionPlan(wd.WatchdogState.PROCESS_DOWN, action, True, "policy said so", 10)
    gated = wd.apply_account_safety_gate(plan, wd.SAFETY_UNKNOWN)
    assert gated.action is wd.Action.ALERT
    assert gated.exit_code == wd.ExitCode.ACCOUNT_UNKNOWN
    assert "UNKNOWN" in gated.reason


@pytest.mark.parametrize("action", [wd.Action.START, wd.Action.RESTART, wd.Action.RELOGIN])
def test_not_clean_never_permits_a_process_action(action: Any) -> None:
    plan = wd.ActionPlan(wd.WatchdogState.PROCESS_DOWN, action, True, "policy said so", 10)
    gated = wd.apply_account_safety_gate(plan, wd.SAFETY_NOT_CLEAN)
    assert gated.action is wd.Action.WAIT
    assert gated.exit_code == wd.ExitCode.ACCOUNT_NOT_CLEAN
    assert "NOT_CLEAN" in gated.reason


@pytest.mark.parametrize("action", [wd.Action.START, wd.Action.RESTART, wd.Action.RELOGIN])
def test_clean_permits_the_policy_decision_to_stand(action: Any) -> None:
    plan = wd.ActionPlan(wd.WatchdogState.PROCESS_DOWN, action, True, "policy", 10)
    assert wd.apply_account_safety_gate(plan, wd.SAFETY_CLEAN) is plan


@pytest.mark.parametrize("action", [wd.Action.NONE, wd.Action.WAIT, wd.Action.ALERT])
def test_non_mutating_actions_pass_through_untouched(action: Any) -> None:
    plan = wd.ActionPlan(wd.WatchdogState.API_READY, action, False, "r", 0)
    for value in (wd.SAFETY_CLEAN, wd.SAFETY_NOT_CLEAN, wd.SAFETY_UNKNOWN):
        assert wd.apply_account_safety_gate(plan, value) is plan


def test_only_clean_is_a_permitting_value() -> None:
    assert wd.SAFETY_PERMITTING == frozenset({wd.SAFETY_CLEAN})
    for value in (wd.SAFETY_NOT_CLEAN, wd.SAFETY_UNKNOWN, "", "clean", "MAYBE"):
        assert value not in wd.SAFETY_PERMITTING


def test_an_unrecognised_safety_value_is_treated_as_unknown() -> None:
    """A typo or a future value must fail closed, not open."""
    plan = wd.ActionPlan(wd.WatchdogState.PROCESS_DOWN, wd.Action.START, True, "p", 10)
    gated = wd.apply_account_safety_gate(plan, "PROBABLY_FINE")
    assert gated.action is wd.Action.ALERT
    assert gated.exit_code == wd.ExitCode.ACCOUNT_UNKNOWN


# -- probeability -------------------------------------------------------------


def test_only_api_ready_can_even_attempt_the_proof() -> None:
    assert wd.probe_is_possible(wd.WatchdogState.API_READY) is True
    for state in (
        wd.WatchdogState.PROCESS_DOWN,
        wd.WatchdogState.API_DOWN,
        wd.WatchdogState.LOGIN_REQUIRED,
        wd.WatchdogState.TWO_FA_PENDING,
        wd.WatchdogState.UNKNOWN,
    ):
        assert wd.probe_is_possible(state) is False, state


# -- run_cycle: nothing is ever launched under a failed proof -----------------


@pytest.mark.parametrize(
    ("kwargs", "state"),
    [
        ({"process": False}, wd.WatchdogState.PROCESS_DOWN),
        (
            {"tcp": True, "current_time": False, "login": wd.LoginMarker.LOGGED_IN},
            wd.WatchdogState.API_DOWN,
        ),
        (
            {"tcp": False, "current_time": False, "login": wd.LoginMarker.LOGIN_FAILED},
            wd.WatchdogState.LOGIN_REQUIRED,
        ),
        (
            {"tcp": False, "current_time": False, "login": wd.LoginMarker.TWO_FA_TIMEOUT},
            wd.WatchdogState.TWO_FA_PENDING,
        ),
    ],
)
def test_no_restart_triggering_state_can_reach_the_launcher(
    tmp_path: Path, kwargs: dict[str, Any], state: Any
) -> None:
    """PROCESS_DOWN / API_DOWN / login failure / 2FA timeout are all UNKNOWN.

    Even with a probe that would answer CLEAN, these states cannot query the
    broker, so the proof is never attempted and the gate refuses.
    """
    plan = wd.run_cycle(
        probes(account=lambda: safety(wd.SAFETY_CLEAN), **kwargs),
        ExplodingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert plan.state is state
    assert plan.action not in wd.MUTATING_ACTIONS
    assert plan.exit_code in (wd.ExitCode.ACCOUNT_UNKNOWN, wd.ExitCode.TWO_FA_WAITING)


def test_a_working_order_blocks_unattended_action(tmp_path: Path) -> None:
    plan = wd.apply_account_safety_gate(
        wd.ActionPlan(wd.WatchdogState.API_DOWN, wd.Action.RESTART, True, "p", 12),
        wd.SAFETY_NOT_CLEAN,
    )
    assert plan.action is wd.Action.WAIT
    # and end-to-end, with the probe reporting a working order:
    result = wd.run_cycle(
        probes(account=lambda: safety(wd.SAFETY_NOT_CLEAN, working_orders=1)),
        ExplodingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert result.action is wd.Action.NONE  # API_READY needs nothing anyway


def test_the_probe_is_not_even_called_when_no_action_is_indicated(
    tmp_path: Path,
) -> None:
    """No broker connection is opened just to observe a healthy Gateway."""
    calls: list[int] = []

    def counting() -> dict[str, Any]:
        calls.append(1)
        return safety(wd.SAFETY_CLEAN)

    wd.run_cycle(
        probes(account=counting),
        RecordingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert calls == []


def test_a_probe_that_raises_becomes_unknown(tmp_path: Path) -> None:
    def boom() -> dict[str, Any]:
        raise OSError("gateway went away mid-probe")

    plan = wd.run_cycle(
        probes(process=False, account=boom),
        ExplodingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert plan.action is wd.Action.ALERT
    assert plan.exit_code == wd.ExitCode.ACCOUNT_UNKNOWN


def test_the_default_account_probe_is_fail_closed() -> None:
    """A caller that forgets to wire the probe must not unlock anything."""
    bundle = wd.Probes(
        process=lambda: True,
        tcp=lambda: True,
        current_time=lambda: True,
        login=lambda: wd.LoginMarker.LOGGED_IN,
        restart_inhibited=lambda: False,
    )
    assert bundle.account_safety()["safety"] == wd.SAFETY_UNKNOWN


def test_restart_budget_cannot_bypass_the_gate(tmp_path: Path) -> None:
    """A fully available budget is not a proof about the account."""
    plan = wd.run_cycle(
        probes(process=False),
        ExplodingLauncher(),
        store(tmp_path),  # zero restarts used: maximum budget
        policy=wd.Policy(max_restarts=99),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert plan.action is wd.Action.ALERT
    assert plan.exit_code == wd.ExitCode.ACCOUNT_UNKNOWN


def test_the_lifecycle_marker_always_inhibits_process_action(tmp_path: Path) -> None:
    """Necessary but not sufficient: present => inhibit, absent => still gated."""
    inhibited = wd.run_cycle(
        probes(process=False, inhibited=True),
        ExplodingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert inhibited.action is wd.Action.WAIT
    assert inhibited.exit_code == wd.ExitCode.RESTART_INHIBITED

    # Absent marker does NOT imply safe — the account gate still refuses.
    absent = wd.run_cycle(
        probes(process=False, inhibited=False),
        ExplodingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert absent.action is wd.Action.ALERT


def test_tcp_open_alone_never_proves_clean(tmp_path: Path) -> None:
    """A listening socket says nothing about the account."""
    plan = wd.run_cycle(
        probes(tcp=True, current_time=False, login=wd.LoginMarker.LOGGED_IN),
        ExplodingLauncher(),
        store(tmp_path),
        policy=wd.Policy(),
        now=1000.0,
        alert=lambda _p: None,
    )
    assert plan.state is wd.WatchdogState.API_DOWN
    assert plan.action is wd.Action.ALERT


def test_req_current_time_alone_never_proves_clean() -> None:
    """API_READY means the API answered — not that the account is clean."""
    report = wd.build_status_report(
        probes(),  # process + tcp + current_time all true
        wd.StateStore(path=Path("unused")),
        policy=wd.Policy(),
        unit="ibgateway-ibc.service",
        now=1000.0,
        observed_at="2026-08-03T12:00:00+00:00",
        deep=False,
    )
    assert report.state == "API_READY"
    assert report.proof_level == wd.ProofLevel.API_ROUND_TRIP.value
    assert report.account_safety == wd.SAFETY_UNKNOWN
    assert report.action_permitted is False


# -- read-only status CLI -----------------------------------------------------


def test_status_never_calls_the_launcher(tmp_path: Path, capsys: Any) -> None:
    exit_code = wd.main(
        [
            "--status",
            "--process-pattern", "definitely-not-running-xyz",
            "--state-file", str(tmp_path / "s.json"),
            "--lock-file", str(tmp_path / "s.lock"),
            "--log-path", str(tmp_path / "absent.log"),
            "--reconciliation-marker", str(tmp_path / "absent.marker"),
        ]
    )
    assert exit_code == wd.ExitCode.OK
    out = capsys.readouterr().out
    assert "read-only" in out
    assert "action_permitted" in out.replace(" ", "_").lower() or "action_permitted" in out
    # No lock and no state file were created: nothing was written.
    assert not (tmp_path / "s.lock").exists()


def test_status_json_is_deterministic_and_structured(tmp_path: Path, capsys: Any) -> None:
    wd.main(
        [
            "--status", "--json",
            "--process-pattern", "definitely-not-running-xyz",
            "--state-file", str(tmp_path / "s.json"),
            "--lock-file", str(tmp_path / "s.lock"),
            "--log-path", str(tmp_path / "absent.log"),
            "--reconciliation-marker", str(tmp_path / "absent.marker"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    for key in (
        "process_running", "api_tcp_open", "login_marker", "lifecycle_marker_present",
        "configured_unit", "restart_budget", "action_permitted", "proof_level",
        "account_safety", "state",
    ):
        assert key in payload, key
    assert payload["action_permitted"] is False
    assert payload["account_safety"] == wd.SAFETY_UNKNOWN
    assert set(payload["restart_budget"]) == {
        "restarts_used", "max_restarts", "relogins_used", "max_relogins"
    }


def test_status_report_never_constructs_a_launcher(monkeypatch: Any, tmp_path: Path) -> None:
    """Structural: SystemdLauncher must not even be instantiated on --status."""

    def explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("no launcher may be constructed on the status path")

    monkeypatch.setattr(wd, "SystemdLauncher", explode)
    assert (
        wd.main(
            [
                "--status",
                "--process-pattern", "definitely-not-running-xyz",
                "--state-file", str(tmp_path / "s.json"),
                "--lock-file", str(tmp_path / "s.lock"),
                "--log-path", str(tmp_path / "absent.log"),
                "--reconciliation-marker", str(tmp_path / "absent.marker"),
            ]
        )
        == wd.ExitCode.OK
    )


def test_deep_probe_status_reports_the_classification(tmp_path: Path) -> None:
    report = wd.build_status_report(
        probes(
            account=lambda: {
                "safety": wd.SAFETY_NOT_CLEAN,
                "reasons": ["WORKING_ORDER_PRESENT"],
                "working_orders": 1,
                "non_zero_positions": 0,
                "unresolved_states": 0,
                "reconciled": False,
                "connect_ok": True,
                "disconnect_ok": True,
            }
        ),
        wd.StateStore(path=tmp_path / "s.json"),
        policy=wd.Policy(),
        unit="ibgateway-ibc.service",
        now=1000.0,
        observed_at="2026-08-03T12:00:00+00:00",
        deep=True,
    )
    assert report.account_safety == wd.SAFETY_NOT_CLEAN
    assert report.proof_level == wd.ProofLevel.FULL_ACCOUNT_SNAPSHOT.value
    assert report.deep_probe is not None
    assert report.deep_probe["working_orders"] == 1
    assert "WORKING_ORDER_PRESENT" in report.deep_probe["reasons"]
    assert report.action_permitted is False
    rendered = report.render()
    assert "NOT_CLEAN" in rendered
    assert "read-only" in rendered


def test_deep_probe_failure_degrades_to_unknown(tmp_path: Path) -> None:
    def boom() -> dict[str, Any]:
        raise TimeoutError("no answer")

    report = wd.build_status_report(
        probes(account=boom),
        wd.StateStore(path=tmp_path / "s.json"),
        policy=wd.Policy(),
        unit="u",
        now=1000.0,
        observed_at="t",
        deep=True,
    )
    assert report.account_safety == wd.SAFETY_UNKNOWN
    assert report.deep_probe is not None
    assert report.deep_probe["reasons"] == ["PROBE_EXCEPTION"]
    # A failed probe cannot claim the highest proof level.
    assert report.proof_level != wd.ProofLevel.FULL_ACCOUNT_SNAPSHOT.value


def test_proof_levels_are_ordered_by_what_they_actually_prove() -> None:
    lvl = wd._proof_level
    assert lvl(process=False, tcp=False, round_trip=False, deep=False) is wd.ProofLevel.NONE
    assert lvl(process=True, tcp=False, round_trip=False, deep=False) is wd.ProofLevel.PROCESS_ONLY
    assert lvl(process=True, tcp=True, round_trip=False, deep=False) is wd.ProofLevel.TCP_OPEN
    assert lvl(process=True, tcp=True, round_trip=True, deep=False) is wd.ProofLevel.API_ROUND_TRIP
    assert (
        lvl(process=True, tcp=True, round_trip=True, deep=True)
        is wd.ProofLevel.FULL_ACCOUNT_SNAPSHOT
    )


# -- sanitization and file permissions ---------------------------------------


def test_status_output_contains_no_account_or_credential(tmp_path: Path, capsys: Any) -> None:
    wd.main(
        [
            "--status", "--json",
            "--account-alias", "paper-poi",
            "--process-pattern", "definitely-not-running-xyz",
            "--state-file", str(tmp_path / "s.json"),
            "--lock-file", str(tmp_path / "s.lock"),
            "--log-path", str(tmp_path / "absent.log"),
            "--reconciliation-marker", str(tmp_path / "absent.marker"),
        ]
    )
    out = capsys.readouterr().out.lower()
    for fragment in ("password", "iblogin", "ibpassword", "du1", "token", "secret"):
        assert fragment not in out, fragment


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_state_and_lock_files_use_restrictive_permissions(tmp_path: Path) -> None:
    lock_path = tmp_path / "watchdog.lock"
    with wd.FileLock(lock_path):
        mode = stat.S_IMODE(lock_path.stat().st_mode)
        assert mode == 0o600, f"lock mode {mode:04o} must be 0600"


def test_the_lock_is_exclusive(tmp_path: Path) -> None:
    lock_path = tmp_path / "watchdog.lock"
    with wd.FileLock(lock_path):
        with pytest.raises(wd.LockBusyError):
            with wd.FileLock(lock_path):
                pass  # pragma: no cover


# -- structural guarantees ----------------------------------------------------


def test_the_watchdog_has_no_order_surface_at_all() -> None:
    for verb in (
        "submit_order", "cancel_order", "modify_order", "place_order", "flatten",
        "reqGlobalCancel", "OrderIntent", "SubmitAuthorization",
    ):
        assert verb not in WATCHDOG_SOURCE, verb


def test_the_watchdog_never_reads_the_environment_or_credentials() -> None:
    for fragment in ("os.environ", "getpass", "config.ini", "IbPassword", "IbLoginId"):
        assert fragment not in WATCHDOG_SOURCE, fragment


def test_there_is_exactly_one_canonical_gateway_unit() -> None:
    assert WATCHDOG_SOURCE.count('default="ibgateway-ibc.service"') == 1
    units = sorted(p.name for p in (REPO_ROOT / "systemd").glob("*.service"))
    assert units == ["ibgateway-ibc.service", "ibgateway-watchdog.service"]


def test_the_mutating_action_set_is_exactly_the_process_actions() -> None:
    assert wd.MUTATING_ACTIONS == frozenset(
        {wd.Action.START, wd.Action.RESTART, wd.Action.RELOGIN}
    )
