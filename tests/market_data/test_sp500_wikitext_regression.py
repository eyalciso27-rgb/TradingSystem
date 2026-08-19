"""The owner-host defect, reproduced and fixed.

The empirical failure: a healthy endpoint, a correct validator, and an empty
snapshot. `refresh --force` reported

    refresh rejected, last-known-good retained: snapshot is empty
    status -> CURRENT_LIST_UNAVAILABLE

because the live page writes ticker cells as exchange templates —
``{{NasdaqSymbol|AAPL}}``, ``{{NyseSymbol|MMM}}``, ``{{NyseSymbol|BRK.B}}``,
``{{BZX link|CBOE}}`` — while the parser assumed a bare ``| AAPL``. Splitting a
row on ``|`` cut every template in half, both halves failed ticker validation,
all 500-odd rows were skipped, and the validator correctly refused the empty
result. Nothing was broken except the parser's idea of the syntax.

These tests run against a fixture recorded from the real API shape, offline.
``test_the_old_parser_assumption_would_have_produced_nothing`` reproduces the
defect itself, so the fix cannot silently rot back.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.sp500_current_universe import (
    MAX_PLAUSIBLE_CONSTITUENTS,
    MIN_PLAUSIBLE_CONSTITUENTS,
    ConstituentSnapshot,
    UniverseStore,
    diagnostic_notes,
    parse_html_constituents,
    parse_wikitext_constituents,
    refresh_universe,
    validate_snapshot,
)
from trading_market_data_core.sp500_wikitable import (
    MAX_DIAGNOSTIC_SAMPLES,
    ParseDiagnostics,
    TableParseError,
    extract_ticker,
    parse_wikitext_table,
)
from trading_market_data_core.trading_readiness import DEFAULT_FRESHNESS, TradingMode

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sp500"
WIKITEXT_FIXTURE = FIXTURES / "wikipedia_constituents.wikitext.json"
HTML_FIXTURE = FIXTURES / "wikipedia_constituents.html.json"

AS_OF = date(2026, 8, 19)
FETCHED_AT = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

#: A table with a header and no rows: structurally valid, empirically useless.
HEADER_ONLY_TABLE = (
    '{| class="wikitable"' + chr(10) + "|-" + chr(10)
    + "! Symbol !! Security" + chr(10) + "|}"
)


def _snapshot() -> ConstituentSnapshot:
    return parse_wikitext_constituents(
        WIKITEXT_FIXTURE.read_bytes(), as_of=AS_OF, fetched_at=FETCHED_AT
    )


def _wikitext() -> str:
    payload = json.loads(WIKITEXT_FIXTURE.read_text(encoding="utf-8"))
    return str(payload["parse"]["wikitext"])


# == the defect itself ========================================================


def test_the_old_parser_assumption_would_have_produced_nothing() -> None:
    """Reproduce the empirical failure against the real syntax.

    This is the previous implementation's ticker step, verbatim in substance:
    split the row on newline-pipe, take the last piece after a ``|``, require a
    canonical ticker. Against template cells it yields nothing, which is exactly
    what the owner saw.
    """
    wikitext = _wikitext()
    recovered = 0
    for block in wikitext.split("|-"):
        cells = [cell.strip() for cell in re.split(r"\n\s*\|", block) if cell.strip()]
        cells = [re.sub(r"^\[\[|\]\]$", "", cell).split("|")[-1].strip() for cell in cells]
        if len(cells) < 2:
            continue
        if re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", cells[0].upper()):
            recovered += 1

    assert recovered < MIN_PLAUSIBLE_CONSTITUENTS, (
        "the old assumption must be shown to fail on the real syntax;"
        f" it recovered {recovered} rows"
    )


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("{{NasdaqSymbol|AAPL}}", "AAPL"),
        ("{{NyseSymbol|MMM}}", "MMM"),
        ("{{NyseSymbol|BRK.B}}", "BRK.B"),
        ("{{NyseSymbol|BF.B}}", "BF.B"),
        ("{{BZX link|CBOE}}", "CBOE"),
        # forms that must keep working
        ("AAPL", "AAPL"),
        ("[[Nasdaq|MSFT]]", "MSFT"),
        ("{{NasdaqSymbol|TSLA}} <!-- a comment -->", "TSLA"),
        ("{{nowrap|{{NyseSymbol|JPM}}}}", "JPM"),
        ("{{NyseSymbol|link=no|CAT}}", "CAT"),
        ('style="text-align:center" | {{NyseSymbol|GE}}', "GE"),
        ("{{NyseSymbol|V}}<ref name=x/>", "V"),
    ],
)
def test_ticker_templates_are_read_generically(cell: str, expected: str) -> None:
    """Not a lookup table of template names: the structure is parsed."""
    assert extract_ticker(cell) == expected


def test_a_template_name_is_never_mistaken_for_the_ticker() -> None:
    """``{{NYSE|MMM}}`` is MMM. Taking the name would look plausible and be wrong."""
    assert extract_ticker("{{NYSE|MMM}}") == "MMM"
    assert extract_ticker("{{BZX link|CBOE}}") == "CBOE"


def test_a_cell_with_no_ticker_yields_none_rather_than_a_guess() -> None:
    assert extract_ticker("") is None
    assert extract_ticker("{{citation needed}}") is None
    assert extract_ticker("[[Some Company Inc.]]") is None


# == the fix, end to end ======================================================


def test_the_real_syntax_now_parses_to_a_plausible_index() -> None:
    snapshot = _snapshot()
    assert snapshot.count >= MIN_PLAUSIBLE_CONSTITUENTS
    assert snapshot.count <= MAX_PLAUSIBLE_CONSTITUENTS


def test_the_count_is_not_pinned_to_exactly_500() -> None:
    """Share classes mean securities outnumber companies; only the range is fixed."""
    snapshot = _snapshot()
    tickers = {c.ticker for c in snapshot.constituents}
    assert {"GOOGL", "GOOG"} <= tickers, "both Alphabet classes are constituents"
    assert {"NWS", "NWSA"} <= tickers

    companies = {c.cik for c in snapshot.constituents if c.cik}
    securities = {c.ticker for c in snapshot.constituents if c.cik}
    assert len(securities) > len(companies), "more securities than companies"
    # The exact row count is deliberately NOT asserted: only the plausible range
    # is a safety property, and the index really does move.
    assert MIN_PLAUSIBLE_CONSTITUENTS <= snapshot.count <= MAX_PLAUSIBLE_CONSTITUENTS


def test_apple_parses_with_the_right_identity_gics_and_date() -> None:
    """The single row the owner can eyeball on the next run."""
    apple = next(c for c in _snapshot().constituents if c.ticker == "AAPL")

    assert apple.name == "Apple Inc."
    assert apple.cik == "0000320193"
    assert apple.security_id == "sec-cik:0000320193"
    assert apple.identity_resolved is True
    assert apple.gics_sector == "Information Technology"
    assert apple.gics_sub_industry == "Technology Hardware, Storage & Peripherals"
    assert apple.date_added == date(1982, 11, 30)


def test_the_parsed_snapshot_validates_and_becomes_last_known_good(
    tmp_path: Path,
) -> None:
    """The whole point: this list is now fit to store."""
    snapshot = _snapshot()
    assert validate_snapshot(snapshot) == ()

    store = UniverseStore(tmp_path)
    path, findings = store.accept(snapshot)
    assert path is not None, findings
    assert store.latest() is not None
    assert store.latest().count == snapshot.count  # type: ignore[union-attr]


# == the columns must stay bound to the columns ===============================


def test_fields_come_from_the_named_columns_not_from_pattern_hunting() -> None:
    """Headquarters must never become Date added, or CIK.

    A parser that scans every cell for "something date-shaped" happens to work
    until a column moves. These rows are bound by the table's own header row.
    """
    rows, diagnostics = parse_wikitext_table(_wikitext())
    assert diagnostics.headers[:8] == (
        "Symbol", "Security", "GICS Sector", "GICS Sub-Industry",
        "Headquarters Location", "Date added", "CIK", "Founded",
    )
    assert diagnostics.schema[:8] == (
        "ticker", "name", "gics_sector", "gics_sub_industry",
        "headquarters", "date_added", "cik", "founded",
    )

    mmm = next(row for row in rows if row.ticker == "MMM")
    assert mmm.headquarters == "St. Paul, Minnesota"
    assert mmm.date_added == "1957-03-04"
    assert mmm.cik == "0000066740"
    assert mmm.founded == "1902"
    assert mmm.gics_sector == "Industrials"
    assert mmm.gics_sub_industry == "Industrial Conglomerates"


def test_a_table_whose_schema_cannot_be_identified_is_refused() -> None:
    """Refusing beats guessing: a moved schema must fail loudly, not silently."""
    with pytest.raises(TableParseError, match="identifiable constituent schema"):
        parse_wikitext_table(
            '{| class="wikitable"\n|-\n! Alpha !! Beta\n|-\n| x || y\n|}'
        )


def test_empty_cells_do_not_shift_the_following_columns() -> None:
    """The row with no CIK keeps every later column in place."""
    rows, _ = parse_wikitext_table(_wikitext())
    unresolved = next(row for row in rows if row.ticker == "ZZQQ")
    assert unresolved.date_added is None
    assert unresolved.cik is None
    assert unresolved.founded == "1998", "Founded must not slide into the empty CIK"
    assert unresolved.headquarters == "Denver, Colorado"


def test_cell_attributes_are_not_mistaken_for_content() -> None:
    rows, _ = parse_wikitext_table(_wikitext())
    boeing = next(row for row in rows if row.ticker == "BA")
    assert boeing.founded == "1916"


def test_a_citation_in_a_cell_is_not_data() -> None:
    rows, _ = parse_wikitext_table(_wikitext())
    brown_forman = next(row for row in rows if row.ticker == "BF.B")
    assert brown_forman.date_added == "1982-10-31"


def test_the_changes_table_further_down_the_page_is_not_the_index() -> None:
    """The page carries a second table; only the constituent schema matches."""
    rows, _ = parse_wikitext_table(_wikitext())
    assert not any(row.ticker in {"QQQX", "QQQY"} for row in rows)


# == identity ================================================================


def test_a_cik_gives_a_stable_sec_identity() -> None:
    for constituent in _snapshot().constituents:
        if constituent.cik:
            assert constituent.security_id == f"sec-cik:{constituent.cik}"
            assert constituent.identity_resolved is True


def test_no_cik_stays_unresolved_and_never_promotes_the_ticker() -> None:
    unresolved = next(c for c in _snapshot().constituents if c.ticker == "ZZQQ")
    assert unresolved.cik is None
    assert unresolved.identity_resolved is False
    assert unresolved.security_id == "unresolved-ticker:ZZQQ"
    assert not unresolved.security_id.startswith("sec-cik:")


def test_share_classes_of_one_company_share_a_cik_but_not_a_ticker() -> None:
    snapshot = _snapshot()
    classes = [c for c in snapshot.constituents if c.ticker in {"GOOGL", "GOOG"}]
    assert len(classes) == 2
    assert {c.cik for c in classes} == {"0001652044"}
    assert validate_snapshot(snapshot) == (), "a shared CIK is not a duplicate ticker"


# == GICS stays optional CURRENT context ======================================


def test_gics_is_parsed_from_its_own_columns() -> None:
    snapshot = _snapshot()
    cboe = next(c for c in snapshot.constituents if c.ticker == "CBOE")
    assert cboe.gics_sector == "Financials"
    assert cboe.gics_sub_industry == "Financial Exchanges & Data"


def test_gics_here_is_free_reconstructed_current_context_only() -> None:
    snapshot = _snapshot()
    assert snapshot.gics_mode == "FREE_RECONSTRUCTED"
    assert snapshot.is_point_in_time_history is False
    assert any("NOT point-in-time history" in note for note in snapshot.notes)


def test_a_missing_sector_does_not_invalidate_the_snapshot() -> None:
    """Sector is optional context; its absence never blocks."""
    rows, diagnostics = parse_wikitext_table(
        _wikitext().replace("|| Utilities || Water Utilities ||", "||  ||  ||", 1)
    )
    assert diagnostics.accepted_rows == len(rows)
    stripped = next(row for row in rows if row.ticker == "ZZQQ")
    assert stripped.gics_sector is None


# == diagnostics ==============================================================


def test_a_healthy_parse_records_what_produced_it() -> None:
    notes = diagnostic_notes(_snapshot())
    joined = "\n".join(notes)
    assert "representation=wikitext" in joined
    assert "table_found=True" in joined
    assert "accepted_rows=503" in joined
    assert "candidate_rows=503" in joined
    assert "skipped_rows=0" in joined


def test_an_implausible_parse_reports_why_not_just_that_it_failed() -> None:
    """The owner's run said only "snapshot is empty". That is now diagnosable."""
    broken = _wikitext().replace("{{NyseSymbol|", "{{UnknownWrapper|((").replace(
        "{{NasdaqSymbol|", "{{UnknownWrapper|((").replace("{{BZX link|", "{{UnknownWrapper|((")
    rows, diagnostics = parse_wikitext_table(broken)

    assert diagnostics.table_found is True
    assert diagnostics.candidate_rows > MIN_PLAUSIBLE_CONSTITUENTS
    assert diagnostics.accepted_rows < MIN_PLAUSIBLE_CONSTITUENTS
    assert diagnostics.skipped_rows > 0
    assert diagnostics.skipped_samples, "the unreadable cells must be quoted back"
    assert any("UnknownWrapper" in sample for sample in diagnostics.skipped_samples)
    assert len(rows) == diagnostics.accepted_rows


