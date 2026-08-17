"""Dataset V1 artifact typing, crosswalk integrity and Form 25 documents.

The owner-host run of PR #84 failed with

    EVIDENCE_INVALID docs/poi/evidence/dataset-v1/identity-crosswalk.json
    evidence is missing a sha256 normalized_checksum

on a perfectly valid crosswalk, because the verifier globbed ``*.json`` and
applied the qualification schema to everything. These tests pin the fix:
classification, not relaxation. Every type keeps its own integrity rules, and a
tampered auxiliary artifact still fails its own validation.

Fully offline: no network, no SEC request, no IBKR connection.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_artifacts import (
    PROMOTABLE_ARTIFACT_TYPES,
    ArtifactError,
    ArtifactType,
    AuxiliaryReport,
    CrosswalkArtifact,
    CrosswalkSourceBinding,
    classify_payload,
    load_and_verify_crosswalk,
    scan_evidence_dir,
    verify_identity_binding,
)
from trading_market_data_core.dataset_v1_form25 import (
    EIGHT_K_CANDIDATE_ITEMS,
    Form25Document,
    candidate_signals,
    effective_dates_by_accession,
    parse_form25_document,
    primary_document_url,
    summarize_form25,
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
from trading_market_data_core.dataset_v1_sources import (
    SecFilingRecord,
    SourceAdapterError,
    parse_xbrl_concept,
    xbrl_facts_to_events,
)

OBSERVED = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _crosswalk(**kw: object) -> CrosswalkArtifact:
    defaults: dict[str, object] = {
        "entries": 5,
        "resolved": ("cik:0000000001=sec-cik:1", "cik:0000000002=sec-cik:2"),
        "ambiguous": (),
        "unresolved": (),
        "conflicting": (),
        "observed_at": OBSERVED,
        "sources": (
            CrosswalkSourceBinding(name="sec-edgar", version="2023Q1", checksum=DIGEST),
        ),
    }
    defaults.update(kw)
    return CrosswalkArtifact(**defaults)  # type: ignore[arg-type]


def _evidence(capability: str = "delisted_security_history"):  # type: ignore[no-untyped-def]
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
        proofs=("inactive_security_resolvable",),
        observed_at=OBSERVED,
    )


# -- 1. artifact classification ------------------------------------------------


def test_qualification_evidence_declares_its_own_type() -> None:
    assert _evidence().to_dict()["artifact_type"] == "qualification_evidence"


def test_explicit_type_wins_over_structure() -> None:
    payload: dict[str, object] = {
        "artifact_type": "identity_crosswalk", "capability": "x",
        "normalized_checksum": DIGEST,
    }
    assert classify_payload(payload) is ArtifactType.IDENTITY_CROSSWALK


def test_legacy_evidence_without_the_field_is_still_recognised() -> None:
    payload: dict[str, object] = {
        "capability": "corporate_actions", "normalized_checksum": DIGEST,
    }
    assert classify_payload(payload) is ArtifactType.QUALIFICATION_EVIDENCE


def test_legacy_crosswalk_without_the_field_is_recognised() -> None:
    payload: dict[str, object] = {
        "resolved": [], "ambiguous": [], "unresolved": [], "conflicting": [],
    }
    assert classify_payload(payload) is ArtifactType.IDENTITY_CROSSWALK


def test_an_unknown_type_fails_closed() -> None:
    with pytest.raises(ArtifactError, match="unknown artifact_type"):
        classify_payload({"artifact_type": "something_invented"})


def test_an_unrecognisable_shape_is_never_guessed() -> None:
    with pytest.raises(ArtifactError, match="refusing to guess"):
        classify_payload({"hello": "world"})


def test_only_qualification_evidence_is_promotable() -> None:
    assert PROMOTABLE_ARTIFACT_TYPES == frozenset({ArtifactType.QUALIFICATION_EVIDENCE})


# -- mixed directory: the exact owner-host failure -----------------------------


def _mixed_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "dataset-v1"
    directory.mkdir()
    _evidence("delisted_security_history").write(directory / "delisted_security_history.json")
    _evidence("corporate_actions").write(directory / "corporate_actions.json")
    _crosswalk().sealed().write(directory / "identity-crosswalk.json")
    AuxiliaryReport(name="form25-coverage", observed_at=OBSERVED).write(
        directory / "form25-coverage.json"
    )
    (directory / "snapshot-report.json").write_text(
        json.dumps({"artifact_type": "snapshot_report", "snapshot_readiness": "PARTIAL_DATA"}),
        encoding="utf-8",
    )
    return directory


def test_mixed_directory_is_classified_by_type_not_by_filename(tmp_path: Path) -> None:
    scan = scan_evidence_dir(_mixed_dir(tmp_path))
    assert len(scan.paths(ArtifactType.QUALIFICATION_EVIDENCE)) == 2
    assert len(scan.paths(ArtifactType.IDENTITY_CROSSWALK)) == 1
    assert len(scan.paths(ArtifactType.AUXILIARY_DATA_QUALITY_REPORT)) == 1
    assert len(scan.paths(ArtifactType.SNAPSHOT_REPORT)) == 1
    assert not scan.unclassified


def test_only_evidence_reaches_the_promotion_path(tmp_path: Path) -> None:
    """The regression: the crosswalk must never be parsed as evidence."""
    scan = scan_evidence_dir(_mixed_dir(tmp_path))
    names = {path.name for path in scan.promotable}
    assert names == {"delisted_security_history.json", "corporate_actions.json"}
    assert "identity-crosswalk.json" not in names


def test_an_unclassifiable_file_is_reported_not_silently_skipped(tmp_path: Path) -> None:
    directory = _mixed_dir(tmp_path)
    (directory / "junk.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    scan = scan_evidence_dir(directory)
    assert [p.name for p, _ in scan.unclassified] == ["junk.json"]


def test_scanning_a_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert scan_evidence_dir(tmp_path / "absent").promotable == ()


# -- 2. crosswalk integrity ----------------------------------------------------


def test_valid_crosswalk_round_trips_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "identity-crosswalk.json"
    checksum = _crosswalk().sealed().write(path)
    loaded = load_and_verify_crosswalk(path)
    assert loaded.checksum == checksum
    assert loaded.clean
    assert loaded.entries == 5


def test_tampered_crosswalk_fails_its_own_validation(tmp_path: Path) -> None:
    path = tmp_path / "identity-crosswalk.json"
    _crosswalk().sealed().write(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resolved"].append("cik:0000000009=sec-cik:9")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="checksum mismatch"):
        load_and_verify_crosswalk(path)


def test_crosswalk_missing_a_checksum_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "identity-crosswalk.json"
    payload = _crosswalk().sealed().to_dict()
    del payload["checksum"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactError, match="missing a sha256 checksum"):
        load_and_verify_crosswalk(path)


def test_checksum_changes_when_identity_changes() -> None:
    first = _crosswalk().sealed()
    second = _crosswalk(resolved=("cik:0000000001=sec-cik:1",)).sealed()
    assert first.checksum != second.checksum


def test_input_ordering_does_not_affect_the_checksum() -> None:
    forward = _crosswalk(
        resolved=("cik:0000000001=sec-cik:1", "cik:0000000002=sec-cik:2")
    ).sealed()
    reversed_ = _crosswalk(
        resolved=("cik:0000000002=sec-cik:2", "cik:0000000001=sec-cik:1")
    ).sealed()
    assert forward.checksum == reversed_.checksum


def test_crosswalk_source_binding_requires_a_real_digest() -> None:
    with pytest.raises(ArtifactError, match="real sha256 checksum"):
        CrosswalkSourceBinding(name="sec", version="1", checksum="sha256:abc")


def test_loading_evidence_as_a_crosswalk_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "delisted_security_history.json"
    _evidence().write(path)
    with pytest.raises(ArtifactError, match="not an identity crosswalk"):
        load_and_verify_crosswalk(path)


def test_identity_binding_reports_unbound_security_ids() -> None:
    crosswalk = _crosswalk().sealed()
    assert verify_identity_binding(["sec-cik:1"], crosswalk) == ()
    assert verify_identity_binding(["sec-cik:1", "sec-cik:99"], crosswalk) == ("sec-cik:99",)


def test_auxiliary_report_declares_it_qualifies_nothing() -> None:
    payload = AuxiliaryReport(name="candidates", observed_at=OBSERVED).to_dict()
    assert payload["artifact_type"] == "auxiliary_data_quality_report"
    assert payload["qualifies_nothing"] is True
    assert classify_payload(payload) is ArtifactType.AUXILIARY_DATA_QUALITY_REPORT


# -- 3. Form 25 primary documents ----------------------------------------------


def _filing(**kw: object) -> SecFilingRecord:
    defaults: dict[str, object] = {
        "cik": "0001418091", "accession": "0001418091-23-000001", "form": "25-NSE",
        "filing_date": date(2023, 1, 10),
        "accepted_at": datetime(2023, 1, 10, 16, 30, tzinfo=UTC),
        "primary_document": "primary_doc.xml",
    }
    defaults.update(kw)
    return SecFilingRecord(**defaults)  # type: ignore[arg-type]


FORM25_XML = b"""<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/form25">
  <issuerNm>Example Corp</issuerNm>
  <exchangeNm>NASDAQ</exchangeNm>
  <rule>12b-2(a)(1)</rule>
  <dateOfDelisting>2023-01-20</dateOfDelisting>
  <lastTradingDate>2023-01-19</lastTradingDate>
