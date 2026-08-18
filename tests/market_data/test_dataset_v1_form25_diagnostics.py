"""Form 25 diagnostics, document correlation, and the IBKR adjustment binding.

The owner run reported `delisting_effective_date_published: 0/36` and, in an
earlier run, `0 TRADES vs ADJUSTED_LAST comparisons` despite the IBKR artifact
carrying 4 observations. These tests make both answerable from evidence rather
than from guesswork, and pin the correlation rule that stops a document being
attached to the wrong filing.

Offline: no network, no SEC request, no IBKR connection.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_form25 import (
    assert_document_correlates,
    parse_form25_document,
    primary_document_url,
    summarize_form25,
)
from trading_market_data_core.dataset_v1_ingest_cli import _load_ibkr_probes
from trading_market_data_core.dataset_v1_qualification import CoverageWindow
from trading_market_data_core.dataset_v1_real_ingestion import (
    qualify_corporate_actions,
    real_source_descriptor,
)
from trading_market_data_core.dataset_v1_sources import (
    CanonicalCorporateEvent,
    CorporateEventType,
    SecFilingRecord,
    SourceAdapterError,
    SourceAuthority,
)

OBSERVED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _filing(**kw: object) -> SecFilingRecord:
    defaults: dict[str, object] = {
        "cik": "0001418091", "accession": "0001418091-23-000001", "form": "25-NSE",
        "filing_date": date(2023, 1, 10),
        "accepted_at": datetime(2023, 1, 10, 16, 30, tzinfo=UTC),
        "primary_document": "primary_doc.xml",
    }
    defaults.update(kw)
    return SecFilingRecord(**defaults)  # type: ignore[arg-type]


WITH_DATE = b"""<?xml version="1.0"?>
<edgarSubmission><issuerNm>Example</issuerNm>
<dateOfDelisting>2023-01-20</dateOfDelisting>
<lastTradingDate>2023-01-19</lastTradingDate></edgarSubmission>
"""
NO_DATE = b"""<?xml version="1.0"?>
<edgarSubmission><issuerNm>Example</issuerNm>
<noticeDate>2023-01-15</noticeDate><rule>12b-2(a)(1)</rule></edgarSubmission>
"""


# -- 9. coverage diagnostics ---------------------------------------------------


def test_coverage_reports_the_numbers_the_owner_needs() -> None:
    documents = [
        parse_form25_document(WITH_DATE, filing=_filing(accession="a-1")),
        parse_form25_document(NO_DATE, filing=_filing(accession="a-2")),
        parse_form25_document(b"plain prose, not a filing", filing=_filing(accession="a-3")),
    ]
    coverage = summarize_form25(documents)
    assert coverage.documents == 3
    assert coverage.with_effective_date == 1
    assert coverage.with_last_trading_date == 1
    assert coverage.xml_documents == 2
    assert coverage.non_xml_documents == 1
    assert not coverage.complete


def test_failures_are_grouped_by_reason() -> None:
    documents = [
        parse_form25_document(NO_DATE, filing=_filing(accession="a-1")),
        parse_form25_document(b"not xml", filing=_filing(accession="a-2")),
    ]
    reasons = summarize_form25(documents).failures_by_reason
    assert reasons["no_effective_date_element"] == 1
    assert reasons["not_xml"] == 1


def test_unmatched_date_like_elements_are_surfaced_for_review() -> None:
    """0/36 must be actionable: report the tags actually seen, do not invent."""
    coverage = summarize_form25([parse_form25_document(NO_DATE, filing=_filing())])
    assert "noticedate" in coverage.unmatched_date_candidates
    # a non-date element is not proposed as a candidate
    assert "issuernm" not in coverage.unmatched_date_candidates


def test_an_allow_listed_tag_is_not_reported_as_unmatched() -> None:
    coverage = summarize_form25([parse_form25_document(WITH_DATE, filing=_filing())])
    assert "dateofdelisting" not in coverage.unmatched_date_candidates


def test_summary_lines_are_printable_and_concise() -> None:
    lines = summarize_form25([parse_form25_document(WITH_DATE, filing=_filing())]).summary_lines()
    joined = "\n".join(lines)
    assert "documents fetched" in joined
    assert "effective-date matches       : 1/1" in joined


def test_coverage_survives_an_empty_population() -> None:
    coverage = summarize_form25([])
    assert coverage.documents == 0
    assert not coverage.complete, "no documents is never complete"


# -- 10. document/filing correlation -------------------------------------------


def test_a_correlated_document_is_accepted() -> None:
    filing = _filing()
    assert_document_correlates(primary_document_url(filing), filing)


def test_a_document_from_another_filing_is_refused() -> None:
    filing = _filing()
    other = primary_document_url(_filing(accession="9999999-99-999999", cik="0000999999"))
    with pytest.raises(SourceAdapterError, match="does not correlate"):
        assert_document_correlates(other, filing)


def test_a_document_with_the_wrong_filename_is_refused() -> None:
    filing = _filing()
    url = primary_document_url(filing).replace("primary_doc.xml", "other_doc.xml")
    with pytest.raises(SourceAdapterError, match="does not correlate"):
        assert_document_correlates(url, filing)


def test_parsing_refuses_a_filing_with_no_primary_document() -> None:
    with pytest.raises(SourceAdapterError, match="declares no primaryDocument"):
        parse_form25_document(WITH_DATE, filing=_filing(primary_document=None))


def test_the_document_checksum_is_bound_to_the_parsed_assertion() -> None:
    document = parse_form25_document(WITH_DATE, filing=_filing())
    assert document.document_checksum.startswith("sha256:")
    assert document.accession == "0001418091-23-000001"
    assert document.cik == "0001418091"
    other = parse_form25_document(NO_DATE, filing=_filing())
    assert other.document_checksum != document.document_checksum


# -- 12. IBKR adjustment observations actually reach the evidence --------------


def _ibkr_artifact(path: Path, observations: int) -> Path:
    """The real POI-IBKR-CORP.json shape produced on the owner host."""
    payload = {
        "version": "poi-ibkr-corporate-qualification-1.0.0",
        "account_alias": "paper-poi",
        "masked_account_id": "DU***567",
        "probes": [
            {"capability": "HISTORICAL_BARS_TRADES", "outcome": "AVAILABLE", "detail": ""},
            {"capability": "ADJUSTED_LAST", "outcome": "AVAILABLE", "detail": ""},
            {"capability": "CONTRACT_IDENTITY_CONID", "outcome": "AVAILABLE", "detail": ""},
            {"capability": "CORPORATE_EVENT_FEED", "outcome": "UNAVAILABLE", "detail": "none"},
        ],
        "adjustment_observations": [
            {
                "symbol": "AAPL", "bar_date": f"2023010{index + 1}",
                "trades_close": "100", "adjusted_last_close": "25",
                "diverges": True, "inference_boundary": "adjustment only",
            }
            for index in range(observations)
        ],
        "notes": [],
        "observed_at": "2026-08-18T00:00:00+00:00",
        "evidence_checksum": DIGEST,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def test_the_real_artifact_shape_yields_probes_and_observations(tmp_path: Path) -> None:
    """Owner run: adjustment_observations = 4. They must reach qualification."""
    path = _ibkr_artifact(tmp_path / "POI-IBKR-CORP.json", observations=4)
    probes, adjustments = _load_ibkr_probes(path)
    assert len(adjustments) == 4, "all 4 observations must be bound"
    assert {p.capability.value for p in probes} >= {"ADJUSTED_LAST", "CORPORATE_EVENT_FEED"}
    assert all(a.diverges for a in adjustments)
    assert adjustments[0].observed_on == date(2023, 1, 1)


def test_bound_observations_credit_the_adjustment_sub_capability(tmp_path: Path) -> None:
    path = _ibkr_artifact(tmp_path / "POI-IBKR-CORP.json", observations=4)
    _, adjustments = _load_ibkr_probes(path)
    evidence = qualify_corporate_actions(
        [
            CanonicalCorporateEvent(
                event_id="e1", security_id="sec-cik:1",
                event_type=CorporateEventType.DELISTING, source_id="sec",
                source_authority=SourceAuthority.OFFICIAL_PUBLIC, source_checksum=DIGEST,
                provenance="SEC Form 25", filing_date=date(2023, 1, 10),
            )
        ],
        adjustment_checks=adjustments,
        source=real_source_descriptor(
            source_id="sec", name="SEC", version="1",
            authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=DIGEST,
            raw_artifact="idx", access_note="",
        ),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    subs = {s.name: s for s in evidence.sub_capabilities}
    assert subs["adjustment_behaviour_characterised"].passed
    assert "4 TRADES vs ADJUSTED_LAST" in subs["adjustment_behaviour_characterised"].detail


def test_zero_observations_do_not_credit_the_sub_capability(tmp_path: Path) -> None:
    """It must not auto-PASS: no observations means the criterion is unmet."""
    path = _ibkr_artifact(tmp_path / "POI-IBKR-CORP.json", observations=0)
    _, adjustments = _load_ibkr_probes(path)
    assert adjustments == []


def test_a_missing_artifact_binds_nothing_without_crashing() -> None:
    probes, adjustments = _load_ibkr_probes(Path("does-not-exist.json"))
    assert probes == [] and adjustments == []


def test_the_ibkr_feed_gap_no_longer_blocks_promotion_forever(tmp_path: Path) -> None:
    """CONTRACT DECISION: SEC alone can satisfy the required event set."""
    path = _ibkr_artifact(tmp_path / "POI-IBKR-CORP.json", observations=4)
    probes, adjustments = _load_ibkr_probes(path)
    events = [
        CanonicalCorporateEvent(
            event_id=f"e-{event_type.value}", security_id="sec-cik:1",
            event_type=event_type, source_id="sec",
            source_authority=SourceAuthority.OFFICIAL_PUBLIC, source_checksum=DIGEST,
            provenance="SEC", filing_date=date(2023, 1, 10),
            known_at=datetime(2023, 1, 10, 16, tzinfo=UTC),
        )
        for event_type in (
            CorporateEventType.SPLIT, CorporateEventType.REVERSE_SPLIT,
            CorporateEventType.DIVIDEND, CorporateEventType.MERGER,
            CorporateEventType.SYMBOL_CHANGE, CorporateEventType.DELISTING,
        )
    ]
    evidence = qualify_corporate_actions(
        events, adjustment_checks=adjustments, ibkr_probes=probes,
        source=real_source_descriptor(
            source_id="sec", name="SEC", version="1",
            authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=DIGEST,
            raw_artifact="idx", access_note="",
        ),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    feed = next(s for s in evidence.sub_capabilities if s.name == "ibkr_event_feed_available")
    assert not feed.passed and not feed.required
    assert evidence.promotion_eligible, "an unavailable feed must not block forever"
    assert not any("ibkr_event_feed" in reason for reason in evidence.blocked_reasons)


def test_a_missing_required_event_type_still_blocks(tmp_path: Path) -> None:
    """The required-set gate stays strict after the informational split."""
    path = _ibkr_artifact(tmp_path / "POI-IBKR-CORP.json", observations=4)
    probes, adjustments = _load_ibkr_probes(path)
    evidence = qualify_corporate_actions(
        [
            CanonicalCorporateEvent(
                event_id="only-dividends", security_id="sec-cik:1",
                event_type=CorporateEventType.DIVIDEND, source_id="sec",
                source_authority=SourceAuthority.OFFICIAL_PUBLIC, source_checksum=DIGEST,
                provenance="SEC XBRL", filing_date=date(2023, 1, 10),
            )
        ],
        adjustment_checks=adjustments, ibkr_probes=probes,
        source=real_source_descriptor(
            source_id="sec", name="SEC", version="1",
            authority=SourceAuthority.OFFICIAL_PUBLIC, raw_checksum=DIGEST,
            raw_artifact="idx", access_note="",
        ),
        coverage=CoverageWindow(date(2023, 1, 1), date(2023, 3, 31)),
        observed_at=OBSERVED,
    )
    assert not evidence.promotion_eligible
    assert any("required sub-capabilities" in r for r in evidence.blocked_reasons)
