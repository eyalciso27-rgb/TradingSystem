"""Track A / A2 — point-in-time dataset foundation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from trading_application.dataset_manifest import (
    DataQuality,
    DatasetError,
    DatasetManifest,
    DatasetRecord,
    DatasetSource,
    DatasetSplit,
    DatasetStatus,
    NoLookaheadAuditor,
    SplitKind,
    build_pit_audit_artifact,
)

EV = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)  # event
OB = EV + timedelta(minutes=1)  # observed
AV = EV + timedelta(minutes=2)  # available
IN = EV + timedelta(minutes=3)  # ingested


def source(sid: str = "sec-edgar") -> DatasetSource:
    return DatasetSource(source_id=sid, version="2026.08", uri=f"file://{sid}", checksum="abc",
                         record_count=1)


def rec(rid: str, *, series: str = "AAPL:close:1d", value: str = "230.5",
        event: datetime = EV, observed: datetime = OB, available: datetime = AV,
        ingested: datetime = IN, superseded: datetime | None = None,
        quality: DataQuality = DataQuality.OK, corrects: str | None = None,
        sid: str = "sec-edgar") -> DatasetRecord:
    return DatasetRecord(
        record_id=rid, series_id=series, value=value, event_at=event, observed_at=observed,
        available_at=available, ingested_at=ingested, source_id=sid, source_version="2026.08",
        quality=quality, superseded_at=superseded, corrects_record_id=corrects,
    )


def manifest(records: tuple[DatasetRecord, ...], *, splits: tuple[DatasetSplit, ...] = (),
             status: DatasetStatus = DatasetStatus.FIXTURE_VALIDATED) -> DatasetManifest:
    return DatasetManifest(dataset_id="ds", dataset_version="v1", sources=(source(),),
                           records=records, splits=splits, status=status)


# -- identity / determinism --------------------------------------------------


def test_default_status_is_real_not_built() -> None:
    m = DatasetManifest(dataset_id="ds", dataset_version="v1", sources=(source(),),
                        records=(rec("r1"),))
    assert m.status is DatasetStatus.REAL_DATASET_NOT_BUILT


def test_fingerprint_is_deterministic_and_order_independent() -> None:
    h = timedelta(hours=1)
    r2 = rec("r2", event=EV + h, observed=OB + h, available=AV + h, ingested=IN + h)
    a = manifest((rec("r1"), r2))
    b = manifest((r2, rec("r1")))
    assert a.fingerprint() == b.fingerprint()
    assert a.snapshot_identity() == b.snapshot_identity()


def test_deterministic_rebuild_same_fingerprint() -> None:
    assert manifest((rec("r1"),)).fingerprint() == manifest((rec("r1"),)).fingerprint()


def test_fixture_cannot_claim_real_dataset_built() -> None:
    with pytest.raises(DatasetError, match="attestation"):
        DatasetManifest(dataset_id="ds", dataset_version="v1", sources=(source(),),
                        records=(rec("r1"),), status=DatasetStatus.REAL_DATASET_BUILT)


# -- point-in-time / as_of ---------------------------------------------------


def test_record_not_visible_before_available() -> None:
    m = manifest((rec("r1"),))
    assert m.visible_records(AV - timedelta(seconds=1)) == ()
    assert len(m.visible_records(AV)) == 1


def test_superseded_correction_has_historical_validity() -> None:
    correction_time = AV + timedelta(days=1)
    original = rec("r1", value="100", superseded=correction_time)
    amended = rec("r2", value="105", event=EV, observed=correction_time, available=correction_time,
                  ingested=correction_time + timedelta(minutes=1), corrects="r1")
    m = manifest((original, amended))
    # before the correction: original visible, amendment not
    early = m.visible_records(AV + timedelta(hours=1))
    assert {r.record_id for r in early} == {"r1"}
    # after the correction: original superseded, amendment visible
    late = m.visible_records(correction_time + timedelta(hours=1))
    assert {r.record_id for r in late} == {"r2"}


# -- duplicate / conflict ----------------------------------------------------


def test_identical_duplicate_is_deduplicated() -> None:
    m = manifest((rec("r1"), rec("r1")))
    assert len(m.deduplicated_records()) == 1


def test_conflicting_record_id_fails_closed() -> None:
    with pytest.raises(DatasetError, match="conflicting content"):
        manifest((rec("r1", value="100"), rec("r1", value="999")))


def test_unknown_source_fails_closed() -> None:
    with pytest.raises(DatasetError, match="unknown source"):
        manifest((rec("r1", sid="mystery"),))


# -- quality classification --------------------------------------------------


def test_missing_quality_must_not_carry_a_value() -> None:
    with pytest.raises(DatasetError, match="MISSING"):
        rec("r1", quality=DataQuality.MISSING, value="123")


def test_missing_record_has_no_value() -> None:
    r = rec("r1", quality=DataQuality.MISSING, value="")
    assert r.quality is DataQuality.MISSING


# -- no-look-ahead auditor ---------------------------------------------------


def test_auditor_passes_clean_dataset() -> None:
    NoLookaheadAuditor().assert_no_lookahead(manifest((rec("r1"), rec("r2"))))


def test_auditor_rejects_observed_before_event() -> None:
    bad = rec("r1", observed=EV - timedelta(seconds=1))
    with pytest.raises(DatasetError, match="no-look-ahead audit FAILED"):
        NoLookaheadAuditor().assert_no_lookahead(manifest((bad,)))


def test_auditor_rejects_ingested_before_available() -> None:
    bad = rec("r1", ingested=AV - timedelta(seconds=1))
    with pytest.raises(DatasetError, match="FAILED"):
        NoLookaheadAuditor().assert_no_lookahead(manifest((bad,)))


# -- split isolation ---------------------------------------------------------


def test_overlapping_splits_are_rejected() -> None:
    train = DatasetSplit("train", SplitKind.TRAIN, EV, EV + timedelta(days=10))
    test = DatasetSplit("test", SplitKind.TEST, EV + timedelta(days=5), EV + timedelta(days=15))
    with pytest.raises(DatasetError, match="contamination"):
        manifest((rec("r1"),), splits=(train, test))


def test_split_contamination_check_passes_for_disjoint_windows() -> None:
    train = DatasetSplit("train", SplitKind.TRAIN, EV - timedelta(days=10), EV - timedelta(days=1))
    test = DatasetSplit("test", SplitKind.TEST, EV, EV + timedelta(days=10))
    m = manifest((rec("r1"),), splits=(train, test))
    NoLookaheadAuditor().assert_no_split_contamination(m)
    assert {r.record_id for r in m.records_in_split(test)} == {"r1"}


# -- PIT audit artifact ------------------------------------------------------


def test_pit_audit_artifact_records_exact_visibility() -> None:
    m = manifest((rec("r1"), rec("r2", available=AV + timedelta(days=1),
                                 ingested=IN + timedelta(days=1))))
    artifact = build_pit_audit_artifact(m, [AV, AV + timedelta(days=1)])
    assert artifact.entries[0].visible_record_ids == ("r1",)
    assert set(artifact.entries[1].visible_record_ids) == {"r1", "r2"}
    assert artifact.artifact_fingerprint.startswith("sha256:")
    # deterministic
    again = build_pit_audit_artifact(m, [AV + timedelta(days=1), AV])
    assert again.artifact_fingerprint == artifact.artifact_fingerprint


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(DatasetError, match="timezone-aware"):
        rec("r1", event=datetime(2026, 8, 10, 13, 0))  # naive
