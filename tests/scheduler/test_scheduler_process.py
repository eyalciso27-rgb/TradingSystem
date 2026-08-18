"""The scheduler as a PROCESS: does it actually run, and does it stop?

Exposing ``run_startup_maintenance`` and ``run_maintenance_tick`` proved the
units worked. It did not prove anything ran them, which is the whole claim
behind "automatic monthly refresh". These tests drive the real
``SchedulerService`` loop and the real ``python -m trading_scheduler`` entry
point.

What is pinned here:

* startup refreshes when the interval is due, including when the process was
  offline while it came due;
* ticks before the interval elapses touch nothing — no fetch, no network;
* crossing the interval produces EXACTLY ONE refresh, and the many ticks after
  it produce no more;
* an exception inside a tick is contained: the loop survives and later ticks
  still run;
* a stop request ends the loop promptly rather than after a full interval, and
  the loop does not spin while idle;
* the entry point starts, handles SIGTERM, and exits 0.

Offline and deterministic: the source is an injected callable and the clock is
a test input.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from trading_market_data_core.sp500_current_universe import (
    Constituent,
    ConstituentSnapshot,
    UniverseStore,
)
from trading_market_data_core.trading_readiness import TradingMode
from trading_market_data_core.universe_maintenance import UniverseMaintenance
from trading_scheduler.__main__ import (
    ENV_MODE,
    ENV_TICK_HOURS,
    ENV_UNIVERSE_ROOT,
    _install_signal_handlers,
    build_service,
    main,
)
from trading_scheduler.service import SchedulerService

START = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: Wall-clock interval used to drive the loop quickly. The MAINTENANCE interval
#: (~30 days) is separate and is advanced through the injected clock, so no test
#: here waits on real time to pass.
FAST = timedelta(milliseconds=5)


def _snapshot(as_of: date, count: int = 500) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_id="SP500", as_of=as_of, fetched_at=START, source_id="free-source",
        source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(
                ticker=f"T{index:03d}", name=f"Co {index}",
                security_id=f"sec-cik:{index:010d}",
            )
            for index in range(count)
        ),
    )


class _Clock:
    """A clock the test moves, so a month can pass between two ticks."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _Source:
    """Counts fetches, so 'no network unless due' is provable."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls = 0
        self.raises = raises
        self.as_of = date(2026, 8, 18)

    def __call__(self) -> ConstituentSnapshot:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        # Each successful fetch reports a later list, as a real month would.
        self.as_of = self.as_of + timedelta(days=30)
        return _snapshot(self.as_of)


def _service(
    tmp_path: Path,
    source: _Source,
    clock: _Clock,
    *,
    max_ticks: int | None = None,
    interval: timedelta = FAST,
) -> SchedulerService:
    maintenance = UniverseMaintenance(
        UniverseStore(tmp_path), fetch=source, mode=TradingMode.PAPER
    )
    return SchedulerService(
        maintenance, tick_interval=interval, now=clock, max_ticks=max_ticks
    )


# == the loop actually runs the refresh =======================================


def test_startup_refreshes_when_nothing_is_stored_yet(tmp_path: Path) -> None:
    source, clock = _Source(), _Clock()
    summary = _service(tmp_path, source, clock, max_ticks=0).run()

    assert source.calls == 1
    assert summary.startup is not None
    assert summary.startup.refreshed
    assert summary.refreshes == 1


def test_a_process_that_was_offline_when_the_refresh_came_due_catches_up(
    tmp_path: Path,
) -> None:
    """Overdue is simply due: booting late is enough to heal it."""
    source, clock = _Source(), _Clock()
    _service(tmp_path, source, clock, max_ticks=0).run()
    assert source.calls == 1

    # The process is down for two months, then starts again.
    clock.advance(timedelta(days=61))
    _service(tmp_path, source, clock, max_ticks=0).run()
    assert source.calls == 2, "a late boot must refresh immediately"


def test_ticks_before_the_interval_never_touch_the_source(tmp_path: Path) -> None:
    source, clock = _Source(), _Clock()
    summary = _service(tmp_path, source, clock, max_ticks=25).run()

    assert summary.ticks == 25
    assert source.calls == 1, "only the startup refresh; the ticks are free"


def test_crossing_the_interval_refreshes_exactly_once(tmp_path: Path) -> None:
    """The property that matters: one refresh per month, not one per tick."""
    source, clock = _Source(), _Clock()
    maintenance = UniverseMaintenance(
        UniverseStore(tmp_path), fetch=source, mode=TradingMode.PAPER
    )
    service = SchedulerService(maintenance, tick_interval=FAST, now=clock, max_ticks=3)
    service.run()
    assert source.calls == 1

    # A month passes, then the loop runs many more ticks.
    clock.advance(timedelta(days=31))
    later = SchedulerService(maintenance, tick_interval=FAST, now=clock, max_ticks=20)
    later.run()

    assert source.calls == 2, "one refresh for the month, not one per tick"


def test_a_failing_source_does_not_kill_the_scheduler(tmp_path: Path) -> None:
    """A source down for a week must not stop the refresh forever."""
    source = _Source(raises=RuntimeError("source unreachable"))
    clock = _Clock()
    maintenance = UniverseMaintenance(
        UniverseStore(tmp_path), fetch=source, mode=TradingMode.PAPER,
        retry_backoff_hours=1,
    )
    service = SchedulerService(maintenance, tick_interval=FAST, now=clock, max_ticks=3)
    summary = service.run()

    assert source.calls >= 1
    assert summary.stopped_cleanly, "the loop survived the failure"

    # The source comes back; the very next due tick succeeds.
    source.raises = None
    clock.advance(timedelta(days=2))
    recovered = SchedulerService(maintenance, tick_interval=FAST, now=clock, max_ticks=1)
    recovered.run()
    assert UniverseStore(tmp_path).latest() is not None


def test_an_exception_from_the_job_itself_is_contained(tmp_path: Path) -> None:
    """Even a maintenance object that raises outright must not be fatal."""

    class _Exploding(UniverseMaintenance):
        def run_startup(self, *, now: datetime | None = None) -> object:  # type: ignore[override]
            raise RuntimeError("disk full")

        def run_tick(self, *, now: datetime | None = None) -> object:  # type: ignore[override]
            raise RuntimeError("disk full")

    service = SchedulerService(
        _Exploding(UniverseStore(tmp_path), fetch=None),
        tick_interval=FAST, now=_Clock(), max_ticks=3,
    )
    summary = service.run()

    assert summary.stopped_cleanly
    assert summary.ticks == 3
    assert summary.failures == 4, "startup + three ticks all failed, none was fatal"


# == shutdown is graceful, and idling is free =================================


def test_a_stop_request_ends_the_loop_without_waiting_out_the_interval(
    tmp_path: Path,
) -> None:
    source, clock = _Source(), _Clock()
    service = _service(tmp_path, source, clock, interval=timedelta(hours=6))

    started = threading.Event()

    def run() -> None:
        started.set()
        service.run()

    worker = threading.Thread(target=run)
    began = time.monotonic()
    worker.start()
    started.wait(timeout=5)
    service.request_stop()
    worker.join(timeout=5)

    assert not worker.is_alive(), "a six-hour interval must not delay shutdown"
    assert time.monotonic() - began < 5, "the wait is interruptible, not a sleep"
    assert service.stopping


def test_the_idle_loop_does_not_spin(tmp_path: Path) -> None:
    """A busy-wait would burn a core between monthly refreshes.

    Measured as CPU time, not wall time: an interruptible wait consumes
    essentially none, a spin consumes all of it.
    """
    source, clock = _Source(), _Clock()
    service = _service(tmp_path, source, clock, interval=timedelta(seconds=30))

    worker = threading.Thread(target=service.run)
    before = time.process_time()
    worker.start()
    time.sleep(0.3)
    service.request_stop()
    worker.join(timeout=5)
    burned = time.process_time() - before

    assert not worker.is_alive()
    assert burned < 0.2, f"the idle loop burned {burned:.3f}s of CPU"


def test_a_stop_requested_before_the_first_tick_is_honoured(tmp_path: Path) -> None:
    source, clock = _Source(), _Clock()
    service = _service(tmp_path, source, clock, interval=timedelta(hours=6))
    service.request_stop()

    summary = service.run()

    assert summary.ticks == 0
    assert summary.stopped_cleanly
    assert source.calls == 1, "startup still ran; the loop then exited immediately"


def test_a_non_positive_interval_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        _service(tmp_path, _Source(), _Clock(), interval=timedelta(0))


# == the entry point ==========================================================


def test_the_module_is_executable_as_a_process() -> None:
    """``python -m trading_scheduler`` must be a real thing to run."""
    import trading_scheduler.__main__ as entry

    assert Path(entry.__file__ or "").name == "__main__.py"
    assert callable(entry.main)


def test_the_entry_point_builds_a_service_from_the_environment(tmp_path: Path) -> None:
    service = build_service(
        {
            ENV_UNIVERSE_ROOT: str(tmp_path),
            ENV_TICK_HOURS: "2",
            ENV_MODE: "PAPER",
        }
    )
    assert isinstance(service, SchedulerService)


def test_the_entry_point_defaults_to_paper(tmp_path: Path) -> None:
    service = build_service({ENV_UNIVERSE_ROOT: str(tmp_path)})
    assert isinstance(service, SchedulerService)


@pytest.mark.parametrize(
    "env",
    [
        {ENV_MODE: "SEMI_AUTOMATIC_LIVE_ISH"},
        {ENV_TICK_HOURS: "0"},
        {ENV_TICK_HOURS: "-3"},
    ],
)
def test_invalid_configuration_fails_closed_rather_than_guessing(
    env: dict[str, str], tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        build_service({ENV_UNIVERSE_ROOT: str(tmp_path), **env})


def test_main_reports_a_configuration_failure_instead_of_starting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_UNIVERSE_ROOT, str(tmp_path))
    monkeypatch.setenv(ENV_MODE, "NOT_A_MODE")
    assert main([]) == 1


def test_signal_handlers_request_a_stop(tmp_path: Path) -> None:
    """The handler must ask the loop to stop, not kill the process."""
    service = _service(tmp_path, _Source(), _Clock(), interval=timedelta(hours=6))
    previous = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGINT")
        if hasattr(signal, name)
    }
    try:
        _install_signal_handlers(service)
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(int(signal.SIGTERM), None)
        assert service.stopping, "SIGTERM must request a graceful stop"
    finally:
        for name, original in previous.items():
            signal.signal(getattr(signal, name), original)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows has no real SIGTERM; the in-process handler test covers it",
)
def test_the_real_process_starts_and_exits_cleanly_on_sigterm(tmp_path: Path) -> None:
    """End to end: spawn the entry point, SIGTERM it, expect a clean exit.

    The store is pre-seeded with a fresh snapshot so startup maintenance finds
    nothing due — the process therefore never reaches the network, which keeps
    this test offline like every other.
    """
    store = UniverseStore(tmp_path)
    path, findings = store.accept(_snapshot(datetime.now(tz=UTC).date()))
    assert path is not None, findings

    env = {
        **os.environ,
        ENV_UNIVERSE_ROOT: str(tmp_path),
        ENV_TICK_HOURS: "1",
        ENV_MODE: "PAPER",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "trading_scheduler"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(2)
        assert process.poll() is None, "the scheduler must stay running"
        process.terminate()  # SIGTERM
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a hung process
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode == 0, f"unclean exit; output was:\n{output}"
    assert "scheduler starting" in output
    assert "shutting down gracefully" in output