def test_diagnostics_are_bounded_and_never_dump_the_payload() -> None:
    broken = _wikitext().replace("{{NyseSymbol|", "{{X|((")
    _, diagnostics = parse_wikitext_table(broken)

    assert len(diagnostics.skipped_samples) <= MAX_DIAGNOSTIC_SAMPLES
    for sample in diagnostics.skipped_samples:
        assert len(sample) <= 100
        assert "\n" not in sample
    rendered = "\n".join(diagnostics.lines())
    assert len(rendered) < 2000, "diagnostics must stay operator-readable"


def test_a_rejected_refresh_surfaces_the_diagnostics(tmp_path: Path) -> None:
    """End to end: the owner's next failed run says what the parser saw."""
    empty = parse_wikitext_constituents(
        json.dumps(
            {
                "parse": {
                    "wikitext": (
                        '{| class="wikitable"\n|-\n! Symbol !! Security\n|-\n'
                        "| {{UnknownWrapper|((AAPL))}} || [[Apple Inc.]]\n|}"
                    )
                }
            }
        ).encode("utf-8"),
        as_of=AS_OF,
        fetched_at=FETCHED_AT,
    )
    _, _, findings = refresh_universe(
        UniverseStore(tmp_path), empty, now=FETCHED_AT,
        policy=DEFAULT_FRESHNESS[TradingMode.PAPER],
    )

    joined = "\n".join(findings)
    assert "snapshot is empty" in joined
    assert "diagnostic: table_found=True" in joined
    assert "diagnostic: candidate_rows=1" in joined
    assert "UnknownWrapper" in joined


