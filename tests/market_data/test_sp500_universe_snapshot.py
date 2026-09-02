"""Hardening for the archived forward S&P 500 snapshot store (offline).

``UniverseStore`` is what turns each monthly refresh into a permanent forward
history: "collecting a snapshot each month builds a genuine forward history
from the day collection starts" (see ``sp500_current_universe`` module
docstring). That claim only holds if an archived dated entry can never be
silently rewritten. These tests prove, independent of the general refresh
tests in ``test_sp500_free_refresh.py`` and ``test_universe_maintenance.py``:

* an archived snapshot is IMMUTABLE — a later candidate for a date already on
  record is refused unless byte-identical (an idempotent retry);
* every archived snapshot carries the required provenance fields and a
  self-authenticating SHA-256 checksum;
* a refresh failure or an immutability conflict never costs the last-known-good
  snapshot;
* an empty or otherwise invalid universe is never served as valid, even when a
  malformed file reaches the archive directory by a path other than
  ``accept()``.

Offline: no network, no external sources, no historical membership
reconstruction, no strategy parameters.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from trading_market_data_core.sp500_current_universe import (
    Constituent,
    ConstituentSnapshot,
    UniverseStore,
    classify_freshness,
    load_snapshot,
    refresh_universe,
    validate_snapshot,
)
from trading_market_data_core.sp500_universe_cli import main as universe_main
from trading_market_data_core.trading_readiness import (
    DEFAULT_FRESHNESS,
    TradingMode,
    UniverseListStatus,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SOURCE_ID = "wikipedia:List_of_S&P_500_companies"


def _snapshot(
    count: int = 500, as_of: date = date(2026, 8, 1), *, ticker_offset: int = 0
) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_id="SP500", as_of=as_of, fetched_at=NOW, source_id=SOURCE_ID,
        source_authority="PUBLIC_SECONDARY",
        constituents=tuple(
            Constituent(
                ticker=f"T{index + ticker_offset:03d}", name=f"Co {index}",
                security_id=f"sec-cik:{index:010d}",
            )
            for index in range(count)
        ),
    )


# == immutability: an archived entry cannot be silently rewritten ============


def test_a_conflicting_candidate_for_an_already_archived_date_is_refused(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    first_path, findings = store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert first_path is not None and findings == ()
    original_bytes = first_path.read_bytes()

    conflicting = _snapshot(as_of=date(2026, 8, 1), ticker_offset=1)
    second_path, refusal = store.accept(conflicting)

    assert second_path is None, "a conflicting rewrite of an archived date must be refused"
    assert refusal and "already archived immutably" in refusal[0]
    assert first_path.read_bytes() == original_bytes, "the archived file must be untouched"
    latest = store.latest()
    assert latest is not None and latest.tickers() == _snapshot(as_of=date(2026, 8, 1)).tickers()


def test_an_identical_resubmission_of_an_archived_date_is_an_idempotent_no_op(
    tmp_path: Path,
) -> None:
    """A crashed-and-retried refresh must not be treated as a violation."""
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    snapshot = _snapshot(as_of=date(2026, 8, 1))
    first_path, first_findings = store.accept(snapshot)
    second_path, second_findings = store.accept(snapshot)

    assert first_findings == () and second_findings == ()
    assert first_path == second_path
    assert len(store.snapshots()) == 1


def test_forward_history_accumulates_while_each_date_stays_independently_immutable(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot(as_of=date(2026, 6, 1)))
    store.accept(_snapshot(as_of=date(2026, 7, 1)))
    store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert len(store.snapshots()) == 3

    # A conflicting rewrite of the MIDDLE date must not disturb the others.
    _, refusal = store.accept(_snapshot(as_of=date(2026, 7, 1), ticker_offset=7))
    assert refusal != ()
    assert len(store.snapshots()) == 3
    as_ofs = {load_snapshot(path).as_of for path in store.snapshots()}
    assert as_ofs == {date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)}


def test_a_tampered_on_disk_entry_is_never_silently_repaired_by_a_good_candidate(
    tmp_path: Path,
) -> None:
    """Corruption must be surfaced, not quietly papered over by the next refresh."""
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, _ = store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["constituents"][0]["ticker"] = "HACKED"
    path.write_text(json.dumps(payload), encoding="utf-8")

    replacement_path, findings = store.accept(_snapshot(as_of=date(2026, 8, 1)))

    assert replacement_path is None
    assert findings and "unreadable" in findings[0]
    reloaded_raw = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded_raw["constituents"][0]["ticker"] == "HACKED", "the file was left alone"


def test_refresh_never_lets_a_same_day_retry_corrupt_the_archived_snapshot(
    tmp_path: Path,
) -> None:
    """The refresh path (not just accept()) inherits the immutability guarantee."""
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot(as_of=date(2026, 8, 1)))

    conflicting = _snapshot(as_of=date(2026, 8, 1), ticker_offset=3)
    current, status, findings = refresh_universe(
        store,
        conflicting,
        now=NOW,
        policy=DEFAULT_FRESHNESS[TradingMode.PAPER],
        allow_normalized_only=True,
    )

    assert current is not None
    assert current.tickers() == _snapshot(as_of=date(2026, 8, 1)).tickers()
    assert status is UniverseListStatus.CURRENT_LIST_VALID
    assert any("already archived immutably" in f for f in findings)


# == provenance and SHA-256 integrity =========================================


def test_an_archived_snapshot_carries_the_required_provenance_fields(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, _ = store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert path is not None
    body = json.loads(path.read_text(encoding="utf-8"))

    for field in (
        "schema_version", "index_id", "as_of", "fetched_at", "source_id",
        "source_authority", "constituent_count", "is_point_in_time_history",
        "checksum",
    ):
        assert field in body, f"missing required provenance field: {field}"
    assert body["source_id"] == SOURCE_ID
    assert body["source_authority"] == "PUBLIC_SECONDARY"
    assert body["is_point_in_time_history"] is False


def test_the_checksum_is_sha256_and_self_authenticates(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, _ = store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert path is not None
    loaded = load_snapshot(path)

    assert loaded.checksum.startswith("sha256:")
    digest = loaded.checksum.removeprefix("sha256:")
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert loaded.checksum == loaded.compute_checksum()


def test_a_checksum_that_does_not_match_its_own_body_is_rejected_by_accept(
    tmp_path: Path,
) -> None:
    corrupted = replace(_snapshot(as_of=date(2026, 8, 1)), checksum="sha256:" + "0" * 64)
    assert any("does not authenticate" in f for f in validate_snapshot(corrupted))

    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, findings = store.accept(corrupted)
    assert path is None
    assert any("does not authenticate" in f for f in findings)
    assert store.snapshots() == ()


def test_latest_skips_a_tampered_archive_entry_rather_than_serving_it(
    tmp_path: Path,
) -> None:
    """Tamper detection must protect ``latest()``, not only ``load_snapshot``."""
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot(as_of=date(2026, 7, 1)))
    tampered_path, _ = store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert tampered_path is not None
    payload = json.loads(tampered_path.read_text(encoding="utf-8"))
    payload["constituents"][0]["ticker"] = "HACKED"
    tampered_path.write_text(json.dumps(payload), encoding="utf-8")

    latest = store.latest()

    assert latest is not None
    assert latest.as_of == date(2026, 7, 1), "the tampered later entry must be skipped, not served"


# == an empty or UNKNOWN universe is never emitted ============================


def test_an_empty_candidate_is_never_written_to_the_archive(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, findings = store.accept(_snapshot(count=0, as_of=date(2026, 8, 1)))
    assert path is None
    assert "snapshot is empty" in findings[0]
    assert store.snapshots() == ()


def test_refresh_with_an_empty_candidate_keeps_the_archive_untouched(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    store.accept(_snapshot(as_of=date(2026, 8, 1)))
    current, status, findings = refresh_universe(
        store, _snapshot(count=0, as_of=date(2026, 8, 31)), now=NOW,
        policy=DEFAULT_FRESHNESS[TradingMode.PAPER],
        allow_normalized_only=True,
    )
    assert current is not None and current.count == 500
    assert status is UniverseListStatus.CURRENT_LIST_VALID
    assert any("snapshot is empty" in f for f in findings)
    assert len(store.snapshots()) == 1, "the empty candidate must not reach the archive"


def test_classify_freshness_never_calls_a_zero_count_snapshot_valid() -> None:
    """Defense in depth: even a self-consistent zero-count file is UNAVAILABLE.

    ``accept()`` already refuses an empty snapshot, but a file could in
    principle reach the archive directory some other way (a hand-edited or
    legacy file). The status classifier is the last line of defence and must
    independently refuse to call an empty list valid or stale.
    """
    hand_placed = ConstituentSnapshot(
        index_id="SP500", as_of=date(2026, 8, 31), fetched_at=NOW, source_id=SOURCE_ID,
        source_authority="PUBLIC_SECONDARY", constituents=(),
    )
    status = classify_freshness(
        hand_placed, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )
    assert status is UniverseListStatus.CURRENT_LIST_UNAVAILABLE


def test_cli_status_never_reports_an_empty_archive_entry_as_valid(
    tmp_path: Path, capsys: object,
) -> None:
    """A malformed zero-count file that bypassed ``accept()`` must not read as OK."""
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    path, _ = store.accept(_snapshot(as_of=date(2026, 8, 1)))
    assert path is not None
    hand_placed = ConstituentSnapshot(
        index_id="SP500", as_of=date(2026, 8, 31), fetched_at=NOW, source_id=SOURCE_ID,
        source_authority="PUBLIC_SECONDARY", constituents=(),
    )
    empty_path = store.root / "SP500-2026-08-31.json"
    empty_path.write_text(
        json.dumps(replace(hand_placed).to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    code = universe_main(["prog", "status", "--root", str(tmp_path)])

    assert code != 0, "an empty universe must never be reported as a healthy status"


def test_a_forward_snapshot_never_declares_itself_point_in_time_history(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path, allow_normalized_only=True)
    _, findings = store.accept(
        replace(_snapshot(as_of=date(2026, 8, 1)), is_point_in_time_history=True)
    )
    assert findings and "never claim" in findings[0]
    assert store.snapshots() == (), "a mislabelled snapshot must never reach the archive"
