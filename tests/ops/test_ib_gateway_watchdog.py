"""IB Gateway watchdog drills (process/socket/session fakes only; no IBKR).

Covers the pure classification/decision core, the run-cycle side effects
(start/restart/relogin with budgets), the lock, the reconciliation gate,
and the safety properties of the shell scripts / systemd units / config.
Nothing here starts a process, opens a broker socket or reads credentials.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "ib_gateway_watchdog", REPO_ROOT / "scripts" / "ib_gateway_watchdog.py"
)
assert _SPEC is not None and _SPEC.loader is not None
wd = importlib.util.module_from_spec(_SPEC)
sys.modules["ib_gateway_watchdog"] = wd
_SPEC.loader.exec_module(wd)


# -- fakes -----------------------------------------------------------------------


class FakeLauncher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def restart(self) -> None:
        self.calls.append("restart")

    def relogin(self) -> None:
        self.calls.append("relogin")


def probes(
    *,
    process: bool = True,
    tcp: bool = True,
    current_time: bool = True,
    login: Any = None,
    inhibited: bool = False,
) -> Any:
    marker = login if login is not None else wd.LoginMarker.LOGGED_IN
    return wd.Probes(
        process=lambda: process,
        tcp=lambda: tcp,
        current_time=lambda: current_time,
        login=lambda: marker,
        restart_inhibited=lambda: inhibited,
    )


def make_store(tmp_path: Path, **kwargs: Any) -> Any:
    store = wd.StateStore(path=tmp_path / "state.json")
    store.restart_times = list(kwargs.get("restart_times", []))
    store.relogin_times = list(kwargs.get("relogin_times", []))
    return store


def cycle(tmp_path: Path, probe: Any, launcher: Any, *, now: float = 1000.0, **pkw: Any) -> Any:
    policy = wd.Policy(**pkw) if pkw else wd.Policy()
    store = make_store(tmp_path)
    alerts: list[Any] = []
    plan = wd.run_cycle(
        probe, launcher, store, policy=policy, now=now, alert=alerts.append
    )
    return plan, store, alerts


# -- parse_login_marker ----------------------------------------------------------


def test_parse_login_marker_precedence() -> None:
    assert wd.parse_login_marker("Second Factor Authentication timed out") is (
        wd.LoginMarker.TWO_FA_TIMEOUT
    )
    assert wd.parse_login_marker("Please complete Second Factor Authentication") is (
        wd.LoginMarker.TWO_FA_PROMPT
    )
    assert wd.parse_login_marker("Login failed - invalid") is wd.LoginMarker.LOGIN_FAILED
    assert wd.parse_login_marker("Login has completed") is wd.LoginMarker.LOGGED_IN
    assert wd.parse_login_marker("just booting") is wd.LoginMarker.NONE


# -- classify_state --------------------------------------------------------------


def test_classify_all_states() -> None:
    S = wd.Signals
    M = wd.LoginMarker
    St = wd.WatchdogState
    assert wd.classify_state(S(False, False, False, M.NONE)) is St.PROCESS_DOWN
    assert wd.classify_state(S(True, False, False, M.TWO_FA_PROMPT)) is St.TWO_FA_PENDING
    assert wd.classify_state(S(True, False, False, M.TWO_FA_TIMEOUT)) is St.TWO_FA_PENDING
    assert wd.classify_state(S(True, False, False, M.LOGIN_FAILED)) is St.LOGIN_REQUIRED
    assert wd.classify_state(S(True, True, True, M.LOGGED_IN)) is St.API_READY
    assert wd.classify_state(S(True, True, False, M.LOGGED_IN)) is St.API_DOWN
    assert wd.classify_state(S(True, False, False, M.NONE)) is St.LOGIN_REQUIRED
    # port open, reqCurrentTime fails, no login evidence -> inconsistent
    assert wd.classify_state(S(True, True, False, M.NONE)) is St.UNKNOWN


# -- run_cycle: healthy / start / restart ----------------------------------------


def test_api_ready_takes_no_action(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, store, alerts = cycle(tmp_path, probes(), launcher)
    assert plan.state is wd.WatchdogState.API_READY
    assert plan.action is wd.Action.NONE
    assert plan.exit_code == wd.ExitCode.OK
    assert launcher.calls == []
    assert alerts == []


def test_process_down_starts_within_budget(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, store, _ = cycle(tmp_path, probes(process=False), launcher)
    assert plan.state is wd.WatchdogState.PROCESS_DOWN
    assert plan.action is wd.Action.START
    assert plan.exit_code == wd.ExitCode.STARTED
    assert launcher.calls == ["start"]
    assert len(store.restart_times) == 1  # recorded


def test_api_down_does_single_restart(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, store, _ = cycle(
        tmp_path, probes(tcp=True, current_time=False, login=wd.LoginMarker.LOGGED_IN), launcher
    )
    assert plan.state is wd.WatchdogState.API_DOWN
    assert plan.action is wd.Action.RESTART
    assert plan.exit_code == wd.ExitCode.RESTARTED
    assert launcher.calls == ["restart"]


def test_restart_budget_exhausted_fails_closed(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    policy = wd.Policy(max_restarts=2, restart_window_seconds=1000.0)
    store = make_store(tmp_path, restart_times=[600.0, 800.0])  # 2 within window
    plan = wd.run_cycle(
        probes(process=False), launcher, store, policy=policy, now=1000.0, alert=lambda _p: None
    )
    assert plan.action is wd.Action.ALERT
    assert plan.exit_code == wd.ExitCode.RESTART_LIMIT
    assert launcher.calls == []  # no restart loop


def test_old_restarts_outside_window_are_pruned(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    policy = wd.Policy(max_restarts=1, restart_window_seconds=100.0)
    store = make_store(tmp_path, restart_times=[10.0])  # far outside the window
    plan = wd.run_cycle(
        probes(process=False), launcher, store, policy=policy, now=1000.0, alert=lambda _p: None
    )
    assert plan.action is wd.Action.START  # old restart pruned, budget available
    assert launcher.calls == ["start"]


# -- 2FA handling ----------------------------------------------------------------


def test_two_fa_prompt_waits_and_alerts_but_never_restarts(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, _, alerts = cycle(
        tmp_path, probes(tcp=False, current_time=False, login=wd.LoginMarker.TWO_FA_PROMPT),
        launcher,
    )
    assert plan.state is wd.WatchdogState.TWO_FA_PENDING
    assert plan.action is wd.Action.WAIT
    assert plan.exit_code == wd.ExitCode.TWO_FA_WAITING  # not a failure
    assert launcher.calls == []  # never an immediate restart
    assert len(alerts) == 1


def test_two_fa_timeout_does_rate_limited_relogin(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, store, _ = cycle(
        tmp_path,
        probes(tcp=False, current_time=False, login=wd.LoginMarker.TWO_FA_TIMEOUT),
        launcher,
    )
    assert plan.action is wd.Action.RELOGIN
    assert plan.exit_code == wd.ExitCode.RELOGIN
    assert launcher.calls == ["relogin"]
    assert len(store.relogin_times) == 1


def test_two_fa_relogin_budget_exhausted(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    policy = wd.Policy(max_relogins=1, relogin_window_seconds=1000.0)
    store = make_store(tmp_path, relogin_times=[900.0])
    plan = wd.run_cycle(
        probes(tcp=False, current_time=False, login=wd.LoginMarker.TWO_FA_TIMEOUT),
        launcher, store, policy=policy, now=1000.0, alert=lambda _p: None,
    )
    assert plan.action is wd.Action.ALERT
    assert plan.exit_code == wd.ExitCode.RESTART_LIMIT
    assert launcher.calls == []


# -- login failures --------------------------------------------------------------


def test_login_failed_relogins_then_hits_limit(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, _, _ = cycle(
        tmp_path, probes(tcp=False, current_time=False, login=wd.LoginMarker.LOGIN_FAILED),
        launcher,
    )
    assert plan.action is wd.Action.RELOGIN
    policy = wd.Policy(max_relogins=1, relogin_window_seconds=1000.0)
    store = make_store(tmp_path, relogin_times=[950.0])
    plan2 = wd.run_cycle(
        probes(tcp=False, current_time=False, login=wd.LoginMarker.LOGIN_FAILED),
        launcher, store, policy=policy, now=1000.0, alert=lambda _p: None,
    )
    assert plan2.action is wd.Action.ALERT
    assert plan2.exit_code == wd.ExitCode.LOGIN_FAIL_LIMIT


def test_login_in_progress_is_benign_wait(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, _, alerts = cycle(
        tmp_path, probes(tcp=False, current_time=False, login=wd.LoginMarker.NONE), launcher
    )
    assert plan.state is wd.WatchdogState.LOGIN_REQUIRED
    assert plan.action is wd.Action.WAIT
    assert plan.exit_code == wd.ExitCode.OK
    assert alerts == []  # nothing wrong yet


# -- UNKNOWN ---------------------------------------------------------------------


def test_unknown_state_fails_closed(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, _, alerts = cycle(
        tmp_path, probes(tcp=True, current_time=False, login=wd.LoginMarker.NONE), launcher
    )
    assert plan.state is wd.WatchdogState.UNKNOWN
    assert plan.exit_code == wd.ExitCode.UNKNOWN_STATE
    assert launcher.calls == []
    assert len(alerts) == 1


# -- reconciliation gate ---------------------------------------------------------


def test_reconciliation_gate_inhibits_restart(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, _, _ = cycle(tmp_path, probes(process=False, inhibited=True), launcher)
    assert plan.action is wd.Action.WAIT
    assert plan.exit_code == wd.ExitCode.RESTART_INHIBITED
    assert launcher.calls == []  # never kill/restart during a gated lifecycle


def test_reconciliation_gate_inhibits_api_down_restart(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    plan, _, _ = cycle(
        tmp_path,
        probes(tcp=True, current_time=False, login=wd.LoginMarker.LOGGED_IN, inhibited=True),
        launcher,
    )
    assert plan.action is wd.Action.WAIT
    assert launcher.calls == []


# -- lock ------------------------------------------------------------------------


def test_file_lock_prevents_two_holders(tmp_path: Path) -> None:
    lock_path = tmp_path / "wd.lock"
    with wd.FileLock(lock_path):
        with pytest.raises(wd.LockBusyError):
            with wd.FileLock(lock_path):
                pass
    # released after the context exits -> can acquire again
    with wd.FileLock(lock_path):
        pass


# -- no order / credential surface -----------------------------------------------


def test_launcher_has_no_order_surface() -> None:
    launcher = wd.SystemdLauncher()
    for surface in ("place_order", "cancel_order", "submit", "flatten", "order"):
        assert not hasattr(launcher, surface)


def test_watchdog_source_has_no_orders_credentials_or_2fa_bypass() -> None:
    assert wd.__file__ is not None
    source = Path(wd.__file__).read_text(encoding="utf-8")
    # no order/broker surface, and no credential HANDLING (reading/prompting)
    for fragment in (
        "place" + "Order", "cancel" + "Order", "reqGlobal" + "Cancel",
        "OrderIntent", "SubmitAuthorization", "IbPassword", "IbLoginId",
        "getpass", "input(", "os.environ",
    ):
        assert fragment not in source, fragment
    # the watchdog never reads the IBC config / credentials file
    assert "config.ini" not in source


def test_alert_line_contains_no_secret_fields(tmp_path: Path, caplog: Any) -> None:
    import logging

    launcher = FakeLauncher()
    with caplog.at_level(logging.WARNING):
        wd.run_cycle(
            probes(process=False), launcher, make_store(tmp_path),
            policy=wd.Policy(), now=1000.0,
        )
    text = "\n".join(record.getMessage() for record in caplog.records)
    for fragment in ("password", "IbPassword", "IbLoginId"):
        assert fragment not in text
