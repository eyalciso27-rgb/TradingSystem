"""Run isolation, run manifests, and the checksum/identity defects the real run found.

Three real defects are pinned here:

1. duplicate corporate event ids on real SEC/XBRL data;
2. standalone verification and promotion disagreeing about the same artifact;
3. a FAILED stage silently inheriting a previous run's evidence.

Offline and deterministic: no network, no SEC request, no IBKR connection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_artifacts import ArtifactError, ArtifactType
from trading_market_data_core.dataset_v1_qualification import (
    QUALIFICATION_SCHEMA_VERSION,
    CoverageWindow,
    DataQualityMetrics,
    IdentityResolutionReport,
    IdentitySemantics,
    QualificationError,
    SourceDescriptor,
    SourceKind,
    TemporalSemantics,
    load_and_verify_evidence,
    promote_capability,
    qualify_source,
)
from trading_market_data_core.dataset_v1_qualification import _canonical as canonical_body
from trading_market_data_core.dataset_v1_run import (
    CurrentRunStatus,
    RunManifest,
    RunStager,
    StageOutcome,
    StageRecord,
    current_run_status,
    file_checksum,
    load_and_verify_run_manifest,
    new_run_id,
    verify_manifest_artifacts,
)
from trading_market_data_core.dataset_v1_run_cli import STAGE_BY_CAPABILITY
from trading_market_data_core.dataset_v1_sources import (
    SourceAdapterError,
    XbrlFact,
)
from trading_market_data_core.dataset_v1_xbrl import (
    XbrlObservation,
    normalize_observations,
    observations_from_facts,
    observations_to_events,
)

OBSERVED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _evidence(capability: str = "corporate_actions"):  # type: ignore[no-untyped-def]
    return qualify_source(
        capability,
        source=SourceDescriptor(
            source_id="sec-edgar", name="SEC EDGAR", version="2023Q1",
            kind=SourceKind.REAL, authority="OFFICIAL_PUBLIC", raw_checksum=DIGEST,
        ),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        temporal=TemporalSemantics(True, True, True),
        identity=IdentitySemantics(True, ("sec_cik",), False),
        metrics=DataQualityMetrics(total_rows=1, accepted=1, historical_changes=1),
        resolution=IdentityResolutionReport(resolved=1),
        proofs=("event_level_action",),
        observed_at=OBSERVED,
    )


# == DEFECT 2: verify and promote must authenticate the SAME body =============


def test_write_load_verify_promote_use_one_authenticated_body(tmp_path: Path) -> None:
    path = tmp_path / "corporate_actions.json"
    _evidence().write(path)
    loaded = load_and_verify_evidence(path)
    # the promotion path must not re-derive a different body
    assert loaded.compute_checksum() == loaded.normalized_checksum
    assert promote_capability(loaded).value == "EMPIRICALLY_VERIFIED"


def test_a_body_key_added_by_a_newer_build_no_longer_splits_the_paths(
    tmp_path: Path,
) -> None:
    """The exact real failure: verify PASSED, then promotion said 'refusing'.

    An artifact written before `artifact_type` existed authenticated one body,
    while promotion re-derived a body that now includes it. Reconstruction must
    never invent a body of its own.
    """
    body = {key: value for key, value in _evidence().body().items() if key != "artifact_type"}
    body["normalized_checksum"] = "sha256:" + hashlib.sha256(canonical_body(body)).hexdigest()
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")

    loaded = load_and_verify_evidence(path)  # standalone verification passes
    # Before the fix, promotion re-derived a body containing `artifact_type` and
    # raised "evidence checksum does not authenticate its body". It must now
    # authenticate the body that was actually verified.
    assert loaded.compute_checksum() == body["normalized_checksum"]
    assert promote_capability(loaded).value == "EMPIRICALLY_VERIFIED"


def test_reconstruction_never_invents_a_body_of_its_own(tmp_path: Path) -> None:
    """A defaulted field must not silently appear in the promoted body."""
    body = dict(_evidence().body())
    source = dict(body["source"])
    del source["authority"]  # written before `authority` existed
    body["source"] = source
    body["normalized_checksum"] = "sha256:" + hashlib.sha256(canonical_body(body)).hexdigest()
    path = tmp_path / "no-authority.json"
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")

    loaded = load_and_verify_evidence(path)
    assert loaded.source.authority == "PUBLIC_SECONDARY", "the default is applied in memory"
    # ...but the authenticated body still has no `authority`, so both paths agree
    assert "authority" not in loaded.authoritative_body()["source"]
    assert loaded.compute_checksum() == body["normalized_checksum"]


def test_schema_version_gate_names_both_versions(tmp_path: Path) -> None:
    body = dict(_evidence().body())
    body["schema_version"] = "dataset-v1-qualification-evidence-0.9.0"
    body["normalized_checksum"] = "sha256:" + hashlib.sha256(canonical_body(body)).hexdigest()
    path = tmp_path / "old.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(QualificationError) as error:
        load_and_verify_evidence(path)
    assert "0.9.0" in str(error.value)
    assert QUALIFICATION_SCHEMA_VERSION in str(error.value)


def test_a_rewritten_artifact_is_byte_stable(tmp_path: Path) -> None:
    first = tmp_path / "a.json"
    _evidence().write(first)
    loaded = load_and_verify_evidence(first)
    second = tmp_path / "b.json"
    loaded.write(second)
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_tampering_still_fails_after_the_unification(tmp_path: Path) -> None:
    path = tmp_path / "corporate_actions.json"
    _evidence().write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"]["historical_changes"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(QualificationError, match="checksum mismatch"):
        load_and_verify_evidence(path)


def test_file_checksum_and_body_checksum_are_named_apart(tmp_path: Path) -> None:
    """A byte digest must never be compared against a canonical body digest."""
    path = tmp_path / "corporate_actions.json"
    body_digest = _evidence().write(path)
    assert file_checksum(path) != body_digest
    assert body_digest.startswith("sha256:") and file_checksum(path).startswith("sha256:")


# == DEFECT 1/2: XBRL observation identity ====================================


def _fact(**kw: object) -> XbrlFact:
    defaults: dict[str, object] = {
        "concept": "CommonStockDividendsPerShareCashPaid",
        "unit": "USD/shares", "value": "0.24",
        "period_start": date(2023, 1, 1), "period_end": date(2023, 3, 31),
        "filed_on": date(2023, 4, 28), "accession": "0000320193-23-000064",
        "form": "10-Q",
    }
    defaults.update(kw)
    return XbrlFact(**defaults)  # type: ignore[arg-type]


def _observations(*facts: XbrlFact) -> tuple[XbrlObservation, ...]:
    return observations_from_facts(
        facts, cik="0000320193", taxonomy="us-gaap", source_checksum=DIGEST
    )


def test_identical_fact_rows_collapse() -> None:
    report = normalize_observations(_observations(_fact(), _fact()))
    assert report.total_rows == 2
    assert len(report.observations) == 1
    assert report.duplicates_collapsed == 1


def test_same_accession_and_value_but_different_period_are_distinct() -> None:
    """A 10-K reports the same dividend figure for several quarters."""
    q1 = _fact(period_start=date(2023, 1, 1), period_end=date(2023, 3, 31))
    q2 = _fact(period_start=date(2023, 4, 1), period_end=date(2023, 6, 30))
    report = normalize_observations(_observations(q1, q2))
    assert len(report.observations) == 2, "different periods are different observations"
    events = observations_to_events(report, security_id="sec-cik:1", source_id="sec-xbrl")
    assert len({event.event_id for event in events}) == 2


def test_the_same_observation_restated_later_is_not_a_second_event() -> None:
    original = _fact(accession="acc-1", filed_on=date(2023, 4, 28))
    restated = _fact(accession="acc-2", filed_on=date(2024, 2, 1))
    report = normalize_observations(_observations(original, restated))
    assert len(report.observations) == 1
    assert report.repeats == 1
    events = observations_to_events(report, security_id="sec-cik:1", source_id="sec-xbrl")
    assert len(events) == 1
    assert "re-reported in ['acc-1']" in events[0].provenance


def test_a_changed_later_value_is_a_revision_not_a_second_event() -> None:
    original = _fact(accession="acc-1", filed_on=date(2023, 4, 28), value="0.24")
    revised = _fact(accession="acc-2", filed_on=date(2024, 2, 1), value="0.25")
    report = normalize_observations(_observations(original, revised))
    assert len(report.observations) == 1
    assert report.revisions == 1
    entry = report.observations[0]
    assert entry.is_revision
    assert entry.observation.value == "0.25", "the later filing supersedes"
    events = observations_to_events(report, security_id="sec-cik:1", source_id="sec-xbrl")
    assert len(events) == 1
    assert "supersedes ['acc-1']" in events[0].provenance


def test_two_values_filed_on_the_same_day_are_a_conflict() -> None:
    a = _fact(accession="acc-1", filed_on=date(2023, 4, 28), value="0.24")
    b = _fact(accession="acc-2", filed_on=date(2023, 4, 28), value="0.25")
    with pytest.raises(SourceAdapterError, match="irreconcilable"):
        normalize_observations(_observations(a, b))


def test_a_conflict_can_be_reported_without_minting_an_event() -> None:
    a = _fact(accession="acc-1", filed_on=date(2023, 4, 28), value="0.24")
    b = _fact(accession="acc-2", filed_on=date(2023, 4, 28), value="0.25")
    report = normalize_observations(_observations(a, b), fail_on_conflict=False)
    assert len(report.conflicts) == 1
    assert report.observations == (), "a conflicted observation yields no event"


def test_event_ids_are_deterministic_and_order_independent() -> None:
    q1 = _fact(period_start=date(2023, 1, 1), period_end=date(2023, 3, 31))
    q2 = _fact(period_start=date(2023, 4, 1), period_end=date(2023, 6, 30))
    forward = observations_to_events(
        normalize_observations(_observations(q1, q2)),
        security_id="sec-cik:1", source_id="sec-xbrl",
    )
    backward = observations_to_events(
        normalize_observations(_observations(q2, q1)),
        security_id="sec-cik:1", source_id="sec-xbrl",
    )
    assert [e.event_id for e in forward] == [e.event_id for e in backward]


def test_ingesting_the_same_filer_twice_does_not_inflate_the_count() -> None:
    """The other half of the real failure: the same filer fetched repeatedly."""
    rows = _observations(_fact(), _fact(), _fact())
    report = normalize_observations(rows)
    events = observations_to_events(report, security_id="sec-cik:1", source_id="sec-xbrl")
    assert len(events) == 1
    assert report.duplicates_collapsed == 2


def test_different_units_are_different_observations() -> None:
    usd = _fact(unit="USD/shares")
    pure = _fact(unit="pure")
    assert len(normalize_observations(_observations(usd, pure)).observations) == 2


def test_observation_id_excludes_value_so_a_revision_keeps_its_identity() -> None:
    original = _observations(_fact(value="0.24"))[0]
    revised = _observations(_fact(value="0.25"))[0]
    assert original.observation_id == revised.observation_id
    assert original.fact_key != revised.fact_key


# == DEFECT 3: run isolation ==================================================


def _manifest(**kw: object) -> RunManifest:
    defaults: dict[str, object] = {
        "run_id": "run-A", "started_at": OBSERVED, "completed_at": OBSERVED,
        "git_sha": "abc123", "owner_host_mode": "owner-host",
        "versions": {"qualification_schema": QUALIFICATION_SCHEMA_VERSION},
    }
    defaults.update(kw)
    return RunManifest(**defaults)  # type: ignore[arg-type]


def test_a_failed_stage_publishes_nothing(tmp_path: Path) -> None:
    stager = RunStager(
        run_id="run-B", staging_root=tmp_path / "runs", published_root=tmp_path / "evidence"
    )
    staged = stager.stage_path("corporate_actions.json")
    _evidence().write(staged)
    published = stager.publish(
        stage="sec_corporate_actions", outcome=StageOutcome.FAILED,
        filenames=["corporate_actions.json"],
    )
    assert published == ()
    assert not (tmp_path / "evidence" / "corporate_actions.json").exists()


def test_a_partial_stage_may_publish(tmp_path: Path) -> None:
    stager = RunStager(
        run_id="run-A", staging_root=tmp_path / "runs", published_root=tmp_path / "evidence"
    )
    _evidence().write(stager.stage_path("corporate_actions.json"))
    published = stager.publish(
        stage="sec_corporate_actions", outcome=StageOutcome.PARTIAL,
        filenames=["corporate_actions.json"],
    )
    assert len(published) == 1
    assert published[0].artifact_type is ArtifactType.QUALIFICATION_EVIDENCE
    assert (tmp_path / "evidence" / "corporate_actions.json").is_file()


def test_run_b_failure_does_not_inherit_run_a_evidence(tmp_path: Path) -> None:
    """The exact scenario: A produced PARTIAL evidence, B's stage failed."""
    evidence_dir = tmp_path / "evidence"
    runs = tmp_path / "runs"

    run_a = RunStager(run_id="run-A", staging_root=runs, published_root=evidence_dir)
    _evidence().write(run_a.stage_path("corporate_actions.json"))
    a_artifacts = run_a.publish(
        stage="sec_corporate_actions", outcome=StageOutcome.PARTIAL,
        filenames=["corporate_actions.json"],
    )
    manifest_a = _manifest(
        run_id="run-A", artifacts=a_artifacts,
        stages=(
            StageRecord(
                name="sec_corporate_actions", outcome=StageOutcome.PARTIAL,
                artifacts=(a_artifacts[0].path,),
            ),
        ),
    )
    assert manifest_a.artifacts_of_type(ArtifactType.QUALIFICATION_EVIDENCE)

    # Run B fails. Run A's file is STILL on disk - that is allowed for audit.
    run_b = RunStager(run_id="run-B", staging_root=runs, published_root=evidence_dir)
    b_artifacts = run_b.publish(
        stage="sec_corporate_actions", outcome=StageOutcome.FAILED,
        filenames=["corporate_actions.json"],
    )
    manifest_b = _manifest(
        run_id="run-B", artifacts=b_artifacts,
        stages=(StageRecord(name="sec_corporate_actions", outcome=StageOutcome.FAILED),),
    )
    assert (evidence_dir / "corporate_actions.json").is_file(), "history is retained"
    assert manifest_b.artifacts_of_type(ArtifactType.QUALIFICATION_EVIDENCE) == ()
    assert (
        current_run_status(
            manifest_b, "corporate_actions", stage_by_capability=STAGE_BY_CAPABILITY
        )
        is CurrentRunStatus.CURRENT_RUN_FAILED
    )
    # and A, whose artifact IS bound, reports current evidence
    assert (
        current_run_status(
            manifest_a, "corporate_actions", stage_by_capability=STAGE_BY_CAPABILITY
        )
        is CurrentRunStatus.CURRENT_RUN_EVIDENCE
    )


