"""IBKR/SEC reconciliation, the required-event gate, and snapshot identity binding.

Three properties are pinned here:

* IBKR adjustment observations corroborate ADJUSTMENT BEHAVIOUR and never become
  event-level facts - a disagreement is recorded, never silently resolved;
* `corporate_actions` PASS requires the production contract's REQUIRED set, so
  proving one event type is a genuine PARTIAL;
* a snapshot cannot claim identities its crosswalk never bound, and a tampered
  crosswalk invalidates the build.

Offline and deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_artifacts import (
    CrosswalkArtifact,
    CrosswalkSourceBinding,
)
from trading_market_data_core.dataset_v1_qualification import (
    CoverageWindow,
    QualificationResult,
    SourceDescriptor,
    SourceKind,
)
from trading_market_data_core.dataset_v1_real_ingestion import (
    REQUIRED_CORPORATE_EVENTS,
    qualify_corporate_actions,
    real_source_descriptor,
    reconcile_events_with_adjustments,
)
from trading_market_data_core.dataset_v1_snapshot_cli import main as snapshot_main
from trading_market_data_core.dataset_v1_sources import (
    AdjustmentCrossCheck,
    CanonicalCorporateEvent,
    CorporateEventType,
    SourceAdapterError,
    SourceAuthority,
)

OBSERVED = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _event(event_type: CorporateEventType, ident: str = "e1") -> CanonicalCorporateEvent:
    return CanonicalCorporateEvent(
        event_id=ident, security_id="sec-cik:1", event_type=event_type,
        source_id="sec-edgar", source_authority=SourceAuthority.OFFICIAL_PUBLIC,
        source_checksum=DIGEST, provenance="SEC filing",
        filing_date=date(2023, 1, 10), known_at=datetime(2023, 1, 10, tzinfo=UTC),
    )


def _check(*, diverges: bool) -> AdjustmentCrossCheck:
    return AdjustmentCrossCheck(
        symbol="AAPL", observed_on=date(2023, 1, 3), trades_close="100",
        adjusted_last_close="25" if diverges else "100", diverges=diverges,
    )


def _source() -> SourceDescriptor:
    return real_source_descriptor(
        source_id="sec-edgar", name="SEC EDGAR", version="2023Q1",
        authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=DIGEST,
        raw_artifact="master.idx", access_note="declared UA",
    )


# -- reconciliation ------------------------------------------------------------


def test_matching_adjustment_behaviour_is_recorded_as_corroboration() -> None:
    outcomes = reconcile_events_with_adjustments(
        [_event(CorporateEventType.SPLIT)], [_check(diverges=True)]
    )
    assert len(outcomes) == 1
    assert outcomes[0].corroborated
    assert "adjustment behaviour only" in outcomes[0].detail


def test_disagreement_is_recorded_explicitly_and_never_resolved() -> None:
    outcomes = reconcile_events_with_adjustments(
        [_event(CorporateEventType.SPLIT)], [_check(diverges=False)]
    )
    assert not outcomes[0].corroborated
    assert "DISAGREEMENT" in outcomes[0].detail
    assert "not resolved" in outcomes[0].detail


def test_absent_observations_are_not_corroboration() -> None:
    outcomes = reconcile_events_with_adjustments([_event(CorporateEventType.SPLIT)], [])
    assert not outcomes[0].corroborated
    assert "NOT corroborated" in outcomes[0].detail


def test_non_price_affecting_events_are_not_reconciled_against_prices() -> None:
    """A delisting is not a price adjustment; pairing them would be noise."""
    assert reconcile_events_with_adjustments(
        [_event(CorporateEventType.DELISTING)], [_check(diverges=True)]
    ) == ()


def test_adjustment_observations_never_create_an_event() -> None:
    """Divergence alone must not add anything to the ledger."""
    evidence = qualify_corporate_actions(
        [], adjustment_checks=[_check(diverges=True)] * 5, source=_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)), observed_at=OBSERVED,
    )
    assert evidence.metrics.total_rows == 0
    assert evidence.metrics.historical_changes == 0
    assert not evidence.promotion_eligible


# -- the required-event gate ---------------------------------------------------


def test_one_proven_event_type_is_partial_not_pass() -> None:
    evidence = qualify_corporate_actions(
        [_event(CorporateEventType.DELISTING)], source=_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)), observed_at=OBSERVED,
    )
    subs = {s.name: s for s in evidence.sub_capabilities}
    assert subs["event_delisting"].passed
    assert not subs["required_event_types_proven"].passed
    assert "SPLIT" in subs["required_event_types_proven"].detail
    assert evidence.result is QualificationResult.PARTIAL


def test_required_types_are_labelled_in_the_evidence() -> None:
    evidence = qualify_corporate_actions(
        [_event(CorporateEventType.DELISTING)], source=_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)), observed_at=OBSERVED,
    )
    subs = {s.name: s for s in evidence.sub_capabilities}
    assert "[REQUIRED]" in subs["event_split"].detail
    assert "[optional]" in subs["event_spinoff"].detail


def test_every_required_type_proven_allows_pass() -> None:
    events = [
        _event(event_type, ident=f"e-{event_type.value}")
        for event_type in REQUIRED_CORPORATE_EVENTS
    ]
    evidence = qualify_corporate_actions(
        events, adjustment_checks=[_check(diverges=True)], source=_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)), observed_at=OBSERVED,
    )
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["required_event_types_proven"] is True
    assert subs["ibkr_sec_reconciled"] is True


def test_duplicate_event_ids_fail_closed() -> None:
    with pytest.raises(SourceAdapterError, match="duplicate corporate event ids"):
        qualify_corporate_actions(
            [_event(CorporateEventType.SPLIT), _event(CorporateEventType.SPLIT)],
            source=_source(),
            coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
            observed_at=OBSERVED,
        )


# -- snapshot identity binding -------------------------------------------------


def _crosswalk_file(path: Path, *, resolved: tuple[str, ...]) -> None:
    CrosswalkArtifact(
        entries=len(resolved), resolved=resolved, ambiguous=(), unresolved=(),
        conflicting=(), observed_at=OBSERVED,
        sources=(CrosswalkSourceBinding(name="sec-edgar", version="2023Q1", checksum=DIGEST),),
    ).sealed().write(path)


def test_snapshot_binds_the_crosswalk_artifact(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    _crosswalk_file(directory / "identity-crosswalk.json", resolved=("cik:1=sec-cik:1",))
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(directory), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["identity_crosswalk"], "the crosswalk digest must be bound"
    assert payload["artifact_type"] == "snapshot_report"
    assert any("identity-crosswalk" in ref for ref in payload["evidence"])


def test_a_tampered_crosswalk_invalidates_the_snapshot(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    path = directory / "identity-crosswalk.json"
    _crosswalk_file(path, resolved=("cik:1=sec-cik:1",))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resolved"].append("cik:2=sec-cik:2")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert snapshot_main(
        ["prog", "--evidence-dir", str(directory), "--ibkr-evidence",
         str(tmp_path / "absent.json")]
    ) == 2


def test_a_crosswalk_in_the_directory_never_reaches_promotion(tmp_path: Path) -> None:
    """The owner-host bug: this used to abort the whole verification."""
    directory = tmp_path / "evidence"
    directory.mkdir()
    _crosswalk_file(directory / "identity-crosswalk.json", resolved=("cik:1=sec-cik:1",))
    out = tmp_path / "report.json"
    code = snapshot_main(
        ["prog", "--evidence-dir", str(directory), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    assert code == 1, "not backtest eligible, but NOT an artifact error"
    states = json.loads(out.read_text(encoding="utf-8"))["capability_states"]
    assert all(state == "BLOCKED_EXTERNAL" for state in states.values())


def test_an_unclassifiable_artifact_aborts_the_snapshot(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    (directory / "junk.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert snapshot_main(
        ["prog", "--evidence-dir", str(directory), "--ibkr-evidence",
         str(tmp_path / "absent.json")]
    ) == 2


def test_source_bindings_flow_from_the_crosswalk(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    _crosswalk_file(directory / "identity-crosswalk.json", resolved=("cik:1=sec-cik:1",))
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(directory), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    assert json.loads(out.read_text(encoding="utf-8"))["snapshot_id"], (
        "a build with a verified crosswalk should still produce a snapshot id"
    )


def test_fixture_kind_still_cannot_promote(tmp_path: Path) -> None:
    """Artifact typing must not become a new way around the promotion gate."""
    from trading_market_data_core.dataset_v1_qualification import (
        DataQualityMetrics,
        IdentityResolutionReport,
        IdentitySemantics,
        TemporalSemantics,
        qualify_source,
    )

    directory = tmp_path / "evidence"
    directory.mkdir()
    qualify_source(
        "delisted_security_history",
        source=SourceDescriptor(
            source_id="fixture", name="fixture", version="1", kind=SourceKind.FIXTURE,
            authority="FIXTURE", raw_checksum=DIGEST,
        ),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        temporal=TemporalSemantics(True, True, True),
        identity=IdentitySemantics(True, ("sec_cik",), False),
        metrics=DataQualityMetrics(total_rows=9, accepted=9, historical_changes=9),
        resolution=IdentityResolutionReport(resolved=9),
        proofs=("inactive_security_resolvable",),
        observed_at=OBSERVED,
    ).write(directory / "delisted_security_history.json")
    out = tmp_path / "report.json"
    snapshot_main(
        ["prog", "--evidence-dir", str(directory), "--ibkr-evidence",
         str(tmp_path / "absent.json"), "--out", str(out)]
    )
    states = json.loads(out.read_text(encoding="utf-8"))["capability_states"]
    assert states["delisted_security_history"] == "BLOCKED_EXTERNAL"
