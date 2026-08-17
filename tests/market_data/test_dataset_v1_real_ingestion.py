"""Dataset V1 real-source ingestion drills: SEC official + S&P official + PIT replay.

Fully offline and deterministic. SEC transport is an injected port driven by
recorded, schema-shaped payloads; the S&P path reads owner-supplied manifests
from tmp_path. No network call is made anywhere in this file.

The point of these tests is that REAL ingestion machinery cannot be talked into
promoting something it did not prove: fixtures never promote, unmapped identity
fails closed, a later-dated baseline is refused as current-universe projection,
and a tampered artifact is rejected.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pytest
from trading_market_data_core.dataset_v1 import CapabilityState
from trading_market_data_core.dataset_v1_qualification import (
    CoverageWindow,
    QualificationError,
    QualificationResult,
    SourceDescriptor,
    SourceKind,
    load_and_verify_evidence,
    promote_capability,
)
from trading_market_data_core.dataset_v1_real_ingestion import (
    ASOF_GICS_MISSING_REQUIREMENT,
    build_crosswalk_report,
    load_index_manifest,
    qualify_corporate_actions,
    qualify_delisted_security_history,
    qualify_pit_membership,
    real_source_descriptor,
    replay_membership_as_of,
)
from trading_market_data_core.dataset_v1_sources import (
    CanonicalCorporateEvent,
    CorporateEventType,
    CrosswalkEntry,
    IbkrCapabilityProbe,
    IbkrReferenceCapability,
    IdentityCrosswalk,
    ProbeOutcome,
    SecFilingRecord,
    SourceAdapterError,
    SourceAuthority,
    combined_artifact_checksum,
    discover_delisting_candidates,
    parse_master_index,
    parse_sec_company_tickers,
    parse_sec_entity_profile,
    parse_sec_filing_shard,
    sec_filing_shards,
    sec_filings_to_events,
)

OBSERVED = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64


# -- SEC pagination + shape validation ----------------------------------------


def test_filing_shards_are_discovered_so_history_is_not_truncated() -> None:
    payload = {
        "cik": "320193",
        "filings": {"recent": {}, "files": [{"name": "CIK0000320193-submissions-001.json"}]},
    }
    assert sec_filing_shards(payload) == ("CIK0000320193-submissions-001.json",)


def test_missing_shard_list_is_not_an_error() -> None:
    assert sec_filing_shards({"cik": "1", "filings": {"recent": {}}}) == ()


def test_malformed_shard_list_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="must be an array"):
        sec_filing_shards({"cik": "1", "filings": {"files": "nope"}})
    with pytest.raises(SourceAdapterError, match="has no name"):
        sec_filing_shards({"cik": "1", "filings": {"files": [{}]}})


def test_older_shard_normalizes_with_the_same_semantics() -> None:
    shard = {
        "form": ["25-NSE"], "accessionNumber": ["0001-21-000001"],
        "filingDate": ["2021-05-04"], "acceptanceDateTime": ["2021-05-04T16:30:00.000Z"],
    }
    records = parse_sec_filing_shard(b"{}", shard, cik="0000000001")
    assert len(records) == 1
    assert records[0].is_delisting_form
    assert records[0].cik == "0000000001"
    assert records[0].accepted_at is not None


# -- SEC master index: discovery, not guessing --------------------------------


MASTER_IDX = b"""Description:           Master Index
Last Data Received:    March 31, 2023

