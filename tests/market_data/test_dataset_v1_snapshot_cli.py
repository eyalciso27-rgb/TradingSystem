"""Dataset V1 snapshot + ingestion CLI drills (offline).

Proves the two things the owner report depends on:

* the snapshot gate is never forced — a blocked capability keeps readiness below
  BACKTEST_ELIGIBLE, and an empirical claim without a verifiable binding fails
  closed rather than building an over-claiming snapshot;
* the ingestion CLI's exit codes distinguish PASS / PARTIAL / FAIL /
  NOT_AVAILABLE, which is what the runner turns into SOURCE EXECUTION status.
  A configuration state can never surface as PASS.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_ingest_cli import (
    EXIT_FAIL,
    EXIT_NOT_AVAILABLE,
    EXIT_PARTIAL,
    EXIT_PASS,
    _exit_code,
)
from trading_market_data_core.dataset_v1_ingest_cli import main as ingest_main
from trading_market_data_core.dataset_v1_qualification import (
    CoverageWindow,
    DataQualityMetrics,
    IdentityResolutionReport,
    IdentitySemantics,
    SourceDescriptor,
    SourceKind,
    SubCapabilityResult,
    TemporalSemantics,
    qualify_source,
)
from trading_market_data_core.dataset_v1_snapshot_cli import main as snapshot_main

DIGEST = "sha256:" + "c" * 64
OBSERVED = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _evidence(capability: str, *, passing: bool):  # type: ignore[no-untyped-def]
    return qualify_source(
        capability,
        source=SourceDescriptor(
            source_id="sec-edgar", name="SEC EDGAR", version="2023Q1",
            kind=SourceKind.REAL, authority="OFFICIAL_PUBLIC", raw_checksum=DIGEST,
        ),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        temporal=TemporalSemantics(True, True, True),
        identity=IdentitySemantics(True, ("dataset_security_id", "sec_cik"), False),
        metrics=DataQualityMetrics(total_rows=3, accepted=3, historical_changes=3),
        resolution=IdentityResolutionReport(resolved=3),
        proofs=("inactive_security_resolvable",) if capability == "delisted_security_history"
        else ("event_level_action",),
        sub_capabilities=(SubCapabilityResult(name="x", passed=passing),),
        observed_at=OBSERVED,
    )


# -- ingestion exit codes ------------------------------------------------------


def test_exit_code_maps_pass_partial_and_fail() -> None:
    assert _exit_code(_evidence("delisted_security_history", passing=True)) == EXIT_PASS
    partial = _evidence("delisted_security_history", passing=False)
    assert _exit_code(partial) == EXIT_PARTIAL
    assert partial.result.value == "PARTIAL"


def test_gics_reports_not_available_never_pass(tmp_path: Path) -> None:
    assert ingest_main(
        ["prog", "gics", "--evidence-dir", str(tmp_path)]
    ) == EXIT_NOT_AVAILABLE


def test_missing_sp_manifest_is_not_available_not_a_pass(tmp_path: Path) -> None:
    code = ingest_main(
        ["prog", "sp-membership", "--manifest", str(tmp_path / "absent.json"),
         "--evidence-dir", str(tmp_path)]
    )
    assert code == EXIT_NOT_AVAILABLE
    assert code != EXIT_PASS


def test_exit_codes_are_distinct() -> None:
    codes = {EXIT_PASS, EXIT_FAIL, EXIT_PARTIAL, EXIT_NOT_AVAILABLE}
    assert len(codes) == 4, "the runner distinguishes these; they must not collide"


# -- snapshot gate -------------------------------------------------------------


def test_blocked_capabilities_keep_readiness_below_backtest_eligible(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _evidence("delisted_security_history", passing=True).write(
        evidence_dir / "delisted_security_history.json"
    )
    out = tmp_path / "report.json"
    code = snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1, "not backtest eligible"
    assert payload["backtest_eligible"] is False
    assert payload["snapshot_readiness"] != "BACKTEST_ELIGIBLE"
    assert "asof_gics" in payload["capability_blockers"]
    assert "pit_sp500_membership" in payload["capability_blockers"]


def test_one_promoted_capability_does_not_promote_the_rest(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _evidence("delisted_security_history", passing=True).write(
        evidence_dir / "delisted_security_history.json"
    )
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    states = json.loads(out.read_text(encoding="utf-8"))["capability_states"]
    assert states["delisted_security_history"] == "EMPIRICALLY_VERIFIED"
    assert states["corporate_actions"] == "BLOCKED_EXTERNAL"
    assert states["asof_gics"] == "BLOCKED_EXTERNAL"


def test_a_tampered_evidence_file_aborts_the_snapshot(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    path = evidence_dir / "delisted_security_history.json"
    _evidence("delisted_security_history", passing=True).write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"]["historical_changes"] = 4321
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--ibkr-evidence",
         str(tmp_path / "absent.json")]
    ) == 2


def test_partial_evidence_never_reaches_the_capability_state(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    _evidence("delisted_security_history", passing=False).write(
        evidence_dir / "delisted_security_history.json"
    )
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(evidence_dir), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    states = json.loads(out.read_text(encoding="utf-8"))["capability_states"]
    assert states["delisted_security_history"] == "BLOCKED_EXTERNAL"
