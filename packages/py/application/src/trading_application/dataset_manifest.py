"""Point-in-time dataset foundation (Track A / A2).

Authority: CLAUDE.md ("Preserve point-in-time semantics and prevent look-ahead
in replay/backtesting"), Data_Model_and_Retention_Specification_v0.6 §24.2
(observed/available/superseded, as_of) — the same visibility rule the DB helper
`trading_persistence.point_in_time` enforces at the row level is enforced here
at the record level, offline and pure.

This module is the OFFLINE dataset machinery: a canonical, immutable
``DatasetManifest`` of point-in-time ``DatasetRecord`` values with per-record
lineage and provenance, a deterministic content fingerprint (identical inputs →
identical fingerprint → identical snapshot identity), a strict no-look-ahead
auditor, correction/amendment handling with historical validity, duplicate and
conflict resolution, train/validation/test split isolation, and a point-in-time
eligibility **audit artifact** proving exactly which records were legally
available at each evaluated ``as_of``.

Status discipline (binding): a manifest built from fixtures may be
``FIXTURE_VALIDATED`` (the machinery works) but is NEVER
``REAL_DATASET_BUILT`` — that status requires an explicit owner attestation and
real sources. **No fixture may yield HISTORICALLY_TESTED / OOS PASS**; those
words do not appear here. Until real historical data exists the canonical status
is ``REAL_DATASET_NOT_BUILT``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from trading_contracts.enums import MarketSessionStatus
from trading_domain.calendar import TradingCalendar

DATASET_FOUNDATION_VERSION = "dataset-manifest-1.0.0"


class DatasetError(Exception):
    """Fail-closed dataset failure (look-ahead, conflict, contamination)."""


class DatasetStatus(StrEnum):
    """Dataset build status — never an evidence/profitability claim."""

    REAL_DATASET_NOT_BUILT = "REAL_DATASET_NOT_BUILT"  # default until real data exists
    FIXTURE_VALIDATED = "FIXTURE_VALIDATED"  # machinery proven with deterministic fixtures
    REAL_DATASET_BUILT = "REAL_DATASET_BUILT"  # requires owner attestation + real sources


class DataQuality(StrEnum):
    """Per-record quality classification (never a silent default)."""

    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"
    CONFLICTING = "CONFLICTING"


class SplitKind(StrEnum):
    """Isolated evaluation splits — test/OOS is never touched during fitting."""

    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    TEST = "TEST"


def normalize_utc(stamp: datetime, name: str) -> datetime:
    """Require a timezone-aware instant and normalize it to UTC (clock #22)."""
    if stamp.tzinfo is None:
        raise DatasetError(f"{name} must be timezone-aware (no naive clocks)")
    return stamp.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DatasetSource:
    """One upstream source with its exact version and content checksum (#20)."""

    source_id: str
    version: str
    uri: str
    checksum: str  # sha256 of the raw source bytes
    record_count: int

    def __post_init__(self) -> None:
        for name in ("source_id", "version", "uri", "checksum"):
            if not str(getattr(self, name)).strip():
                raise DatasetError(f"DatasetSource.{name} is mandatory")
        if self.record_count < 0:
            raise DatasetError("record_count must be non-negative")


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    """One immutable point-in-time datum with lineage and four timestamps.

    Timestamps (Data_Model §24.2 / the canonical four-timestamp model):
    ``event_at`` (when it happened) ≤ ``observed_at`` (when the source noticed)
    ≤ ``available_at`` (first moment usable in a decision) ≤ ``ingested_at``
    (when the system ingested it). ``superseded_at`` (if a later correction
    exists) bounds this record's historical validity.
    """

    record_id: str
    series_id: str  # e.g. "AAPL:close:1d"
    value: str  # opaque; decimals serialized as strings
    event_at: datetime
    observed_at: datetime
    available_at: datetime
    ingested_at: datetime
    source_id: str
    source_version: str
    quality: DataQuality = DataQuality.OK
    revision: int = 1
    superseded_at: datetime | None = None
    corrects_record_id: str | None = None
    lineage_ref: str | None = None  # upstream/raw record this derives from

    def __post_init__(self) -> None:
        if not self.record_id.strip() or not self.series_id.strip():
            raise DatasetError("record_id and series_id are mandatory")
        if not self.source_id.strip() or not self.source_version.strip():
            raise DatasetError(f"record {self.record_id!r} has no source provenance")
        # Normalize + require tz-aware.
        object.__setattr__(self, "event_at", normalize_utc(self.event_at, "event_at"))
        object.__setattr__(self, "observed_at", normalize_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "available_at", normalize_utc(self.available_at, "available_at"))
        object.__setattr__(self, "ingested_at", normalize_utc(self.ingested_at, "ingested_at"))
        if self.superseded_at is not None:
            object.__setattr__(
                self, "superseded_at", normalize_utc(self.superseded_at, "superseded_at")
            )
        if self.quality is DataQuality.MISSING and self.value.strip():
            raise DatasetError(f"record {self.record_id!r} is MISSING but carries a value")
        if self.quality is not DataQuality.MISSING and not self.value.strip():
            raise DatasetError(f"record {self.record_id!r} has no value but is not MISSING")

    def content_fingerprint(self) -> str:
        material = {
            "record_id": self.record_id,
            "series_id": self.series_id,
            "value": self.value,
            "event_at": self.event_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "source_id": self.source_id,
            "source_version": self.source_version,
            "quality": self.quality.value,
            "revision": self.revision,
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
            "corrects_record_id": self.corrects_record_id,
            "lineage_ref": self.lineage_ref,
        }
        return "sha256:" + hashlib.sha256(_canonical(material)).hexdigest()

    def visible_at(self, as_of: datetime) -> bool:
        """as_of eligibility: available and not yet superseded at ``as_of``."""
        as_of = normalize_utc(as_of, "as_of")
        return self.available_at <= as_of and (
            self.superseded_at is None or self.superseded_at > as_of
        )


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    """A named, isolated evaluation window [start, end) (half-open)."""

    split_id: str
    kind: SplitKind
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not self.split_id.strip():
            raise DatasetError("split_id is mandatory")
        object.__setattr__(self, "start", normalize_utc(self.start, "split.start"))
        object.__setattr__(self, "end", normalize_utc(self.end, "split.end"))
        if self.end <= self.start:
            raise DatasetError(f"split {self.split_id!r} must end after it starts")

    def contains(self, stamp: datetime) -> bool:
        return self.start <= normalize_utc(stamp, "stamp") < self.end


def _canonical(material: object) -> bytes:
    return json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """An immutable, deterministically-fingerprinted point-in-time dataset."""

    dataset_id: str
    dataset_version: str
    sources: tuple[DatasetSource, ...]
    records: tuple[DatasetRecord, ...]
    splits: tuple[DatasetSplit, ...] = ()
    status: DatasetStatus = DatasetStatus.REAL_DATASET_NOT_BUILT
    real_source_attestation: str | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or not self.dataset_version.strip():
            raise DatasetError("dataset_id and dataset_version are mandatory")
        self._assert_unique_and_consistent()
        self._assert_sources_known()
        self._assert_split_isolation()
        if self.status is DatasetStatus.REAL_DATASET_BUILT and not (
            self.real_source_attestation or ""
        ).strip():
            raise DatasetError(
                "REAL_DATASET_BUILT requires an explicit owner source attestation;"
                " fixtures may never claim it (fail closed)"
            )

    # -- integrity -------------------------------------------------------------
    def _assert_unique_and_consistent(self) -> None:
        by_id: dict[str, str] = {}
        for record in self.records:
            fingerprint = record.content_fingerprint()
            existing = by_id.get(record.record_id)
            if existing is None:
                by_id[record.record_id] = fingerprint
            elif existing != fingerprint:
                # Same id, different content: corruption — a correction MUST use
                # a new record_id and set corrects_record_id, never reuse the id.
                raise DatasetError(
                    f"record_id {record.record_id!r} appears with conflicting"
                    " content; fail closed (a correction needs a new record_id)"
                )

    def _assert_sources_known(self) -> None:
        known = {source.source_id for source in self.sources}
        for record in self.records:
            if record.source_id not in known:
                raise DatasetError(
                    f"record {record.record_id!r} references unknown source"
                    f" {record.source_id!r}; fail closed"
                )

    def _assert_split_isolation(self) -> None:
        # No two splits may overlap in time; TEST must not overlap TRAIN/VAL.
        ordered = sorted(self.splits, key=lambda split: split.start)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.start < earlier.end:
                raise DatasetError(
                    f"splits {earlier.split_id!r} and {later.split_id!r} overlap;"
                    " split contamination is rejected"
                )

    # -- identity --------------------------------------------------------------
    def deduplicated_records(self) -> tuple[DatasetRecord, ...]:
        """Records with identical duplicates collapsed (first occurrence wins)."""
        seen: set[str] = set()
        unique: list[DatasetRecord] = []
        for record in sorted(self.records, key=lambda r: (r.record_id, r.content_fingerprint())):
            if record.record_id in seen:
                continue
            seen.add(record.record_id)
            unique.append(record)
        return tuple(unique)

    def fingerprint(self) -> str:
        """Order-independent content fingerprint; identical inputs → identical."""
        material = {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "sources": sorted(
                (
                    {
                        "source_id": s.source_id,
                        "version": s.version,
                        "uri": s.uri,
                        "checksum": s.checksum,
                        "record_count": s.record_count,
                    }
                    for s in self.sources
                ),
                key=lambda entry: str(entry["source_id"]),
            ),
            "records": sorted(
                record.content_fingerprint() for record in self.records
            ),
            "splits": sorted(
                (
                    {
                        "split_id": split.split_id,
                        "kind": split.kind.value,
                        "start": split.start.isoformat(),
                        "end": split.end.isoformat(),
                    }
                    for split in self.splits
                ),
                key=lambda entry: str(entry["split_id"]),
            ),
        }
        return "sha256:" + hashlib.sha256(_canonical(material)).hexdigest()

    def snapshot_identity(self) -> str:
        """Immutable snapshot identity derived from the content fingerprint."""
        return "snapshot:" + self.fingerprint().split(":", 1)[1][:32]

    # -- point-in-time queries -------------------------------------------------
    def visible_records(self, as_of: datetime) -> tuple[DatasetRecord, ...]:
        as_of = normalize_utc(as_of, "as_of")
        return tuple(
            record
            for record in self.deduplicated_records()
            if record.visible_at(as_of)
        )

    def records_in_split(self, split: DatasetSplit) -> tuple[DatasetRecord, ...]:
        return tuple(
            record for record in self.deduplicated_records() if split.contains(record.available_at)
        )


# -- no-look-ahead auditor ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class LookaheadViolation:
    record_id: str
    reason: str


class NoLookaheadAuditor:
    """Proves a dataset contains no look-ahead and no split contamination."""

    def audit_records(self, manifest: DatasetManifest) -> tuple[LookaheadViolation, ...]:
        violations: list[LookaheadViolation] = []
        for record in manifest.records:
            # A source cannot observe or publish a datum before it occurred, and
            # nothing may be ingested before it was available.
            if record.observed_at < record.event_at:
                violations.append(
                    LookaheadViolation(record.record_id, "observed_at precedes event_at")
                )
            if record.available_at < record.observed_at:
                violations.append(
                    LookaheadViolation(record.record_id, "available_at precedes observed_at")
                )
            if record.ingested_at < record.available_at:
                violations.append(
                    LookaheadViolation(record.record_id, "ingested_at precedes available_at")
                )
            if record.superseded_at is not None and record.superseded_at <= record.available_at:
                violations.append(
                    LookaheadViolation(record.record_id, "superseded_at not after available_at")
                )
        return tuple(violations)

    def assert_no_lookahead(self, manifest: DatasetManifest) -> None:
        violations = self.audit_records(manifest)
        if violations:
            raise DatasetError(
                "no-look-ahead audit FAILED: "
                + "; ".join(f"{v.record_id}:{v.reason}" for v in violations)
            )

    def assert_no_split_contamination(self, manifest: DatasetManifest) -> None:
        """Every record belongs to at most one split; fail closed otherwise."""
        for record in manifest.deduplicated_records():
            owning = [
                split.split_id
                for split in manifest.splits
                if split.contains(record.available_at)
            ]
            if len(owning) > 1:
                raise DatasetError(
                    f"record {record.record_id!r} falls in multiple splits {owning};"
                    " split contamination is rejected"
                )


# -- point-in-time eligibility audit artifact --------------------------------


@dataclass(frozen=True, slots=True)
class AsOfEligibility:
    as_of: datetime
    visible_record_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PointInTimeAuditArtifact:
    """Evidence: for each evaluated ``as_of``, exactly what was legally available."""

    dataset_id: str
    dataset_version: str
    dataset_fingerprint: str
    entries: tuple[AsOfEligibility, ...]
    artifact_fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "dataset_fingerprint": self.dataset_fingerprint,
            "foundation_version": DATASET_FOUNDATION_VERSION,
            "entries": [
                {"as_of": entry.as_of.isoformat(), "visible": list(entry.visible_record_ids)}
                for entry in self.entries
            ],
            "artifact_fingerprint": self.artifact_fingerprint,
        }


