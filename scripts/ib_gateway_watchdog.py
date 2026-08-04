"""IB Gateway (Paper) watchdog — passive supervision for IBC.

Runs one cycle per invocation (driven by a systemd timer). It observes the
Gateway/IBC process, the API port (4002) and the IBC log, classifies a single
state, and — subject to the ACCOUNT-SAFETY GATE below — takes at most one
controlled action.

    PROCESS_DOWN  -> cannot query the broker  => account safety UNKNOWN
    LOGIN_REQUIRED-> cannot prove cleanliness  => UNKNOWN
    TWO_FA_PENDING-> cannot prove cleanliness  => UNKNOWN (never bypassed)
    API_DOWN      -> cannot prove cleanliness  => UNKNOWN
    API_READY     -> the read-only probe may run; no action is needed anyway
    UNKNOWN       -> alert + fail closed

## The account-safety gate (the reason this file was rewritten)

Process liveness is **not** account safety. Restarting the Gateway while an
order is working, a position is open or the broker state is unresolved can
orphan that order and destroy the evidence needed to reconcile it. So no
unattended START / RESTART / RELOGIN may proceed unless a COMPLETE read-only
snapshot proves the account ``CLEAN``:

    CLEAN      -> policy may be evaluated (budgets, locks, 2FA still apply)
    NOT_CLEAN  -> WAIT/ALERT, launcher is never called
    UNKNOWN    -> ALERT, fail closed, launcher is never called

**Connectivity failure is UNKNOWN; it is never CLEAN.** Historical, cached or
last-known-clean state proves nothing about *now* and is never consulted.

The practical consequence is deliberate and important: **every state that
could trigger a restart is definitionally UNKNOWN**, because a down process or
a dead API cannot answer a broker query. The unattended watchdog therefore
**supervises and alerts; it does not restart.** Recovering the Gateway is an
owner-authorized action performed under the canonical runbook — see
``docs/runbooks/CHAOS_RECOVERY_DRILLS.md``. That manual capability is separate
from, and unaffected by, this gate.

Hard rules:
- NO credentials: the watchdog never reads or logs the IB login id/password;
  those live only in the IBC config file that IBC itself reads.
- NO orders / NO broker execution: there is no order, cancel, modify or submit
  surface anywhere, and the safety probe is handed a read-only facade that
  does not define one.
- 2FA is never bypassed: TWO_FA_PENDING waits for the owner.
- One restart manager, one Gateway service (``ibgateway-ibc.service``): a file
  lock prevents concurrent launches and windowed budgets prevent loops.
  Budgets and locks may never *override* the account-safety gate.
- The lifecycle/reconciliation marker still INHIBITS action. It is necessary
  but NOT sufficient — its absence proves nothing on its own.

Everything with side effects (process probe, socket, log read, launching,
broker probe) is injected, so the classification and decision logic is pure
and fully tested with fakes — no real IBKR contact, ever.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

WATCHDOG_VERSION = "ib-gateway-watchdog-2.0.0"

#: Canonical account-safety vocabulary. Mirrored as plain strings so this
#: script imports on a host without the application packages installed; the
#: real classification comes from
#: ``trading_execution_agent.account_safety`` and is imported lazily.
SAFETY_CLEAN = "CLEAN"
SAFETY_NOT_CLEAN = "NOT_CLEAN"
SAFETY_UNKNOWN = "UNKNOWN"
#: The only value that may permit an unattended action.
SAFETY_PERMITTING: frozenset[str] = frozenset({SAFETY_CLEAN})

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 4002  # paper API port; the live port is never used here
DEFAULT_PROCESS_PATTERN = "ibgateway"

logger = logging.getLogger("ib_gateway_watchdog")

#: Broker account identifiers are SENSITIVE OPERATIONAL DATA — not credentials.
#: They cannot be used to log in, but they identify the account under
#: management, so they must not reach a journal, a terminal or an evidence
#: file. IBKR's own client library logs them at INFO through the
#: ``managedAccounts`` callback, so containment cannot rely on our own call
#: sites being careful: every record on every handler is filtered.
#:
#: Matches account-shaped ids with a ONE to FOUR letter prefix, and partial
#: forms, so a truncated id cannot leak either.
#:
#: The previous ``[A-Z]{1,2}`` covered only the two shapes that happened to be
#: in front of us. Broker and sub-account identifiers are not limited to those:
#: a three- or four-letter prefix would have passed straight through the
#: filter. The bound is widened rather than guessed narrow — over-redacting a
#: log line costs nothing; under-redacting one is permanent.
_ACCOUNT_ID_PATTERN = re.compile(r"\b[A-Z]{1,4}\d{5,}\b")
ACCOUNT_ID_PLACEHOLDER = "<redacted-account-id>"

#: Dependency loggers that must never be raised to INFO/DEBUG by us. ibapi
#: emits full account identifiers at INFO.
_DEPENDENCY_LOGGERS = ("ibapi", "ibapi.client", "ibapi.wrapper", "ibapi.decoder")


def redact_account_ids(text: object) -> str:
    """Replace anything account-id shaped. Safe to apply to arbitrary text."""
    return _ACCOUNT_ID_PATTERN.sub(ACCOUNT_ID_PLACEHOLDER, str(text))


class AccountIdRedactingFilter(logging.Filter):
    """Scrub account-shaped tokens from every record, whatever emitted it.

    Applied to handlers rather than to a logger, so records propagated from
    dependency loggers are filtered too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            try:
                record.msg = record.getMessage()
                record.args = ()
            except Exception:  # noqa: BLE001 - never let logging break the run
                record.args = ()
        record.msg = redact_account_ids(record.msg)
        if record.exc_info:
            # Exception TEXT can carry an id just as easily as a log message.
            record.exc_text = redact_account_ids(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        return True


def configure_logging(*, verbose: bool = False) -> None:
    """Install our own handler and pin dependency levels.

    Deliberately NOT ``logging.basicConfig(level=logging.INFO)``: that raises
    the ROOT level, which turns on INFO for every library in the process —
    including ibapi, whose ``managedAccounts`` callback logs the complete
    account identifier. This is the defect that leaked an id to stderr.

    Must be called before any IBKR connection is attempted.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(AccountIdRedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    # Root stays at WARNING: third-party INFO is never wanted here.
    root.setLevel(logging.WARNING)

    # Our own diagnostics remain useful.
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Belt and braces: pin the dependency loggers explicitly, so raising the
    # root level later cannot re-enable their INFO output.
    for name in _DEPENDENCY_LOGGERS:
        dependency = logging.getLogger(name)
        dependency.setLevel(logging.WARNING)
        dependency.addFilter(AccountIdRedactingFilter())


class WatchdogState(StrEnum):
    PROCESS_DOWN = "PROCESS_DOWN"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    TWO_FA_PENDING = "TWO_FA_PENDING"
    API_DOWN = "API_DOWN"
    API_READY = "API_READY"
    UNKNOWN = "UNKNOWN"


class ObservedState(StrEnum):
    """What OS / TCP / log observation ALONE can support.

    Deliberately a separate vocabulary from :class:`WatchdogState`, which is
    the ACTION state machine and includes ``API_READY``. Plain ``--status``
    never exercises the API, so it must not be able to say API_READY at all —
    not "says it carefully", but *cannot spell it*. A listening socket is not
    a working API and is never evidence about the account.
    """

    PROCESS_DOWN = "PROCESS_DOWN"
    TWO_FA_PENDING = "TWO_FA_PENDING"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    API_PORT_CLOSED = "API_PORT_CLOSED"
    #: Process up, port accepting connections, no login problem in the log —
    #: and the API itself was NOT exercised. The honest ceiling of plain status.
    API_PORT_OPEN_UNVERIFIED = "API_PORT_OPEN_UNVERIFIED"


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
    #: An action was otherwise indicated but the account could not be
    #: proven clean. This is the normal outcome of an unattended cycle that
    #: sees a problem — it is a refusal, not a crash.
    ACCOUNT_UNKNOWN = 42
    ACCOUNT_NOT_CLEAN = 43


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


def classify_observation(
    *, process_running: bool, api_tcp_open: bool, login_marker: LoginMarker
) -> ObservedState:
    """Classify from OS/TCP/log signals only — no API round trip involved.

    Same priority order as :func:`classify_state`, minus every branch that
    needs the API to have answered. There is no ``API_READY`` outcome here by
    construction.
    """
    if not process_running:
        return ObservedState.PROCESS_DOWN
    if login_marker in (LoginMarker.TWO_FA_PROMPT, LoginMarker.TWO_FA_TIMEOUT):
        return ObservedState.TWO_FA_PENDING
    if login_marker is LoginMarker.LOGIN_FAILED:
        return ObservedState.LOGIN_REQUIRED
    if not api_tcp_open:
        return ObservedState.API_PORT_CLOSED
    return ObservedState.API_PORT_OPEN_UNVERIFIED


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


def probe_is_possible(state: WatchdogState) -> bool:
    """Can an account-safety proof even be attempted in this state?

    Only when the API is answering. A down process, a dead API, an incomplete
    login and a pending 2FA all make the broker unreachable, so the account
    cannot be proven clean and the answer is UNKNOWN — never CLEAN.
    """
    return state is WatchdogState.API_READY


#: Actions that change the Gateway process. These are the ones the gate guards.
MUTATING_ACTIONS: frozenset[Action] = frozenset(
    {Action.START, Action.RESTART, Action.RELOGIN}
)


def apply_account_safety_gate(plan: ActionPlan, account_safety: str) -> ActionPlan:
    """Downgrade any process-mutating action unless the account is proven CLEAN.

    This runs AFTER the policy decision on purpose: budgets, locks and 2FA
    semantics are preserved exactly as they were, and this gate sits in front
    of all of them. A budget that still has room, or a lock that is free, can
    never re-open a gate the account state has closed.
    """
    if plan.action not in MUTATING_ACTIONS:
        return plan
    if account_safety in SAFETY_PERMITTING:
        return plan
    if account_safety == SAFETY_NOT_CLEAN:
        return ActionPlan(
            plan.state,
            Action.WAIT,
            True,
            (
                f"{plan.action.value} refused: account NOT_CLEAN (a working order,"
                " a position, an unresolved state or a reconciliation finding"
                " exists); owner action required"
            ),
            ExitCode.ACCOUNT_NOT_CLEAN,
        )
    return ActionPlan(
        plan.state,
        Action.ALERT,
        True,
        (
            f"{plan.action.value} refused: account safety UNKNOWN (the read-only"
            " proof could not be completed); failing closed — connectivity"
            " failure is never CLEAN"
        ),
        ExitCode.ACCOUNT_UNKNOWN,
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


#: Modes for /run/ibgateway and the files in it. The lifecycle marker inhibits
#: unattended action and the state file rate-limits it, so both are supervision
#: authority: they are enforced explicitly, never inherited from a umask.
RUNTIME_DIR_MODE = 0o700
RUNTIME_FILE_MODE = 0o600


def ensure_runtime_dir(path: Path) -> None:
    """Guarantee ``path``'s parent exists at 0700 — no permissive fallback.

    /run/ibgateway has one canonical owner, ``/etc/tmpfiles.d/ibgateway.conf``.
    This is the last resort for a host where tmpfiles has not run: it may
    create the directory, but only at 0700. ``Path.mkdir(mode=...)`` is masked
    by the umask, so the mode is set explicitly afterwards.

    An EXISTING directory is left alone deliberately — widening or narrowing
    someone else's directory is not this process's call. Drift is a finding for
    ``scripts/host_operations_audit.py``, not something to paper over here.
    """
    parent = path.parent
    if parent.is_dir():
        return
    parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        parent.chmod(RUNTIME_DIR_MODE)


@dataclass
class StateStore:
    """Persists restart/relogin timestamps as JSON (no secrets).

    The file holds the restart BUDGET, which is a safety control: it is what
    rate-limits unattended action. So the write is atomic (a crash must not
    truncate the budget to zero and silently re-arm the watchdog) and the mode
    is enforced rather than inherited from the invoking service's umask.
    """

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
        """Write 0600 and atomically; a failed write keeps the previous state.

        ``Path.write_text`` was none of those things: the mode came from the
        umask, and a crash mid-write left truncated JSON that ``load`` would
        silently read as an empty budget — i.e. a full set of fresh restarts.
        """
        ensure_runtime_dir(self.path)
        payload = json.dumps(
            {"restart_times": self.restart_times, "relogin_times": self.relogin_times}
        )
        # Same directory, so os.replace is a true atomic rename and never a
        # cross-filesystem copy. mkstemp opens O_EXCL at 0600.
        handle_fd, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Explicit: the 0600 guarantee must not depend on mkstemp's
            # platform semantics or on the caller's umask.
            os.chmod(temp_path, RUNTIME_FILE_MODE)
            # Atomic: a concurrent reader sees either the old file or the new
            # one, never a partially written one.
            os.replace(temp_path, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise


class LockBusyError(Exception):
    """Another watchdog instance holds the lock; skip this cycle."""


class FileLock:
    """Exclusive lock via O_CREAT|O_EXCL; prevents two concurrent launches."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        ensure_runtime_dir(self._path)
        try:
            self._fd = os.open(
                str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, RUNTIME_FILE_MODE
            )
        except FileExistsError as error:
            raise LockBusyError(f"lock held: {self._path}") from error
        # os.open's mode is masked by the umask. This lock is what prevents two
        # concurrent launches, so its mode is enforced, not merely requested.
        with contextlib.suppress(OSError):
            os.chmod(str(self._path), RUNTIME_FILE_MODE)
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
    #: Complete read-only account-safety proof. Returns a mapping with at
    #: least ``safety`` and ``reasons``. Defaults to fail-closed UNKNOWN so
    #: a caller that forgets to wire it can never unlock an action.
    account_safety: Callable[[], dict[str, Any]] = field(
        default=lambda: {
            "safety": SAFETY_UNKNOWN,
            "reasons": ["NOT_PROBEABLE_IN_THIS_STATE"],
        }
    )


@dataclass(frozen=True, slots=True)
class StatusProbes:
    """The ONLY probe surface `--status` may touch.

    It defines **no** API round-trip probe. That is structural, not a
    convention: plain status previously called ``Probes.current_time``, whose
    real implementation opened an IBKR session, sent ``startApi``, requested
    the current time and disconnected — while the command was documented as
    opening no broker session. Removing the call would have left the next
    edit one attribute away from reintroducing it, so the attribute is gone.

    ``account_safety`` is wired ONLY for ``--deep-probe`` and defaults to a
    fail-closed UNKNOWN, so plain status cannot construct a broker adapter.
    """

    process: Callable[[], bool]
    tcp: Callable[[], bool]
    login: Callable[[], LoginMarker]
    restart_inhibited: Callable[[], bool]
    account_safety: Callable[[], dict[str, Any]] = field(
        default=lambda: {
            "safety": SAFETY_UNKNOWN,
            "reasons": ["NOT_PROBED"],
        }
    )


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

    # The account-safety gate. Only attempt the proof when the state could
    # possibly support one AND an action is actually on the table; otherwise
    # the answer is UNKNOWN by construction and no broker call is made.
    safety = SAFETY_UNKNOWN
    safety_report: dict[str, Any] = {
        "safety": SAFETY_UNKNOWN,
        "reasons": ["NOT_PROBEABLE_IN_THIS_STATE"],
    }
    if plan.action in MUTATING_ACTIONS and probe_is_possible(state):
        try:
            safety_report = dict(probes.account_safety())
            safety = str(safety_report.get("safety", SAFETY_UNKNOWN))
        except Exception:  # noqa: BLE001 - any probe failure => UNKNOWN
            safety_report = {
                "safety": SAFETY_UNKNOWN,
                "reasons": ["PROBE_EXCEPTION"],
            }
            safety = SAFETY_UNKNOWN
    plan = apply_account_safety_gate(plan, safety)

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


# -- read-only status reporting (WP3) --------------------------------------------
#
# `--status` and `--deep-probe --status` are STRICTLY read-only: they observe,
# classify and print. They construct no launcher, and a test asserts the
# launcher is never even instantiated on these paths.


class ProofLevel(StrEnum):
    """How much was actually PROVEN, stated explicitly rather than implied.

    A higher level is not "more confident about the same thing" — each level
    proves a strictly different claim, and only the last one says anything
    about the ACCOUNT.
    """

    #: Nothing was observed (probe unavailable).
    NONE = "NONE"
    #: The OS says a process matching the pattern exists. Says nothing about
    #: the API and nothing about the account.
    PROCESS_ONLY = "PROCESS_ONLY"
    #: A TCP connection to the API port succeeded. A listening socket is NOT
    #: proof that the Gateway is logged in, and NEVER proof the account is clean.
    TCP_OPEN = "TCP_OPEN"
    #: The API answered a request (reqCurrentTime-class round trip). Still
    #: says nothing about orders, positions or reconciliation.
    API_ROUND_TRIP = "API_ROUND_TRIP"
    #: A complete read-only account snapshot was obtained and classified.
    #: This is the ONLY level that can support CLEAN.
    FULL_ACCOUNT_SNAPSHOT = "FULL_ACCOUNT_SNAPSHOT"


@dataclass(frozen=True, slots=True)
class StatusReport:
    """Sanitized, deterministic status. Contains no credential and no account id."""

    watchdog_version: str
    observed_at: str
    process_running: bool
    api_tcp_open: bool
    #: None when the API was NOT exercised (plain status). Deliberately not
    #: False: "not tested" and "tested and failed" are different facts.
    api_round_trip_ok: bool | None
    login_marker: str
    lifecycle_marker_present: bool
    configured_unit: str
    restart_count: int
    max_restarts: int
    relogin_count: int
    max_relogins: int
    #: Always present: what OS/TCP/log observation alone supports.
    observed_state: str
    #: The ACTION state machine's verdict. None unless the API was actually
    #: exercised, because API_READY is not observable from a socket.
    state: str | None
    proof_level: str
    account_safety: str
    action_permitted: bool
    action_permitted_reason: str
    deep_probe: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "watchdog_version": self.watchdog_version,
            "observed_at": self.observed_at,
            "process_running": self.process_running,
            "api_tcp_open": self.api_tcp_open,
            "api_round_trip_ok": self.api_round_trip_ok,
            "login_marker": self.login_marker,
            "lifecycle_marker_present": self.lifecycle_marker_present,
            "configured_unit": self.configured_unit,
            "restart_budget": {
                "restarts_used": self.restart_count,
                "max_restarts": self.max_restarts,
                "relogins_used": self.relogin_count,
                "max_relogins": self.max_relogins,
            },
            "observed_state": self.observed_state,
            "state": self.state,
            "proof_level": self.proof_level,
            "account_safety": self.account_safety,
            "action_permitted": self.action_permitted,
            "action_permitted_reason": self.action_permitted_reason,
        }
        if self.deep_probe is not None:
            payload["deep_probe"] = self.deep_probe
        return payload

    def render(self) -> str:
        """Human-readable form. Same facts, no extra claims."""
        lines = [
            f"IB Gateway watchdog status ({self.watchdog_version})",
            f"  observed_at              : {self.observed_at}",
            f"  process_running          : {self.process_running}",
            f"  api_tcp_open             : {self.api_tcp_open}",
            f"  api_round_trip_ok        : {self.api_round_trip_ok}",
            f"  login_marker             : {self.login_marker}",
            f"  lifecycle_marker_present : {self.lifecycle_marker_present}",
            f"  configured_unit          : {self.configured_unit}",
            f"  restart_budget           : {self.restart_count}/{self.max_restarts}"
            f" restarts, {self.relogin_count}/{self.max_relogins} relogins",
            f"  observed_state           : {self.observed_state}",
            f"  state                    : {self.state}",
            f"  proof_level              : {self.proof_level}",
            f"  account_safety           : {self.account_safety}",
            f"  action_permitted         : {self.action_permitted}",
            f"  reason                   : {self.action_permitted_reason}",
        ]
        if self.deep_probe is not None:
            lines.append("  deep probe:")
            for key in (
                "safety",
                "reasons",
                "working_orders",
                "non_zero_positions",
                "unresolved_states",
                "reconciled",
                "blocking_findings",
                "connect_ok",
                "api_round_trip_ok",
                "disconnect_ok",
                "detail",
            ):
                if key in self.deep_probe:
                    lines.append(f"    {key:<22}: {self.deep_probe[key]}")
        lines.append("")
        lines.append(
            "  NOTE: this command is read-only. It never starts, restarts or"
            " relogins the Gateway."
        )
        return "\n".join(lines)


