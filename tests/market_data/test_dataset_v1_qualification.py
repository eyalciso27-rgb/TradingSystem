"""Dataset V1 real-source qualification drills (offline; nothing fabricated).

Proves the promotion boundary: only a REAL source with genuine historical
coverage, revision-safe temporal semantics, stable identity, clean validation and
an authenticated evidence checksum can promote a capability. Fixtures and
current-state snapshots never can.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1 import CapabilityState, DatasetV1
from trading_market_data_core.dataset_v1_builder import (
    AdjustmentPolicy,
    BuildConfig,
    EvidenceBinding,
    ReadinessLevel,
    SourceBinding,
    build_snapshot,
)
from trading_market_data_core.dataset_v1_qualification import (
    QUALIFIABLE_CAPABILITIES,
    CoverageWindow,
    DataQualityMetrics,
    IdentityResolutionReport,
    IdentitySemantics,
    QualificationError,
    QualificationResult,
    QualificationRun,
    SourceDescriptor,
    SourceKind,
    TemporalSemantics,
    capability_states_from_evidence,
    load_and_verify_evidence,
    promote_capability,
    qualify_source,
)
from trading_market_data_core.dataset_v1_qualification_cli import main as cli_main

OBSERVED = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
COVERAGE = CoverageWindow(date(2010, 1, 1), date(2026, 6, 30))
GOOD_TEMPORAL = TemporalSemantics(
    provides_effective_dates=True, provides_known_at=True, revision_safe=True
)
GOOD_IDENTITY = IdentitySemantics(
    provides_stable_identifier=True, identifier_kinds=("security_id", "conid"), ticker_only=False
)
GOOD_METRICS = DataQualityMetrics(
    total_rows=500, accepted=498, duplicates_collapsed=2, historical_changes=12
)
CLEAN_RESOLUTION = IdentityResolutionReport(resolved=498)
PROOFS = {
    "pit_sp500_membership": ("historical_addition", "historical_removal"),
    "asof_gics": ("historical_classification_change",),
    "delisted_security_history": ("inactive_security_resolvable",),
    "corporate_actions": ("event_level_action",),
}


def _source(
    kind: SourceKind = SourceKind.REAL, *, raw: str | None = "sha256:" + "a" * 64
) -> SourceDescriptor:
    return SourceDescriptor(
        source_id="src-1", name="owner-approved-source", version="2026.06", kind=kind,
        raw_checksum=raw, raw_artifact="membership-2026-06.csv",
    )


def _qualify(capability: str = "pit_sp500_membership", **kw: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "source": _source(), "coverage": COVERAGE, "temporal": GOOD_TEMPORAL,
        "identity": GOOD_IDENTITY, "metrics": GOOD_METRICS, "resolution": CLEAN_RESOLUTION,
        "validation_errors": (), "proofs": PROOFS[capability], "observed_at": OBSERVED,
    }
    base.update(kw)
    return qualify_source(capability, **base)  # type: ignore[arg-type]


# -- the promotion boundary ---------------------------------------------------


def test_real_source_meeting_every_criterion_passes_and_promotes() -> None:
    evidence = _qualify()
    assert evidence.result is QualificationResult.PASS
    assert evidence.promotion_eligible is True
    assert evidence.blocked_reasons == ()
    assert promote_capability(evidence) is CapabilityState.EMPIRICALLY_VERIFIED


def test_fixture_source_can_never_be_promoted() -> None:
    evidence = _qualify(source=_source(SourceKind.FIXTURE))
    assert evidence.promotion_eligible is False
    assert any("FIXTURE" in reason for reason in evidence.blocked_reasons)
    assert promote_capability(evidence) is CapabilityState.BLOCKED_EXTERNAL


def test_snapshot_only_source_cannot_satisfy_a_historical_capability() -> None:
    evidence = _qualify(source=_source(SourceKind.SNAPSHOT_ONLY))
    assert any("SNAPSHOT_ONLY" in reason for reason in evidence.blocked_reasons)
    assert promote_capability(evidence) is CapabilityState.BLOCKED_EXTERNAL


def test_zero_historical_changes_blocks_promotion() -> None:
    evidence = _qualify(metrics=replace(GOOD_METRICS, historical_changes=0))
    assert any("historical change" in reason for reason in evidence.blocked_reasons)
    assert promote_capability(evidence) is CapabilityState.BLOCKED_EXTERNAL


def test_missing_temporal_semantics_block_promotion() -> None:
    no_known_at = _qualify(temporal=replace(GOOD_TEMPORAL, provides_known_at=False))
    assert any("knowledge/observation time" in r for r in no_known_at.blocked_reasons)
    not_revision_safe = _qualify(temporal=replace(GOOD_TEMPORAL, revision_safe=False))
    assert any("revision-safe" in r for r in not_revision_safe.blocked_reasons)
    no_effective = _qualify(temporal=replace(GOOD_TEMPORAL, provides_effective_dates=False))
    assert any("effective dates" in r for r in no_effective.blocked_reasons)


def test_ticker_only_identity_blocks_promotion() -> None:
    evidence = _qualify(
        identity=IdentitySemantics(provides_stable_identifier=False, ticker_only=True)
    )
    assert any("ticker-only" in reason for reason in evidence.blocked_reasons)
    assert promote_capability(evidence) is CapabilityState.BLOCKED_EXTERNAL


def test_missing_raw_source_checksum_blocks_promotion() -> None:
    evidence = _qualify(source=_source(raw=None))
    assert any("raw source checksum" in reason for reason in evidence.blocked_reasons)


def test_validation_errors_and_dirty_identity_resolution_block_promotion() -> None:
    with_errors = _qualify(validation_errors=("overlapping interval",))
    assert any("validation error" in reason for reason in with_errors.blocked_reasons)
    ambiguous = _qualify(resolution=IdentityResolutionReport(resolved=10, ambiguous=3))
    assert any("identity resolution is not clean" in r for r in ambiguous.blocked_reasons)
    unresolved = _qualify(resolution=IdentityResolutionReport(resolved=10, unresolved=1))
    assert any("unresolved=1" in r for r in unresolved.blocked_reasons)


def test_missing_capability_proof_blocks_promotion() -> None:
    evidence = _qualify(proofs=("historical_addition",))  # removal proof missing
    assert any("missing required proofs" in reason for reason in evidence.blocked_reasons)


def test_unbounded_coverage_blocks_promotion() -> None:
    evidence = _qualify(coverage=CoverageWindow(None, None))
    assert any("coverage window is unbounded" in r for r in evidence.blocked_reasons)


def test_real_source_with_partial_data_reports_partial_not_pass() -> None:
    evidence = _qualify(metrics=replace(GOOD_METRICS, historical_changes=0))
    assert evidence.result is QualificationResult.PARTIAL  # real, some data, not eligible
    assert evidence.promotion_eligible is False


def test_unknown_capability_and_naive_timestamp_fail_closed() -> None:
    with pytest.raises(QualificationError, match="unknown qualifiable capability"):
        qualify_source(
            "not_a_capability", source=_source(), coverage=COVERAGE, temporal=GOOD_TEMPORAL,
            identity=GOOD_IDENTITY, metrics=GOOD_METRICS, resolution=CLEAN_RESOLUTION,
            observed_at=OBSERVED,
        )
    with pytest.raises(QualificationError, match="timezone-aware"):
        _qualify(observed_at=datetime(2026, 8, 17))  # noqa: DTZ001


# -- authenticated evidence ---------------------------------------------------


def test_evidence_round_trips_and_verifies(tmp_path: Path) -> None:
    evidence = _qualify()
    path = tmp_path / "pit_sp500_membership.json"
    checksum = evidence.write(path)
    loaded = load_and_verify_evidence(path)
    assert loaded.normalized_checksum == checksum
    assert loaded.capability == "pit_sp500_membership"
    assert loaded.source.raw_checksum == evidence.source.raw_checksum
    assert promote_capability(loaded) is CapabilityState.EMPIRICALLY_VERIFIED


def test_tampered_evidence_is_rejected(tmp_path: Path) -> None:
    evidence = _qualify()
    path = tmp_path / "e.json"
    evidence.write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source"]["kind"] = "REAL"
    payload["metrics"]["historical_changes"] = 9999  # tamper, keep the old checksum
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(QualificationError, match="checksum mismatch"):
        load_and_verify_evidence(path)


def test_in_memory_tampering_cannot_promote() -> None:
    evidence = _qualify(source=_source(SourceKind.FIXTURE))
    # hand-flip the verdict without resealing: promotion must refuse
    forged = replace(evidence, promotion_eligible=True, result=QualificationResult.PASS)
    with pytest.raises(QualificationError, match="does not authenticate"):
        promote_capability(forged)


def test_source_version_change_alters_the_evidence_checksum() -> None:
    first = _qualify()
    second = _qualify(source=replace(_source(), version="2026.07"))
    assert first.normalized_checksum != second.normalized_checksum


def test_evidence_is_deterministic_for_identical_inputs() -> None:
    assert _qualify().normalized_checksum == _qualify().normalized_checksum


def test_evidence_contains_no_secret_material() -> None:
    body = json.dumps(_qualify().to_dict())
    for forbidden in ("password", "api_key", "token", "secret"):
        assert forbidden not in body.lower()


# -- run-level aggregation ----------------------------------------------------


def test_states_stay_blocked_without_evidence() -> None:
    states = capability_states_from_evidence(())
    for capability in QUALIFIABLE_CAPABILITIES:
        assert states[capability] is CapabilityState.BLOCKED_EXTERNAL


def test_mixed_capability_states_are_allowed() -> None:
    passing = _qualify("pit_sp500_membership")
    failing = _qualify("asof_gics", source=_source(SourceKind.SNAPSHOT_ONLY))
    run = QualificationRun(evidences=(passing, failing))
    states = run.states()
    assert states["pit_sp500_membership"] is CapabilityState.EMPIRICALLY_VERIFIED
    assert states["asof_gics"] is CapabilityState.BLOCKED_EXTERNAL
    assert run.full_dataset_contract_ready() is False  # one blocked cell is enough


def test_full_contract_ready_requires_every_required_capability() -> None:
    ibkr = {
        name: CapabilityState.EMPIRICALLY_VERIFIED
        for name in (
            "connectivity_paper_du_gate", "daily_trades_rth_schema",
            "contract_identity_conid", "head_timestamp_coverage", "bid_ask_midpoint",
            "symbol_change_discovery",
        )
    }
    three = QualificationRun(
        evidences=tuple(_qualify(c) for c in QUALIFIABLE_CAPABILITIES[:3]), base_states=ibkr
    )
    assert three.full_dataset_contract_ready() is False
    all_four = QualificationRun(
        evidences=tuple(_qualify(c) for c in QUALIFIABLE_CAPABILITIES), base_states=ibkr
    )
    assert all_four.full_dataset_contract_ready() is True


def test_base_states_cannot_promote_a_qualifiable_capability() -> None:
    # a caller cannot smuggle a qualifiable cell in through base_states
    smuggled = {"pit_sp500_membership": CapabilityState.EMPIRICALLY_VERIFIED}
    states = capability_states_from_evidence((), base_states=smuggled)
    assert states["pit_sp500_membership"] is CapabilityState.BLOCKED_EXTERNAL


# -- readiness gate integration ----------------------------------------------


def _tiny_dataset() -> DatasetV1:
    return DatasetV1()


def test_snapshot_cannot_be_backtest_eligible_while_a_cell_is_blocked(tmp_path: Path) -> None:
    evidence = _qualify("pit_sp500_membership")
    artifact = tmp_path / "pit_sp500_membership.json"
    evidence.write(artifact)
    # EvidenceBinding authenticates the FILE bytes; QualificationEvidence
    # authenticates the canonical body. Both are real, and they differ by design.
    import hashlib

    file_digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    run = QualificationRun(evidences=(evidence,))
    snapshot = build_snapshot(
        _tiny_dataset(),
        BuildConfig(
            as_of=date(2026, 6, 30), known_as_of=OBSERVED, index_id="SP500",
            adjustment_policy=AdjustmentPolicy.RAW,
        ),
        sources=(SourceBinding("src-1", "2026.06", "sha256:" + "a" * 64),),
        evidence=(EvidenceBinding("pit_sp500_membership", artifact, file_digest),),
        capability_states=run.states(),
    )
    assert snapshot.manifest.readiness is not ReadinessLevel.BACKTEST_ELIGIBLE
    assert snapshot.backtest_eligible is False
    assert set(snapshot.manifest.blocked_capabilities) >= {"asof_gics", "corporate_actions"}


# -- CLI ----------------------------------------------------------------------


def test_cli_criteria_and_discover_report_the_truth(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_main(["prog", "criteria"]) == 0
    text = capsys.readouterr().out
    assert "pit_sp500_membership" in text and "historical changes required" in text

    assert cli_main(
        ["prog", "discover", "--config", str(tmp_path / "none.yaml"),
         "--evidence-dir", str(tmp_path / "empty")]
    ) == 0
    out = capsys.readouterr().out
    assert "no real historical source is declared" in out
    assert "NO EVIDENCE" in out


def test_cli_verify_rejects_tampered_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "e.json"
    _qualify().write(path)
    assert cli_main(["prog", "verify", "--evidence", str(path)]) == 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"]["accepted"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cli_main(["prog", "verify", "--evidence", str(path)]) == 1
    assert "EVIDENCE_INVALID" in capsys.readouterr().out


def test_cli_report_is_non_zero_while_the_contract_is_not_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_main(
        ["prog", "report", "--config", str(tmp_path / "none.yaml"),
         "--evidence-dir", str(tmp_path / "empty"),
         "--ibkr-evidence", str(tmp_path / "no-ibkr.json")]
    )
    assert rc == 1  # an owner run cannot be mistaken for success
    payload = json.loads(capsys.readouterr().out)
    assert payload["full_dataset_contract_ready"] is False