def build_pit_audit_artifact(
    manifest: DatasetManifest, as_of_points: Iterable[datetime]
) -> PointInTimeAuditArtifact:
    """Produce the point-in-time eligibility audit artifact (#21)."""
    entries: list[AsOfEligibility] = []
    for raw in sorted(normalize_utc(point, "as_of") for point in as_of_points):
        visible = tuple(record.record_id for record in manifest.visible_records(raw))
        entries.append(AsOfEligibility(as_of=raw, visible_record_ids=visible))
    material = {
        "dataset_fingerprint": manifest.fingerprint(),
        "entries": [
            {"as_of": entry.as_of.isoformat(), "visible": list(entry.visible_record_ids)}
            for entry in entries
        ],
    }
    artifact_fingerprint = "sha256:" + hashlib.sha256(_canonical(material)).hexdigest()
    return PointInTimeAuditArtifact(
        dataset_id=manifest.dataset_id,
        dataset_version=manifest.dataset_version,
        dataset_fingerprint=manifest.fingerprint(),
        entries=tuple(entries),
        artifact_fingerprint=artifact_fingerprint,
    )


# -- optional market-calendar alignment (#23) --------------------------------


def assert_aligned_to_calendar(
    records: Sequence[DatasetRecord], calendar: TradingCalendar
) -> None:
    """Fail closed if any record's event_at falls when the market was CLOSED.

    Reuses the canonical domain ``TradingCalendar`` (IMP-0704) — no parallel
    calendar logic. Used only where a series requires calendar alignment.
    """
    for record in records:
        if calendar.session_status(record.event_at) is MarketSessionStatus.CLOSED:
            raise DatasetError(
                f"record {record.record_id!r} event_at {record.event_at.isoformat()}"
                f" falls in a CLOSED session for calendar {calendar.calendar_id!r}"
            )


__all__ = [
    "DATASET_FOUNDATION_VERSION",
    "AsOfEligibility",
    "DataQuality",
    "DatasetError",
    "DatasetManifest",
    "DatasetRecord",
    "DatasetSource",
    "DatasetSplit",
    "DatasetStatus",
    "LookaheadViolation",
    "NoLookaheadAuditor",
    "PointInTimeAuditArtifact",
    "SplitKind",
    "assert_aligned_to_calendar",
    "build_pit_audit_artifact",
    "normalize_utc",
]