def test_diagnostics_report_headers_that_are_not_recognised() -> None:
    _, diagnostics = parse_wikitext_table(
        '{| class="wikitable"\n|-\n! Symbol !! Security !! Some New Column\n'
        "|-\n| {{NyseSymbol|MMM}} || [[3M]] || whatever\n|}"
    )
    assert diagnostics.unmapped_headers == ("Some New Column",)
    assert "unrecognised_headers=['Some New Column']" in "\n".join(diagnostics.lines())


# == the secondary representation of the SAME page ============================


def test_the_rendered_html_of_the_same_page_parses_equivalently() -> None:
    """MediaWiki has already expanded the templates in this representation."""
    snapshot = parse_html_constituents(
        HTML_FIXTURE.read_bytes(), as_of=AS_OF, fetched_at=FETCHED_AT
    )
    assert validate_snapshot(snapshot) == ()
    assert MIN_PLAUSIBLE_CONSTITUENTS <= snapshot.count <= MAX_PLAUSIBLE_CONSTITUENTS

    apple = next(c for c in snapshot.constituents if c.ticker == "AAPL")
    assert apple.cik == "0000320193"
    assert apple.security_id == "sec-cik:0000320193"
    assert apple.gics_sector == "Information Technology"


def test_the_fallback_keeps_the_same_provenance_and_meaning() -> None:
    """A second view of one page is not a second source."""
    wikitext_snapshot = _snapshot()
    html_snapshot = parse_html_constituents(
        HTML_FIXTURE.read_bytes(), as_of=AS_OF, fetched_at=FETCHED_AT
    )

    assert html_snapshot.source_id == wikitext_snapshot.source_id
    assert html_snapshot.source_authority == wikitext_snapshot.source_authority
    assert html_snapshot.is_point_in_time_history is False
    assert html_snapshot.gics_mode == wikitext_snapshot.gics_mode
    assert "representation=html" in "\n".join(diagnostic_notes(html_snapshot))


