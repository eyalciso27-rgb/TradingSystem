"""Immutability guard for the FROZEN baseline-trend-breakout-v1 manifest.

The owner approved and froze this strategy on 2026-08-13. Freezing is an
immutable state transition: the recorded parameters, the fusion manifest and the
deterministic fingerprint must never drift. Any edit that changes a value (or the
manifest) changes the fingerprint and fails this test — which is exactly the
point. A legitimate change requires a NEW version_id, not an edit to v1.
"""

from __future__ import annotations

from pathlib import Path

from trading_application.strategy_freeze import (
    MANDATORY_OWNER_PARAMETERS,
    FreezeStatus,
    StrategyFreezeRecord,
)
from trading_application.strategy_freeze_cli import load_freeze_record
from trading_application.strategy_manifest import ManifestRole

MANIFEST = Path(__file__).resolve().parents[2] / "docs/strategies/baseline-trend-breakout-v1.yaml"

#: The owner-approved fingerprint (invariant across DRAFT->FROZEN).
APPROVED_FINGERPRINT = (
    "sha256:75f299b89e408ef332cd39a46f92d417ef637b3c5a5c1712801774c9e84dfed0"
)
APPROVAL_REF = "OWNER-EYAL-2026-08-13-BASELINE-TREND-BREAKOUT-V1"


def _record() -> StrategyFreezeRecord:
    return load_freeze_record(MANIFEST)


def test_manifest_file_exists() -> None:
    assert MANIFEST.is_file(), f"missing frozen manifest: {MANIFEST}"


def test_status_is_frozen_with_full_approval() -> None:
    r = _record()
    assert r.strategy_id == "baseline-trend-breakout-v1"
    assert r.version_id == "v1"
    assert r.status is FreezeStatus.FROZEN
    assert r.owner_approval_ref == APPROVAL_REF
    assert r.frozen_at is not None and r.frozen_at.tzinfo is not None
    assert (r.source_commit or "").strip()
    # evidence may never be attached at freeze time
    assert r.evidence_refs == ()


def test_fingerprint_is_exactly_the_approved_value() -> None:
    assert _record().fingerprint() == APPROVED_FINGERPRINT


def test_exactly_the_34_mandatory_parameters_present() -> None:
    r = _record()
    assert len(r.parameters) == 34
    assert len(r.parameter_keys()) == 34
    assert r.missing_mandatory_parameters() == ()
    assert sorted(r.parameter_keys() - set(MANDATORY_OWNER_PARAMETERS)) == []


def test_every_parameter_has_provenance() -> None:
    r = _record()
    assert all(p.value.strip() for p in r.parameters)
    assert all(p.source.strip() for p in r.parameters)
    assert all(p.supplied_at.tzinfo is not None for p in r.parameters)


def test_fusion_manifest_is_the_approved_structural_representation() -> None:
    m = _record().fusion_manifest
    assert m is not None
    assert len(m.entries) == 1
    entry = m.entries[0]
    assert entry.assessment_key == "technical.breakout"
    assert entry.role is ManifestRole.REQUIRED
    assert entry.required_status is not None and entry.required_status.value == "TRIGGERED"
    # no thresholds / lookbacks / weights added
    assert entry.min_score is None
    assert entry.weight is None
    assert entry.min_calibration is None