def test_run_c_success_uses_only_its_own_artifacts(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    run_c = RunStager(
        run_id="run-C", staging_root=tmp_path / "runs", published_root=evidence_dir
    )
    _evidence().write(run_c.stage_path("corporate_actions.json"))
    artifacts = run_c.publish(
        stage="sec_corporate_actions", outcome=StageOutcome.SUCCEEDED,
        filenames=["corporate_actions.json"],
    )
    manifest = _manifest(
        run_id="run-C", artifacts=artifacts,
        stages=(
            StageRecord(
                name="sec_corporate_actions", outcome=StageOutcome.SUCCEEDED,
                artifacts=(artifacts[0].path,),
            ),
        ),
    )
    assert [a.stage for a in manifest.artifacts] == ["sec_corporate_actions"]
    assert (
        current_run_status(manifest, "corporate_actions", stage_by_capability=STAGE_BY_CAPABILITY)
        is CurrentRunStatus.CURRENT_RUN_EVIDENCE
    )


def test_a_stage_that_never_ran_is_no_current_evidence() -> None:
    manifest = _manifest(
        stages=(StageRecord(name="sp500_membership", outcome=StageOutcome.NOT_RUN),)
    )
    assert (
        current_run_status(
            manifest, "pit_sp500_membership", stage_by_capability=STAGE_BY_CAPABILITY
        )
        is CurrentRunStatus.NO_CURRENT_EVIDENCE
    )


# == run manifest authentication ==============================================


def test_run_manifest_round_trips_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "run-manifest.json"
    checksum = _manifest().write(path)
    loaded = load_and_verify_run_manifest(path)
    assert loaded.checksum == checksum
    assert loaded.run_id == "run-A"


def test_a_tampered_run_manifest_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "run-manifest.json"
    _manifest().write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["run_id"] = "run-Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        load_and_verify_run_manifest(path)


def test_manifest_ordering_does_not_change_its_checksum() -> None:
    stages = (
        StageRecord(name="a", outcome=StageOutcome.SUCCEEDED),
        StageRecord(name="b", outcome=StageOutcome.PARTIAL),
    )
    forward = _manifest(stages=stages).to_dict()["checksum"]
    backward = _manifest(stages=tuple(reversed(stages))).to_dict()["checksum"]
    assert forward == backward


def test_a_substituted_artifact_is_detected(tmp_path: Path) -> None:
    """Stale artifact substitution must invalidate the run."""
    evidence = tmp_path / "corporate_actions.json"
    _evidence().write(evidence)
    from trading_market_data_core.dataset_v1_run import RunArtifact

    manifest = _manifest(
        artifacts=(
            RunArtifact(
                name="corporate_actions", artifact_type=ArtifactType.QUALIFICATION_EVIDENCE,
                path=str(evidence), file_checksum=file_checksum(evidence),
                stage="sec_corporate_actions",
            ),
        )
    )
    assert verify_manifest_artifacts(manifest, root=tmp_path) == ()
    # swap in a different artifact after the run
    _evidence("delisted_security_history").write(evidence)
    findings = verify_manifest_artifacts(manifest, root=tmp_path)
    assert findings and "file bytes changed" in findings[0]


def test_a_missing_bound_artifact_is_detected(tmp_path: Path) -> None:
    from trading_market_data_core.dataset_v1_run import RunArtifact

    manifest = _manifest(
        artifacts=(
            RunArtifact(
                name="gone", artifact_type=ArtifactType.QUALIFICATION_EVIDENCE,
                path=str(tmp_path / "gone.json"), file_checksum=DIGEST,
            ),
        )
    )
    findings = verify_manifest_artifacts(manifest, root=tmp_path)
    assert findings and "missing" in findings[0]


def test_run_ids_are_unique_enough_to_separate_runs() -> None:
    assert new_run_id().startswith("run-")
    assert new_run_id(datetime(2026, 8, 18, tzinfo=UTC)).startswith("run-20260818T000000Z")


def test_ibkr_capabilities_are_not_current_when_the_ibkr_stage_did_not_run() -> None:
    """POI-IBKR-HIST.json persists between runs; that is not current evidence."""
    manifest = _manifest(
        stages=(StageRecord(name="ibkr_historical", outcome=StageOutcome.NOT_RUN),)
    )
    for capability in ("bid_ask_midpoint", "daily_trades_rth_schema"):
        assert (
            current_run_status(
                manifest, capability, stage_by_capability=STAGE_BY_CAPABILITY
            )
            is CurrentRunStatus.NO_CURRENT_EVIDENCE
        )


def test_ibkr_capabilities_are_current_when_the_stage_published(tmp_path: Path) -> None:
    artifact = tmp_path / "POI-IBKR-HIST.json"
    artifact.write_text("{}", encoding="utf-8")
    from trading_market_data_core.dataset_v1_run import RunArtifact

    manifest = _manifest(
        stages=(
            StageRecord(
                name="ibkr_historical", outcome=StageOutcome.SUCCEEDED,
                artifacts=(str(artifact),),
            ),
        ),
        # CURRENT_RUN_EVIDENCE requires the artifact to be BOUND, not merely
        # declared by the stage - that contradiction is what the real run hit.
        artifacts=(
            RunArtifact(
                name="POI-IBKR-HIST",
                artifact_type=ArtifactType.IBKR_HISTORICAL_EVIDENCE,
                path=str(artifact), file_checksum=DIGEST, stage="ibkr_historical",
            ),
        ),
    )
    assert (
        current_run_status(
            manifest, "bid_ask_midpoint", stage_by_capability=STAGE_BY_CAPABILITY
        )
        is CurrentRunStatus.CURRENT_RUN_EVIDENCE
    )


def test_an_unmapped_capability_defaults_to_no_current_evidence() -> None:
    """Defaulting to 'current' would quietly bless anything left on disk."""
    manifest = _manifest()
    assert (
        current_run_status(manifest, "something_new", stage_by_capability=STAGE_BY_CAPABILITY)
        is CurrentRunStatus.NO_CURRENT_EVIDENCE
    )


# == the exact real-run contradiction: stages ran, artifacts bound = 0 ========


def test_a_stage_whose_artifact_was_rejected_is_not_current_evidence() -> None:
    """The real run: IBKR ran, its artifacts were rejected, and the manifest
    bound zero - yet the report said CURRENT_RUN_EVIDENCE. Now impossible."""
    manifest = _manifest(
        stages=(
            StageRecord(
                name="ibkr_historical", outcome=StageOutcome.SUCCEEDED,
                artifacts=("docs/poi/evidence/POI-IBKR-HIST.json",),
            ),
        ),
        artifacts=(),  # nothing was accepted into the ledger
        failure_reasons=("ibkr_historical: artifact declares no artifact_type",),
    )
    assert (
        current_run_status(
            manifest, "bid_ask_midpoint", stage_by_capability=STAGE_BY_CAPABILITY
        )
        is CurrentRunStatus.CURRENT_RUN_FAILED
    )


def test_bound_artifacts_for_stage_is_the_only_current_truth() -> None:
    from trading_market_data_core.dataset_v1_run import (
        RunArtifact,
        bound_artifacts_for_stage,
    )

    bound = RunArtifact(
        name="corporate_actions", artifact_type=ArtifactType.QUALIFICATION_EVIDENCE,
        path="corporate_actions.json", file_checksum=DIGEST,
        stage="sec_corporate_actions",
    )
    manifest = _manifest(artifacts=(bound,))
    assert bound_artifacts_for_stage(manifest, "sec_corporate_actions") == (bound,)
    assert bound_artifacts_for_stage(manifest, "ibkr_historical") == ()


def test_manifest_checksum_covers_the_artifact_list() -> None:
    from trading_market_data_core.dataset_v1_run import RunArtifact

    empty = _manifest().to_dict()["checksum"]
    with_one = _manifest(
        artifacts=(
            RunArtifact(
                name="x", artifact_type=ArtifactType.QUALIFICATION_EVIDENCE,
                path="x.json", file_checksum=DIGEST, stage="s",
            ),
        )
    ).to_dict()["checksum"]
    assert empty != with_one, "a changed artifact list must change the checksum"


def test_manifest_checksum_covers_the_snapshot_binding() -> None:
    plain = _manifest().to_dict()["checksum"]
    bound = _manifest(snapshot_binding={"POI-IBKR-HIST": DIGEST}).to_dict()["checksum"]
    assert plain != bound
