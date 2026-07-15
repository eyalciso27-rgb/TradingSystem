"""IB Gateway (Paper) watchdog — health/restart supervision for IBC.

Runs one cycle per invocation (driven by a systemd timer). It observes the
Gateway/IBC process, the API port (4002) and the IBC log, classifies a single
state, and takes at most one controlled action:

    PROCESS_DOWN  -> start via IBC (rate-limited, lock-guarded)
    LOGIN_REQUIRED-> IBC enters credentials itself; on repeated LOGIN_FAILED do
                     a rate-limited controlled relogin, else alert fail-closed
    TWO_FA_PENDING-> WAIT for the owner's phone approval (never an immediate
                     restart); on 2FA timeout do a rate-limited relogin
    API_DOWN      -> a single restart within a defined window
    API_READY     -> no action
    UNKNOWN       -> alert + fail closed

Hard rules:
- NO credentials: the watchdog never reads or logs the IB login id/password;
  those live only in the IBC config file that IBC itself reads.
- NO orders / NO broker execution: it can only start/stop/relogin the Gateway
  process; there is no order, cancel or submit surface anywhere.
- 2FA is never bypassed: TWO_FA_PENDING waits for the owner.
- One restart manager: a file lock prevents two concurrent launches and a
  windowed budget prevents restart/relogin loops.
- A reconciliation gate can INHIBIT any restart/kill (for a future order
  lifecycle): when inhibited, the watchdog only alerts and waits.

Everything with side effects (process probe, socket, log read, launching) is
injected, so the classification and decision logic is pure and fully tested
with fakes — no real IBKR contact, ever.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

WATCHDOG_VERSION = "ib-gateway-watchdog-1.0.0"

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 4002  # paper API port; the live port is never used here
DEFAULT_PROCESS_PATTERN = "ibgateway"

logger = logging.getLogger("ib_gateway_watchdog")


class WatchdogState(StrEnum):
    PROCESS_DOWN = "PROCESS_DOWN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    TWO_FA_PENDING = "TWO_FA_PENDING"
    API_DOWN = "API_DOWN"
    API_READY = "API_READY"
    UNKNOWN = "UNKNOWN"


class LoginMarker(StrEnum):
    NONE = "NONE"
    LOGGED_IN = "LOGGED_IN"
    LOGIN_FAILED = "LOGIN_FAILED"
    TWO_FA_PROMPT = "TWO_FA_PROMPT"
    TWO_FA_TIMEOUT = "TWO_FA_TIMEOUT"


class Action(StrEnum):
    NONE = "NONE"
    START = "START"
    RESTART = "RESTART"
    RELOGIN = "RELOGIN"
    WAIT = "WAIT"
    ALERT = "ALERT"  # no operational action; alert / fail-closed only


# Distinct exit codes so the systemd journal and any future alert router can
# tell the situations apart. 0 = healthy/benign; 2x = waiting (not a failure);
# 3x/4x = fail-closed alerts.
class ExitCode:
    OK = 0
    STARTED = 10
    RELOGIN = 11
    RESTARTED = 12
    TWO_FA_WAITING = 20
    RESTART_INHIBITED = 21
    LOCK_BUSY = 22
    RESTART_LIMIT = 30
    UNKNOWN_STATE = 40
    LOGIN_FAIL_LIMIT = 41


@dataclass(frozen=True, slots=True)
class Signals:
    """A single observation cycle's raw signals (all already gathered)."""

    process_running: bool
    api_tcp_open: bool
    current_time_ok: bool
    login_marker: LoginMarker


@dataclass(frozen=True, slots=True)
class Policy:
    """Windowed restart/relogin budgets to prevent loops."""

    max_restarts: int = 3
    restart_window_seconds: float = 1800.0
    max_relogins: int = 3
    relogin_window_seconds: float = 1800.0


@dataclass(frozen=True, slots=True)
class ActionPlan:
    state: WatchdogState
    action: Action
    alert: bool
    reason: str
    exit_code: int


# -- pure classification + decision (fully unit-tested) --------------------------