def _proof_level(
    *, process: bool, tcp: bool, round_trip: bool, deep: bool
) -> ProofLevel:
    """The highest claim the observations actually support."""
    if deep:
        return ProofLevel.FULL_ACCOUNT_SNAPSHOT
    if round_trip:
        return ProofLevel.API_ROUND_TRIP
    if tcp:
        return ProofLevel.TCP_OPEN
    if process:
        return ProofLevel.PROCESS_ONLY
    return ProofLevel.NONE


def build_status_report(
    probes: StatusProbes,
    store: StateStore,
    *,
    policy: Policy,
    unit: str,
    now: float,
    observed_at: str,
    deep: bool = False,
) -> StatusReport:
    """Observe and classify WITHOUT taking any action, and — in plain mode —
    WITHOUT touching the broker protocol at all.

    Plain status is OS/TCP/log only: process inspection, a raw TCP-open check,
    the sanitized IBC login marker, and lifecycle/state-file metadata. It
    constructs no IBKR transport, sends no ``startApi``, requests no current
    time, receives no ``managedAccounts``, builds no account-safety adapter,
    creates no launcher, takes no lock and writes no state.

    Deep status adds exactly ONE read-only broker session, and every fact it
    reports — connectivity, round trip, Paper validation, orders, positions,
    unresolved states, reconciliation, disconnect and the CLEAN/NOT_CLEAN/
    UNKNOWN verdict — is derived from that single captured snapshot.
    """
    process_running = probes.process()
    api_tcp_open = probes.tcp()
    login_marker = probes.login()
    inhibited = probes.restart_inhibited()

    observed = classify_observation(
        process_running=process_running,
        api_tcp_open=api_tcp_open,
        login_marker=login_marker,
    )

    deep_report: dict[str, Any] | None = None
    safety = SAFETY_UNKNOWN
    #: None means "not tested", which is not the same fact as False.
    round_trip: bool | None = None
    action_state: WatchdogState | None = None

    if deep:
        try:
            deep_report = dict(probes.account_safety())
            safety = str(deep_report.get("safety", SAFETY_UNKNOWN))
        except Exception as error:  # noqa: BLE001 - any failure => UNKNOWN
            deep_report = {
                "safety": SAFETY_UNKNOWN,
                "reasons": ["PROBE_EXCEPTION"],
                "detail": type(error).__name__,
            }
            safety = SAFETY_UNKNOWN
        # The round trip is a FACT OF THE SAME SESSION, not a second probe.
        observed_round_trip = deep_report.get("api_round_trip_ok")
        round_trip = bool(observed_round_trip) if observed_round_trip is not None else False
        action_state = classify_state(
            Signals(
                process_running=process_running,
                api_tcp_open=api_tcp_open,
                current_time_ok=round_trip,
                login_marker=login_marker,
            )
        )

    restart_count = _within_window(store.restart_times, now, policy.restart_window_seconds)
    relogin_count = _within_window(store.relogin_times, now, policy.relogin_window_seconds)

    # Would an unattended process action be permitted right now? Deep mode can
    # answer it the same way run_cycle does, without doing it. Plain mode
    # cannot answer it at all, and says so rather than guessing.
    permitted = False
    if action_state is None:
        reason = (
            "not evaluated: plain status observes the process, the TCP port and"
            " the IBC log only; it never tests the API or the account"
        )
    else:
        plan = decide_action(
            action_state,
            login_marker,
            restart_count=restart_count,
            relogin_count=relogin_count,
            policy=policy,
            restart_inhibited=inhibited,
        )
        gated = apply_account_safety_gate(plan, safety)
        permitted = gated.action in MUTATING_ACTIONS
        if permitted:
            reason = f"{gated.action.value} would be permitted: account proven CLEAN"
        elif plan.action in MUTATING_ACTIONS:
            reason = gated.reason
        elif inhibited:
            reason = "no process action indicated; lifecycle marker present (inhibited)"
        else:
            reason = f"no process action indicated in state {action_state.value}"

    return StatusReport(
        watchdog_version=WATCHDOG_VERSION,
        observed_at=observed_at,
        process_running=process_running,
        api_tcp_open=api_tcp_open,
        api_round_trip_ok=round_trip,
        login_marker=login_marker.value,
        lifecycle_marker_present=inhibited,
        configured_unit=unit,
        restart_count=restart_count,
        max_restarts=policy.max_restarts,
        relogin_count=relogin_count,
        max_relogins=policy.max_relogins,
        observed_state=observed.value,
        state=None if action_state is None else action_state.value,
        proof_level=_proof_level(
            process=process_running,
            tcp=api_tcp_open,
            round_trip=bool(round_trip),
            deep=deep and safety != SAFETY_UNKNOWN,
        ).value,
        account_safety=safety,
        action_permitted=permitted,
        action_permitted_reason=reason,
        deep_probe=deep_report,
    )