</edgarSubmission>
"""

FORM25_NO_DATE = b"""<?xml version="1.0"?>
<edgarSubmission>
  <issuerNm>Example Corp</issuerNm>
  <rule>12b-2(a)(1)</rule>
</edgarSubmission>
"""


def test_primary_document_url_uses_the_archive_layout() -> None:
    url = primary_document_url(_filing())
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/1418091/000141809123000001/primary_doc.xml"
    )


def test_a_filing_without_a_primary_document_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="no primaryDocument"):
        primary_document_url(_filing(primary_document=None))


def test_published_effective_date_is_extracted_with_provenance() -> None:
    document = parse_form25_document(FORM25_XML, filing=_filing())
    assert document.effective_on == date(2023, 1, 20)
    assert document.last_trading_on == date(2023, 1, 19)
    assert document.publishes_effective_date
    provenance = {item.field_name: item for item in document.provenance}
    assert provenance["effective_on"].source_element == "dateofdelisting"
    assert provenance["effective_on"].raw_value == "2023-01-20"
    assert document.document_checksum.startswith("sha256:")


def test_absent_effective_date_stays_unknown_and_is_never_computed() -> None:
    """Rule 12d2-2's ten-day period must never be turned into a date."""
    document = parse_form25_document(FORM25_NO_DATE, filing=_filing())
    assert document.effective_on is None
    assert any("NEVER computed" in finding for finding in document.findings)
    assert effective_dates_by_accession([document]) == {}