def parse_login_marker(log_text: str) -> LoginMarker:
    """Classify the most relevant login marker from recent IBC log text.

    Ordered by urgency: an explicit 2FA timeout dominates a 2FA prompt, which
    dominates a failure, which dominates a completed login.
    """
    lowered = log_text.lower()
    if "second factor authentication" in lowered and (
        "timed out" in lowered or "timeout" in lowered
    ):
        return LoginMarker.TWO_FA_TIMEOUT
    if (
        "second factor authentication" in lowered
        or "security code card" in lowered
        or "ibkey" in lowered
        or "ib key" in lowered
    ):
        return LoginMarker.TWO_FA_PROMPT
    if "login failed" in lowered or "invalid username or password" in lowered:
        return LoginMarker.LOGIN_FAILED
    if "login has completed" in lowered or "gateway is ready" in lowered:
        return LoginMarker.LOGGED_IN
    return LoginMarker.NONE


def classify_state(signals: Signals) -> WatchdogState:
    """Map raw signals to exactly one state (deterministic priority order)."""
    if not signals.process_running:
        return WatchdogState.PROCESS_DOWN
    if signals.login_marker in (LoginMarker.TWO_FA_PROMPT, LoginMarker.TWO_FA_TIMEOUT):
        return WatchdogState.TWO_FA_PENDING
    if signals.login_marker is LoginMarker.LOGIN_FAILED:
        return WatchdogState.LOGIN_REQUIRED
    if signals.api_tcp_open and signals.current_time_ok:
        return WatchdogState.API_READY
    if signals.login_marker is LoginMarker.LOGGED_IN:
        # Logged in but the API is not answering yet.
        return WatchdogState.API_DOWN
    if not signals.api_tcp_open:
        # Process up, no API socket yet — IBC is still logging in.
        return WatchdogState.LOGIN_REQUIRED
    # Port open but reqCurrentTime fails and there is no login evidence:
    # an inconsistent state — fail closed.
    return WatchdogState.UNKNOWN


def decide_action(
    state: WatchdogState,
    marker: LoginMarker,
    *,
    restart_count: int,
    relogin_count: int,
    policy: Policy,
    restart_inhibited: bool,
) -> ActionPlan:
    """Decide the single controlled action for this cycle (pure)."""
    if state is WatchdogState.API_READY:
        return ActionPlan(state, Action.NONE, False, "API ready", ExitCode.OK)

    if state is WatchdogState.TWO_FA_PENDING:
        if marker is LoginMarker.TWO_FA_TIMEOUT:
            if restart_inhibited:
                return ActionPlan(
                    state, Action.WAIT, True,
                    "2FA timeout but restart inhibited by reconciliation gate",
                    ExitCode.RESTART_INHIBITED,
                )
            if relogin_count < policy.max_relogins:
                return ActionPlan(
                    state, Action.RELOGIN, True,
                    "2FA timeout: controlled relogin (a new phone approval will arrive)",
                    ExitCode.RELOGIN,
                )
            return ActionPlan(
                state, Action.ALERT, True,
                "2FA relogin budget exhausted; failing closed",
                ExitCode.RESTART_LIMIT,
            )
        # A pending 2FA prompt: wait for the owner. Never an immediate restart.
        return ActionPlan(
            state, Action.WAIT, True,
            "2FA pending: awaiting owner approval on IBKR Mobile",
            ExitCode.TWO_FA_WAITING,
        )

    if state is WatchdogState.PROCESS_DOWN:
        if restart_inhibited:
            return ActionPlan(
                state, Action.WAIT, True,
                "process down but restart inhibited by reconciliation gate",
                ExitCode.RESTART_INHIBITED,
            )
        if restart_count < policy.max_restarts:
            return ActionPlan(
                state, Action.START, True, "starting IB Gateway via IBC", ExitCode.STARTED
            )
        return ActionPlan(
            state, Action.ALERT, True,
            "restart budget exhausted; not starting; failing closed",
            ExitCode.RESTART_LIMIT,
        )

    if state is WatchdogState.API_DOWN:
        if restart_inhibited:
            return ActionPlan(
                state, Action.WAIT, True,
                "API down but restart inhibited by reconciliation gate",
                ExitCode.RESTART_INHIBITED,
            )
        if restart_count < policy.max_restarts:
            return ActionPlan(
                state, Action.RESTART, True,
                "API down after login: single controlled restart",
                ExitCode.RESTARTED,
            )
        return ActionPlan(
            state, Action.ALERT, True,
            "restart budget exhausted; API still down; failing closed",
            ExitCode.RESTART_LIMIT,
        )

    if state is WatchdogState.LOGIN_REQUIRED:
        if marker is LoginMarker.LOGIN_FAILED:
            if restart_inhibited:
                return ActionPlan(
                    state, Action.WAIT, True,
                    "login failed but restart inhibited by reconciliation gate",
                    ExitCode.RESTART_INHIBITED,
                )
            if relogin_count < policy.max_relogins:
                return ActionPlan(
                    state, Action.RELOGIN, True,
                    "login failed: controlled relogin", ExitCode.RELOGIN,
                )
            return ActionPlan(
                state, Action.ALERT, True,
                "login failure budget exhausted; failing closed (check credentials)",
                ExitCode.LOGIN_FAIL_LIMIT,
            )
        # Process up, IBC still entering credentials — benign in-progress wait.
        return ActionPlan(
            state, Action.WAIT, False,
            "login in progress (IBC entering credentials)", ExitCode.OK,
        )

    return ActionPlan(
        state, Action.ALERT, True, "unknown state; failing closed", ExitCode.UNKNOWN_STATE
    )


