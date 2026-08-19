"""Scheduled jobs, backfill, retention and maintenance.

This app is the canonical home for periodic work, so the automatic monthly
universe refresh lives here rather than in a scheduler invented for it.

The runnable process is :mod:`trading_scheduler.__main__` — ``python -m
trading_scheduler`` — which the deployment starts (``deploy/Dockerfile.scheduler``,
the ``scheduler`` service in ``deploy/compose.yaml``). That process is what makes
the refresh automatic; the functions below are the units it drives, and calling
them from somewhere else does not schedule anything.

Two entry points, both cheap to call:

* :func:`run_startup_maintenance` on application boot — it also covers the case
  where the process was offline when the refresh came due, because overdue is
  simply due;
* :func:`run_maintenance_tick` from the periodic loop — it compares timestamps
  and returns, reaching the network only when the monthly interval has actually
  elapsed.

The CLI (``sp500_universe_cli``) remains a diagnostic and manual-override tool.
It is NOT how the monthly refresh normally happens.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from trading_market_data_core.sp500_current_universe import (
    ConstituentSnapshot,
    UniverseStore,
    fetch_current_constituents,
)
from trading_market_data_core.trading_readiness import TradingMode
from trading_market_data_core.universe_maintenance import (
    DEFAULT_TICK_INTERVAL_HOURS,
    MaintenanceOutcome,
    UniverseMaintenance,
)

#: Where accepted monthly snapshots are kept permanently.
DEFAULT_UNIVERSE_ROOT = Path("docs/poi/evidence/sp500-current")


def build_universe_maintenance(
    *,
    mode: TradingMode = TradingMode.PAPER,
    root: Path | None = None,
    fetch: Callable[[], ConstituentSnapshot] | None = None,
) -> UniverseMaintenance:
    """The maintenance job for one mode.

    Paper and Live share this infrastructure and differ only in the freshness
    tolerance carried by the mode's policy.
    """
    store = UniverseStore(root or DEFAULT_UNIVERSE_ROOT)
    return UniverseMaintenance(
        store,
        fetch=fetch if fetch is not None else _default_fetch,
        mode=mode,
    )


def _default_fetch() -> ConstituentSnapshot:
    """The real free source. Only ever called when a refresh is genuinely due."""
    return fetch_current_constituents(now=datetime.now(tz=UTC))


def run_startup_maintenance(
    *,
    mode: TradingMode = TradingMode.PAPER,
    root: Path | None = None,
    fetch: Callable[[], ConstituentSnapshot] | None = None,
    now: datetime | None = None,
) -> MaintenanceOutcome:
    """Call once on application boot, in Paper and in Live alike."""
    return build_universe_maintenance(mode=mode, root=root, fetch=fetch).run_startup(now=now)


def run_maintenance_tick(
    *,
    mode: TradingMode = TradingMode.PAPER,
    root: Path | None = None,
    fetch: Callable[[], ConstituentSnapshot] | None = None,
    now: datetime | None = None,
) -> MaintenanceOutcome:
    """Call periodically from the maintenance loop; cheap unless refresh is due."""
    return build_universe_maintenance(mode=mode, root=root, fetch=fetch).run_tick(now=now)


__all__: list[str] = [
    "DEFAULT_TICK_INTERVAL_HOURS",
    "DEFAULT_UNIVERSE_ROOT",
    "build_universe_maintenance",
    "run_maintenance_tick",
    "run_startup_maintenance",
]
