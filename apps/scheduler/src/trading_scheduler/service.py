"""The scheduler as an actual long-running process, not a callable.

Exposing ``run_startup_maintenance`` and ``run_maintenance_tick`` did not make
the monthly refresh automatic — it made it *invocable*, which is a different
thing, and something still had to invoke it. This module is that something: a
supervised loop that owns its own lifecycle.

What it does, in order:

1. run startup maintenance once, so a process that was offline when the refresh
   came due catches up immediately on boot (overdue is simply due);
2. sleep until the next tick, waking early if the process is asked to stop;
3. run one tick, which is cheap and touches the network only when the monthly
   interval has genuinely elapsed;
4. repeat until stopped, then exit cleanly.

Two properties matter more than the loop itself:

* **it does not busy-wait** — between ticks the loop makes exactly ONE blocking
  wait for the whole interval. The default wait is ``Event.wait(timeout)``, so
  an idle scheduler consumes no CPU and a stop request wakes it instantly rather
  than after hours;
* **a failing tick does not kill the scheduler** — an exception in one tick is
  logged and the loop continues. A source that is down for a week must not mean
  the refresh never happens again once it returns. Only a stop request ends the
  loop.

The wait is injectable for the same reason the clock is: "does not spin" is a
statement about the SHAPE of the loop — one blocking wait per tick, for the full
interval — not about how many CPU cycles a particular machine happened to burn.
Timing that on an emulated ARM64 runner measures the emulator. A test can
substitute a recording wait and check the shape directly, which is both stronger
and architecture-independent.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from trading_market_data_core.universe_maintenance import (
    DEFAULT_TICK_INTERVAL_HOURS,
    MaintenanceOutcome,
    UniverseMaintenance,
)

SCHEDULER_SERVICE_VERSION = "trading-scheduler-service-1.0.0"

LOGGER = logging.getLogger("trading_scheduler")


@dataclass(frozen=True, slots=True)
class SchedulerRunSummary:
    """What one process lifetime actually did. Returned for operators/tests."""

    startup: MaintenanceOutcome | None
    ticks: int
    #: How many times the loop blocked. Exactly one per tick: a spinning loop
    #: would show many waits per tick, or ticks with no wait between them.
    waits: int
    refreshes: int
    failures: int
    stopped_cleanly: bool

    def summary(self) -> str:
        return (
            f"scheduler stopped after {self.ticks} tick(s):"
            f" {self.refreshes} refresh(es), {self.failures} failure(s);"
            f" clean={self.stopped_cleanly}"
        )


class SchedulerService:
    """Runs universe maintenance for the lifetime of the process."""

    def __init__(
        self,
        maintenance: UniverseMaintenance,
        *,
        tick_interval: timedelta | None = None,
        now: Callable[[], datetime] | None = None,
        max_ticks: int | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._maintenance = maintenance
        # `or` would be wrong here: timedelta(0) is falsy, so a zero interval
        # would silently become six hours instead of being refused.
        self._interval = (
            tick_interval
            if tick_interval is not None
            else timedelta(hours=DEFAULT_TICK_INTERVAL_HOURS)
        )
        if self._interval <= timedelta(0):
            raise ValueError("tick interval must be positive")
        self._now = now or (lambda: datetime.now(tz=UTC))
        #: Bounded runs exist for tests and one-shot operational checks. None —
        #: the production default — means "until stopped".
        self._max_ticks = max_ticks
        self._stop = threading.Event()
        #: Blocks for up to ``seconds`` and returns True if a stop was requested
        #: meanwhile. The default is the stop event's own wait: one syscall, no
        #: polling, and immediately interruptible.
        self._wait: Callable[[float], bool] = wait or self._stop.wait

    # -- lifecycle ------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the loop to finish. Safe from a signal handler or any thread."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> SchedulerRunSummary:
        """Run startup maintenance, then tick until asked to stop."""
        LOGGER.info(
            "scheduler starting: tick every %s (%s)",
            self._interval,
            SCHEDULER_SERVICE_VERSION,
        )
        startup = self._run_once("startup", self._maintenance.run_startup)
        ticks = 0
        waits = 0
        refreshes = 1 if startup is not None and startup.refreshed else 0
        failures = 1 if startup is None else 0

        while not self._stop.is_set():
            if self._max_ticks is not None and ticks >= self._max_ticks:
                break
            # ONE blocking wait for the whole interval — never a poll loop.
            # It returns True the moment a stop is requested, so shutdown is
            # immediate even mid-interval.
            waits += 1
            if self._wait(self._interval.total_seconds()):
                break
            outcome = self._run_once("tick", self._maintenance.run_tick)
            ticks += 1
            if outcome is None:
                failures += 1
            elif outcome.refreshed:
                refreshes += 1

        LOGGER.info("scheduler stopped after %d tick(s)", ticks)
        return SchedulerRunSummary(
            startup=startup,
            ticks=ticks,
            waits=waits,
            refreshes=refreshes,
            failures=failures,
            stopped_cleanly=True,
        )

    # -- one supervised invocation -------------------------------------------

    def _run_once(
        self,
        label: str,
        job: Callable[..., MaintenanceOutcome],
    ) -> MaintenanceOutcome | None:
        """Run one maintenance job. An exception is contained, never fatal."""
        try:
            outcome = job(now=self._now())
        except Exception:
            # A broken source, a full disk, a malformed response: all are
            # reasons to try again next tick, none is a reason for the
            # scheduler to die and stop refreshing forever.
            LOGGER.exception("universe maintenance %s failed; continuing", label)
            return None
        LOGGER.info("%s", outcome.summary())
        for finding in outcome.findings:
            LOGGER.info("universe maintenance %s: %s", label, finding)
        return outcome


__all__ = [
    "SCHEDULER_SERVICE_VERSION",
    "SchedulerRunSummary",
    "SchedulerService",
]