def _within_window(timestamps: list[float], now: float, window: float) -> int:
    return sum(1 for stamp in timestamps if now - stamp <= window)


# -- injectable I/O seams --------------------------------------------------------


class Launcher(Protocol):
    """Starts/stops/relogins the Gateway. Never places orders."""

    def start(self) -> None: ...

    def restart(self) -> None: ...

    def relogin(self) -> None: ...


class SystemdLauncher:
    """Default launcher: drives a systemd service. No order surface."""

    def __init__(self, unit: str = "ibgateway-ibc.service") -> None:
        self._unit = unit

    def _run(self, verb: str) -> None:
        subprocess.run(["systemctl", verb, self._unit], check=True)  # noqa: S603,S607

    def start(self) -> None:
        self._run("start")

    def restart(self) -> None:
        self._run("restart")

    def relogin(self) -> None:
        # A controlled relogin is a clean restart: IBC performs a fresh login
        # (and, per its config, a fresh 2FA request).
        self._run("restart")


@dataclass
class StateStore:
    """Persists restart/relogin timestamps as JSON (no secrets)."""

    path: Path
    restart_times: list[float] = field(default_factory=list)
    relogin_times: list[float] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> StateStore:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path)
        return cls(
            path=path,
            restart_times=[float(x) for x in raw.get("restart_times", [])],
            relogin_times=[float(x) for x in raw.get("relogin_times", [])],
        )

    def prune(self, now: float, policy: Policy) -> None:
        self.restart_times = [
            s for s in self.restart_times if now - s <= policy.restart_window_seconds
        ]
        self.relogin_times = [
            s for s in self.relogin_times if now - s <= policy.relogin_window_seconds
        ]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"restart_times": self.restart_times, "relogin_times": self.relogin_times}
            ),
            encoding="utf-8",
        )


class LockBusyError(Exception):
    """Another watchdog instance holds the lock; skip this cycle."""


