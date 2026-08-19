"""``python -m trading_scheduler`` — the canonical scheduler process.

This is the entry point a deployment starts. Running it is what makes the
monthly S&P 500 refresh automatic: no operator remembers a command, and the
CLI stays what it always was, a diagnostic and manual override.

Operationally it behaves like a well-mannered container process:

* configuration comes from the environment, with defaults that work;
* logs go to stdout in the repository's structured JSON format, so the refresh
  is visible to whoever is on call;
* ``SIGTERM`` (how a container is stopped) and ``SIGINT`` (Ctrl-C) both request
  a graceful stop, and the loop wakes from its sleep immediately rather than
  being killed mid-interval;
* the exit code reports the outcome: ``0`` clean, ``1`` if the process could not
  be started at all.

It runs Paper by default. Paper and Live share this infrastructure and differ
only in freshness tolerance — running it does not authorize Paper Auto or Live
trading, which remain governed by the project's separate prohibitions.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import FrameType

from trading_market_data_core.trading_readiness import TradingMode
from trading_market_data_core.universe_maintenance import DEFAULT_TICK_INTERVAL_HOURS
from trading_observability.logging import configure_logging

from trading_scheduler import DEFAULT_UNIVERSE_ROOT, build_universe_maintenance
from trading_scheduler.service import SchedulerService

#: Environment variables this process reads. Kept few and boring on purpose.
ENV_UNIVERSE_ROOT = "TRADING_UNIVERSE_ROOT"
ENV_TICK_HOURS = "TRADING_SCHEDULER_TICK_HOURS"
ENV_MODE = "TRADING_MODE"
ENV_LOG_LEVEL = "TRADING_LOG_LEVEL"

LOGGER = logging.getLogger("trading_scheduler")


def _mode_from_env(raw: str | None) -> TradingMode:
    """PAPER unless LIVE is named explicitly. An unknown value is an error.

    Silently falling back to a mode nobody asked for is exactly the kind of
    guess that must never happen on the trading axis.
    """
    if raw is None or not raw.strip():
        return TradingMode.PAPER
    candidate = raw.strip().upper()
    for mode in TradingMode:
        if mode.value.upper() == candidate or mode.name == candidate:
            return mode
    raise ValueError(
        f"{ENV_MODE}={raw!r} is not a known trading mode"
        f" ({[mode.value for mode in TradingMode]})"
    )


def _tick_interval_from_env(raw: str | None) -> timedelta:
    if raw is None or not raw.strip():
        return timedelta(hours=DEFAULT_TICK_INTERVAL_HOURS)
    hours = float(raw)
    if hours <= 0:
        raise ValueError(f"{ENV_TICK_HOURS}={raw!r} must be positive")
    return timedelta(hours=hours)


def build_service(env: dict[str, str] | None = None) -> SchedulerService:
    """Assemble the service from the environment. No side effects yet."""
    source = env if env is not None else dict(os.environ)
    mode = _mode_from_env(source.get(ENV_MODE))
    root = Path(source.get(ENV_UNIVERSE_ROOT) or DEFAULT_UNIVERSE_ROOT)
    interval = _tick_interval_from_env(source.get(ENV_TICK_HOURS))
    return SchedulerService(
        build_universe_maintenance(mode=mode, root=root),
        tick_interval=interval,
    )


def _install_signal_handlers(service: SchedulerService) -> None:
    """Ask the loop to stop on SIGTERM/SIGINT instead of dying mid-refresh.

    Signals not present on the running platform are skipped rather than treated
    as an error, so the same entry point runs under a container and on a
    developer machine.
    """

    def handle(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info("received signal %s; shutting down gracefully", signum)
        service.request_stop()

    for name in ("SIGTERM", "SIGINT"):
        received = getattr(signal, name, None)
        if received is None:
            continue
        try:
            signal.signal(received, handle)
        except (ValueError, OSError):
            # Not the main thread, or unsupported here. The loop still stops
            # through request_stop(); it simply has no signal to hear.
            LOGGER.warning("could not install a %s handler", name)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scheduler until it is asked to stop."""
    del argv  # configuration is environment-driven; no flags to parse
    level = getattr(logging, os.environ.get(ENV_LOG_LEVEL, "INFO").upper(), logging.INFO)
    try:
        service = build_service()
    except ValueError as error:
        # Configure logging first so the failure is visible in the same place
        # every other scheduler log line goes.
        configure_logging(service="trading-scheduler", environment="unknown", level=level)
        LOGGER.error("scheduler configuration is invalid: %s", error)
        return 1
    configure_logging(
        service="trading-scheduler",
        environment=os.environ.get(ENV_MODE, TradingMode.PAPER.value),
        level=level,
    )
    _install_signal_handlers(service)
    summary = service.run()
    LOGGER.info("%s", summary.summary())
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