def test_unparseable_date_is_recorded_as_unknown_not_guessed() -> None:
    raw = FORM25_XML.replace(b"2023-01-20", b"whenever")
    document = parse_form25_document(raw, filing=_filing())
    assert document.effective_on is None
    assert any("unparseable" in finding for finding in document.findings)


def test_observed_elements_are_recorded_for_widening_the_allow_list() -> None:
    document = parse_form25_document(FORM25_NO_DATE, filing=_filing())
    assert "issuernm" in document.observed_elements
    assert "rule" in document.observed_elements


def test_a_non_xml_document_extracts_nothing_and_says_so() -> None:
    document = parse_form25_document(b"Not a filing, just prose.", filing=_filing())
    assert document.effective_on is None
    assert any("not XML" in finding for finding in document.findings)


def test_a_document_declaring_entities_is_refused() -> None:
    hostile = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>'
    with pytest.raises(SourceAdapterError, match="DTD/ENTITY"):
        parse_form25_document(hostile, filing=_filing())


def test_an_empty_document_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="empty Form 25 document"):
        parse_form25_document(b"", filing=_filing())


def test_coverage_summary_drives_partial_semantics() -> None:
    with_date = parse_form25_document(FORM25_XML, filing=_filing())
    without = parse_form25_document(FORM25_NO_DATE, filing=_filing(accession="b"))
    coverage = summarize_form25([with_date, without])
    assert coverage.documents == 2
    assert coverage.with_effective_date == 1
    assert not coverage.complete, "1 of 2 is PARTIAL, never PASS"


