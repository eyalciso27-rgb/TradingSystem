"""The supervised autonomous-Paper runtime loop (A1).

This is the orchestrator, and deliberately NOT a submitter: it owns single-
instance ownership, startup and periodic reconciliation, the durable halt view
and the tick cadence, and it hands each entry intent to the canonical
``ExecutionService`` -- the one component that may submit. With the only
production authorization being DenyAll, an entry-eligible runtime still submits
nothing, which is exactly what Phase A proves.

Shape of the loop, mirroring the scheduler's discipline:

* one blocking wait per tick (never a poll loop), interruptible the instant a
  stop is requested, so an idle runtime burns no CPU and shuts down promptly;
* every tick is exception-contained -- a transient failure blocks entries for
  that tick and is logged, but never kills the loop;
* the state is derived from proven facts every tick, so the status can never
  claim READY while a fact says BLOCKED.

Fail-closed is the spine. Lost ownership, an unreadable halt store, an UNKNOWN
reconciliation, a disconnect, or any unattributed position/order all make
``entry_eligible`` false, and no entry intent is pulled while it is false. Losing
ownership additionally stops the loop: if another instance may now own
execution, this one must not keep doing trading work.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading_application.emergency import EntryContext
from trading_contracts.enums import Environment

from trading_execution_agent.runtime.ports import (
    ConnectionFacts,
    ExecutionPort,
    HaltGate,
    IntentSource,
    OwnershipGuard,
    ReconciliationDriver,
)
from trading_execution_agent.runtime.reconciliation_state import ReconciliationState
from trading_execution_agent.runtime.states import (
    AuthorizationMode,
    LifecyclePhase,
    RuntimeStatus,
)

RUNTIME_VERSION = "autonomous-paper-runtime-0.1.0"

LOGGER = logging.getLogger("trading_execution_agent.runtime")

DEFAULT_TICK_INTERVAL = timedelta(seconds=5)
DEFAULT_RECONCILIATION_INTERVAL = timedelta(minutes=5)


class OwnershipNotAcquiredError(RuntimeError):
    """Another instance owns execution; this one refuses to run (fail closed)."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Everything the loop needs that is not a collaborator."""

    environment: Environment = Environment.PAPER
    authorization_mode: AuthorizationMode = AuthorizationMode.DENYALL
    tick_interval: timedelta = DEFAULT_TICK_INTERVAL
    reconciliation_interval: timedelta = DEFAULT_RECONCILIATION_INTERVAL
    #: Bounded runs exist for tests and one-shot checks. None means until stopped.
    max_ticks: int | None = None

    def __post_init__(self) -> None:
        if self.tick_interval <= timedelta(0):
            raise ValueError("tick interval must be positive")
        if self.reconciliation_interval <= timedelta(0):
            raise ValueError("reconciliation interval must be positive")
        if self.environment is Environment.LIVE:
            # The runtime is Paper-only by construction; LIVE is never wired.
            raise ValueError("the autonomous runtime is Paper-only; LIVE is refused")


@dataclass(frozen=True, slots=True)
class RuntimeRunSummary:
    """What one process lifetime did. Returned for operators/tests."""

    ticks: int
    reconciliations: int
    #: MUST be 0 in Phase A. A non-zero value means DenyAll was bypassed.
    submissions: int
    entries_attempted: int
    ownership_lost: bool
    stopped_cleanly: bool

    def summary(self) -> str:
        return (
            f"runtime stopped after {self.ticks} tick(s):"
            f" {self.reconciliations} reconciliation(s),"
            f" {self.entries_attempted} entry attempt(s),"
            f" {self.submissions} submission(s);"
            f" ownership_lost={self.ownership_lost} clean={self.stopped_cleanly}"
        )


