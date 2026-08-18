"""Typed IBKR artifact checksum layers, sealed by ONE implementation.

The owner run after PR #87 had both IBKR artifacts fail to bind:

    declared   sha256:c1e5c794a2ea10c0bc7e349a4c589e642bcde7aad4ecf3d3ef3ed39279aa26a6
    recomputed sha256:3bd352d7181ccbf3223acbf72e378941f2c492f717da5b71006389d95a47667f

Root cause: the writer assigned the INNER ``evidence_checksum`` to the OUTER
``normalized_checksum`` field, so every verifier recomputed a different value.

Three layers, never cross-compared:

  A. INNER ``evidence_checksum``   -> the qualification body only
  B. OUTER ``normalized_checksum`` -> the complete typed envelope
  C. BYTES ``file_checksum``       -> the serialized file (manifest only)

Offline: no network, no IBKR connection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from trading_broker_ibkr.poi_corporate_actions_qualification import (
    CorporateQualificationEvidence,
)
from trading_broker_ibkr.poi_historical_qualification import QualificationEvidence
from trading_market_data_core.dataset_v1_artifacts import ArtifactType, classify_payload
from trading_market_data_core.dataset_v1_ibkr_artifacts import (
    IbkrArtifactError,
    debug_canonical_body,
    inner_evidence_checksum,
    load_and_verify_ibkr_evidence,
    outer_envelope_checksum,
    seal_typed_ibkr_artifact,
    verify_typed_ibkr_payload,
)
from trading_market_data_core.dataset_v1_run import file_checksum

OBSERVED = "2026-08-18T00:00:00+00:00"

ENVELOPE = {
    "artifact_type", "schema_version", "producer", "capability_scope",
    "normalized_checksum", "run_id", "evidence_checksum",
}


def _historical() -> QualificationEvidence:
    """Shaped like the real owner artifact: six capabilities, all PASS."""
    evidence = QualificationEvidence(account_alias="paper-poi", observed_at=OBSERVED)
    evidence.masked_account_id = "DU***567"
    evidence.server_time = "2026-08-18T00:00:00Z"
    evidence.capabilities = {
        "bid_ask_midpoint": "PASS",
        "connectivity_paper_du_gate": "PASS",
        "contract_identity_conid": "PASS",
        "daily_trades_rth_schema": "PASS",
        "head_timestamp_coverage": "PASS",
        "symbol_change_discovery": "PASS",
    }
    evidence.active_name_price_qualification_sufficient = True
    evidence.full_dataset_contract_ready = False
    evidence.evidence_checksum = inner_evidence_checksum(evidence.body_for_checksum())
    return evidence


def _corporate() -> CorporateQualificationEvidence:
    """Shaped like the real owner artifact: 4 observations, feed UNAVAILABLE."""
    evidence = CorporateQualificationEvidence(account_alias="paper-poi", observed_at=OBSERVED)
    evidence.masked_account_id = "DU***567"
    evidence.probes = [
        {"capability": "HISTORICAL_BARS_TRADES", "outcome": "AVAILABLE", "detail": ""},
        {"capability": "ADJUSTED_LAST", "outcome": "AVAILABLE", "detail": ""},
        {"capability": "CONTRACT_IDENTITY_CONID", "outcome": "AVAILABLE", "detail": ""},
        {"capability": "CORPORATE_EVENT_FEED", "outcome": "UNAVAILABLE", "detail": "none"},
    ]
    evidence.adjustment_observations = [
        {
            "symbol": "AAPL", "bar_date": f"2023010{index + 1}", "trades_close": "100",
            "adjusted_last_close": "25", "diverges": True,
            "inference_boundary": "adjustment only",
        }
        for index in range(4)
    ]
    evidence.evidence_checksum = inner_evidence_checksum(evidence.body_for_checksum())
    return evidence


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# -- the three layers ----------------------------------------------------------


def test_inner_and_outer_are_different_digests_of_different_bodies() -> None:
    evidence = _historical()
    payload = evidence.to_dict()
    inner = payload["evidence_checksum"]
    outer = payload["normalized_checksum"]
    assert inner != outer, "the two layers must never carry the same value"
    assert outer == outer_envelope_checksum(payload)
    assert inner == inner_evidence_checksum(evidence.body_for_checksum())


def test_the_owner_defect_is_gone_for_the_historical_artifact(tmp_path: Path) -> None:
    """declared == recomputed, which is exactly what failed on the owner host."""
    path = _write(tmp_path / "POI-IBKR-HIST.json", _historical().to_dict())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["normalized_checksum"] == outer_envelope_checksum(payload)
    assert load_and_verify_ibkr_evidence(path).normalized_checksum


def test_the_owner_defect_is_gone_for_the_corporate_artifact(tmp_path: Path) -> None:
    path = _write(tmp_path / "POI-IBKR-CORP.json", _corporate().to_dict())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["normalized_checksum"] == outer_envelope_checksum(payload)
    assert load_and_verify_ibkr_evidence(path).normalized_checksum


def test_file_checksum_is_a_third_distinct_layer(tmp_path: Path) -> None:
    path = _write(tmp_path / "POI-IBKR-HIST.json", _historical().to_dict())
    payload = json.loads(path.read_text(encoding="utf-8"))
    digests = {
        payload["evidence_checksum"], payload["normalized_checksum"], file_checksum(path)
    }
    assert len(digests) == 3, "inner, outer and bytes are three different things"


def test_normalized_checksum_is_excluded_from_its_own_body() -> None:
    payload = _historical().to_dict()
    first = outer_envelope_checksum(payload)
    payload_again = dict(payload)
    payload_again["normalized_checksum"] = "sha256:" + "f" * 64
    assert outer_envelope_checksum(payload_again) == first


# -- sealing lifecycle ---------------------------------------------------------


def test_sealing_is_idempotent_and_deterministic() -> None:
    body: dict[str, Any] = {"artifact_type": "ibkr_historical_evidence", "b": 2, "a": 1}
    assert seal_typed_ibkr_artifact(body) == seal_typed_ibkr_artifact(body)
    sealed = seal_typed_ibkr_artifact(body)
    assert seal_typed_ibkr_artifact(sealed) == sealed, "resealing must not drift"


def test_mutating_a_sealed_field_is_detected() -> None:
    payload = _historical().to_dict()
    verify_typed_ibkr_payload(payload)
    capabilities = payload["capabilities"]
    assert isinstance(capabilities, dict)
    capabilities["bid_ask_midpoint"] = "FAIL"
    with pytest.raises(IbkrArtifactError, match="checksum mismatch"):
        verify_typed_ibkr_payload(payload)


def test_changed_envelope_metadata_changes_the_outer_digest() -> None:
    payload = _historical().to_dict()
    before = payload["normalized_checksum"]
    payload["producer"] = "something-else"
    assert outer_envelope_checksum(payload) != before


def test_envelope_only_change_leaves_the_inner_digest_stable() -> None:
    """The intended contract: typing metadata never disturbs the inner digest."""
    payload = _historical().to_dict()
    inner_before = payload["evidence_checksum"]
    payload["producer"] = "renamed-producer"
    payload["run_id"] = "run-Z"
    body = {key: value for key, value in payload.items() if key not in ENVELOPE}
    assert inner_evidence_checksum(body) == inner_before


def test_tampering_with_the_inner_body_breaks_verification(tmp_path: Path) -> None:
    path = _write(tmp_path / "POI-IBKR-HIST.json", _historical().to_dict())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["masked_account_id"] = "DU***000"
    _write(path, payload)
    with pytest.raises(IbkrArtifactError, match="checksum mismatch"):
        load_and_verify_ibkr_evidence(path)


def test_debug_utility_shows_the_body_a_verifier_uses() -> None:
    payload = _historical().to_dict()
    text, digest = debug_canonical_body(payload)
    assert digest == payload["normalized_checksum"]
    assert "ibkr_historical_evidence" in text
    assert "normalized_checksum" not in text


# -- native writer vs migration ------------------------------------------------


def test_the_native_writer_emits_a_fully_valid_typed_artifact(tmp_path: Path) -> None:
    """Regeneration must not depend on the one-time migration wrapper."""
    path = _write(tmp_path / "POI-IBKR-HIST.json", _historical().to_dict())
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert classify_payload(payload) is ArtifactType.IBKR_HISTORICAL_EVIDENCE
    artifact = load_and_verify_ibkr_evidence(path)
    assert artifact.schema_version and artifact.producer
    assert set(artifact.capability_states()) == set(artifact.capability_scope)


def test_a_regenerated_artifact_needs_no_migration(tmp_path: Path) -> None:
    from trading_market_data_core.dataset_v1_ibkr_cli import main as ibkr_main

    path = _write(tmp_path / "POI-IBKR-HIST.json", _historical().to_dict())
    assert ibkr_main(["prog", "migrate", "--path", str(path)]) == 3
    assert ibkr_main(["prog", "verify", "--path", str(path)]) == 0


def test_corporate_artifact_binds_even_though_the_feed_is_unavailable(tmp_path: Path) -> None:
    """An observed UNAVAILABLE probe is a result, not a broken artifact."""
    path = _write(tmp_path / "POI-IBKR-CORP.json", _corporate().to_dict())
    artifact = load_and_verify_ibkr_evidence(path)
    probes = {p["capability"]: p["outcome"] for p in artifact.payload["probes"]}
    assert probes["CORPORATE_EVENT_FEED"] == "UNAVAILABLE"
    assert artifact.capability_scope == (), "it qualifies nothing by itself"