def test_full_coverage_is_reported_as_complete() -> None:
    documents = [
        parse_form25_document(FORM25_XML, filing=_filing()),
        parse_form25_document(FORM25_XML, filing=_filing(accession="b")),
    ]
    assert summarize_form25(documents).complete


def test_effective_dates_map_only_published_values() -> None:
    documents = [
        parse_form25_document(FORM25_XML, filing=_filing(accession="has-date")),
        parse_form25_document(FORM25_NO_DATE, filing=_filing(accession="no-date")),
    ]
    mapped = effective_dates_by_accession(documents)
    assert mapped == {"has-date": date(2023, 1, 20)}


# -- 8-K item codes are leads, never events ------------------------------------


def test_eight_k_items_are_candidate_signals_only() -> None:
    signals = candidate_signals("2.01,9.01")
    assert len(signals) == 1
    assert "ambiguous direction" in signals[0]
    assert "2.01" in EIGHT_K_CANDIDATE_ITEMS


def test_unknown_items_produce_no_signal() -> None:
    assert candidate_signals("9.01") == ()
    assert candidate_signals("") == ()


# -- XBRL: real split and dividend facts ---------------------------------------


def _concept(tag: str, unit: str, entries: list[dict[str, object]]) -> dict[str, object]:
    return {"cik": 1418091, "tag": tag, "units": {unit: entries}}


def test_dividend_facts_become_events_with_period_provenance() -> None:
    payload = _concept(
        "CommonStockDividendsPerShareDeclared", "USD/shares",
        [{"val": 0.24, "start": "2023-01-01", "end": "2023-03-31", "filed": "2023-04-28",
          "accn": "0000320193-23-000064", "form": "10-Q"}],
    )
    events = xbrl_facts_to_events(
        parse_xbrl_concept(b"{}", payload), security_id="sec-cik:1",
        source_id="sec-xbrl", source_checksum=DIGEST,
    )
    assert len(events) == 1
    assert events[0].event_type.value == "DIVIDEND"
    assert events[0].filing_date == date(2023, 4, 28)
    # XBRL does not publish an ex-date, so it is not asserted
    assert events[0].effective_on is None
    assert "period 2023-01-01..2023-03-31" in events[0].provenance
    assert "0000320193-23-000064" in events[0].provenance


def test_split_ratio_above_one_is_a_split_and_below_one_is_a_reverse_split() -> None:
    def _events(ratio: object):  # type: ignore[no-untyped-def]
        payload = _concept(
            "StockholdersEquityNoteStockSplitConversionRatio1", "pure",
            [{"val": ratio, "filed": "2020-08-31", "accn": "acc-1", "form": "8-K"}],
        )
        return xbrl_facts_to_events(
            parse_xbrl_concept(b"{}", payload), security_id="sec-cik:1",
            source_id="sec-xbrl", source_checksum=DIGEST,
        )

    assert _events(4)[0].event_type.value == "SPLIT"
    assert _events(0.05)[0].event_type.value == "REVERSE_SPLIT"
    assert _events(1) == (), "a 1:1 ratio is not an event"