CIK|Company Name|Form Type|Date Filed|File Name
--------------------------------------------------------------------------------
1418091|Twitter, Inc.|25-NSE|2023-01-10|edgar/data/1418091/0001-23-000001.txt
320193|Apple Inc.|10-K|2023-02-02|edgar/data/320193/0000320193-23-000006.txt
777001|Some Corp|25|2023-03-15|edgar/data/777001/0000777001-23-000002.txt
"""


def test_master_index_parses_the_official_catalogue() -> None:
    rows = parse_master_index(MASTER_IDX)
    assert len(rows) == 3
    assert rows[0].cik == "0001418091"
    assert rows[0].form == "25-NSE"
    assert rows[0].date_filed == date(2023, 1, 10)


def test_master_index_without_a_header_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="no header separator"):
        parse_master_index(b"CIK|Company|Form|Date|File\n1|x|25|2023-01-01|a.txt\n")


def test_master_index_malformed_row_fails_closed_rather_than_skipping() -> None:
    broken = MASTER_IDX + b"1|only|three\n"
    with pytest.raises(SourceAdapterError, match="expected 5"):
        parse_master_index(broken)


def test_candidate_discovery_is_bounded_and_deterministic() -> None:
    rows = parse_master_index(MASTER_IDX)
    first = discover_delisting_candidates(rows, limit=1)
    second = discover_delisting_candidates(rows, limit=1)
    assert first == second, "a qualification run must be reproducible"
    assert len(first) == 1
    assert first[0].form == "25-NSE"  # earliest by (date, cik, filename)
    # a 10-K is not a delisting and is never a candidate
    assert all(row.form in ("25", "25-NSE") for row in discover_delisting_candidates(rows, limit=9))


def test_candidate_discovery_rejects_a_nonpositive_limit() -> None:
    with pytest.raises(SourceAdapterError, match="limit must be positive"):
        discover_delisting_candidates(parse_master_index(MASTER_IDX), limit=0)


def test_combined_checksum_covers_every_consumed_response() -> None:
    """Binding only the discovery index would leave the filings unauthenticated."""
    index, first, second = b"index", b"submissions-1", b"submissions-2"
    full = combined_artifact_checksum([index, first, second])
    assert full != combined_artifact_checksum([index])
    # a change in ANY consumed response changes the digest
    assert full != combined_artifact_checksum([index, first, b"submissions-2b"])


def test_combined_checksum_is_stable_regardless_of_fetch_order() -> None:
    parts = [b"a", b"b", b"c"]
    assert combined_artifact_checksum(parts) == combined_artifact_checksum(list(reversed(parts)))


def test_combined_checksum_refuses_an_empty_set() -> None:
    with pytest.raises(SourceAdapterError, match="empty set"):
        combined_artifact_checksum([])


# -- SEC identity comes from SEC ----------------------------------------------


def test_entity_profile_supplies_identity_and_a_non_ticker_security_id() -> None:
    payload = {
        "cik": "1418091", "name": "Twitter, Inc.", "tickers": ["TWTR"],
        "exchanges": ["NYSE"], "formerNames": [{"name": "Twitter Inc"}],
    }
    profile = parse_sec_entity_profile(b"{}", payload)
    assert profile.cik == "0001418091"
    assert profile.security_id == "sec-cik:0001418091"  # survives delisting
    assert "TWTR" not in profile.security_id, "identity must not be a ticker"
    assert profile.former_names == ("Twitter Inc",)


def test_entity_profile_malformed_shape_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="must be an array"):
        parse_sec_entity_profile(b"{}", {"cik": "1", "tickers": "TWTR"})
    with pytest.raises(SourceAdapterError, match="no cik"):
        parse_sec_entity_profile(b"{}", {"name": "x"})


def test_company_tickers_mapping_normalizes_cik_padding() -> None:
    records = parse_sec_company_tickers(
        b"{}", {"0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."}}
    )
    assert records[0].cik == "0000320193"
    assert records[0].ticker == "AAPL"


def test_company_tickers_missing_fields_fail_closed() -> None:
    with pytest.raises(SourceAdapterError, match="missing cik/ticker"):
        parse_sec_company_tickers(b"{}", {"0": {"cik_str": 1}})


# -- Form 25 → canonical events ------------------------------------------------


def _filing(form: str = "25-NSE", accession: str = "acc-1") -> SecFilingRecord:
    return SecFilingRecord(
        cik="0001418091", accession=accession, form=form,
        filing_date=date(2023, 1, 10),
        accepted_at=datetime(2023, 1, 10, 16, 30, tzinfo=UTC),
    )


def test_form_25_becomes_a_delisting_event_with_full_provenance() -> None:
    events = sec_filings_to_events(
        [_filing()], security_id_by_cik={"0001418091": "sec-cik:0001418091"},
        source_id="sec-edgar", source_checksum=DIGEST,
    )
    assert len(events) == 1
    event = events[0]
    assert event.event_type is CorporateEventType.DELISTING
    assert event.filing_date == date(2023, 1, 10)
    assert event.known_at is not None
    assert "25-NSE" in event.provenance
    # the statutory effective date is NEVER computed
    assert event.effective_on is None


def test_form_15_is_deregistration_not_delisting() -> None:
    events = sec_filings_to_events(
        [_filing(form="15-12B")], security_id_by_cik={"0001418091": "x"},
        source_id="s", source_checksum=DIGEST,
    )
    assert events[0].event_type is CorporateEventType.DEREGISTRATION


def test_a_10k_produces_no_event() -> None:
    assert sec_filings_to_events(
        [_filing(form="10-K")], security_id_by_cik={"0001418091": "x"},
        source_id="s", source_checksum=DIGEST,
    ) == ()


def test_unmapped_identity_is_skipped_never_guessed() -> None:
    assert sec_filings_to_events(
        [_filing()], security_id_by_cik={}, source_id="s", source_checksum=DIGEST
    ) == ()


def test_an_undated_event_cannot_be_constructed() -> None:
    with pytest.raises(SourceAdapterError, match="no effective/ex/filing date"):
        CanonicalCorporateEvent(
            event_id="e1", security_id="s1", event_type=CorporateEventType.SPLIT,
            source_id="s", source_authority=SourceAuthority.OFFICIAL_PUBLIC,
            source_checksum=DIGEST, provenance="p",
        )


def test_an_event_without_provenance_cannot_be_constructed() -> None:
    with pytest.raises(SourceAdapterError, match="event-level provenance"):
        CanonicalCorporateEvent(
            event_id="e1", security_id="s1", event_type=CorporateEventType.SPLIT,
            source_id="s", source_authority=SourceAuthority.OFFICIAL_PUBLIC,
            source_checksum=DIGEST, provenance="  ", ex_date=date(2023, 1, 1),
        )


def test_an_event_with_a_cosmetic_checksum_cannot_be_constructed() -> None:
    with pytest.raises(SourceAdapterError, match="sha256 source checksum"):
        CanonicalCorporateEvent(
            event_id="e1", security_id="s1", event_type=CorporateEventType.SPLIT,
            source_id="s", source_authority=SourceAuthority.OFFICIAL_PUBLIC,
            source_checksum="sha256:abc", provenance="p", ex_date=date(2023, 1, 1),
        )


# -- delisted security history qualification -----------------------------------


def _crosswalk() -> IdentityCrosswalk:
    return IdentityCrosswalk(
        [CrosswalkEntry(security_id="sec-cik:0001418091", sec_cik="0001418091")]
    )


def _real_source() -> SourceDescriptor:
    return real_source_descriptor(
        source_id="sec-edgar", name="SEC EDGAR", version="2023Q1",
        authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=DIGEST,
        raw_artifact="master.idx", access_note="declared UA, rate limited",
    )


def test_delisting_qualification_records_precise_sub_results() -> None:
    evidence = qualify_delisted_security_history(
        [_filing()], crosswalk=_crosswalk(),
        security_id_by_cik={"0001418091": "sec-cik:0001418091"},
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["delisting_filing_observed"] is True
    assert subs["knowledge_time_captured"] is True
    assert subs["identity_resolvable_without_current_contract"] is True
    # no published effective date in the metadata: reported, never invented
    assert subs["delisting_effective_date_published"] is False
    assert evidence.result is QualificationResult.PARTIAL
    assert not evidence.promotion_eligible


def test_identity_resolution_does_not_depend_on_a_current_ibkr_contract() -> None:
    """A delisted name has no live conId; CIK identity must still resolve."""
    evidence = qualify_delisted_security_history(
        [_filing()], crosswalk=_crosswalk(),
        security_id_by_cik={"0001418091": "sec-cik:0001418091"},
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["identity_resolvable_without_current_contract"] is True
    assert "inactive_security_resolvable" in evidence.proofs


def test_unresolved_cik_blocks_and_is_itemised() -> None:
    evidence = qualify_delisted_security_history(
        [_filing()], crosswalk=_crosswalk(), security_id_by_cik={},
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    assert not evidence.promotion_eligible
    assert any("unresolved CIK" in error for error in evidence.validation_errors)


def test_a_published_effective_date_flips_the_sub_capability() -> None:
    evidence = qualify_delisted_security_history(
        [_filing()], crosswalk=_crosswalk(),
        security_id_by_cik={"0001418091": "sec-cik:0001418091"},
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
        effective_dates={"acc-1": date(2023, 1, 20)},
    )
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["delisting_effective_date_published"] is True


# -- corporate actions ---------------------------------------------------------


def _event(event_type: CorporateEventType, ident: str = "e") -> CanonicalCorporateEvent:
    return CanonicalCorporateEvent(
        event_id=ident, security_id="sec-cik:0001418091", event_type=event_type,
        source_id="sec-edgar", source_authority=SourceAuthority.OFFICIAL_PUBLIC,
        source_checksum=DIGEST, provenance="SEC filing",
        filing_date=date(2023, 1, 10), known_at=datetime(2023, 1, 10, 16, tzinfo=UTC),
    )


def test_partial_corporate_coverage_lists_each_event_type() -> None:
    evidence = qualify_corporate_actions(
        [_event(CorporateEventType.DELISTING)],
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["event_delisting"] is True
    assert subs["event_split"] is False
    assert subs["event_dividend"] is False
    assert subs["event_merger"] is False
    assert evidence.result is QualificationResult.PARTIAL
    assert not evidence.promotion_eligible


def test_adjustment_checks_are_never_counted_as_events() -> None:
    """The core boundary: adjustment behaviour is not an event ledger."""
    from trading_market_data_core.dataset_v1_sources import AdjustmentCrossCheck

    checks = [
        AdjustmentCrossCheck(
            symbol="AAPL", observed_on=date(2023, 1, 3), trades_close="100.00",
            adjusted_last_close="25.00", diverges=True,
        )
    ]
    with_checks = qualify_corporate_actions(
        [_event(CorporateEventType.DELISTING)], adjustment_checks=checks,
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    without = qualify_corporate_actions(
        [_event(CorporateEventType.DELISTING)],
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    assert with_checks.metrics.historical_changes == without.metrics.historical_changes == 1
    subs = {s.name: s.passed for s in with_checks.sub_capabilities}
    assert subs["adjustment_behaviour_characterised"] is True
    # 3 divergences would still not add a single event
    assert with_checks.metrics.total_rows == 1


def test_ibkr_entitlement_gap_is_recorded_not_treated_as_failure() -> None:
    probes = [
        IbkrCapabilityProbe(
            capability=IbkrReferenceCapability.CORPORATE_EVENT_FEED,
            outcome=ProbeOutcome.ENTITLEMENT_REQUIRED,
            detail="account lacks the fundamentals entitlement",
        )
    ]
    evidence = qualify_corporate_actions(
        [_event(CorporateEventType.DELISTING)], ibkr_probes=probes,
        source=_real_source(),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    feed = next(s for s in evidence.sub_capabilities if s.name == "ibkr_event_feed_available")
    assert feed.passed is False
    assert "ENTITLEMENT_REQUIRED" in feed.detail
    # SEC evidence still counts: the run continues rather than aborting
    assert evidence.metrics.historical_changes == 1


# -- S&P official membership manifest + PIT replay -----------------------------


def _manifest_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "index_id": "SP500",
        "coverage": {"start": "2021-01-01", "end": "2021-12-31"},
        "baseline": {
            "as_of": "2021-01-01",
            "security_ids": ["sec-cik:1", "sec-cik:2"],
            "source_url": "https://example.invalid/official-baseline",
            "source_document_checksum": DIGEST,
        },
        "security_id_by_ticker": {"NEWCO": "sec-cik:3", "OLDCO": "sec-cik:2"},
        "announcements": [
            {
                "announced_on": "2021-03-01", "effective_on": "2021-03-22",
                "added_ticker": "NEWCO", "removed_ticker": "OLDCO",
                "source_url": "https://example.invalid/announcement",
                "source_document_checksum": OTHER_DIGEST,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _write_manifest(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "sp500-announcements.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_official_manifest_loads_with_both_time_axes(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    assert manifest.index_id == "SP500"
    assert manifest.announcements[0].announced_on < manifest.announcements[0].effective_on
    assert manifest.manifest_checksum.startswith("sha256:")


def test_a_baseline_dated_after_the_window_start_is_refused(tmp_path: Path) -> None:
    """Today's constituent list must never be projected backwards."""
    payload = _manifest_payload()
    baseline = cast("dict[str, object]", payload["baseline"])
    payload["baseline"] = {**baseline, "as_of": "2026-08-17"}
    with pytest.raises(SourceAdapterError, match="project a later universe backwards"):
        load_index_manifest(_write_manifest(tmp_path, payload))


