"""Automatic monthly universe refresh: startup + periodic maintenance.

The monthly refresh is a PRODUCTION requirement, not a CLI chore. The owner must
never have to remember a command, so this runs from the application's own
lifecycle:

* ``run_startup()`` — on boot, load the last-known-good snapshot, work out when
  it was last successfully refreshed, and refresh if that is due or overdue.
  This is what covers the "application was offline when the refresh came due"
  case: overdue is simply due.
* ``run_tick()`` — called periodically by the maintenance loop. It is CHEAP: it
  compares timestamps and returns, touching the network ONLY when the interval
  has actually elapsed. It is safe to call often, so repeated trade cycles cost
  nothing.

Failure handling is conservative in both paths: a failed or invalid refresh
never deletes or overwrites the last-known-good snapshot, the failure is
recorded, and a backoff keeps a broken source from being hammered. Accepted
snapshots are kept permanently, which builds our own forward history from the
day collection starts — never a reconstruction of earlier dates.

Paper and Live share this infrastructure; only their freshness tolerance
differs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading_market_data_core.dataset_v1_sources import SourceAdapterError
from trading_market_data_core.sp500_current_universe import (
    REFRESH_INTERVAL_DAYS,
    ConstituentSnapshot,
    RawConstituentRefresh,
    UniverseStore,
    classify_freshness,
    refresh_universe,
)
from trading_market_data_core.trading_readiness import (
    DEFAULT_FRESHNESS,
    FreshnessPolicy,
    TradingMode,
    UniverseListStatus,
)

MAINTENANCE_VERSION = "universe-maintenance-1.0.0"

#: How long to wait after a failed refresh before trying again. Prevents a
#: broken or rate-limiting source from being hammered by a frequent tick.
DEFAULT_RETRY_BACKOFF_HOURS = 6

#: How often the maintenance loop should CALL run_tick. The call is cheap; the
#: fetch inside it still happens only when the monthly interval is due.
DEFAULT_TICK_INTERVAL_HOURS = 6


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    """When the refresh last succeeded and last failed. Persisted beside the data."""

    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_failure_at: datetime | None = None
    consecutive_failures: int = 0
    last_failure_detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "version": MAINTENANCE_VERSION,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_attempt_at": (
                self.last_attempt_at.isoformat() if self.last_attempt_at else None
            ),
            "last_failure_at": (
                self.last_failure_at.isoformat() if self.last_failure_at else None
            ),
            "consecutive_failures": self.consecutive_failures,
            "last_failure_detail": self.last_failure_detail,
        }

    @staticmethod
    def from_dict(payload: object) -> MaintenanceState:
        if not isinstance(payload, dict):
            return MaintenanceState()

        def _stamp(key: str) -> datetime | None:
            raw = payload.get(key)
            if not raw:
                return None
            try:
                return datetime.fromisoformat(str(raw))
            except ValueError:
                return None

        return MaintenanceState(
            last_success_at=_stamp("last_success_at"),
            last_attempt_at=_stamp("last_attempt_at"),
            last_failure_at=_stamp("last_failure_at"),
            consecutive_failures=int(payload.get("consecutive_failures", 0) or 0),
            last_failure_detail=str(payload.get("last_failure_detail", "")),
        )


@dataclass(frozen=True, slots=True)
class MaintenanceOutcome:
    """What one startup or tick actually did. Never silent."""

    trigger: str
    attempted: bool
    refreshed: bool
    status: UniverseListStatus
    snapshot_as_of: str | None
    constituent_count: int
    findings: tuple[str, ...] = ()
    state: MaintenanceState = field(default_factory=MaintenanceState)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": MAINTENANCE_VERSION,
            "trigger": self.trigger,
            "attempted": self.attempted,
            "refreshed": self.refreshed,
            "status": self.status.value,
            "snapshot_as_of": self.snapshot_as_of,
            "constituent_count": self.constituent_count,
            "findings": list(self.findings),
            "state": self.state.to_dict(),
        }

    def summary(self) -> str:
        action = (
            "refreshed" if self.refreshed else "attempted" if self.attempted else "no fetch"
        )
        return (
            f"universe maintenance [{self.trigger}]: {action};"
            f" status={self.status.value} count={self.constituent_count}"
            f" as_of={self.snapshot_as_of}"
        )


class UniverseMaintenance:
    """Owns the automatic refresh lifecycle for ONE universe store.

    The same object serves Paper and Live; only the freshness policy differs.
    """

    def __init__(
        self,
        store: UniverseStore,
        *,
        fetch: Callable[[], ConstituentSnapshot | RawConstituentRefresh] | None,
        mode: TradingMode = TradingMode.PAPER,
        policy: FreshnessPolicy | None = None,
        interval_days: int = REFRESH_INTERVAL_DAYS,
        retry_backoff_hours: int = DEFAULT_RETRY_BACKOFF_HOURS,
        state_path: Path | None = None,
    ) -> None:
        self._store = store
        self._fetch = fetch
        self._mode = mode
        self._policy = policy or DEFAULT_FRESHNESS[mode]
        self._interval = timedelta(days=interval_days)
        self._backoff = timedelta(hours=retry_backoff_hours)
        self._state_path = state_path or (store.root / "maintenance-state.json")

    # -- persisted state ------------------------------------------------------

    def load_state(self) -> MaintenanceState:
        if not self._state_path.is_file():
            return MaintenanceState()
        try:
            return MaintenanceState.from_dict(
                json.loads(self._state_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError):
            return MaintenanceState()

    def _save_state(self, state: MaintenanceState) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    # -- the due decision -----------------------------------------------------

    def refresh_due(self, *, now: datetime) -> tuple[bool, str]:
        """Cheap: timestamps only, no I/O beyond the local store.

        Overdue is simply due, which is what makes an offline period self-heal
        on the next startup.
        """
        latest = self._store.latest()
        if latest is None:
            return True, "no snapshot stored yet"
        state = self.load_state()
        reference = state.last_success_at
        if reference is None:
            # No recorded success: fall back to the snapshot's own date, so a
            # store populated before this mechanism existed still ages out.
            elapsed = now - datetime.combine(latest.as_of, datetime.min.time(), tzinfo=UTC)
        else:
            elapsed = now - reference
        if elapsed < self._interval:
            remaining = self._interval - elapsed
            return False, f"not due for another {remaining.days} day(s)"
        return True, f"due: {elapsed.days} day(s) since the last successful refresh"

    def _in_backoff(self, state: MaintenanceState, *, now: datetime) -> tuple[bool, str]:
        if state.consecutive_failures == 0 or state.last_failure_at is None:
            return False, ""
        # Linear backoff, capped, so a broken source is retried but not hammered.
        wait = min(self._backoff * state.consecutive_failures, timedelta(days=1))
        if now - state.last_failure_at < wait:
            return True, (
                f"in retry backoff after {state.consecutive_failures} failure(s);"
                f" next attempt in {(wait - (now - state.last_failure_at))}"
            )
        return False, ""

    # -- the two entry points -------------------------------------------------

    def run_startup(self, *, now: datetime | None = None) -> MaintenanceOutcome:
        """Called on application boot. Refreshes when due OR overdue."""
        return self._run(trigger="startup", now=now or datetime.now(tz=UTC), honour_backoff=False)

    def run_tick(self, *, now: datetime | None = None) -> MaintenanceOutcome:
        """Called periodically. Cheap unless the monthly interval has elapsed."""
        return self._run(trigger="tick", now=now or datetime.now(tz=UTC), honour_backoff=True)

    def _run(
        self, *, trigger: str, now: datetime, honour_backoff: bool
    ) -> MaintenanceOutcome:
        state = self.load_state()
        due, reason = self.refresh_due(now=now)
        findings = [reason]

        if not due:
            return self._outcome(trigger, False, False, now, findings, state)

        if honour_backoff:
            backing_off, detail = self._in_backoff(state, now=now)
            if backing_off:
                findings.append(detail)
                return self._outcome(trigger, False, False, now, findings, state)

        if self._fetch is None:
            findings.append("refresh is due but no source is configured")
            return self._outcome(trigger, False, False, now, findings, state)

        candidate: ConstituentSnapshot | RawConstituentRefresh | None = None
        failure = ""
        try:
            candidate = self._fetch()
        except SourceAdapterError as error:
            failure = f"fetch failed: {error}"
        except Exception as error:  # noqa: BLE001 - any source failure is a failure
            failure = f"fetch raised {type(error).__name__}: {error}"
        if failure:
            findings.append(failure)

        before = self._store.latest()
        current, _, refresh_findings = refresh_universe(
            self._store, candidate, now=now, policy=self._policy
        )
        findings.extend(refresh_findings)
        # A refresh counts as successful only when the stored snapshot ADVANCED.
        refreshed = (
            candidate is not None
            and current is not None
            and (before is None or current.as_of > before.as_of)
        )

        if refreshed:
            state = MaintenanceState(
                last_success_at=now, last_attempt_at=now,
                last_failure_at=state.last_failure_at, consecutive_failures=0,
                last_failure_detail="",
            )
        else:
            detail = failure or "; ".join(refresh_findings) or "refresh produced no new snapshot"
            state = MaintenanceState(
                last_success_at=state.last_success_at, last_attempt_at=now,
                last_failure_at=now,
                consecutive_failures=state.consecutive_failures + 1,
                last_failure_detail=detail,
            )
        self._save_state(state)
        return self._outcome(trigger, True, refreshed, now, findings, state)

    def _outcome(
        self,
        trigger: str,
        attempted: bool,
        refreshed: bool,
        now: datetime,
        findings: list[str],
        state: MaintenanceState,
    ) -> MaintenanceOutcome:
        current = self._store.latest()
        return MaintenanceOutcome(
            trigger=trigger,
            attempted=attempted,
            refreshed=refreshed,
            status=classify_freshness(current, now=now, policy=self._policy),
            snapshot_as_of=current.as_of.isoformat() if current else None,
            constituent_count=current.count if current else 0,
            findings=tuple(findings),
            state=state,
        )

    # -- what the trading path consumes --------------------------------------

    def current_status(self, *, now: datetime | None = None) -> UniverseListStatus:
        """Freshness of the stored list. No fetch, safe to call per decision."""
        moment = now or datetime.now(tz=UTC)
        return classify_freshness(self._store.latest(), now=moment, policy=self._policy)

    def current_snapshot(self) -> ConstituentSnapshot | None:
        return self._store.latest()


__all__ = [
    "DEFAULT_RETRY_BACKOFF_HOURS",
    "DEFAULT_TICK_INTERVAL_HOURS",
    "MAINTENANCE_VERSION",
    "MaintenanceOutcome",
    "MaintenanceState",
    "UniverseMaintenance",
]
