"""Manifest binding is the ONLY authority for current-run evidence.

The real owner run showed three contradictions this file pins shut:

* the manifest reported ``artifacts bound: 0`` because the IBKR artifacts were
  untyped, yet the IBKR capabilities were labelled ``CURRENT_RUN_EVIDENCE``;
* the snapshot listed ``POI-IBKR-HIST=sha256:...`` although the manifest bound
  nothing, because the snapshot read the file directly;
* a verified identity crosswalk existed but the snapshot recorded
  ``identity_crosswalk: null``.

Directory contents are not authority. The manifest is.

Offline: no network, no IBKR connection.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_artifacts import (
    ArtifactError,
    ArtifactType,
    CrosswalkArtifact,
    CrosswalkSourceBinding,
    classify_payload,
)
from trading_market_data_core.dataset_v1_ibkr_artifacts import (
    IBKR_CORPORATE_ARTIFACT_TYPE,
    IBKR_HISTORICAL_ARTIFACT_TYPE,
    IbkrArtifactError,
    is_legacy_ibkr_evidence,
    load_and_verify_ibkr_evidence,
    migrate_legacy_ibkr_evidence,
)
from trading_market_data_core.dataset_v1_qualification import (
    CoverageWindow,
    DataQualityMetrics,
    IdentityResolutionReport,
    IdentitySemantics,
    SourceDescriptor,
    SourceKind,
    TemporalSemantics,
    qualify_source,
)
from trading_market_data_core.dataset_v1_run import (
    RunArtifact,
    RunManifest,
    StageOutcome,
    StageRecord,
    file_checksum,
)
from trading_market_data_core.dataset_v1_snapshot_cli import main as snapshot_main

OBSERVED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


# -- 1. the writers and the registry must agree on the type strings -----------


def test_ibkr_artifact_types_match_the_registry() -> None:
    """broker-ibkr emits literal strings; they must equal ArtifactType."""
    from trading_broker_ibkr.poi_corporate_actions_qualification import (
        CorporateQualificationEvidence,
    )
    from trading_broker_ibkr.poi_historical_qualification import QualificationEvidence

    assert QualificationEvidence.ARTIFACT_TYPE == IBKR_HISTORICAL_ARTIFACT_TYPE
    assert QualificationEvidence.ARTIFACT_TYPE == ArtifactType.IBKR_HISTORICAL_EVIDENCE.value
    assert CorporateQualificationEvidence.ARTIFACT_TYPE == IBKR_CORPORATE_ARTIFACT_TYPE
    assert (
        CorporateQualificationEvidence.ARTIFACT_TYPE
        == ArtifactType.IBKR_CORPORATE_EVIDENCE.value
    )


def test_a_freshly_written_ibkr_artifact_is_typed_and_bindable(tmp_path: Path) -> None:
    from trading_broker_ibkr.poi_historical_qualification import QualificationEvidence

    evidence = QualificationEvidence(account_alias="paper-poi", observed_at=OBSERVED.isoformat())
    evidence.capabilities = {"connectivity_paper_du_gate": "PASS"}
    payload = evidence.to_dict()
    assert payload["artifact_type"] == "ibkr_historical_evidence"
    assert payload["capability_scope"]
    assert classify_payload(payload) is ArtifactType.IBKR_HISTORICAL_EVIDENCE


# -- 2. legacy evidence: explicit migration only ------------------------------


def _legacy_hist(path: Path) -> Path:
    """The exact committed pre-typing shape."""
    body = {
        "poi_id": "POI-IBKR-HIST",
        "qualification_version": "poi-ibkr-hist-qualification-1.0.0",
        "account_alias": "paper-poi",
        "capabilities": {"connectivity_paper_du_gate": "PASS"},
        "symbols": [],
        "observed_at": OBSERVED.isoformat(),
    }
    import hashlib

    serialized = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["evidence_checksum"] = "sha256:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()
    path.write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_untyped_legacy_evidence_is_never_classified_by_shape(tmp_path: Path) -> None:
    payload = json.loads(_legacy_hist(tmp_path / "POI-IBKR-HIST.json").read_text(encoding="utf-8"))
    with pytest.raises(ArtifactError, match="untyped legacy IBKR evidence"):
        classify_payload(payload)


def test_untyped_legacy_evidence_cannot_be_loaded_as_typed(tmp_path: Path) -> None:
    path = _legacy_hist(tmp_path / "POI-IBKR-HIST.json")
    with pytest.raises(IbkrArtifactError, match="untyped legacy IBKR evidence"):
        load_and_verify_ibkr_evidence(path)


def test_migration_types_it_without_changing_observations(tmp_path: Path) -> None:
    path = _legacy_hist(tmp_path / "POI-IBKR-HIST.json")
    before = json.loads(path.read_text(encoding="utf-8"))
    artifact = migrate_legacy_ibkr_evidence(path)
    artifact.write(path)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["capabilities"] == before["capabilities"]
    # the digest authenticating the OBSERVATIONS survives migration untouched
    assert after["evidence_checksum"] == before["evidence_checksum"]
    assert after["artifact_type"] == "ibkr_historical_evidence"
    assert load_and_verify_ibkr_evidence(path).capability_scope


def test_a_file_that_is_not_legacy_shape_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "random.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert is_legacy_ibkr_evidence({"hello": "world"}) is None
    with pytest.raises(IbkrArtifactError, match="refusing to guess"):
        migrate_legacy_ibkr_evidence(path)


def test_a_tampered_typed_artifact_fails_closed(tmp_path: Path) -> None:
    path = _legacy_hist(tmp_path / "POI-IBKR-HIST.json")
    migrate_legacy_ibkr_evidence(path).write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["capabilities"]["connectivity_paper_du_gate"] = "FAIL"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IbkrArtifactError, match="checksum mismatch"):
        load_and_verify_ibkr_evidence(path)


# -- 3. snapshot uses ONLY manifest-bound artifacts ---------------------------


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


def _manifest_file(path: Path, artifacts: tuple[RunArtifact, ...], stages: tuple[StageRecord, ...]):  # type: ignore[no-untyped-def]
    RunManifest(
        run_id="run-C", started_at=OBSERVED, completed_at=OBSERVED, git_sha="abc",
        owner_host_mode="owner-host", versions={}, stages=stages, artifacts=artifacts,
    ).write(path)
    return path


def test_snapshot_ignores_a_valid_but_unbound_artifact(tmp_path: Path) -> None:
    """A perfectly good artifact from an earlier run must not be used."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    stale = evidence_dir / "corporate_actions.json"
    _evidence().write(stale)  # valid, verified, and NOT bound

    manifest = _manifest_file(tmp_path / "run-manifest.json", (), ())
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest),
         "--ibkr-evidence", str(tmp_path / "absent.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["evidence"] == [], "directory contents are not authority"
    assert payload["capability_states"]["corporate_actions"] == "BLOCKED_EXTERNAL"


def test_snapshot_will_not_read_ibkr_evidence_off_disk_when_a_manifest_exists(
    tmp_path: Path,
) -> None:
    """The real defect: the snapshot listed POI-IBKR-HIST with 0 bound."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    ibkr = tmp_path / "POI-IBKR-HIST.json"
    _legacy_hist(ibkr)
    manifest = _manifest_file(tmp_path / "run-manifest.json", (), ())
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest),
         "--ibkr-evidence", str(ibkr), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert not any("IBKR" in ref for ref in payload["evidence"])
    assert payload["capability_states"]["bid_ask_midpoint"] == "BLOCKED_EXTERNAL"


def test_snapshot_uses_evidence_the_manifest_binds(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    bound = evidence_dir / "corporate_actions.json"
    _evidence().write(bound)
    manifest = _manifest_file(
        tmp_path / "run-manifest.json",
        (
            RunArtifact(
                name="corporate_actions", artifact_type=ArtifactType.QUALIFICATION_EVIDENCE,
                path=str(bound), file_checksum=file_checksum(bound),
                stage="sec_corporate_actions",
            ),
        ),
        (StageRecord(name="sec_corporate_actions", outcome=StageOutcome.SUCCEEDED),),
    )
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest),
         "--ibkr-evidence", str(tmp_path / "absent.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert any("corporate_actions" in ref for ref in payload["evidence"])
    assert payload["run_id"] == "run-C"
    assert payload["run_manifest_checksum"]


def test_a_substituted_artifact_under_the_same_name_is_refused(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    bound = evidence_dir / "corporate_actions.json"
    _evidence().write(bound)
    manifest = _manifest_file(
        tmp_path / "run-manifest.json",
        (
            RunArtifact(
                name="corporate_actions", artifact_type=ArtifactType.QUALIFICATION_EVIDENCE,
                path=str(bound), file_checksum=file_checksum(bound),
                stage="sec_corporate_actions",
            ),
        ),
        (StageRecord(name="sec_corporate_actions", outcome=StageOutcome.SUCCEEDED),),
    )
    _evidence("delisted_security_history").write(bound)  # same filename, other content
    assert snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest),
         "--ibkr-evidence", str(tmp_path / "absent.json")]
    ) == 2


# -- 4. crosswalk must be bound ------------------------------------------------


def _crosswalk_file(path: Path) -> Path:
    CrosswalkArtifact(
        entries=2, resolved=("cik:1=sec-cik:1", "cik:2=sec-cik:2"), ambiguous=(),
        unresolved=(), conflicting=(), observed_at=OBSERVED,
        sources=(CrosswalkSourceBinding(name="sec-edgar", version="2023Q1", checksum=DIGEST),),
    ).sealed().write(path)
    return path


def test_a_bound_crosswalk_appears_in_the_snapshot(tmp_path: Path) -> None:
    """The real run verified a clean crosswalk but recorded null."""
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    crosswalk = _crosswalk_file(evidence_dir / "identity-crosswalk.json")
    manifest = _manifest_file(
        tmp_path / "run-manifest.json",
        (
            RunArtifact(
                name="identity-crosswalk", artifact_type=ArtifactType.IDENTITY_CROSSWALK,
                path=str(crosswalk), file_checksum=file_checksum(crosswalk),
                stage="identity_crosswalk",
            ),
        ),
        (StageRecord(name="identity_crosswalk", outcome=StageOutcome.SUCCEEDED),),
    )
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest),
         "--ibkr-evidence", str(tmp_path / "absent.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["identity_crosswalk"], "a bound crosswalk must not be null"
    assert any("identity-crosswalk" in ref for ref in payload["evidence"])


def test_an_unbound_crosswalk_is_not_silently_used(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _crosswalk_file(evidence_dir / "identity-crosswalk.json")
    manifest = _manifest_file(tmp_path / "run-manifest.json", (), ())
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest),
         "--ibkr-evidence", str(tmp_path / "absent.json"), "--out", str(out)]
    )
    assert json.loads(out.read_text(encoding="utf-8"))["identity_crosswalk"] is None
