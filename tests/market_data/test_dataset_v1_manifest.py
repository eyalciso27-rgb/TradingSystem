"""A-WP2 — dataset manifest lineage and no-look-ahead binding (closure proof).

This is a deterministic, OFFLINE proof that the Dataset V1 manifest machinery
already in this repository satisfies the A-WP2 acceptance shape end to end:

* a ``DatasetManifest`` binds every record to an immutable SHA-256 fingerprint
  and to explicit point-in-time timestamps (``trading_application.dataset_manifest``);
* the market-data-core builder binds evidence and source artifacts to their
  file-content SHA-256 (never a filename or a label) and re-verifies it, so a
  post-hoc substitution is rejected, not silently trusted
  (``trading_market_data_core.dataset_v1_builder``);
* an owner run's manifest binds every artifact it produced to a file-content
  checksum, and a byte change on disk after the run is detected
  (``trading_market_data_core.dataset_v1_run``);
* each reference capability is classified into exactly one of three states —
  ``EMPIRICALLY_VERIFIED`` (task vocabulary: COMPLETE),
  ``CONTRACT_READY`` (PARTIAL — fixture proves the machinery only, never
  empirical evidence), and ``BLOCKED_EXTERNAL`` (EXTERNAL_BLOCKED) —
  never fabricated (``trading_market_data_core.dataset_v1_ingestion``);
* a look-ahead violation is rejected, both as an explicit fail-closed audit
  (``NoLookaheadAuditor``) and structurally, by excluding any record that was
  not yet knowable at the evaluated ``as_of`` / ``known_as_of`` cutoff from a
  materialized snapshot and from a bitemporal as-of query.

Everything here uses in-memory or ``tmp_path`` fixtures only. No network call,
no broker/SEC/S&P access, no strategy parameter is touched, and nothing here
claims real integration evidence: capabilities that are proven only from a
``FIXTURE`` source stay ``CONTRACT_READY`` at best, never ``EMPIRICALLY_VERIFIED``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from trading_application.dataset_manifest import (
    DatasetError,
    DatasetManifest,
    DatasetRecord,
    DatasetSource,
    NoLookaheadAuditor,
)
from trading_market_data_core.dataset_v1 import (
    DATASET_V1_REQUIRED_CAPABILITIES,
    CapabilityState,
    DailyPriceRecord,
    DatasetV1,
    EffectiveInterval,
    MembershipRecord,
    PriceAdjustmentStatus,
    Provenance,
    SecurityIdentity,
)
from trading_market_data_core.dataset_v1_artifacts import ArtifactError, ArtifactType
from trading_market_data_core.dataset_v1_builder import (
    AdjustmentPolicy,
    BuildConfig,
    BuilderError,
    EvidenceBinding,
    ReadinessLevel,
    SourceBinding,
    build_snapshot,
)
from trading_market_data_core.dataset_v1_ingestion import (
    SourceCapabilities,
    SourceKind,
    assess_reference_capability,
)
from trading_market_data_core.dataset_v1_run import (
    RunArtifact,
    RunManifest,
    StageOutcome,
    StageRecord,
    file_checksum,
    load_and_verify_run_manifest,
    verify_manifest_artifacts,
)

AS_OF = date(2021, 6, 30)
KNOWN_AS_OF = datetime(2021, 7, 1, tzinfo=UTC)


# -- helpers ------------------------------------------------------------------


def _manifest_source(source_id: str = "fixture-source") -> DatasetSource:
    return DatasetSource(
        source_id=source_id, version="2026.08", uri=f"file://{source_id}",
        checksum="sha256:" + "0" * 64, record_count=1,
    )


def _evidence_binding(tmp_path: Path, name: str = "reference-evidence") -> EvidenceBinding:
    """A genuinely verifiable evidence artifact — its checksum is over its
    ACTUAL bytes on disk, exactly what ``DatasetManifest`` evidence must bind."""
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"artifact": name}), encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return EvidenceBinding(name=name, path=path, checksum=digest)


# -- 1. DatasetManifest binds immutable SHA-256 + point-in-time timestamps ----


def test_dataset_manifest_fingerprint_is_immutable_sha256() -> None:
    """``DatasetManifest``/``DatasetRecord`` fingerprints are literal sha256
    digests over content INCLUDING the four point-in-time timestamps; identical
    content always yields an identical fingerprint, and a record_id may never
    be reused with different content (that is corruption, not a correction)."""
    event = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
    record = DatasetRecord(
        record_id="r1", series_id="AAPL:close:1d", value="230.5",
        event_at=event, observed_at=event, available_at=event, ingested_at=event,
        source_id="fixture-source", source_version="2026.08",
    )
    manifest = DatasetManifest(
        dataset_id="ds-awp2", dataset_version="v1", sources=(_manifest_source(),),
        records=(record,),
    )
    fingerprint = record.content_fingerprint()
    assert fingerprint.startswith("sha256:")
    assert manifest.fingerprint().startswith("sha256:")

    same_content = DatasetRecord(
        record_id="r1", series_id="AAPL:close:1d", value="230.5",
        event_at=event, observed_at=event, available_at=event, ingested_at=event,
        source_id="fixture-source", source_version="2026.08",
    )
    assert same_content.content_fingerprint() == fingerprint  # immutable, not a label

    conflicting = DatasetRecord(
        record_id="r1", series_id="AAPL:close:1d", value="999.9",  # same id, different content
        event_at=event, observed_at=event, available_at=event, ingested_at=event,
        source_id="fixture-source", source_version="2026.08",
    )
    with pytest.raises(DatasetError, match="conflicting content"):
        DatasetManifest(
            dataset_id="ds-awp2", dataset_version="v1", sources=(_manifest_source(),),
            records=(record, conflicting),
        )


def test_dataset_manifest_record_not_visible_before_it_is_available() -> None:
    """Point-in-time binding: a record is only visible at an ``as_of`` on or
    after its ``available_at`` — the earliest instant it could legally be used
    in a decision. Nothing here treats a record as usable ahead of time."""
    event = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
    available = event + timedelta(minutes=5)
    record = DatasetRecord(
        record_id="r1", series_id="AAPL:close:1d", value="230.5",
        event_at=event, observed_at=event, available_at=available, ingested_at=available,
        source_id="fixture-source", source_version="2026.08",
    )
    assert record.visible_at(available - timedelta(minutes=1)) is False
    assert record.visible_at(available) is True


# -- 2. rejects a look-ahead violation (DatasetManifest audit + bitemporal) ---


def test_no_lookahead_auditor_rejects_a_look_ahead_violation() -> None:
    """The literal requirement: a record whose source claims to have observed a
    fact BEFORE it happened is a look-ahead violation and is rejected fail-closed."""
    event = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)
    clean = DatasetRecord(
        record_id="clean", series_id="AAPL:close:1d", value="230.5",
        event_at=event, observed_at=event, available_at=event, ingested_at=event,
        source_id="fixture-source", source_version="2026.08",
    )
    look_ahead = DatasetRecord(
        record_id="look-ahead", series_id="AAPL:close:1d", value="231.0",
        event_at=event, observed_at=event - timedelta(days=1),  # "observed" before it happened
        available_at=event, ingested_at=event,
        source_id="fixture-source", source_version="2026.08",
    )
    clean_manifest = DatasetManifest(
        dataset_id="ds-awp2", dataset_version="v1", sources=(_manifest_source(),),
        records=(clean,),
    )
    NoLookaheadAuditor().assert_no_lookahead(clean_manifest)  # must not raise

    violating_manifest = DatasetManifest(
        dataset_id="ds-awp2", dataset_version="v1", sources=(_manifest_source(),),
        records=(clean, look_ahead),
    )
    with pytest.raises(DatasetError, match="no-look-ahead audit FAILED"):
        NoLookaheadAuditor().assert_no_lookahead(violating_manifest)


def test_bitemporal_correction_does_not_leak_backward_before_it_is_known() -> None:
    """No-look-ahead at the Dataset V1 reference layer: a LATER restatement of
    index membership must be invisible to a query whose knowledge cutoff
    precedes the restatement's own ``known_at``. A decision made in the past
    can never see a correction from the future."""
    original = MembershipRecord(
        record_id="m-orig", index_id="SP500", security_id="sec-a",
        interval=EffectiveInterval(date(2015, 1, 1)),
        known_at=datetime(2015, 1, 1, tzinfo=UTC),
        provenance=Provenance("fixture-source", datetime(2015, 1, 1, tzinfo=UTC)),
    )
    correction = MembershipRecord(
        record_id="m-corrected", index_id="SP500", security_id="sec-a",
        interval=EffectiveInterval(date(2015, 1, 1), date(2020, 12, 31)),  # discovered later
        known_at=datetime(2022, 1, 1, tzinfo=UTC),
        provenance=Provenance("fixture-source", datetime(2022, 1, 1, tzinfo=UTC)),
        supersedes="m-orig",
    )
    dataset = DatasetV1(
        securities=(SecurityIdentity("sec-a", "AAA", "NASDAQ"),),
        memberships=(original, correction),
    )
    # Evaluated as of 2021-06-30 with a knowledge cutoff BEFORE the correction
    # was known: the original (still-a-member) view holds. This is the
    # look-ahead rejection — the restatement has not happened yet.
    early_cutoff = datetime(2021, 6, 30, tzinfo=UTC)
    assert dataset.members_as_of("SP500", date(2021, 6, 30), known_as_of=early_cutoff) == frozenset(
        {"sec-a"}
    )
    # Once the correction IS known, the restated view applies.
    late_cutoff = datetime(2022, 6, 30, tzinfo=UTC)
    assert dataset.members_as_of("SP500", date(2021, 6, 30), known_as_of=late_cutoff) == frozenset()


def test_builder_excludes_bars_after_as_of_and_bars_not_yet_knowable() -> None:
    """The materialized snapshot enforces BOTH look-ahead axes: an event that
    has not yet happened by ``as_of``, and an event that happened but was not
    yet knowable by ``known_as_of`` — both are look-ahead and both are excluded."""
    knowable = Provenance("fixture-source", datetime(2021, 6, 29, tzinfo=UTC))
    unknowable = Provenance("fixture-source", datetime(2026, 1, 1, tzinfo=UTC))
    dataset = DatasetV1(
        securities=(SecurityIdentity("sec-a", "AAA", "NASDAQ"),),
        memberships=(
            MembershipRecord(
                record_id="m-a", index_id="SP500", security_id="sec-a",
                interval=EffectiveInterval(date(2015, 1, 1)),
                known_at=datetime(2015, 1, 1, tzinfo=UTC), provenance=knowable,
            ),
        ),
        prices=(
            DailyPriceRecord(
                "sec-a", date(2021, 6, 29), Decimal("100"), knowable,
                adjustment_status=PriceAdjustmentStatus.RAW_UNADJUSTED,
            ),
            DailyPriceRecord(  # after as_of: the event has not happened yet
                "sec-a", date(2021, 7, 1), Decimal("999"), knowable,
                adjustment_status=PriceAdjustmentStatus.RAW_UNADJUSTED,
            ),
            DailyPriceRecord(  # before as_of but not yet knowable at the cutoff
                "sec-a", date(2021, 6, 28), Decimal("77"), unknowable,
                adjustment_status=PriceAdjustmentStatus.RAW_UNADJUSTED,
            ),
        ),
    )
    config = BuildConfig(
        as_of=AS_OF, known_as_of=KNOWN_AS_OF, index_id="SP500",
        adjustment_policy=AdjustmentPolicy.RAW,
    )
    snapshot = build_snapshot(
        dataset, config, sources=(SourceBinding("fixture-source", "v1", "sha256:" + "a" * 64),)
    )
    assert {p.trading_date for p in snapshot.prices} == {date(2021, 6, 29)}


# -- 3. market-data-core binds immutable artifact SHA-256 (never a label) ----


def test_snapshot_manifest_binds_evidence_and_source_to_file_content_sha256(
    tmp_path: Path,
) -> None:
    """The builder binds evidence/source to the ACTUAL file bytes, and a
    post-hoc substitution of those bytes is rejected on re-verification —
    the immutable-artifact-SHA-256 half of the A-WP2 requirement."""
    evidence = _evidence_binding(tmp_path)
    source = SourceBinding(name="fixture-source", version="v1", checksum=evidence.checksum)
    dataset = DatasetV1(securities=(SecurityIdentity("sec-a", "AAA", "NASDAQ"),))
    config = BuildConfig(
        as_of=AS_OF, known_as_of=KNOWN_AS_OF, index_id="SP500",
        adjustment_policy=AdjustmentPolicy.RAW,
    )
    snapshot = build_snapshot(dataset, config, sources=(source,), evidence=(evidence,))
    assert snapshot.manifest.evidence == (f"reference-evidence={evidence.checksum}",)
    assert snapshot.manifest.sources == (source,)

    # Tamper the evidence bytes AFTER the snapshot was produced: the binding
    # authenticates CONTENT, not a filename, so re-verification fails closed.
    evidence.path.write_text(json.dumps({"artifact": "tampered"}), encoding="utf-8")
    with pytest.raises(BuilderError, match="checksum mismatch"):
        evidence.verify()

    # A fabricated (never-computed) checksum is rejected outright, even before
    # any tampering — a decorative digest cannot authorize a build.
    fabricated = EvidenceBinding(
        name="reference-evidence", path=evidence.path, checksum="sha256:" + "f" * 64
    )
    with pytest.raises(BuilderError, match="checksum mismatch"):
        fabricated.verify()


def test_run_manifest_binds_immutable_artifact_sha256_and_detects_tamper(
    tmp_path: Path,
) -> None:
    """An owner run's manifest binds each artifact it produced to a
    file-content SHA-256. A byte change on disk after the run — even without
    touching the manifest — is detected, and a hand-edited manifest checksum
    is rejected on reload. This is the run-level half of immutable binding."""
    artifact_path = tmp_path / "pit_sp500_membership.json"
    artifact_path.write_text(
        json.dumps(
            {
                "artifact_type": "auxiliary_data_quality_report",
                "schema_version": "dataset-v1-artifacts-1.0.0",
                "qualifies_nothing": True,
            }
        ),
        encoding="utf-8",
    )
    checksum = file_checksum(artifact_path)

    manifest = RunManifest(
        run_id="run-awp2-test-0001",
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        completed_at=datetime(2026, 8, 1, 0, 5, tzinfo=UTC),
        git_sha="deadbeef",
        owner_host_mode="fixture",
        versions={"builder": "1.0.0"},
        stages=(
            StageRecord(
                name="sp500_membership", outcome=StageOutcome.SUCCEEDED,
                artifacts=(str(artifact_path),),
            ),
        ),
        artifacts=(
            RunArtifact(
                name="pit_sp500_membership",
                artifact_type=ArtifactType.AUXILIARY_DATA_QUALITY_REPORT,
                path=str(artifact_path), file_checksum=checksum, stage="sp500_membership",
            ),
        ),
    )
    manifest_path = tmp_path / "run-manifest.json"
    written_checksum = manifest.write(manifest_path)
    assert written_checksum.startswith("sha256:")

    reloaded = load_and_verify_run_manifest(manifest_path)
    assert verify_manifest_artifacts(reloaded, root=tmp_path) == ()

    # Tamper the BOUND artifact's bytes after the run: detected even though
    # the manifest itself was never touched.
    artifact_path.write_text(
        json.dumps(
            {
                "artifact_type": "auxiliary_data_quality_report",
                "schema_version": "dataset-v1-artifacts-1.0.0",
                "qualifies_nothing": True,
                "tampered": True,
            }
        ),
        encoding="utf-8",
    )
    findings = verify_manifest_artifacts(reloaded, root=tmp_path)
    assert findings and "file bytes changed since the run" in findings[0]

    # A hand-edited manifest checksum must fail closed on reload.
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["git_sha"] = "hand-edited-post-hoc"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        load_and_verify_run_manifest(manifest_path)


# -- 4. classifies capabilities as COMPLETE / PARTIAL / EXTERNAL_BLOCKED -----


def test_capability_classification_complete_partial_external_blocked() -> None:
    """Task vocabulary <-> implementation mapping, proven from fixtures alone:

    * COMPLETE        -> ``CapabilityState.EMPIRICALLY_VERIFIED``
    * PARTIAL          -> ``CapabilityState.CONTRACT_READY``
      (a clean FIXTURE source proves the machinery only; it can never be
      promoted to COMPLETE, however clean its metrics look)
    * EXTERNAL_BLOCKED -> ``CapabilityState.BLOCKED_EXTERNAL``
    """
    full_caps = SourceCapabilities(
        provides_effective_dates=True, provides_known_at=True, provides_stable_identity=True
    )

    complete = assess_reference_capability(
        "pit_sp500_membership", source_kind=SourceKind.REAL, capabilities=full_caps,
        validation_errors=(), historical_change_count=3,
        real_evidence_checksum="sha256:" + "1" * 64,
    )
    assert complete.state is CapabilityState.EMPIRICALLY_VERIFIED
    assert complete.promotable

    partial = assess_reference_capability(
        "asof_gics", source_kind=SourceKind.FIXTURE, capabilities=full_caps,
        validation_errors=(), historical_change_count=2,
        real_evidence_checksum="sha256:" + "2" * 64,
    )
    assert partial.state is CapabilityState.CONTRACT_READY
    assert not partial.promotable

    external_blocked = assess_reference_capability(
        "delisted_security_history", source_kind=SourceKind.REAL,
        capabilities=SourceCapabilities(
            provides_effective_dates=False, provides_known_at=False,
            provides_stable_identity=True,
        ),
        validation_errors=(), historical_change_count=0, real_evidence_checksum=None,
    )
    assert external_blocked.state is CapabilityState.BLOCKED_EXTERNAL
    assert not external_blocked.promotable


def test_snapshot_readiness_reflects_capability_classification_not_a_wish(
    tmp_path: Path,
) -> None:
    """Readiness is DERIVED from the classification, never asserted
    independently of it: mixing one COMPLETE capability with the rest
    EXTERNAL_BLOCKED yields PARTIAL_DATA, and the blocked set names exactly
    the capabilities that are not COMPLETE."""
    evidence = _evidence_binding(tmp_path)
    source = SourceBinding("fixture-source", "v1", evidence.checksum)
    states = dict.fromkeys(DATASET_V1_REQUIRED_CAPABILITIES, CapabilityState.BLOCKED_EXTERNAL)
    states["pit_sp500_membership"] = CapabilityState.EMPIRICALLY_VERIFIED  # COMPLETE
    dataset = DatasetV1(securities=(SecurityIdentity("sec-a", "AAA", "NASDAQ"),))
    config = BuildConfig(
        as_of=AS_OF, known_as_of=KNOWN_AS_OF, index_id="SP500",
        adjustment_policy=AdjustmentPolicy.RAW,
    )
    snapshot = build_snapshot(
        dataset, config, sources=(source,), evidence=(evidence,), capability_states=states,
    )
    assert snapshot.manifest.readiness is ReadinessLevel.PARTIAL_DATA
    assert "pit_sp500_membership" not in snapshot.manifest.blocked_capabilities
    assert "asof_gics" in snapshot.manifest.blocked_capabilities  # still EXTERNAL_BLOCKED
    assert not snapshot.backtest_eligible