def test_a_zero_dividend_is_not_an_event() -> None:
    payload = _concept(
        "CommonStockDividendsPerShareDeclared", "USD/shares",
        [{"val": 0, "filed": "2023-04-28", "accn": "acc", "form": "10-Q"}],
    )
    assert xbrl_facts_to_events(
        parse_xbrl_concept(b"{}", payload), security_id="s", source_id="x",
        source_checksum=DIGEST,
    ) == ()


def test_restated_duplicate_facts_collapse_to_one_event() -> None:
    entry = {"val": 0.24, "filed": "2023-04-28", "accn": "acc-1", "form": "10-Q"}
    payload = _concept(
        "CommonStockDividendsPerShareDeclared", "USD/shares", [entry, dict(entry)]
    )
    events = xbrl_facts_to_events(
        parse_xbrl_concept(b"{}", payload), security_id="s", source_id="x",
        source_checksum=DIGEST,
    )
    assert len(events) == 1, "a restated fact must not inflate the event count"


def test_malformed_xbrl_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="no tag"):
        parse_xbrl_concept(b"{}", {"units": {}})
    with pytest.raises(SourceAdapterError, match="missing val/filed/accn"):
        parse_xbrl_concept(b"{}", _concept("X", "pure", [{"val": 1}]))
    with pytest.raises(SourceAdapterError, match="must be an array"):
        parse_xbrl_concept(b"{}", {"tag": "X", "units": {"pure": {}}})


def test_xbrl_parsing_is_deterministic() -> None:
    payload = _concept(
        "CommonStockDividendsPerShareDeclared", "USD/shares",
        [{"val": 0.24, "filed": "2023-04-28", "accn": "b", "form": "10-Q"},
         {"val": 0.23, "filed": "2023-01-28", "accn": "a", "form": "10-Q"}],
    )
    first = parse_xbrl_concept(b"{}", payload)
    second = parse_xbrl_concept(b"{}", payload)
    assert first == second
    assert [f.accession for f in first] == ["a", "b"], "sorted by (filed, accession)"


def test_form25_document_dict_is_auditable() -> None:
    payload: dict[str, object] = parse_form25_document(FORM25_XML, filing=_filing()).to_dict()
    assert payload["effective_on"] == "2023-01-20"
    assert payload["document_checksum"]
    provenance = payload["provenance"]
    assert isinstance(provenance, list) and provenance
    assert isinstance(Form25Document.to_dict, object)


# -- SEC pacing is one budget for the whole run --------------------------------


def test_every_stage_shares_one_paced_sec_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Separate clients would each open an unpaced budget.

    With three stages fetching from SEC, per-stage clients would let the
    effective request rate reach a multiple of the published ceiling.
    """
    from trading_market_data_core.dataset_v1_ingest_cli import _sec_transport

    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "Test Owner test@example.invalid")
    _sec_transport.cache_clear()
    first_fetcher, first_client = _sec_transport()
    second_fetcher, second_client = _sec_transport()
    assert first_client is second_client, "one throttle state for the whole run"
    assert first_fetcher is second_fetcher
    _sec_transport.cache_clear()


def test_pace_is_public_so_document_fetches_share_the_budget() -> None:
    """A document fetch must be able to pace without touching a private method."""
    from trading_market_data_core.dataset_v1_sources import SecEdgarClient

    slept: list[float] = []

    class _Fetcher:
        def get_json(self, url: str, *, headers: object) -> tuple[bytes, object]:
            return b"{}", {}

    client = SecEdgarClient(_Fetcher(), user_agent="Owner owner@example.invalid",
                            sleep=slept.append)
    client.pace()
    client.pace()
    assert slept, "the second paced call must respect the published ceiling"