def test_an_announcement_outside_the_declared_window_is_refused(tmp_path: Path) -> None:
    payload = _manifest_payload()
    announcements = cast("list[dict[str, object]]", payload["announcements"])
    payload["announcements"] = [{**announcements[0], "effective_on": "2024-01-05"}]
    with pytest.raises(SourceAdapterError, match="outside the declared window"):
        load_index_manifest(_write_manifest(tmp_path, payload))


def test_a_document_whose_checksum_drifted_fails_closed(tmp_path: Path) -> None:
    document = tmp_path / "official.txt"
    document.write_text("official announcement text", encoding="utf-8")
    payload = _manifest_payload()
    announcements = cast("list[dict[str, object]]", payload["announcements"])
    payload["announcements"] = [{**announcements[0], "document_path": "official.txt"}]
    path = _write_manifest(tmp_path, payload)
    with pytest.raises(SourceAdapterError, match="checksum mismatch"):
        load_index_manifest(path, document_root=tmp_path)


def test_pit_replay_honours_effective_time(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    before = replay_membership_as_of(manifest, as_of=date(2021, 3, 21))
    after = replay_membership_as_of(manifest, as_of=date(2021, 3, 22))
    assert before == ("sec-cik:1", "sec-cik:2")
    assert after == ("sec-cik:1", "sec-cik:3"), "removal applied, addition applied"


def test_pit_replay_refuses_to_leak_an_unannounced_change(tmp_path: Path) -> None:
    """Knowledge time: a change not yet announced is invisible (no look-ahead)."""
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    known_before = datetime(2021, 2, 1, tzinfo=UTC)
    members = replay_membership_as_of(
        manifest, as_of=date(2021, 6, 1), known_as_of=known_before
    )
    assert members == ("sec-cik:1", "sec-cik:2"), "announced 2021-03-01: not yet knowable"


def test_pit_replay_before_the_baseline_anchor_fails_closed(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    with pytest.raises(SourceAdapterError, match="before the baseline anchor"):
        replay_membership_as_of(manifest, as_of=date(2020, 6, 1))


def test_replay_is_deterministic(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    runs = {replay_membership_as_of(manifest, as_of=date(2021, 12, 31)) for _ in range(5)}
    assert len(runs) == 1


def _sp_source() -> SourceDescriptor:
    return real_source_descriptor(
        source_id="spdji", name="S&P DJI official announcements", version="1",
        authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=DIGEST,
        raw_artifact="manifest", access_note="owner-supplied official documents",
    )


def test_bounded_window_can_pass_for_that_window(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    evidence = qualify_pit_membership(manifest, source=_sp_source(), observed_at=OBSERVED)
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["baseline_anchored_at_window_start"] is True
    assert subs["historical_additions_observed"] is True
    assert subs["historical_removals_observed"] is True
    assert subs["replay_deterministic"] is True
    assert evidence.result is QualificationResult.PASS
    assert evidence.promotion_eligible


def test_a_wider_production_requirement_downgrades_to_partial(tmp_path: Path) -> None:
    """Bounded coverage must not be presented as full history."""
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    evidence = qualify_pit_membership(
        manifest, source=_sp_source(), observed_at=OBSERVED,
        production_window_required=(date(2015, 1, 1), date(2026, 1, 1)),
    )
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["covers_required_production_window"] is False
    assert evidence.result is QualificationResult.PARTIAL
    assert not evidence.promotion_eligible
    assert any("sub-capabilities not proven" in r for r in evidence.blocked_reasons)


def test_unmapped_change_ticker_fails_closed(tmp_path: Path) -> None:
    payload = _manifest_payload(security_id_by_ticker={"OLDCO": "sec-cik:2"})
    manifest = load_index_manifest(_write_manifest(tmp_path, payload))
    evidence = qualify_pit_membership(manifest, source=_sp_source(), observed_at=OBSERVED)
    subs = {s.name: s.passed for s in evidence.sub_capabilities}
    assert subs["identity_mapped_for_every_change"] is False
    assert not evidence.promotion_eligible


# -- promotion boundary + tamper evidence --------------------------------------


def test_real_authenticated_evidence_promotes(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    evidence = qualify_pit_membership(manifest, source=_sp_source(), observed_at=OBSERVED)
    assert promote_capability(evidence) is CapabilityState.EMPIRICALLY_VERIFIED


def test_the_same_metrics_from_a_fixture_source_never_promote(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    fixture_source = SourceDescriptor(
        source_id="fixture", name="fixture", version="1", kind=SourceKind.FIXTURE,
        authority=SourceAuthority.FIXTURE.value, raw_checksum=DIGEST,
    )
    evidence = qualify_pit_membership(manifest, source=fixture_source, observed_at=OBSERVED)
    assert not evidence.promotion_eligible
    assert promote_capability(evidence) is CapabilityState.BLOCKED_EXTERNAL


def test_a_tampered_artifact_is_rejected_on_load(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    evidence = qualify_pit_membership(manifest, source=_sp_source(), observed_at=OBSERVED)
    path = tmp_path / "pit_sp500_membership.json"
    evidence.write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"]["historical_changes"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(QualificationError, match="checksum mismatch"):
        load_and_verify_evidence(path)


def test_a_changed_source_checksum_changes_the_evidence_checksum(tmp_path: Path) -> None:
    manifest = load_index_manifest(_write_manifest(tmp_path, _manifest_payload()))
    first = qualify_pit_membership(manifest, source=_sp_source(), observed_at=OBSERVED)
    other = real_source_descriptor(
        source_id="spdji", name="S&P DJI official announcements", version="1",
        authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=OTHER_DIGEST,
        raw_artifact="manifest", access_note="owner-supplied official documents",
    )
    second = qualify_pit_membership(manifest, source=other, observed_at=OBSERVED)
    assert first.normalized_checksum != second.normalized_checksum


# -- identity crosswalk reporting ----------------------------------------------


def test_crosswalk_report_is_machine_readable_and_clean() -> None:
    crosswalk = IdentityCrosswalk(
        [
            CrosswalkEntry(security_id="sec-cik:1", sec_cik="0000000001", ibkr_con_id=11),
            CrosswalkEntry(security_id="sec-cik:2", sec_cik="0000000002"),
        ]
    )
    report = build_crosswalk_report(
        crosswalk, ciks=["0000000001", "0000000002"], con_ids=[11], entry_count=2
    )
    assert report.clean
    assert report.to_dict()["resolved"] == [
        "cik:0000000001=sec-cik:1", "cik:0000000002=sec-cik:2", "conid:11=sec-cik:1",
    ]


def test_crosswalk_report_surfaces_unresolved_identity() -> None:
    crosswalk = IdentityCrosswalk([CrosswalkEntry(security_id="sec-cik:1", sec_cik="0000000001")])
    report = build_crosswalk_report(crosswalk, ciks=["0000000009"], entry_count=1)
    assert not report.clean
    assert report.unresolved == ("cik:0000000009",)
    assert report.as_resolution().clean is False


def test_ticker_only_entry_is_rejected_at_the_boundary() -> None:
    with pytest.raises(SourceAdapterError, match="ticker alone is never historical identity"):
        IdentityCrosswalk([CrosswalkEntry(security_id="x", current_ticker="AAPL")])


# -- as-of GICS: the gap is documented, never filled ---------------------------


def test_asof_gics_requirement_names_what_is_missing_and_refuses_inference() -> None:
    text = ASOF_GICS_MISSING_REQUIREMENT
    assert "HISTORICAL GICS" in text
    assert "SIC" in text, "must explain SEC's SIC is not a GICS substitute"
    assert "refused" in text