def test_the_html_path_also_keeps_unresolved_identity_unresolved() -> None:
    snapshot = parse_html_constituents(
        HTML_FIXTURE.read_bytes(), as_of=AS_OF, fetched_at=FETCHED_AT
    )
    unresolved = next(c for c in snapshot.constituents if c.ticker == "ZZQQ")
    assert unresolved.identity_resolved is False
    assert unresolved.security_id == "unresolved-ticker:ZZQQ"


def test_the_fallback_is_only_reached_when_the_primary_is_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy wikitext parse must not cost a second request."""
    from trading_market_data_core import sp500_current_universe as module

    requested: list[str] = []

    def fake_fetch(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
        requested.append(url)
        return WIKITEXT_FIXTURE.read_bytes()

    monkeypatch.setattr(module, "_fetch_json", fake_fetch)
    snapshot = module.fetch_current_constituents(as_of=AS_OF, now=FETCHED_AT)

    assert len(requested) == 1, "the primary representation sufficed"
    assert validate_snapshot(snapshot) == ()
    assert any("representation=wikitext" in n for n in diagnostic_notes(snapshot))


def test_the_same_page_rendered_html_rescues_an_unusable_wikitext_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly the owner's failure mode, with the fallback in place."""
    from trading_market_data_core import sp500_current_universe as module

    unusable = json.dumps({"parse": {"wikitext": HEADER_ONLY_TABLE}}).encode("utf-8")
    requested: list[str] = []

    def fake_fetch(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
        requested.append(url)
        return unusable if "prop=wikitext" in url else HTML_FIXTURE.read_bytes()

    monkeypatch.setattr(module, "_fetch_json", fake_fetch)
    snapshot = module.fetch_current_constituents(as_of=AS_OF, now=FETCHED_AT)

    assert len(requested) == 2
    assert "prop=text" in requested[1], "the fallback is the SAME page, another view"
    assert validate_snapshot(snapshot) == ()
    assert any("representation=html" in n for n in diagnostic_notes(snapshot))
    assert snapshot.source_id == module.FREE_SOURCE_ID, "provenance is unchanged"


def test_a_failing_fallback_never_hides_what_the_primary_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_market_data_core import sp500_current_universe as module
    from trading_market_data_core.dataset_v1_sources import SourceAdapterError

    unusable = json.dumps({"parse": {"wikitext": HEADER_ONLY_TABLE}}).encode("utf-8")

    def fake_fetch(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
        if "prop=wikitext" in url:
            return unusable
        raise SourceAdapterError("fallback unreachable")

    monkeypatch.setattr(module, "_fetch_json", fake_fetch)
    snapshot = module.fetch_current_constituents(as_of=AS_OF, now=FETCHED_AT)

    assert snapshot.count == 0
    assert any("representation=wikitext" in n for n in diagnostic_notes(snapshot))
    assert validate_snapshot(snapshot), "still refused, and still diagnosable"


# == last-known-good safety is untouched ======================================


def test_an_empty_parse_is_still_refused(tmp_path: Path) -> None:
    store = UniverseStore(tmp_path)
    good = _snapshot()
    store.accept(good)

    empty = parse_wikitext_constituents(
        json.dumps(
            {"parse": {"wikitext": '{| class="wikitable"\n|-\n! Symbol !! Security\n|}'}}
        ).encode("utf-8"),
        as_of=date(2026, 9, 19),
        fetched_at=datetime(2026, 9, 19, tzinfo=UTC),
    )
    current, _, findings = refresh_universe(
        store, empty, now=datetime(2026, 9, 19, tzinfo=UTC),
        policy=DEFAULT_FRESHNESS[TradingMode.PAPER],
    )

    assert current is not None
    assert current.count == good.count, "the good list must survive a bad refresh"
    assert any("snapshot is empty" in finding for finding in findings)


def test_a_stored_list_with_an_unresolved_constituent_can_be_read_back(
    tmp_path: Path,
) -> None:
    """A second defect on the same path, found while fixing the first.

    ``identity_resolved`` is part of the checksummed body, and the loader used
    to drop it — so any list containing a CIK-less constituent failed its own
    integrity check on the way back in, and ``latest()`` skipped it. The
    last-known-good list would have silently ceased to exist the moment a real
    row arrived without a CIK. Never observed before now only because the parse
    had never produced a row at all.
    """
    store = UniverseStore(tmp_path)
    snapshot = _snapshot()
    assert any(not c.identity_resolved for c in snapshot.constituents)

    path, findings = store.accept(snapshot)
    assert path is not None, findings

    reloaded = store.latest()
    assert reloaded is not None, "a stored last-known-good list must be readable"
    assert reloaded.checksum == snapshot.checksum
    unresolved = next(c for c in reloaded.constituents if c.ticker == "ZZQQ")
    assert unresolved.identity_resolved is False
    assert unresolved.identity_kind == "ticker_operational_unresolved"


def test_a_payload_without_a_table_is_refused() -> None:
    from trading_market_data_core.dataset_v1_sources import SourceAdapterError

    with pytest.raises(SourceAdapterError, match="no wikitext table"):
        parse_wikitext_constituents(
            b'{"parse": {"wikitext": "just prose, no table"}}',
            as_of=AS_OF, fetched_at=FETCHED_AT,
        )


def test_diagnostics_are_a_plain_value_safe_to_print() -> None:
    diagnostics = ParseDiagnostics(representation="wikitext", table_found=False)
    assert diagnostics.lines()[0] == "representation=wikitext"
    assert diagnostics.accepted_rows == 0