def _build_account_safety_probe(  # pragma: no cover - host-only wiring
    *,
    host: str,
    port: int,
    account_alias: str,
    account_secret: str,
    secrets_root: str | None,
) -> Callable[[], dict[str, Any]]:
    """Build the real read-only account-safety probe (owner host only).

    Everything is imported lazily so this script still imports, and is fully
    tested, on a machine with no application packages and no ibapi. Any failure
    at any point becomes UNKNOWN — never CLEAN.

    The secret STORE ROOT is passed in as a path (``--secrets-root``); this
    module never reads the environment and never touches a credential value.
    The account id itself is resolved by the canonical ``FileSecretStore`` and
    is used only to construct the adapter — it is never logged or printed.
    """

    def probe() -> dict[str, Any]:
        try:
            from trading_broker_ibkr.paper_adapter import build_paper_adapter
            from trading_config.secrets import FileSecretStore
            from trading_contracts.enums import Environment
            from trading_execution_agent.account_safety import (
                ProbeConfig,
                ReadOnlyBrokerFacade,
                probe_account_safety,
            )

            if not secrets_root:
                return {
                    "safety": SAFETY_UNKNOWN,
                    "reasons": ["PROBE_EXCEPTION"],
                    "detail": "secret store root not configured (--secrets-root)",
                }
            store = FileSecretStore(Path(secrets_root), Environment.PAPER)
            account_id = store.get(account_secret).reveal()
            broker = build_paper_adapter(
                account_aliases={account_alias: account_id}, host=host, port=port
            )
            view = ReadOnlyBrokerFacade(broker)
            report = probe_account_safety(
                view,
                ProbeConfig(account_alias=account_alias, host=host, port=port),
                now=datetime.now(tz=UTC),
            )
            return report.as_dict()
        except Exception as error:  # noqa: BLE001 - any failure => UNKNOWN
            return {
                "safety": SAFETY_UNKNOWN,
                "reasons": ["PROBE_EXCEPTION"],
                "detail": type(error).__name__,
            }

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
    parser.add_argument(
        "--status",
        action="store_true",
        help="READ-ONLY: print structured status and exit. Never starts,"
        " restarts or relogins anything.",
    )
    parser.add_argument(
        "--deep-probe",
        action="store_true",
        help="with --status: additionally run the complete READ-ONLY"
        " account-safety probe (CLEAN / NOT_CLEAN / UNKNOWN)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit deterministic JSON (evidence capture)"
    )
    parser.add_argument("--account-alias", default="paper-poi")
    parser.add_argument("--account-secret", default="ibkr/paper-account-id")
    parser.add_argument(
        "--secrets-root",
        default=None,
        help="path to the PAPER secret store root used ONLY to resolve the"
        " account id for the read-only probe; without it the probe is UNKNOWN",
    )
    args = parser.parse_args(argv)

    # BEFORE anything else, and specifically before any IBKR connection: pin
    # dependency log levels and install the account-id redaction filter.
    configure_logging()

    policy = Policy(
        max_restarts=args.max_restarts,
        restart_window_seconds=args.restart_window,
        max_relogins=args.max_relogins,
        relogin_window_seconds=args.relogin_window,
    )
    log_path = Path(args.log_path)
    store = StateStore.load(Path(args.state_file))

    if args.status:
        # READ-ONLY path. No launcher is constructed, no lock is taken, no
        # state is written — and StatusProbes defines no API round-trip probe,
        # so plain status cannot reach the broker protocol at all.
        #
        # The account-safety probe (the only thing that opens a session) is
        # wired ONLY for --deep-probe; otherwise the fail-closed default
        # stands and no adapter is ever constructed.
        status_probes = StatusProbes(
            process=lambda: default_process_probe(args.process_pattern),
            tcp=lambda: default_tcp_probe(args.host, args.port),
            login=lambda: default_login_probe(log_path),
            restart_inhibited=_default_restart_inhibited(Path(args.reconciliation_marker)),
            **(
                {
                    "account_safety": _build_account_safety_probe(
                        host=args.host,
                        port=args.port,
                        account_alias=args.account_alias,
                        account_secret=args.account_secret,
                        secrets_root=args.secrets_root,
                    )
                }
                if args.deep_probe
                else {}
            ),
        )
        report = build_status_report(
            status_probes,
            store,
            policy=policy,
            unit=args.unit,
            now=time.time(),
            observed_at=datetime.now(tz=UTC).isoformat(),
            deep=args.deep_probe,
        )
        rendered = (
            json.dumps(report.as_dict(), indent=2, sort_keys=True)
            if args.json
            else report.render()
        )
        # Last-resort scrub: the model is already sanitized, but printing is
        # the one place an id could reach a terminal or an evidence file.
        print(redact_account_ids(rendered))
        return ExitCode.OK

    probes = Probes(
        process=lambda: default_process_probe(args.process_pattern),
        tcp=lambda: default_tcp_probe(args.host, args.port),
        current_time=_build_default_current_time_probe(args.host, args.port),
        login=lambda: default_login_probe(log_path),
        restart_inhibited=_default_restart_inhibited(Path(args.reconciliation_marker)),
        account_safety=_build_account_safety_probe(
            host=args.host,
            port=args.port,
            account_alias=args.account_alias,
            account_secret=args.account_secret,
            secrets_root=args.secrets_root,
        ),
    )
    launcher = SystemdLauncher(args.unit)
    try:
        with FileLock(Path(args.lock_file)):
            plan = run_cycle(probes, launcher, store, policy=policy, now=time.time())
            return plan.exit_code
    except LockBusyError:
        logger.info("another watchdog instance holds the lock; skipping this cycle")
        return ExitCode.LOCK_BUSY


if __name__ == "__main__":
    sys.exit(main())
