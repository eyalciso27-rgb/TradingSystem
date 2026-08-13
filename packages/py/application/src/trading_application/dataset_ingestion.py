"""Real-data ingestion readiness (Track A / A4).

Offline machinery to ingest real historical bars into the point-in-time dataset
foundation (A2) — WITHOUT selecting or inventing a paid provider. The real
source is an INJECTED ``HistoricalBarSource`` port; IBKR (via the existing
broker/market-data boundary) or any approved provider can implement it. This
module never imports a provider and never invents data.

Provides:

* an injected ingestion **port** and a raw ``HistoricalBar`` record;
* an **immutable** raw-storage layout with a sha256 sidecar (re-writing a shard
  with different bytes fails closed) and checksum-verified reads;
* **idempotent, resumable** ingestion driven by a durable per-series checkpoint;
* normalization of a raw bar into a canonical point-in-time ``DatasetRecord``
  (A2), producing the **normalized point-in-time layout**;
* **gap**, **coverage** and **provenance** reports;
* JSON (de)serialization of a ``DatasetManifest`` for the validation CLI;
* a declarative **minimum dataset requirement** for
  ``baseline-trend-breakout-v1`` that references OWNER-supplied universe/windows
  and invents no numbers.

Status stays ``REAL_DATASET_NOT_BUILT`` until real bars actually exist; the
machinery here is proven with deterministic fixtures only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from trading_application.dataset_manifest import (
    DatasetError,
    DatasetManifest,
    DatasetRecord,
    DatasetSource,
    DatasetSplit,
    DatasetStatus,
    SplitKind,
    normalize_utc,
)

INGESTION_VERSION = "dataset-ingestion-1.0.0"


class IngestionError(Exception):
    """Fail-closed ingestion failure (checksum mismatch, immutability breach)."""


@dataclass(frozen=True, slots=True)
class HistoricalBar:
    """One raw historical bar as returned by a source (before normalization)."""

    symbol: str
    interval: str  # e.g. "1d", "1m"
    bar_open_at: datetime  # session/bar open instant
    bar_close_at: datetime  # bar CLOSE — the moment its OHLC becomes knowable
    open: str
    high: str
    low: str
    close: str
    volume: str
    source_id: str
    source_version: str

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.interval.strip():
            raise IngestionError("symbol and interval are mandatory")
        for name in ("bar_open_at", "bar_close_at"):
            object.__setattr__(self, name, normalize_utc(getattr(self, name), name))
        if self.bar_close_at <= self.bar_open_at:
            raise IngestionError(f"{self.symbol} bar close must be after its open")

    @property
    def series_id(self) -> str:
        return f"{self.symbol}:{self.interval}"

    @property
    def record_id(self) -> str:
        return f"{self.series_id}:{self.bar_close_at.isoformat()}"

    def ohlcv_json(self) -> str:
        return json.dumps(
            {"o": self.open, "h": self.high, "low": self.low, "c": self.close, "v": self.volume},
            sort_keys=True, separators=(",", ":"),
        )


@runtime_checkable
class HistoricalBarSource(Protocol):
    """The injected real historical-bar source (IBKR / approved provider)."""

    @property
    def source_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def fetch_bars(
        self, series_id: str, *, start: datetime, end: datetime, as_of: datetime
    ) -> tuple[HistoricalBar, ...]: ...


# -- immutable raw storage ---------------------------------------------------


def raw_shard_path(root: Path, source_id: str, series_id: str) -> Path:
    """Deterministic path for one raw series shard under the storage root."""
    safe_series = series_id.replace(":", "__").replace("/", "_")
    safe_source = source_id.replace("/", "_")
    return Path(root) / "raw" / safe_source / f"{safe_series}.jsonl"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_raw_shard(
    root: Path, source_id: str, series_id: str, bars: Sequence[HistoricalBar]
) -> str:
    """Write a raw shard immutably with a checksum sidecar; return the checksum.

    Re-writing a shard with IDENTICAL bytes is idempotent; re-writing with
    DIFFERENT bytes fails closed (raw history is immutable).
    """
    path = raw_shard_path(root, source_id, series_id)
    lines = []
    for bar in sorted(bars, key=lambda b: b.bar_close_at):
        lines.append(
            json.dumps(
                {
                    "symbol": bar.symbol, "interval": bar.interval,
                    "bar_open_at": bar.bar_open_at.isoformat(),
                    "bar_close_at": bar.bar_close_at.isoformat(),
                    "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "volume": bar.volume,
                    "source_id": bar.source_id, "source_version": bar.source_version,
                },
                sort_keys=True, separators=(",", ":"),
            )
        )
    payload = ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
    checksum = _sha256(payload)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if path.exists():
        existing = path.read_bytes()
        if _sha256(existing) != checksum:
            raise IngestionError(
                f"immutable raw shard {path} would change content; fail closed"
            )
        return checksum
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    sidecar.write_text(checksum, encoding="utf-8")
    return checksum


def read_raw_shard(root: Path, source_id: str, series_id: str) -> bytes:
    """Read a raw shard and verify it against its checksum sidecar."""
    path = raw_shard_path(root, source_id, series_id)
    if not path.is_file():
        raise IngestionError(f"raw shard not found: {path}")
    data = path.read_bytes()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file():
        raise IngestionError(f"missing checksum sidecar for {path}")
    expected = sidecar.read_text(encoding="utf-8").strip()
    if _sha256(data) != expected:
        raise IngestionError(f"checksum mismatch for {path}; raw data is corrupt")
    return data


# -- durable checkpoint / resume ---------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestionCheckpoint:
    """Durable per-series cursor: the last successfully ingested bar close."""

    cursors: dict[str, str]  # series_id -> last bar_close_at ISO

    def cursor_for(self, series_id: str) -> datetime | None:
        raw = self.cursors.get(series_id)
        return datetime.fromisoformat(raw) if raw else None

    def advanced(self, series_id: str, bar_close_at: datetime) -> IngestionCheckpoint:
        merged = dict(self.cursors)
        current = self.cursor_for(series_id)
        stamp = normalize_utc(bar_close_at, "bar_close_at")
        if current is None or stamp > current:
            merged[series_id] = stamp.isoformat()
        return IngestionCheckpoint(cursors=merged)


def load_checkpoint(path: Path) -> IngestionCheckpoint:
    if not path.is_file():
        return IngestionCheckpoint(cursors={})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise IngestionError(f"corrupt checkpoint {path}: {error}") from error
    if not isinstance(data, dict) or "cursors" not in data or not isinstance(data["cursors"], dict):
        raise IngestionError(f"corrupt checkpoint {path}: bad shape (fail closed)")
    return IngestionCheckpoint(cursors={str(k): str(v) for k, v in data["cursors"].items()})


def save_checkpoint(path: Path, checkpoint: IngestionCheckpoint) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cursors": checkpoint.cursors}, sort_keys=True, indent=2), encoding="utf-8"
    )


# -- normalization to the point-in-time layout -------------------------------


def normalize_bar(bar: HistoricalBar, *, ingested_at: datetime) -> DatasetRecord:
    """Raw bar → canonical point-in-time DatasetRecord.

    A bar's OHLC is knowable only at its CLOSE, so ``event_at`` = ``observed_at``
    = ``available_at`` = ``bar_close_at`` (no look-ahead — a bar can never be
    used before it closed). ``ingested_at`` is when the system stored it.
    """
    ingested = normalize_utc(ingested_at, "ingested_at")
    if ingested < bar.bar_close_at:
        raise IngestionError(
            f"ingested_at precedes bar close for {bar.record_id}; look-ahead, fail closed"
        )
    return DatasetRecord(
        record_id=bar.record_id,
        series_id=bar.series_id,
        value=bar.ohlcv_json(),
        event_at=bar.bar_close_at,
        observed_at=bar.bar_close_at,
        available_at=bar.bar_close_at,
        ingested_at=ingested,
        source_id=bar.source_id,
        source_version=bar.source_version,
    )


@dataclass(frozen=True, slots=True)
class IngestionResult:
    records: tuple[DatasetRecord, ...]
    checkpoint: IngestionCheckpoint
    raw_checksum: str
    ingested_count: int
    skipped_count: int


def ingest_series(
    source: HistoricalBarSource,
    root: Path,
    *,
    series_id: str,
    start: datetime,
    end: datetime,
    as_of: datetime,
    ingested_at: datetime,
    checkpoint: IngestionCheckpoint,
) -> IngestionResult:
    """Fetch, immutably store, and normalize one series. Idempotent + resumable.

    Bars at or before the durable cursor are skipped, so re-running (or resuming
    after a crash) never produces duplicate records and never re-ingests.
    """
    cursor = checkpoint.cursor_for(series_id)
    bars = source.fetch_bars(series_id, start=start, end=end, as_of=as_of)
    fresh = [
        bar for bar in bars
        if cursor is None or bar.bar_close_at > cursor
    ]
    checksum = write_raw_shard(root, source.source_id, series_id, bars)
    records: list[DatasetRecord] = []
    new_checkpoint = checkpoint
    for bar in sorted(fresh, key=lambda b: b.bar_close_at):
        records.append(normalize_bar(bar, ingested_at=ingested_at))
        new_checkpoint = new_checkpoint.advanced(series_id, bar.bar_close_at)
    return IngestionResult(
        records=tuple(records),
        checkpoint=new_checkpoint,
        raw_checksum=checksum,
        ingested_count=len(records),
        skipped_count=len(bars) - len(fresh),
    )


# -- reports -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoverageReport:
    per_series: dict[str, tuple[str, str, int]]  # series -> (first, last, count)


def coverage_report(records: Sequence[DatasetRecord]) -> CoverageReport:
    per_series: dict[str, list[DatasetRecord]] = {}
    for record in records:
        per_series.setdefault(record.series_id, []).append(record)
    summary: dict[str, tuple[str, str, int]] = {}
    for series_id, series_records in per_series.items():
        ordered = sorted(series_records, key=lambda r: r.available_at)
        summary[series_id] = (
            ordered[0].available_at.isoformat(),
            ordered[-1].available_at.isoformat(),
            len(ordered),
        )
    return CoverageReport(per_series=summary)


def gap_report(
    records: Sequence[DatasetRecord], series_id: str, expected: Sequence[datetime]
) -> tuple[str, ...]:
    """Expected bar-close instants with no ingested record (never auto-filled)."""
    present = {
        record.available_at for record in records if record.series_id == series_id
    }
    missing = [
        normalize_utc(stamp, "expected").isoformat()
        for stamp in expected
        if normalize_utc(stamp, "expected") not in present
    ]
    return tuple(sorted(missing))


@dataclass(frozen=True, slots=True)
class ProvenanceReport:
    sources: tuple[dict[str, object], ...]


def provenance_report(sources: Sequence[DatasetSource]) -> ProvenanceReport:
    return ProvenanceReport(
        sources=tuple(
            {
                "source_id": s.source_id, "version": s.version, "uri": s.uri,
                "checksum": s.checksum, "record_count": s.record_count,
            }
            for s in sorted(sources, key=lambda s: s.source_id)
        )
    )


# -- manifest (de)serialization for the CLI ----------------------------------


def serialize_manifest(manifest: DatasetManifest) -> str:
    """Canonical JSON of a DatasetManifest (for the validation CLI)."""
    return json.dumps(
        {
            "dataset_id": manifest.dataset_id,
            "dataset_version": manifest.dataset_version,
            "status": manifest.status.value,
            "real_source_attestation": manifest.real_source_attestation,
            "sources": [
                {
                    "source_id": s.source_id, "version": s.version, "uri": s.uri,
                    "checksum": s.checksum, "record_count": s.record_count,
                }
                for s in manifest.sources
            ],
            "records": [
                {
                    "record_id": r.record_id, "series_id": r.series_id, "value": r.value,
                    "event_at": r.event_at.isoformat(), "observed_at": r.observed_at.isoformat(),
                    "available_at": r.available_at.isoformat(),
                    "ingested_at": r.ingested_at.isoformat(),
                    "source_id": r.source_id, "source_version": r.source_version,
                    "quality": r.quality.value, "revision": r.revision,
                    "superseded_at": r.superseded_at.isoformat() if r.superseded_at else None,
                    "corrects_record_id": r.corrects_record_id, "lineage_ref": r.lineage_ref,
                }
                for r in manifest.records
            ],
            "splits": [
                {
                    "split_id": sp.split_id, "kind": sp.kind.value,
                    "start": sp.start.isoformat(), "end": sp.end.isoformat(),
                }
                for sp in manifest.splits
            ],
        },
        sort_keys=True, indent=2,
    )


def deserialize_manifest(text: str) -> DatasetManifest:
    from trading_application.dataset_manifest import DataQuality  # local: avoid cycle at top

    try:
        data = json.loads(text)
    except ValueError as error:
        raise IngestionError(f"invalid manifest JSON: {error}") from error
    try:
        sources = tuple(
            DatasetSource(
                source_id=str(s["source_id"]), version=str(s["version"]), uri=str(s["uri"]),
                checksum=str(s["checksum"]), record_count=int(s["record_count"]),
            )
            for s in data["sources"]
        )
        records = tuple(
            DatasetRecord(
                record_id=str(r["record_id"]), series_id=str(r["series_id"]), value=str(r["value"]),
                event_at=datetime.fromisoformat(r["event_at"]),
                observed_at=datetime.fromisoformat(r["observed_at"]),
                available_at=datetime.fromisoformat(r["available_at"]),
                ingested_at=datetime.fromisoformat(r["ingested_at"]),
                source_id=str(r["source_id"]), source_version=str(r["source_version"]),
                quality=DataQuality(str(r.get("quality", "OK"))),
                revision=int(r.get("revision", 1)),
                superseded_at=(
                    datetime.fromisoformat(r["superseded_at"]) if r.get("superseded_at") else None
                ),
                corrects_record_id=r.get("corrects_record_id"), lineage_ref=r.get("lineage_ref"),
            )
            for r in data["records"]
        )
        splits = tuple(
            DatasetSplit(
                split_id=str(sp["split_id"]), kind=SplitKind(str(sp["kind"])),
                start=datetime.fromisoformat(sp["start"]), end=datetime.fromisoformat(sp["end"]),
            )
            for sp in data.get("splits", [])
        )
        return DatasetManifest(
            dataset_id=str(data["dataset_id"]), dataset_version=str(data["dataset_version"]),
            sources=sources, records=records, splits=splits,
            status=DatasetStatus(str(data.get("status", "REAL_DATASET_NOT_BUILT"))),
            real_source_attestation=data.get("real_source_attestation"),
        )
    except (KeyError, ValueError, DatasetError) as error:
        raise IngestionError(f"invalid manifest: {error}") from error


# -- minimum dataset requirement for the baseline hypothesis -----------------


@dataclass(frozen=True, slots=True)
class MinimumDatasetRequirement:
    """What the baseline hypothesis needs — references owner inputs, invents none."""

    strategy_id: str
    required_series_kinds: tuple[str, ...]
    required_fields: tuple[str, ...]
    owner_supplied_inputs: tuple[str, ...]
    notes: str


def baseline_minimum_requirement() -> MinimumDatasetRequirement:
    """The minimum real dataset for ``baseline-trend-breakout-v1``.

    Deliberately declarative: it names the DATA needed (daily OHLCV bars and a
    trading calendar) and defers every NUMBER (universe, history depth, split
    windows) to the owner's freeze — this module invents no lookback, threshold
    or window.
    """
    return MinimumDatasetRequirement(
        strategy_id="baseline-trend-breakout-v1",
        required_series_kinds=("daily-OHLCV-bars", "trading-calendar"),
        required_fields=("open", "high", "low", "close", "volume"),
        owner_supplied_inputs=(
            "universe.definition",  # which instruments
            "splits.train", "splits.validation", "splits.test_oos",  # history depth & windows
            "data.point_in_time_requirement",
        ),
        notes=(
            "IBKR historical daily bars via the existing broker/market-data boundary can"
            " satisfy OHLCV where architecturally valid; no external provider is selected"
            " here and no strategy number is invented."
        ),
    )


__all__ = [
    "INGESTION_VERSION",
    "CoverageReport",
    "HistoricalBar",
    "HistoricalBarSource",
    "IngestionCheckpoint",
    "IngestionError",
    "IngestionResult",
    "MinimumDatasetRequirement",
    "ProvenanceReport",
    "baseline_minimum_requirement",
    "coverage_report",
    "deserialize_manifest",
    "gap_report",
    "ingest_series",
    "load_checkpoint",
    "normalize_bar",
    "provenance_report",
    "raw_shard_path",
    "read_raw_shard",
    "save_checkpoint",
    "serialize_manifest",
    "write_raw_shard",
]
