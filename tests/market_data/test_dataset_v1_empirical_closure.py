"""Empirical closure: SEC concept discovery, S&P documents, GICS decision.

Driven by what the real owner run measured:

* the split concept returned ``http_ok=0`` - a 404 for a GUESSED name, which
  says nothing about whether the filers reported a split. Discovery inverts it:
  read what the filer actually reported, then map only reviewed semantics;
* no official S&P documents had ever been supplied, so the membership path had
  never run end to end. It now works from a directory drop with no hand-written
  manifest;
* as-of GICS is finalised as a LICENSE blocker rather than left open.

Offline: no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_real_ingestion import (
    ASOF_GICS_BLOCKER,
    load_index_manifest,
    replay_membership_as_of,
)
from trading_market_data_core.dataset_v1_sec_concepts import (
    REVIEWED_CONCEPT_SEMANTICS,
    DiscoveredConcept,
    parse_company_facts,
    reviewed_concepts_for,
    select_corporate_concepts,
    semantics_for,
)
from trading_market_data_core.dataset_v1_sources import CorporateEventType, SourceAdapterError
from trading_market_data_core.dataset_v1_sp_documents import (
    build_manifest_payload,
    discover_sp_documents,
    parse_sp_document,
)

# ---------------------------------------------------------------- fixtures

COMPANY_FACTS: dict[str, object] = {
    "cik": 320193,
    "facts": {
        "us-gaap": {
            "CommonStockDividendsPerShareDeclared": {
                "units": {"USD/shares": [{"val": 0.24, "filed": "2023-04-28", "accn": "a"}]}
            },
            "StockholdersEquityNoteStockSplitConversionRatio1": {
                "units": {"pure": [{"val": 4, "filed": "2020-08-31", "accn": "b"}]}
            },
            "StockholdersEquityReverseStockSplit": {
                "units": {"pure": [{"val": 1, "filed": "2021-01-01", "accn": "c"}]}
            },
            "SplitAdjustedWeightedAverageSharesOutstanding": {
                "units": {"shares": [{"val": 100, "filed": "2021-01-01", "accn": "d"}]}
            },
            "Revenues": {"units": {"USD": [{"val": 1, "filed": "2021-01-01", "accn": "e"}]}},
        }
    },
}

ANNOUNCEMENT_HTML = b"""<html><body>
<p>NEW YORK - S&amp;P Dow Jones Indices will make the following change.</p>
<p>Announcement Date: March 1, 2021</p>
<p>Newco Inc. (NASDAQ: NEWCO) will replace Oldco Corp. (NYSE: OLDCO) in the
S&amp;P 500 effective prior to the open of trading on March 22, 2021.</p>
<p>Effective Date: March 22, 2021</p>
</body></html>
"""


# ---------------------------------------------------- SEC concept discovery


def test_company_facts_enumerates_what_the_filer_reported() -> None:
    discovered = parse_company_facts(b"{}", COMPANY_FACTS)
    names = {item.concept for item in discovered}
    assert "StockholdersEquityNoteStockSplitConversionRatio1" in names
    assert "Revenues" in names
    ratio = next(i for i in discovered if i.concept.endswith("ConversionRatio1"))
    assert ratio.fact_count == 1 and ratio.units == ("pure",)


def test_malformed_company_facts_fails_closed() -> None:
    with pytest.raises(SourceAdapterError, match="no facts object"):
        parse_company_facts(b"{}", {"cik": 1})
    with pytest.raises(SourceAdapterError, match="must be an object"):
        parse_company_facts(b"{}", {"facts": {"us-gaap": []}})


def test_only_reviewed_concepts_are_accepted() -> None:
    selection = select_corporate_concepts(parse_company_facts(b"{}", COMPANY_FACTS))
    accepted = {item.concept for item in selection.accepted}
    assert "StockholdersEquityNoteStockSplitConversionRatio1" in accepted
    assert "CommonStockDividendsPerShareDeclared" in accepted
    assert "Revenues" not in accepted


def test_a_split_shaped_name_alone_is_only_a_candidate() -> None:
    """The rule the spec insists on: a name match is never sufficient."""
    selection = select_corporate_concepts(parse_company_facts(b"{}", COMPANY_FACTS))
    candidates = {item.concept for item, _ in selection.unmapped_candidates}
    assert "SplitAdjustedWeightedAverageSharesOutstanding" in candidates
    accepted = {item.concept for item in selection.accepted}
    assert "SplitAdjustedWeightedAverageSharesOutstanding" not in accepted


def test_a_reviewed_but_non_event_concept_is_rejected_with_a_reason() -> None:
    selection = select_corporate_concepts(parse_company_facts(b"{}", COMPANY_FACTS))
    rejected = {item.concept: reason for item, reason in selection.rejected}
    assert "StockholdersEquityReverseStockSplit" in rejected
    assert "prose" in rejected["StockholdersEquityReverseStockSplit"]


def test_every_reviewed_concept_carries_a_written_rationale() -> None:
    for item in REVIEWED_CONCEPT_SEMANTICS:
        assert item.rationale.strip(), item.concept
        if item.mints_events:
            assert item.value_meaning.strip(), item.concept


def test_split_direction_comes_from_the_ratio_not_the_name() -> None:
    semantics = semantics_for("us-gaap", "StockholdersEquityNoteStockSplitConversionRatio1")
    assert semantics is not None
    assert semantics.event_type is CorporateEventType.SPLIT
    assert "never from the concept name" in semantics.rationale


def test_a_concept_with_no_facts_is_not_accepted() -> None:
    empty = DiscoveredConcept(
        taxonomy="us-gaap",
        concept="CommonStockDividendsPerShareDeclared",
        units=("USD/shares",),
        fact_count=0,
    )
    selection = select_corporate_concepts([empty])
    assert selection.accepted == ()
    assert "reported no facts" in selection.rejected[0][1]


def test_reviewed_concepts_can_be_listed_per_event_type() -> None:
    assert reviewed_concepts_for(CorporateEventType.DIVIDEND)
    assert reviewed_concepts_for(CorporateEventType.SPLIT)


def test_selection_summary_names_candidates_for_review() -> None:
    selection = select_corporate_concepts(parse_company_facts(b"{}", COMPANY_FACTS))
    text = "\n".join(selection.summary_lines())
    assert "concepts discovered" in text
    assert "CANDIDATE" in text


# ------------------------------------------------------- S&P documents


def _drop(tmp_path: Path, name: str, content: bytes) -> Path:
    directory = tmp_path / "sp500-documents"
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(content)
    return directory


def test_an_official_announcement_yields_both_dates_and_the_replacement(
    tmp_path: Path,
) -> None:
    directory = _drop(tmp_path, "announcement.html", ANNOUNCEMENT_HTML)
    document = parse_sp_document(directory / "announcement.html")
    assert document.announced_on is not None
    assert document.effective_on is not None
    assert document.announced_on < document.effective_on
    assert document.added_tickers == ("NEWCO",)
    assert document.removed_tickers == ("OLDCO",)
    assert document.is_usable


def test_every_parsed_field_keeps_its_label_and_snippet(tmp_path: Path) -> None:
    directory = _drop(tmp_path, "announcement.html", ANNOUNCEMENT_HTML)
    document = parse_sp_document(directory / "announcement.html")
    fields = {match.field_name for match in document.matches}
    assert {"announced_on", "effective_on", "replacement"} <= fields
    for match in document.matches:
        assert match.label and match.snippet


def test_two_different_effective_dates_fail_closed(tmp_path: Path) -> None:
    doc = ANNOUNCEMENT_HTML.replace(
        b"<p>Effective Date: March 22, 2021</p>",
        b"<p>Effective Date: March 22, 2021</p><p>Effective Date: April 5, 2021</p>",
    )
    directory = _drop(tmp_path, "ambiguous.html", doc)
    document = parse_sp_document(directory / "ambiguous.html")
    assert document.effective_on is None
    assert "effective_on" in document.ambiguous_fields
    assert not document.is_usable


def test_a_document_with_no_change_is_reported_not_guessed(tmp_path: Path) -> None:
    directory = _drop(tmp_path, "nochange.html", b"<html><body><p>Nothing here.</p></body></html>")
    document = parse_sp_document(directory / "nochange.html")
    assert not document.is_usable
    assert any("no explicit addition or removal" in f for f in document.findings)


def test_an_unparseable_pdf_is_reported_rather_than_guessed(tmp_path: Path) -> None:
    directory = _drop(tmp_path, "scan.pdf", b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    document = parse_sp_document(directory / "scan.pdf")
    assert document.content_kind == "pdf"
    assert not document.is_usable
    assert any("no extractable text" in f for f in document.findings)


def test_discovery_reports_unsupported_files(tmp_path: Path) -> None:
    directory = _drop(tmp_path, "announcement.html", ANNOUNCEMENT_HTML)
    (directory / "notes.docx").write_bytes(b"binary")
    discovery = discover_sp_documents(directory)
    assert discovery.unsupported == ("notes.docx",)
    assert len(discovery.usable) == 1


def test_a_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    assert discover_sp_documents(tmp_path / "absent").documents == ()


def test_the_manifest_is_generated_from_the_directory(tmp_path: Path) -> None:
    """No hand-authored JSON: the owner drops documents and runs one command."""
    directory = _drop(tmp_path, "announcement.html", ANNOUNCEMENT_HTML)
    payload = build_manifest_payload(discover_sp_documents(directory), index_id="SP500")
    assert payload["index_id"] == "SP500"
    announcement = payload["announcements"][0]  # type: ignore[index]
    assert announcement["added_ticker"] == "NEWCO"
    assert announcement["source_document_checksum"].startswith("sha256:")


def test_the_generated_manifest_loads_and_replays(tmp_path: Path) -> None:
    directory = _drop(tmp_path, "announcement.html", ANNOUNCEMENT_HTML)
    payload = build_manifest_payload(discover_sp_documents(directory), index_id="SP500")
    manifest_path = directory / "sp500-announcements.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = load_index_manifest(manifest_path, document_root=directory)
    from datetime import date

    members = replay_membership_as_of(manifest, as_of=date(2021, 3, 22))
    assert members, "a replay must produce a membership set"


def test_generation_refuses_when_nothing_is_usable(tmp_path: Path) -> None:
    directory = _drop(tmp_path, "nochange.html", b"<html><body>nothing</body></html>")
    with pytest.raises(SourceAdapterError, match="no usable official S&P document"):
        build_manifest_payload(discover_sp_documents(directory), index_id="SP500")


def test_owner_documents_are_not_copied_into_the_manifest(tmp_path: Path) -> None:
    """Only the digest, filename and parsed fields travel - never the content."""
    directory = _drop(tmp_path, "announcement.html", ANNOUNCEMENT_HTML)
    payload = build_manifest_payload(discover_sp_documents(directory), index_id="SP500")
    serialized = json.dumps(payload)
    assert "S&P Dow Jones Indices will make" not in serialized


# ------------------------------------------------------------- GICS decision


def test_gics_is_formalised_as_a_license_blocker() -> None:
    assert ASOF_GICS_BLOCKER["blocker_class"] == "LICENSE_REQUIRED"
    assert ASOF_GICS_BLOCKER["state"] == "BLOCKED_EXTERNAL"


def test_the_blocker_explains_why_sic_is_not_gics() -> None:
    reviewed = ASOF_GICS_BLOCKER["sources_reviewed"]
    assert isinstance(reviewed, dict)
    assert "SIC" in reviewed["sec"]
    assert "different taxonomy" in reviewed["sec"]


def test_the_blocker_explains_why_current_gics_is_insufficient() -> None:
    assert "look-ahead" in str(ASOF_GICS_BLOCKER["why_current_gics_is_insufficient"])


def test_the_blocker_states_what_a_licensed_provider_must_supply() -> None:
    required = ASOF_GICS_BLOCKER["licensed_provider_must_supply"]
    assert isinstance(required, list)
    joined = " ".join(required)
    assert "effective date" in joined
    assert "knowledge" in joined
    assert "non-ticker" in joined


def test_the_blocker_states_minimum_acceptance_criteria() -> None:
    criteria = ASOF_GICS_BLOCKER["minimum_qualification_acceptance"]
    assert isinstance(criteria, list) and len(criteria) >= 4
    assert any("historical classification CHANGE" in item for item in criteria)


def test_every_approved_source_was_reviewed_and_recorded() -> None:
    reviewed = ASOF_GICS_BLOCKER["sources_reviewed"]
    assert isinstance(reviewed, dict)
    assert {"ibkr", "sec", "sp_dji_public", "issuer_exchange_public", "free_secondary"} <= set(
        reviewed
    )
