"""Form 25 HTML/text parsing — the documents SEC actually serves.

The real owner run settled the question with data: 5 documents fetched, 0 parsed
as XML, 5 non-XML. So the next step was never "add more XML tags"; it was to
parse HTML safely.

These tests pin the safety rules: only a date beside an EXPLICIT lifecycle label
is used, ambiguity fails closed, statutory dates are never computed, and active
content can never contribute text.

Offline: no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from trading_market_data_core.dataset_v1_form25 import parse_form25_document, summarize_form25
from trading_market_data_core.dataset_v1_form25_html import (
    html_summary,
    html_to_text,
    parse_form25_html,
    unmatched_relevant_phrases,
)
from trading_market_data_core.dataset_v1_sources import SecFilingRecord

TABLE_DOC = b"""<html><body>
<table>
  <tr><td>Name of Issuer</td><td>Example Corporation</td></tr>
  <tr><td>Name of Exchange</td><td>NASDAQ Stock Market LLC</td></tr>
  <tr><td>Effective Date of Delisting</td><td>2023-01-20</td></tr>
  <tr><td>Last Trading Date</td><td>January 19, 2023</td></tr>
</table>
</body></html>
"""

LABEL_DOC = b"""<html><body>
<p>Pursuant to Rule 12d2-2(a)(1) the exchange certifies removal.</p>
<p>Date of Delisting: 01/20/2023</p>
</body></html>
"""

PROSE_ONLY = b"""<html><body>
<p>The registrant was incorporated on 1998-04-01 and has delisted securities.</p>
<p>This report was signed on March 5, 2023.</p>
</body></html>
"""

AMBIGUOUS = b"""<html><body>
<p>Effective Date of Delisting: 2023-01-20</p>
<p>Effective Date of Delisting: 2023-02-28</p>
</body></html>
"""

HOSTILE = b"""<html><body>
<script>var d = "Effective Date of Delisting: 2023-01-20";</script>
<p onclick="alert('Date of Delisting: 2020-01-01')">Nothing published here.</p>
</body></html>
"""


def _filing() -> SecFilingRecord:
    return SecFilingRecord(
        cik="0001418091", accession="0001418091-23-000001", form="25-NSE",
        filing_date=date(2023, 1, 10),
        accepted_at=datetime(2023, 1, 10, 16, 30, tzinfo=UTC),
        primary_document="primary_doc.htm",
    )


# -- table and label layouts ---------------------------------------------------


def test_table_layout_yields_both_lifecycle_dates() -> None:
    result = parse_form25_html(TABLE_DOC)
    assert result.content_kind == "html"
    assert result.effective_on == date(2023, 1, 20)
    assert result.last_trading_on == date(2023, 1, 19)
    assert result.issuer_name and "Example Corporation" in result.issuer_name


def test_label_value_layout_with_a_slash_date() -> None:
    result = parse_form25_html(LABEL_DOC)
    assert result.effective_on == date(2023, 1, 20)
    assert result.rule_provision is not None


def test_every_match_keeps_its_label_and_snippet() -> None:
    result = parse_form25_html(TABLE_DOC)
    effective = next(m for m in result.matches if m.field_name == "effective_on")
    assert "delisting" in effective.label
    assert "2023-01-20" in effective.snippet


# -- the safety rules ----------------------------------------------------------


def test_a_date_in_unrelated_prose_is_never_used() -> None:
    """Incorporation and signature dates must not become lifecycle dates."""
    result = parse_form25_html(PROSE_ONLY)
    assert result.effective_on is None
    assert result.last_trading_on is None
    assert any("NEVER computed" in finding for finding in result.findings)


def test_two_different_dates_under_one_label_fail_closed() -> None:
    result = parse_form25_html(AMBIGUOUS)
    assert result.effective_on is None
    assert "effective_on" in result.ambiguous_fields
    assert any("failing closed" in finding for finding in result.findings)


def test_the_same_date_repeated_is_not_ambiguous() -> None:
    doc = AMBIGUOUS.replace(b"2023-02-28", b"2023-01-20")
    result = parse_form25_html(doc)
    assert result.effective_on == date(2023, 1, 20)
    assert not result.ambiguous_fields


def test_active_content_cannot_contribute_a_date() -> None:
    result = parse_form25_html(HOSTILE)
    assert result.active_content_stripped
    assert result.effective_on is None, "a date inside <script> is not published data"


def test_statutory_dates_are_never_computed() -> None:
    """Filing + 10 days must never appear as an effective date."""
    result = parse_form25_html(PROSE_ONLY)
    assert result.effective_on is None


def test_an_empty_document_is_reported_not_guessed() -> None:
    result = parse_form25_html(b"")
    assert result.content_kind == "empty"
    assert result.effective_on is None


# -- robustness ----------------------------------------------------------------


def test_malformed_but_readable_html_still_parses() -> None:
    broken = b"<html><body><p>Effective Date of Delisting: 2023-01-20<p></body>"
    assert parse_form25_html(broken).effective_on == date(2023, 1, 20)


def test_encoding_variation_is_handled() -> None:
    doc = (
        b'<html><head><meta charset="iso-8859-1"></head><body>'
        b"<p>Effective Date of Delisting: 2023-01-20</p>"
        b"<p>Issuer: Caf\xe9 Corp</p></body></html>"
    )
    result = parse_form25_html(doc)
    assert result.effective_on == date(2023, 1, 20)


def test_plain_text_documents_are_recognised() -> None:
    text = b"FORM 25\nEffective Date of Delisting: 2023-01-20\n"
    result = parse_form25_html(text)
    assert result.content_kind == "text"
    assert result.effective_on == date(2023, 1, 20)


def test_table_cells_do_not_merge_across_columns() -> None:
    """A label in one cell must not absorb an unrelated neighbouring value."""
    text, kind, _ = html_to_text(TABLE_DOC)
    assert kind == "html"
    assert "|" in text or "\n" in text


def test_unmatched_relevant_phrases_guide_the_next_iteration() -> None:
    text, _, _ = html_to_text(PROSE_ONLY)
    phrases = unmatched_relevant_phrases(text, labels_seen=())
    assert "delist" in phrases


def test_matched_labels_suppress_the_phrase_hint() -> None:
    text, _, _ = html_to_text(TABLE_DOC)
    assert unmatched_relevant_phrases(text, labels_seen=("date of delisting",)) == ()


# -- aggregate diagnostics -----------------------------------------------------


def test_html_summary_reports_the_population() -> None:
    summary = html_summary(
        [parse_form25_html(TABLE_DOC), parse_form25_html(PROSE_ONLY), parse_form25_html(AMBIGUOUS)]
    )
    assert summary["documents"] == 3
    assert summary["effective_date_matches"] == 1
    assert summary["ambiguous_documents"] == 1
    reasons = summary["failures_by_reason"]
    assert isinstance(reasons, dict)
    assert reasons.get("ambiguous_dates") == 1


# -- integration with the Form 25 document pipeline ----------------------------


def test_the_document_pipeline_uses_the_html_parser() -> None:
    document = parse_form25_document(TABLE_DOC, filing=_filing())
    assert document.content_kind == "html"
    assert document.effective_on == date(2023, 1, 20)
    assert document.document_checksum.startswith("sha256:")
    fields = {p.field_name for p in document.provenance}
    assert "effective_on" in fields


def test_coverage_counts_html_documents_correctly() -> None:
    """content_kind is recorded, not inferred from whether elements were seen."""
    documents = [
        parse_form25_document(TABLE_DOC, filing=_filing()),
        parse_form25_document(PROSE_ONLY, filing=_filing()),
    ]
    coverage = summarize_form25(documents)
    assert coverage.documents == 2
    assert coverage.non_xml_documents == 2, "both are HTML"
    assert coverage.xml_documents == 0
    assert coverage.with_effective_date == 1
    assert not coverage.complete


def test_an_ambiguous_document_never_reports_an_effective_date() -> None:
    document = parse_form25_document(AMBIGUOUS, filing=_filing())
    assert document.effective_on is None
    assert any("failing closed" in finding for finding in document.findings)