class PaperRuntime:
    """The long-running autonomous-Paper orchestrator (DenyAll in Phase A)."""

    def __init__(
        self,
        *,
        ownership: OwnershipGuard,
        reconciliation: ReconciliationDriver,
        halts: HaltGate,
        execution: ExecutionPort,
        intents: IntentSource,
        connection_facts: ConnectionFacts,
        config: RuntimeConfig | None = None,
        now: Callable[[], datetime] | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._ownership = ownership
        self._reconciliation = reconciliation
        self._halts = halts
        self._execution = execution
        self._intents = intents
        self._facts = connection_facts
        self._config = config or RuntimeConfig()
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._stop = threading.Event()
        #: One blocking wait per tick; the default is the stop event's own wait,
        #: so an idle runtime uses no CPU and a stop wakes it immediately.
        self._wait: Callable[[float], bool] = wait or self._stop.wait

        # -- derived state, updated each tick --------------------------------
        self._phase = LifecyclePhase.INIT
        self._ownership_proven = False
        self._ownership_lost = False
        self._recon: ReconciliationState | None = None
        self._halt_active = False
        self._last_ibkr_connected: bool | None = None
        self._needs_reconcile = False
        self._last_tick_at: datetime | None = None
        self._last_reconciliation_at: datetime | None = None
        self._ticks = 0
        self._reconciliations = 0
        self._entries_attempted = 0
        self._submissions = 0
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Acquire ownership, then run startup reconciliation. Fail closed."""
        if self._started:
            return
        LOGGER.info("runtime starting (%s)", RUNTIME_VERSION)
        try:
            acquired = self._ownership.try_acquire()
        except Exception as error:  # noqa: BLE001 - unproven ownership blocks
            self._phase = LifecyclePhase.FAILED
            raise OwnershipNotAcquiredError(
                f"could not prove single-instance ownership: {type(error).__name__}"
            ) from error
        if not acquired:
            self._phase = LifecyclePhase.FAILED
            raise OwnershipNotAcquiredError(
                "another runtime instance already owns execution; refusing to run"
            )
        self._ownership_proven = True

        # Observe connectivity BEFORE the first reconciliation, so the status
        # after start() reflects the real broker/market-data facts (and the
        # first tick does not redundantly re-trigger reconciliation).
        self._note_connectivity(self._facts.ibkr_connected())
        self._phase = LifecyclePhase.RECONCILING
        self._run_reconciliation(startup=True)
        self._phase = LifecyclePhase.ACTIVE
        self._started = True
        LOGGER.info("runtime started; state=%s", self.status().state.value)

    def run(self) -> RuntimeRunSummary:
        """Start (if needed) and tick until asked to stop."""
        if not self._started:
            self.start()
        while not self._stop.is_set():
            if self._config.max_ticks is not None and self._ticks >= self._config.max_ticks:
                break
            self.tick()
            if self._stop.is_set():
                break
            # ONE blocking wait for the whole interval; returns True the instant
            # a stop is requested, so shutdown is immediate even mid-interval.
            if self._wait(self._config.tick_interval.total_seconds()):
                break
        if self._phase not in (LifecyclePhase.FAILED,):
            self._phase = LifecyclePhase.STOPPING
        summary = RuntimeRunSummary(
            ticks=self._ticks,
            reconciliations=self._reconciliations,
            submissions=self._submissions,
            entries_attempted=self._entries_attempted,
            ownership_lost=self._ownership_lost,
            stopped_cleanly=not self._ownership_lost,
        )
        LOGGER.info("%s", summary.summary())
        return summary

    def tick(self) -> None:
        """One supervised iteration of work. Exception-contained; no wait."""
        try:
            self._tick_body()
        except Exception:  # noqa: BLE001 - a bad tick blocks entries, never kills
            LOGGER.exception("runtime tick failed; entries blocked this tick")
            # A failed tick leaves the last proven facts in place; it never
            # invents a pass. The next tick retries.
        finally:
            self._ticks += 1
            self._last_tick_at = self._now()

    def request_stop(self) -> None:
        """Ask the loop to finish. Safe from a signal handler or any thread."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def close(self) -> None:
        """Release every resource the runtime owns. Safe to repeat."""
        self._phase = (
            LifecyclePhase.FAILED
            if self._phase is LifecyclePhase.FAILED
            else LifecyclePhase.STOPPING
        )
        try:
            self._ownership.release()
        finally:
            self._ownership_proven = False
        LOGGER.info("runtime closed")

    # -- status --------------------------------------------------------------

    def status(self) -> RuntimeStatus:
        """The machine-readable runtime status, derived from proven facts."""
        recon = self._recon
        return RuntimeStatus(
            phase=self._phase,
            authorization_mode=self._config.authorization_mode,
            ownership_proven=self._ownership_proven,
            ibkr_connected=self._last_ibkr_connected,
            market_data_connected=self._facts_market_data(),
            reconciliation_clean=recon.clean if recon is not None else None,
            halt_active=self._halt_active,
            unknown_positions=recon.unknown_positions if recon is not None else None,
            unknown_orders=recon.unknown_orders if recon is not None else None,
            last_tick_at=self._last_tick_at,
            last_reconciliation_at=self._last_reconciliation_at,
            detail=recon.reason if recon is not None else "no reconciliation yet",
        )

    # -- internals -----------------------------------------------------------

    def _tick_body(self) -> None:
        # 1. Ownership must still be provably ours. A lost lock means another
        #    instance may now own execution: block entries AND stop the loop.
        if not self._ownership.is_held():
            self._ownership_proven = False
            self._ownership_lost = True
            LOGGER.critical(
                "single-instance ownership LOST; stopping runtime to avoid two"
                " owners of execution"
            )
            self.request_stop()
            return

        # 2. Connectivity facts drive reconnect/disconnect handling.
        ibkr = self._facts.ibkr_connected()
        self._note_connectivity(ibkr)

        # 3. Reconcile on demand (disconnect/reconnect) or on cadence.
        if self._needs_reconcile or self._reconciliation_due():
            self._run_reconciliation(startup=False)

        # 4. The durable halt view, re-read every tick (never cached).
        env_context = EntryContext(environment=self._config.environment)
        self._halt_active = self._halts.entry_halt_active(env_context)

        # 5. Entry work only while every fact says eligible.
        if not self.status().entry_eligible:
            return
        self._attempt_entries()

    def _note_connectivity(self, ibkr: bool | None) -> None:
        previous = self._last_ibkr_connected
        self._last_ibkr_connected = ibkr
        if ibkr is False:
            # A disconnect invalidates any prior CLEAN proof: block and force a
            # fresh reconciliation before the next entry.
            self._needs_reconcile = True
        elif ibkr is True and previous is not True:
            # A (re)connection: prove the current session CLEAN before entering.
            self._needs_reconcile = True

    def _reconciliation_due(self) -> bool:
        if self._last_reconciliation_at is None:
            return True
        return (
            self._now() - self._last_reconciliation_at
            >= self._config.reconciliation_interval
        )

    def _run_reconciliation(self, *, startup: bool) -> None:
        previous_phase = self._phase
        self._phase = LifecyclePhase.RECONCILING
        try:
            if startup:
                state = self._reconciliation.run_startup()
            else:
                state = self._reconciliation.run_periodic()
        except Exception:  # noqa: BLE001 - a failed reconciliation blocks entries
            LOGGER.exception("reconciliation run failed; entries remain blocked")
            # Preserve any prior state's counts but force blocked semantics: a
            # failed reconciliation is never a CLEAN one.
            self._recon = ReconciliationState(
                clean=False,
                entry_blocked=True,
                unknown_positions=None,
                unknown_orders=None,
                session_token=(self._recon.session_token if self._recon else None),
                checked_at=self._now(),
                reason="reconciliation run failed",
            )
            self._needs_reconcile = True
            if not startup:
                self._phase = previous_phase
            return
        self._recon = state
        self._reconciliations += 1
        self._last_reconciliation_at = state.checked_at
        self._needs_reconcile = False
        if not startup:
            self._phase = previous_phase

    def _attempt_entries(self) -> None:
        env = self._config.environment
        for intent in self._intents.next_intents(self._now()):
            # Per-intent halt re-check: a symbol/strategy/source-scoped halt must
            # stop exactly its target even when the environment is not halted.
            context = EntryContext(
                environment=env,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                source_id=intent.source_id,
            )
            if self._halts.entry_halt_active(context):
                LOGGER.info(
                    "entry halted for intent; skipping",
                    extra={"symbol": intent.symbol, "strategy_id": intent.strategy_id},
                )
                continue
            self._entries_attempted += 1
            result = self._execution.execute(intent)
            if result.submitted:
                # Under DenyAll this is impossible. If it ever happens the
                # invariant is broken: record it loudly. The runtime never
                # submits itself, so there is nothing here to undo.
                self._submissions += 1
                LOGGER.critical(
                    "INVARIANT VIOLATION: an entry was submitted under DenyAll;"
                    " the authorization boundary was bypassed"
                )

    def _facts_market_data(self) -> bool | None:
        try:
            return self._facts.market_data_connected()
        except Exception:  # noqa: BLE001 - unknown, never assumed connected
            return None


__all__ = [
    "DEFAULT_RECONCILIATION_INTERVAL",
    "DEFAULT_TICK_INTERVAL",
    "RUNTIME_VERSION",
    "OwnershipNotAcquiredError",
    "PaperRuntime",
    "RuntimeConfig",
    "RuntimeRunSummary",
]
