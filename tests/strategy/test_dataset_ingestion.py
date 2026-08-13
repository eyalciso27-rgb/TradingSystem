"""Track A / A4 — real-data ingestion readiness (offline, fixtures only)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from trading_application.dataset_cli import main as dataset_main
from trading_application.dataset_ingestion import (
    HistoricalBar,
    IngestionCheckpoint,
    IngestionError,
    baseline_minimum_requirement,
    coverage_report,
    deserialize_manifest,
    gap_report,
    ingest_series,
    load_checkpoint,
    normalize_bar,
    read_raw_shard,
    save_checkpoint,
    serialize_manifest,
    write_raw_shard,
)
from trading_application.dataset_manifest import (
    DatasetManifest,
    DatasetSource,
    DatasetStatus,
)

DAY = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)  # a bar close (RTH close ~ 20:00 UTC)
INGEST = DAY + timedelta(minutes=5)


def bar(day_offset: int, *, close: str = "100") -> HistoricalBar:
    close_at = DAY + timedelta(days=day_offset)
    return HistoricalBar(
        symbol="AAPL", interval="1d", bar_open_at=close_at - timedelta(hours=6),
        bar_close_at=close_at, open="99", high="101", low="98", close=close, volume="1000000",
        source_id="ibkr", source_version="hist-1",
    )


class FakeSource:
    def __init__(self, bars: tuple[HistoricalBar, ...]) -> None:
        self._bars = bars

    @property
    def source_id(self) -> str:
        return "ibkr"

    @property
    def version(self) -> str:
        return "hist-1"

    def fetch_bars(self, series_id, *, start, end, as_of):  # type: ignore[no-untyped-def]
        return self._bars


# -- immutable raw storage + checksum ---------------------------------------


def test_raw_shard_is_immutable_and_checksum_verified(tmp_path: Path) -> None:
    bars = (bar(0), bar(1))
    checksum = write_raw_shard(tmp_path, "ibkr", "AAPL:1d", bars)
    assert checksum.startswith("sha256:")
    # idempotent identical rewrite
    assert write_raw_shard(tmp_path, "ibkr", "AAPL:1d", bars) == checksum
    # read verifies checksum
    assert b"AAPL" in read_raw_shard(tmp_path, "ibkr", "AAPL:1d")
    # a conflicting rewrite fails closed
    with pytest.raises(IngestionError, match="immutable"):
        write_raw_shard(tmp_path, "ibkr", "AAPL:1d", (bar(0, close="999"), bar(1)))


def test_corrupt_raw_shard_is_detected(tmp_path: Path) -> None:
    write_raw_shard(tmp_path, "ibkr", "AAPL:1d", (bar(0),))
    from trading_application.dataset_ingestion import raw_shard_path

    raw_shard_path(tmp_path, "ibkr", "AAPL:1d").write_bytes(b"tampered\n")
    with pytest.raises(IngestionError, match="checksum mismatch"):
        read_raw_shard(tmp_path, "ibkr", "AAPL:1d")


# -- normalization -----------------------------------------------------------


def test_normalize_bar_is_point_in_time() -> None:
    record = normalize_bar(bar(0), ingested_at=INGEST)
    assert record.event_at == record.available_at == DAY
    assert record.ingested_at == INGEST
    assert '"c":"100"' in record.value


def test_normalize_bar_rejects_ingest_before_close() -> None:
    with pytest.raises(IngestionError, match="look-ahead"):
        normalize_bar(bar(0), ingested_at=DAY - timedelta(minutes=1))


# -- idempotent + resumable ingestion ---------------------------------------


def test_ingestion_is_idempotent_and_resumable(tmp_path: Path) -> None:
    source = FakeSource((bar(0), bar(1), bar(2)))
    ck = IngestionCheckpoint(cursors={})
    first = ingest_series(source, tmp_path, series_id="AAPL:1d", start=DAY,
                          end=DAY + timedelta(days=3), as_of=INGEST + timedelta(days=3),
                          ingested_at=INGEST + timedelta(days=3), checkpoint=ck)
    assert first.ingested_count == 3
    # resume: same source, same cursor -> nothing new (no duplicates)
    second = ingest_series(source, tmp_path, series_id="AAPL:1d", start=DAY,
                           end=DAY + timedelta(days=3), as_of=INGEST + timedelta(days=3),
                           ingested_at=INGEST + timedelta(days=3), checkpoint=first.checkpoint)
    assert second.ingested_count == 0
    assert second.skipped_count == 3


def test_checkpoint_persists_and_reloads(tmp_path: Path) -> None:
    ck = IngestionCheckpoint(cursors={}).advanced("AAPL:1d", DAY)
    path = tmp_path / "ck.json"
    save_checkpoint(path, ck)
    reloaded = load_checkpoint(path)
    assert reloaded.cursor_for("AAPL:1d") == DAY


def test_corrupt_checkpoint_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ck.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(IngestionError, match="corrupt checkpoint"):
        load_checkpoint(path)


# -- reports -----------------------------------------------------------------


def test_gap_report_names_missing_bars_without_filling() -> None:
    records = (normalize_bar(bar(0), ingested_at=INGEST), normalize_bar(bar(2), ingested_at=INGEST +
              timedelta(days=2)))
    expected = [DAY, DAY + timedelta(days=1), DAY + timedelta(days=2)]
    gaps = gap_report(records, "AAPL:1d", expected)
    assert gaps == ((DAY + timedelta(days=1)).isoformat(),)


def test_coverage_report_summarizes_series() -> None:
    records = (normalize_bar(bar(0), ingested_at=INGEST),
               normalize_bar(bar(1), ingested_at=INGEST + timedelta(days=1)))
    report = coverage_report(records)
    first, last, count = report.per_series["AAPL:1d"]
    assert count == 2 and first == DAY.isoformat()


# -- manifest round-trip + CLI ----------------------------------------------


def _manifest() -> DatasetManifest:
    records = tuple(normalize_bar(bar(i), ingested_at=INGEST + timedelta(days=i)) for i in range(3))
    src = DatasetSource(source_id="ibkr", version="hist-1", uri="ibkr://hist", checksum="sha256:x",
                        record_count=3)
    return DatasetManifest(dataset_id="baseline", dataset_version="v1", sources=(src,),
                           records=records, status=DatasetStatus.FIXTURE_VALIDATED)


def test_manifest_json_roundtrip() -> None:
    m = _manifest()
    restored = deserialize_manifest(serialize_manifest(m))
    assert restored.fingerprint() == m.fingerprint()


def test_dataset_cli_audit_and_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(serialize_manifest(_manifest()), encoding="utf-8")
    assert dataset_main(["prog", "audit", str(path)]) == 0
    assert "AUDIT_OK" in capsys.readouterr().out
    assert dataset_main(["prog", "fingerprint", str(path)]) == 0
    assert "sha256:" in capsys.readouterr().out
    assert dataset_main(["prog", "status", str(path)]) == 0
    assert "FIXTURE_VALIDATED" in capsys.readouterr().out
    assert dataset_main(["prog", "coverage", str(path)]) == 0
    assert "AAPL:1d" in capsys.readouterr().out


def test_dataset_cli_never_reports_real_built_for_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(serialize_manifest(_manifest()), encoding="utf-8")
    dataset_main(["prog", "status", str(path)])
    out = capsys.readouterr().out
    assert "REAL_DATASET_BUILT" not in out
    assert "HISTORICALLY_TESTED" not in out


# -- minimum requirement invents nothing ------------------------------------


def test_baseline_minimum_requirement_defers_numbers_to_owner() -> None:
    req = baseline_minimum_requirement()
    assert "open" in req.required_fields and "close" in req.required_fields
    # every number (universe, windows, depth) is deferred to owner freeze inputs
    assert "universe.definition" in req.owner_supplied_inputs
    assert "splits.test_oos" in req.owner_supplied_inputs
    # invents no lookback/threshold in the notes
    assert "lookback" not in req.notes.lower()