class FileLock:
    """Exclusive lock via O_CREAT|O_EXCL; prevents two concurrent launches."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise LockBusyError(f"lock held: {self._path}") from error
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self._path.unlink()
        except OSError:
            pass


# -- default real probes (host only) ---------------------------------------------


def default_process_probe(pattern: str = DEFAULT_PROCESS_PATTERN) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            ["pgrep", "-f", pattern],  # noqa: S607
            capture_output=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def default_tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def default_login_probe(log_path: Path, tail_bytes: int = 20000) -> LoginMarker:
    try:
        with open(log_path, "rb") as handle:  # noqa: PTH123
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return LoginMarker.NONE
    return parse_login_marker(text)


# -- alerting (log/journal only; no Telegram/WhatsApp yet) -----------------------


def emit_alert(plan: ActionPlan) -> None:
    """Structured, credential-free alert to the journal. Distinct exit codes
    carry severity for a future alert router."""
    logger.warning(
        "ALERT state=%s action=%s exit=%s reason=%s",
        plan.state.value,
        plan.action.value,
        plan.exit_code,
        plan.reason,
    )


# -- orchestration ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Probes:
    process: Callable[[], bool]
    tcp: Callable[[], bool]
    current_time: Callable[[], bool]
    login: Callable[[], LoginMarker]
    restart_inhibited: Callable[[], bool]


def run_cycle(
    probes: Probes,
    launcher: Launcher,
    store: StateStore,
    *,
    policy: Policy,
    now: float,
    alert: Callable[[ActionPlan], None] = emit_alert,
) -> ActionPlan:
    """Execute one supervision cycle and return the chosen plan."""
    store.prune(now, policy)
    signals = Signals(
        process_running=probes.process(),
        api_tcp_open=probes.tcp(),
        current_time_ok=probes.current_time(),
        login_marker=probes.login(),
    )
    state = classify_state(signals)
    plan = decide_action(
        state,
        signals.login_marker,
        restart_count=_within_window(store.restart_times, now, policy.restart_window_seconds),
        relogin_count=_within_window(store.relogin_times, now, policy.relogin_window_seconds),
        policy=policy,
        restart_inhibited=probes.restart_inhibited(),
    )

    if plan.action is Action.START:
        launcher.start()
        store.restart_times.append(now)
    elif plan.action is Action.RESTART:
        launcher.restart()
        store.restart_times.append(now)
    elif plan.action is Action.RELOGIN:
        launcher.relogin()
        store.relogin_times.append(now)

    store.save()
    if plan.alert:
        alert(plan)
    logger.info("state=%s action=%s exit=%s", plan.state.value, plan.action.value, plan.exit_code)
    return plan


def _build_default_current_time_probe(host: str, port: int) -> Callable[[], bool]:
    """A deeper API-readiness probe: TCP reachable AND reqCurrentTime answers.

    The IBKR API client is imported lazily inside the read-only PoI session and
    only on the host; any failure means "not ready" (fail closed). This keeps
    the watchdog import-light and SDK-free at module load.
    """

    def probe() -> bool:
        if not default_tcp_probe(host, port):
            return False
        try:
            from trading_broker_ibkr.poi_readonly_session import create_session

            session = create_session()
            session.connect(host, port)
            try:
                return bool(str(session.req_current_time()).strip())
            finally:
                session.disconnect()
        except Exception:  # noqa: BLE001 - any failure => not ready
            return False

    return probe


def _default_restart_inhibited(marker_path: Path) -> Callable[[], bool]:
    def probe() -> bool:
        # A future order lifecycle drops this marker while active; the watchdog
        # must not restart/kill without a reconciliation gate.
        return marker_path.exists()

    return probe


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "IB Gateway (Paper) watchdog — one supervision cycle. Never handles"
            " credentials, never places orders, never bypasses 2FA."
        )
    )
    parser.add_argument("--host", default=DEFAULT_API_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--process-pattern", default=DEFAULT_PROCESS_PATTERN)
    parser.add_argument("--log-path", default="/home/opc/ibc/logs/ibc.log")
    parser.add_argument("--state-file", default="/run/ibgateway/watchdog-state.json")
    parser.add_argument("--lock-file", default="/run/ibgateway/watchdog.lock")
    parser.add_argument(
        "--reconciliation-marker",
        default="/run/ibgateway/order-lifecycle.active",
        help="if this file exists, restarts are inhibited (reconciliation gate)",
    )
    parser.add_argument("--unit", default="ibgateway-ibc.service")
    parser.add_argument("--max-restarts", type=int, default=3)
    parser.add_argument("--restart-window", type=float, default=1800.0)
    parser.add_argument("--max-relogins", type=int, default=3)
    parser.add_argument("--relogin-window", type=float, default=1800.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    policy = Policy(
        max_restarts=args.max_restarts,
        restart_window_seconds=args.restart_window,
        max_relogins=args.max_relogins,
        relogin_window_seconds=args.relogin_window,
    )
    log_path = Path(args.log_path)
    probes = Probes(
        process=lambda: default_process_probe(args.process_pattern),
        tcp=lambda: default_tcp_probe(args.host, args.port),
        current_time=_build_default_current_time_probe(args.host, args.port),
        login=lambda: default_login_probe(log_path),
        restart_inhibited=_default_restart_inhibited(Path(args.reconciliation_marker)),
    )
    launcher = SystemdLauncher(args.unit)
    store = StateStore.load(Path(args.state_file))
    try:
        with FileLock(Path(args.lock_file)):
            plan = run_cycle(probes, launcher, store, policy=policy, now=time.time())
            return plan.exit_code
    except LockBusyError:
        logger.info("another watchdog instance holds the lock; skipping this cycle")
        return ExitCode.LOCK_BUSY


if __name__ == "__main__":
    sys.exit(main())
