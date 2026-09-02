"""Automatic monthly universe refresh — production behaviour, not a CLI chore.

The owner must never have to run a command each month, so refresh is driven by
the application lifecycle: once at startup and cheaply from the maintenance
loop. These tests prove the network is touched ONLY when the monthly interval
has genuinely elapsed, and that a bad refresh can never cost us the
last-known-good list.

Offline: the source is an injected callable, never a real fetch.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_sources import SourceAdapterError
from trading_market_data_core.sp500_current_universe import (
    Constituent,
    ConstituentSnapshot,
    RawConstituentRefresh,
    UniverseStore,
)
from trading_market_data_core.trading_readiness import TradingMode, UniverseListStatus
from trading_market_data_core.universe_maintenance import UniverseMaintenance

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _snapshot(count: int = 500, as_of: date = date(2026, 8, 1)) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_id="SP500", as_of=as_of, fetched_at=NOW, source_id="free-source",
        source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(
                ticker=f"T{index:03d}", name=f"Co {index}",
                security_id=f"sec-cik:{index:010d}",
            )
            for index in range(count)
        ),
    )


class _Source:
    """Counts fetches so 'no network unless due' is provable."""

    def __init__(
        self,
        snapshot: ConstituentSnapshot | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self.calls = 0
        self._snapshot = snapshot
        self._raises = raises

    def __call__(self) -> RawConstituentRefresh:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        assert self._snapshot is not None
        raw = self._snapshot.to_dict()["as_of"].encode("utf-8") + b"\n" + json.dumps(
            [row.to_dict() for row in self._snapshot.constituents],
            sort_keys=True,
        ).encode("utf-8")
        return RawConstituentRefresh(snapshot=self._snapshot, raw_bytes=raw)


def _maintenance(
    tmp_path: Path, source: _Source, *, mode: TradingMode = TradingMode.PAPER
) -> tuple[UniverseMaintenance, UniverseStore]:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    return UniverseMaintenance(store, fetch=source, mode=mode), store


# == startup ===================================================================


def test_startup_with_a_fresh_snapshot_does_not_fetch(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 8, 15)))
    maintenance._save_state(  # noqa: SLF001 - seeding a recent success
        type(maintenance.load_state())(last_success_at=NOW - timedelta(days=3))
    )
    outcome = maintenance.run_startup(now=NOW)
    assert source.calls == 0, "a fresh snapshot must not trigger a fetch"
    assert not outcome.attempted
    assert outcome.status is UniverseListStatus.CURRENT_LIST_VALID


def test_startup_with_an_overdue_snapshot_refreshes(tmp_path: Path) -> None:
    """The offline-past-due-date case: overdue is simply due."""
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    outcome = maintenance.run_startup(now=NOW)
    assert source.calls == 1
    assert outcome.refreshed
    assert outcome.snapshot_as_of == "2026-08-17"


def test_startup_with_no_snapshot_refreshes(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    outcome = maintenance.run_startup(now=NOW)
    assert source.calls == 1
    assert outcome.refreshed
    assert outcome.constituent_count == 500


def test_startup_records_the_successful_refresh_time(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    maintenance.run_startup(now=NOW)
    state = maintenance.load_state()
    assert state.last_success_at == NOW
    assert state.consecutive_failures == 0


# == the running application ===================================================


def test_a_tick_before_the_interval_does_not_fetch(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    maintenance.run_startup(now=NOW)
    assert source.calls == 1
    for minute in range(1, 6):
        outcome = maintenance.run_tick(now=NOW + timedelta(minutes=minute))
        assert not outcome.attempted
    assert source.calls == 1, "ticks inside the interval must not fetch"


def test_repeated_trade_cycles_never_trigger_repeated_fetches(tmp_path: Path) -> None:
    """A cheap due-check is safe to call as often as the app likes."""
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    maintenance.run_startup(now=NOW)
    for tick in range(200):
        maintenance.run_tick(now=NOW + timedelta(seconds=tick))
    assert source.calls == 1


def test_an_application_running_past_the_due_date_refreshes(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    maintenance.run_startup(now=NOW)
    assert source.calls == 1
    later = NOW + timedelta(days=31)
    source._snapshot = _snapshot(as_of=date(2026, 9, 18))  # noqa: SLF001
    outcome = maintenance.run_tick(now=later)
    assert source.calls == 2
    assert outcome.refreshed


def test_an_offline_period_self_heals_on_the_next_startup(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    maintenance.run_startup(now=NOW)
    # process stops; comes back two months later
    source._snapshot = _snapshot(as_of=date(2026, 10, 18))  # noqa: SLF001
    outcome = maintenance.run_startup(now=NOW + timedelta(days=61))
    assert source.calls == 2
    assert outcome.refreshed
    assert outcome.snapshot_as_of == "2026-10-18"


# == failure and invalid refreshes never cost the last-known-good =============


def test_a_failed_fetch_keeps_the_last_known_good(tmp_path: Path) -> None:
    source = _Source(raises=SourceAdapterError("source unreachable"))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    outcome = maintenance.run_startup(now=NOW)
    assert source.calls == 1
    assert not outcome.refreshed
    current = store.latest()
    assert current is not None and current.as_of == date(2026, 5, 1)
    assert any("fetch failed" in finding for finding in outcome.findings)


def test_an_unexpected_exception_is_contained(tmp_path: Path) -> None:
    source = _Source(raises=RuntimeError("boom"))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    outcome = maintenance.run_startup(now=NOW)
    assert not outcome.refreshed
    assert store.latest() is not None


@pytest.mark.parametrize(
    ("label", "bad"),
    [
        ("empty", _snapshot(count=0, as_of=date(2026, 8, 17))),
        ("implausible count", _snapshot(count=3, as_of=date(2026, 8, 17))),
    ],
)
def test_an_invalid_refresh_keeps_the_last_known_good(
    tmp_path: Path, label: str, bad: ConstituentSnapshot
) -> None:
    source = _Source(bad)
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    outcome = maintenance.run_startup(now=NOW)
    assert not outcome.refreshed, label
    current = store.latest()
    assert current is not None and current.count == 500
    assert current.as_of == date(2026, 5, 1)


def test_a_duplicate_ticker_snapshot_is_rejected(tmp_path: Path) -> None:
    duplicated = ConstituentSnapshot(
        index_id="SP500", as_of=date(2026, 8, 17), fetched_at=NOW,
        source_id="free-source", source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(ticker="DUP", name="Dup", security_id=f"sec-cik:{i:010d}")
            for i in range(450)
        ),
    )
    source = _Source(duplicated)
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    outcome = maintenance.run_startup(now=NOW)
    assert not outcome.refreshed
    assert store.latest() is not None and store.latest().count == 500  # type: ignore[union-attr]


def test_a_snapshot_with_a_broken_checksum_is_rejected(tmp_path: Path) -> None:
    """Spec section 6: a checksum/integrity problem must never be accepted."""
    from dataclasses import replace

    corrupted = replace(
        _snapshot(as_of=date(2026, 8, 17)), checksum="sha256:" + "0" * 64
    )
    source = _Source(corrupted)
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    outcome = maintenance.run_startup(now=NOW)
    assert not outcome.refreshed
    current = store.latest()
    assert current is not None and current.as_of == date(2026, 5, 1)
    assert any("does not authenticate" in finding for finding in outcome.findings)


def test_failures_are_recorded_and_backoff_prevents_hammering(tmp_path: Path) -> None:
    source = _Source(raises=SourceAdapterError("down"))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    maintenance.run_startup(now=NOW)
    assert source.calls == 1
    assert maintenance.load_state().consecutive_failures == 1
    # ticks during the backoff window must not re-hit the source
    for minute in (1, 30, 120):
        maintenance.run_tick(now=NOW + timedelta(minutes=minute))
    assert source.calls == 1, "backoff must stop a broken source being hammered"


def test_backoff_expires_and_the_source_is_retried(tmp_path: Path) -> None:
    source = _Source(raises=SourceAdapterError("down"))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    maintenance.run_startup(now=NOW)
    outcome = maintenance.run_tick(now=NOW + timedelta(hours=12))
    assert source.calls == 2
    assert outcome.attempted


def test_startup_ignores_backoff_so_a_restart_always_tries(tmp_path: Path) -> None:
    source = _Source(raises=SourceAdapterError("down"))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    maintenance.run_startup(now=NOW)
    maintenance.run_startup(now=NOW + timedelta(minutes=1))
    assert source.calls == 2


# == persistence and both modes ================================================


def test_a_successful_refresh_stores_a_new_dated_snapshot(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, store = _maintenance(tmp_path, source)
    store.accept(_snapshot(as_of=date(2026, 5, 1)))
    maintenance.run_startup(now=NOW)
    stored = {path.name for path in store.snapshots()}
    assert "SP500-2026-05-01.json" in stored, "history is kept, never replaced"
    assert "SP500-2026-08-17.json" in stored


def test_accepted_snapshots_build_forward_history_only(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, store = _maintenance(tmp_path, source)
    maintenance.run_startup(now=NOW)
    latest = store.latest()
    assert latest is not None
    assert latest.is_point_in_time_history is False


@pytest.mark.parametrize("mode", [TradingMode.PAPER, TradingMode.LIVE])
def test_both_modes_use_the_same_mechanism(tmp_path: Path, mode: TradingMode) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path / mode.value, source, mode=mode)
    outcome = maintenance.run_startup(now=NOW)
    assert outcome.refreshed
    assert outcome.status is UniverseListStatus.CURRENT_LIST_VALID


def test_live_and_paper_differ_only_in_freshness_tolerance(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    paper, store = _maintenance(tmp_path, source, mode=TradingMode.PAPER)
    store.accept(_snapshot(as_of=date(2026, 6, 25)))
    live = UniverseMaintenance(store, fetch=source, mode=TradingMode.LIVE)
    assert paper.current_status(now=NOW) is UniverseListStatus.CURRENT_LIST_STALE
    assert live.current_status(now=NOW) is UniverseListStatus.CURRENT_LIST_UNAVAILABLE


# == the scheduler app owns the lifecycle, not the CLI ========================


def test_the_scheduler_app_exposes_startup_and_tick(tmp_path: Path) -> None:
    """Automatic operation must not require the CLI."""
    from trading_scheduler import run_maintenance_tick, run_startup_maintenance

    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    started = run_startup_maintenance(root=tmp_path, fetch=source, now=NOW)
    assert started.refreshed
    assert source.calls == 1
    ticked = run_maintenance_tick(root=tmp_path, fetch=source, now=NOW + timedelta(minutes=5))
    assert not ticked.attempted
    assert source.calls == 1


def test_the_scheduler_app_serves_both_modes(tmp_path: Path) -> None:
    from trading_scheduler import run_startup_maintenance

    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        source = _Source(_snapshot(as_of=date(2026, 8, 17)))
        outcome = run_startup_maintenance(
            mode=mode, root=tmp_path / mode.value, fetch=source, now=NOW
        )
        assert outcome.refreshed


def test_the_outcome_summary_is_operator_readable(tmp_path: Path) -> None:
    source = _Source(_snapshot(as_of=date(2026, 8, 17)))
    maintenance, _ = _maintenance(tmp_path, source)
    summary = maintenance.run_startup(now=NOW).summary()
    assert "universe maintenance [startup]" in summary
    assert "refreshed" in summary
