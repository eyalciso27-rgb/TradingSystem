"""End-to-end: typed IBKR artifact -> manifest bind -> report -> snapshot.

The owner run reached ``current_run = CURRENT_RUN_FAILED`` for the six IBKR
capabilities because the artifacts would not authenticate. This proves the whole
chain with no transformation drift, using artifacts shaped like the real ones.

Offline: no network, no IBKR connection.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_broker_ibkr.poi_corporate_actions_qualification import (
    CorporateQualificationEvidence,
)
from trading_broker_ibkr.poi_historical_qualification import QualificationEvidence
from trading_market_data_core.dataset_v1_artifacts import ArtifactType
from trading_market_data_core.dataset_v1_ibkr_artifacts import (
    inner_evidence_checksum,
    load_and_verify_ibkr_evidence,
)
from trading_market_data_core.dataset_v1_run import (
    CurrentRunStatus,
    StageOutcome,
    current_run_status,
)
from trading_market_data_core.dataset_v1_run_cli import STAGE_BY_CAPABILITY
from trading_market_data_core.dataset_v1_run_cli import main as run_main
from trading_market_data_core.dataset_v1_snapshot_cli import main as snapshot_main

OBSERVED = "2026-08-18T00:00:00+00:00"
IBKR_CAPABILITIES = (
    "bid_ask_midpoint",
    "connectivity_paper_du_gate",
    "contract_identity_conid",
    "daily_trades_rth_schema",
    "head_timestamp_coverage",
    "symbol_change_discovery",
)


def _historical_payload() -> dict[str, Any]:
    evidence = QualificationEvidence(account_alias="paper-poi", observed_at=OBSERVED)
    evidence.masked_account_id = "DU***567"
    evidence.capabilities = dict.fromkeys(IBKR_CAPABILITIES, "PASS")
    evidence.active_name_price_qualification_sufficient = True
    evidence.full_dataset_contract_ready = False
    evidence.evidence_checksum = inner_evidence_checksum(evidence.body_for_checksum())
    return evidence.to_dict()


def _corporate_payload() -> dict[str, Any]:
    evidence = CorporateQualificationEvidence(account_alias="paper-poi", observed_at=OBSERVED)
    evidence.masked_account_id = "DU***567"
    evidence.probes = [
        {"capability": "CORPORATE_EVENT_FEED", "outcome": "UNAVAILABLE", "detail": "none"},
    ]
    evidence.adjustment_observations = [
        {
            "symbol": "AAPL", "bar_date": "20230103", "trades_close": "100",
            "adjusted_last_close": "25", "diverges": True, "inference_boundary": "adjustment",
        }
    ]
    evidence.evidence_checksum = inner_evidence_checksum(evidence.body_for_checksum())
    return evidence.to_dict()


def _run(tmp_path: Path, *, publish_ibkr: bool) -> tuple[Path, Path]:
    """Record stages the way the runner does, then assemble the manifest."""
    run_root = tmp_path / "runs"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    hist = evidence_dir / "POI-IBKR-HIST.json"
    hist.write_text(json.dumps(_historical_payload(), indent=2), encoding="utf-8")
    corp = evidence_dir / "POI-IBKR-CORP.json"
    corp.write_text(json.dumps(_corporate_payload(), indent=2), encoding="utf-8")

    outcome = "PASS" if publish_ibkr else "FAILED"
    for stage, artifact in (("ibkr_historical", hist), ("ibkr_corporate", corp)):
        run_main(
            ["prog", "stage", "--run-id", "run-X", "--run-root", str(run_root),
             "--stage-name", stage, "--outcome",
             "SUCCEEDED" if outcome == "PASS" else "FAILED", "--artifact", str(artifact)]
        )
    run_main(["prog", "manifest", "--run-id", "run-X", "--run-root", str(run_root)])
    return run_root / "run-X" / "run-manifest.json", evidence_dir


# -- manifest binding ----------------------------------------------------------


def test_a_freshly_written_historical_artifact_binds(tmp_path: Path) -> None:
    manifest_path, _ = _run(tmp_path, publish_ibkr=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bound = {a["artifact_type"] for a in manifest["artifacts"]}
    assert "ibkr_historical_evidence" in bound
    assert not manifest["failure_reasons"], manifest["failure_reasons"]


def test_a_freshly_written_corporate_artifact_binds(tmp_path: Path) -> None:
    manifest_path, _ = _run(tmp_path, publish_ibkr=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    bound = {a["artifact_type"] for a in manifest["artifacts"]}
    assert "ibkr_corporate_evidence" in bound


def test_binding_survives_a_read_back_of_the_written_file(tmp_path: Path) -> None:
    _, evidence_dir = _run(tmp_path, publish_ibkr=True)
    artifact = load_and_verify_ibkr_evidence(evidence_dir / "POI-IBKR-HIST.json")
    assert set(artifact.capability_states()) == set(IBKR_CAPABILITIES)
    assert all(v == "PASS" for v in artifact.capability_states().values())


# -- current-run report --------------------------------------------------------


def test_current_run_evidence_becomes_true_once_bound(tmp_path: Path) -> None:
    from trading_market_data_core.dataset_v1_run import load_and_verify_run_manifest

    manifest_path, _ = _run(tmp_path, publish_ibkr=True)
    manifest = load_and_verify_run_manifest(manifest_path)
    for capability in IBKR_CAPABILITIES:
        assert (
            current_run_status(
                manifest, capability, stage_by_capability=STAGE_BY_CAPABILITY
            )
            is CurrentRunStatus.CURRENT_RUN_EVIDENCE
        )


def test_a_failed_ibkr_stage_still_reports_failed(tmp_path: Path) -> None:
    from trading_market_data_core.dataset_v1_run import load_and_verify_run_manifest

    manifest_path, _ = _run(tmp_path, publish_ibkr=False)
    manifest = load_and_verify_run_manifest(manifest_path)
    assert (
        current_run_status(
            manifest, "bid_ask_midpoint", stage_by_capability=STAGE_BY_CAPABILITY
        )
        is not CurrentRunStatus.CURRENT_RUN_EVIDENCE
    )


def test_the_report_separates_execution_from_artifact_authentication(
    tmp_path: Path, capsys: object
) -> None:
    manifest_path, evidence_dir = _run(tmp_path, publish_ibkr=True)
    code = run_main(
        ["prog", "report", "--manifest", str(manifest_path),
         "--ibkr-evidence", str(evidence_dir / "POI-IBKR-HIST.json")]
    )
    assert code == 1  # not fully ready, which is correct
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    payload = json.loads(out[out.index("{") :])
    stage = payload["stages"]["ibkr_historical"]
    assert stage["execution_status"] == StageOutcome.SUCCEEDED.value
    assert stage["artifact_status"] == "BOUND"
    assert stage["current_run_evidence"] is True


def test_a_rejected_artifact_reports_failed_authentication(tmp_path: Path, capsys: object) -> None:
    """Execution PASS must never imply the artifact authenticated."""
    run_root = tmp_path / "runs"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)
    broken = evidence_dir / "POI-IBKR-HIST.json"
    payload = _historical_payload()
    payload["capabilities"]["bid_ask_midpoint"] = "FAIL"  # digest no longer matches
    broken.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    run_main(
        ["prog", "stage", "--run-id", "run-Y", "--run-root", str(run_root),
         "--stage-name", "ibkr_historical", "--outcome", "SUCCEEDED",
         "--artifact", str(broken)]
    )
    run_main(["prog", "manifest", "--run-id", "run-Y", "--run-root", str(run_root)])
    manifest_path = run_root / "run-Y" / "run-manifest.json"
    run_main(["prog", "report", "--manifest", str(manifest_path)])
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    report = json.loads(out[out.rindex("{\n  \"capabilities\"") :]) if False else None
    del report
    payload_text = out[out.index("{") :]
    parsed = json.loads(payload_text)
    stage = parsed["stages"]["ibkr_historical"]
    assert stage["execution_status"] == "SUCCEEDED"
    assert stage["artifact_status"] == "FAILED_AUTHENTICATION"
    assert stage["current_run_evidence"] is False


# -- snapshot ------------------------------------------------------------------


def test_the_snapshot_binds_the_current_run_ibkr_artifact(tmp_path: Path) -> None:
    manifest_path, evidence_dir = _run(tmp_path, publish_ibkr=True)
    out = tmp_path / "snapshot.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest_path),
         "--ibkr-evidence", str(evidence_dir / "POI-IBKR-HIST.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert any("POI-IBKR-HIST" in ref for ref in payload["evidence"])
    for capability in IBKR_CAPABILITIES:
        assert payload["capability_states"][capability] == "EMPIRICALLY_VERIFIED"


def test_the_snapshot_stays_below_backtest_eligible(tmp_path: Path) -> None:
    """Recovering the six IBKR cells must not force readiness."""
    manifest_path, evidence_dir = _run(tmp_path, publish_ibkr=True)
    out = tmp_path / "snapshot.json"
    code = snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--run-manifest", str(manifest_path),
         "--ibkr-evidence", str(evidence_dir / "POI-IBKR-HIST.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1
    assert payload["backtest_eligible"] is False
    assert payload["snapshot_readiness"] != "BACKTEST_ELIGIBLE"
    for blocked in ("pit_sp500_membership", "asof_gics"):
        assert blocked in payload["capability_blockers"]


def test_manifest_records_the_artifact_type_for_each_ibkr_artifact(tmp_path: Path) -> None:
    manifest_path, _ = _run(tmp_path, publish_ibkr=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_stage = {a["stage"]: a["artifact_type"] for a in manifest["artifacts"]}
    assert by_stage["ibkr_historical"] == ArtifactType.IBKR_HISTORICAL_EVIDENCE.value
    assert by_stage["ibkr_corporate"] == ArtifactType.IBKR_CORPORATE_EVIDENCE.value


def test_observed_at_is_stable_across_write_and_verify(tmp_path: Path) -> None:
    """A wall-clock field must not drift between sealing and verification."""
    _, evidence_dir = _run(tmp_path, publish_ibkr=True)
    path = evidence_dir / "POI-IBKR-HIST.json"
    first = load_and_verify_ibkr_evidence(path).observed_at
    second = load_and_verify_ibkr_evidence(path).observed_at
    assert first == second == datetime.fromisoformat(OBSERVED).astimezone(UTC)
