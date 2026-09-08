"""Deterministic, offline proofs for the SEC EDGAR Sector-SPDR GICS candidate.

Task A-WP2-GICS-SECTOR-SECONDARY-PIT-EVAL. These tests prove the
machinery in ``dataset_v1_sec_sector_spdr_gics`` does what its module
docstring claims -- deterministic identity resolution, no-look-ahead filing
selection, fail-closed on a partial fund set or a later filing, and correct
composition with ``dataset_v1_wikipedia_pit.cross_check_sectors`` for BOTH a
sufficient-evidence (admitted) and an insufficient-evidence (refused)
outcome. No network call is made anywhere in this file; the one real,
recorded experiment lives under
``docs/poi/evidence/dataset-v1/sec-sector-spdr-gics/``,
produced by ``scripts/run_sec_sector_spdr_gics_crosscheck_experiment.py``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_sec_sector_spdr_gics import (
    GICS_SECTOR_NAMES,
    SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR,
    SectorSpdrFilingFetch,
    SectorSpdrFilingRef,
    SectorSpdrHolding,
    SectorSpdrSnapshot,
    build_secondary_sector_mapping,
    discover_nport_candidate_accessions,
    fetch_nport_filing,
    normalize_company_name,
    parse_nport_primary_doc,
    select_sector_spdr_snapshot_as_of,
)
from trading_market_data_core.dataset_v1_sources import SecEdgarClient, SourceAdapterError
from trading_market_data_core.dataset_v1_wikipedia_pit import (
    MINIMUM_SECTOR_AGREEMENT_RATIO,
    MINIMUM_SECTOR_COVERAGE_RATIO,
    WikipediaConstituentObservation,
    WikipediaRevisionRef,
    WikipediaRevisionSnapshot,
    cross_check_sectors,
)
from trading_market_data_core.sp500_current_universe import (
    MAX_PLAUSIBLE_CONSTITUENTS,
    MIN_PLAUSIBLE_CONSTITUENTS,
    raw_artifact_checksum,
)

UA = "TradingSystem Research owner@example.com"
CIK = "0001064641"


# -- fund-keyword sanity -------------------------------------------------------


def test_fund_keyword_table_covers_exactly_the_11_gics_sectors() -> None:
    assert len(SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR) == 11
    assert set(SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR.values()) == GICS_SECTOR_NAMES


# -- normalize_company_name: deterministic, generic, never per-company --------


@pytest.mark.parametrize(
    ("sec_name", "wiki_name"),
    [
        ("WW Grainger Inc", "W. W. Grainger"),
        ("CDW Corp/DE", "CDW"),
        ("TJX Cos Inc/The", "TJX Companies"),
        ("Amazon.com Inc", "Amazon"),
        ("Becton Dickinson & Co", "Becton Dickinson and Co"),
    ],
)
def test_normalize_company_name_reconciles_generic_formatting_differences(
    sec_name: str, wiki_name: str
) -> None:
    assert normalize_company_name(sec_name) == normalize_company_name(wiki_name)


def test_normalize_company_name_does_not_bridge_legal_to_brand_names() -> None:
    """A real, honest limitation: this is NOT solved by generic normalization,
    and this module never fabricates a per-company alias to bridge it."""
    assert normalize_company_name("Deere & Co") != normalize_company_name("John Deere")
    assert normalize_company_name("International Business Machines Corp") != normalize_company_name(
        "IBM"
    )


def test_normalize_company_name_drops_parenthetical_share_class_annotations() -> None:
    assert normalize_company_name("Alphabet Inc (Class A)") == normalize_company_name(
        "Alphabet Inc"
    )




def test_normalize_company_name_keeps_interior_identity_words() -> None:
    assert (
        normalize_company_name("American International Group Inc")
        == "AMERICANINTERNATIONALGROUP"
    )
    assert normalize_company_name("American Express Co") == "AMERICANEXPRESS"

def test_normalize_company_name_fails_closed_on_empty_input() -> None:
    with pytest.raises(SourceAdapterError, match="empty"):
        normalize_company_name("   ")


def test_normalize_company_name_fails_closed_when_nothing_survives() -> None:
    with pytest.raises(SourceAdapterError, match="normalized to nothing"):
        normalize_company_name("Inc")


# -- parse_nport_primary_doc: pure XML parsing, no network --------------------

_NPORT_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/nport">
  <formData>
    <genInfo>
      <seriesId>{series_id}</seriesId>
      <seriesName>{series_name}</seriesName>
      <repPdDate>{rep_pd_date}</repPdDate>
    </genInfo>
    <invstOrSecs>
      <invstOrSec>
        <name>Example Common Stock Inc</name>
        <assetCat>EC</assetCat>
      </invstOrSec>
      <invstOrSec>
        <name>State Street Global Advisors</name>
        <assetCat>STIV</assetCat>
      </invstOrSec>
      <invstOrSec>
        <name></name>
        <assetCat>EC</assetCat>
      </invstOrSec>
    </invstOrSecs>
  </formData>
</edgarSubmission>
"""


def _nport_xml(
    *,
    series_id: str = "S000006415",
    series_name: str = "The Technology Select Sector SPDR Fund",
    rep_pd_date: str = "2023-12-31",
) -> bytes:
    return _NPORT_XML_TEMPLATE.format(
        series_id=series_id, series_name=series_name, rep_pd_date=rep_pd_date
    ).encode("utf-8")


def test_parse_nport_primary_doc_extracts_filing_and_ec_only_holdings() -> None:
    filing, holdings = parse_nport_primary_doc(
        _nport_xml(), cik=CIK, accession_number="0001752724-24-043154"
    )
    assert filing.series_id == "S000006415"
    assert filing.gics_sector == "Information Technology"
    assert filing.report_period_date == date(2023, 12, 31)
    # the STIV cash-sweep line and the empty-name line are both excluded
    assert len(holdings) == 1
    assert holdings[0].issuer_name == "Example Common Stock Inc"
    assert holdings[0].asset_category == "EC"


def test_parse_nport_primary_doc_fails_closed_on_missing_fields() -> None:
    bad = _NPORT_XML_TEMPLATE.format(
        series_id="", series_name="The Technology Select Sector SPDR Fund", rep_pd_date="2023-12-31"
    ).encode("utf-8")
    with pytest.raises(SourceAdapterError, match="missing"):
        parse_nport_primary_doc(bad, cik=CIK, accession_number="acc-1")


def test_parse_nport_primary_doc_fails_closed_on_unparseable_date() -> None:
    with pytest.raises(SourceAdapterError, match="unparseable repPdDate"):
        parse_nport_primary_doc(
            _nport_xml(rep_pd_date="not-a-date"), cik=CIK, accession_number="acc-1"
        )


def test_parse_nport_primary_doc_fails_closed_on_unrecognised_series() -> None:
    with pytest.raises(SourceAdapterError, match="does not match exactly one"):
        parse_nport_primary_doc(
            _nport_xml(series_name="Some Unrelated Fund"), cik=CIK, accession_number="acc-1"
        )


def test_parse_nport_primary_doc_fails_closed_on_malformed_xml() -> None:
    with pytest.raises(SourceAdapterError, match="unparseable N-PORT XML"):
        parse_nport_primary_doc(b"<not valid", cik=CIK, accession_number="acc-1")


# -- SectorSpdrFilingFetch / SectorSpdrSnapshot: fail-closed construction -----


def _fetch(
    *,
    gics_sector: str,
    series_id: str,
    holdings: tuple[SectorSpdrHolding, ...] = (),
    report_period_date: date = date(2023, 12, 31),
    accession_number: str | None = None,
) -> SectorSpdrFilingFetch:
    raw = f"fixture-{series_id}".encode()
    filing = SectorSpdrFilingRef(
        cik=CIK,
        accession_number=accession_number or f"acc-{series_id}",
        series_id=series_id,
        series_name=f"The {gics_sector} Select Sector SPDR Fund",
        gics_sector=gics_sector,
        report_period_date=report_period_date,
    )
    return SectorSpdrFilingFetch(
        filing=filing, holdings=holdings, raw_bytes=raw, raw_checksum=raw_artifact_checksum(raw)
    )


def test_filing_fetch_rejects_a_raw_checksum_mismatch() -> None:
    filing = SectorSpdrFilingRef(
        cik=CIK,
        accession_number="acc-1",
        series_id="S1",
        series_name="X",
        gics_sector="Energy",
        report_period_date=date(2023, 12, 31),
    )
    with pytest.raises(SourceAdapterError, match="raw_checksum"):
        SectorSpdrFilingFetch(
            filing=filing,
            holdings=(),
            raw_bytes=b"real bytes",
            raw_checksum="sha256:" + "a" * 64,
        )


def _all_11_fetches(as_of: date = date(2023, 12, 31)) -> tuple[SectorSpdrFilingFetch, ...]:
    return tuple(
        _fetch(gics_sector=sector, series_id=f"S{i}", report_period_date=as_of)
        for i, sector in enumerate(sorted(GICS_SECTOR_NAMES))
    )


def test_snapshot_accepts_a_complete_11_fund_set() -> None:
    snapshot = SectorSpdrSnapshot(as_of=date(2023, 12, 31), filings=_all_11_fetches())
    assert len(snapshot.filings) == 11


def test_snapshot_fails_closed_on_a_partial_fund_set() -> None:
    incomplete = _all_11_fetches()[:10]
    with pytest.raises(SourceAdapterError, match="missing"):
        SectorSpdrSnapshot(as_of=date(2023, 12, 31), filings=incomplete)


def test_snapshot_fails_closed_on_a_later_filing_no_look_ahead() -> None:
    fetches = list(_all_11_fetches())
    fetches[0] = _fetch(
        gics_sector=sorted(GICS_SECTOR_NAMES)[0],
        series_id="S0",
        report_period_date=date(2024, 1, 31),  # after as_of
    )
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        SectorSpdrSnapshot(as_of=date(2023, 12, 31), filings=tuple(fetches))


# -- select_sector_spdr_snapshot_as_of: no-look-ahead discovery, injected ----


def test_select_sector_spdr_snapshot_as_of_picks_the_latest_filing_at_or_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two candidate periods per fund; the later-than-as_of one must never win."""
    sectors = sorted(GICS_SECTOR_NAMES)

    def fake_discover(*, cik: str, client: SecEdgarClient) -> tuple[tuple[str, date], ...]:
        del cik, client
        # newest filing date first, mirrors the real discovery ordering
        accessions: list[tuple[str, date]] = []
        for i, _sector in enumerate(sectors):
            accessions.append((f"late-{i}", date(2024, 2, 27)))
            accessions.append((f"early-{i}", date(2023, 11, 1)))
        return tuple(accessions)

    def fake_fetch(
        accession_number: str, *, cik: str, client: SecEdgarClient, bytes_fetcher: object
    ) -> SectorSpdrFilingFetch:
        del cik, client, bytes_fetcher
        kind, index_str = accession_number.split("-")
        index = int(index_str)
        report_period_date = date(2023, 12, 31) if kind == "late" else date(2023, 9, 30)
        return _fetch(
            gics_sector=sectors[index],
            series_id=f"S{index}",
            report_period_date=report_period_date,
            accession_number=accession_number,
        )

    snapshot = select_sector_spdr_snapshot_as_of(
        date(2023, 12, 31),
        client=object(),  # type: ignore[arg-type]
        bytes_fetcher=object(),  # type: ignore[arg-type]
        discover_fn=fake_discover,
        fetch_fn=fake_fetch,
    )
    assert len(snapshot.filings) == 11
    reported_periods = {fetch.filing.report_period_date for fetch in snapshot.filings}
    # the "late" filings report 2023-12-31, exactly at_or_before as_of -- selected
    assert reported_periods == {date(2023, 12, 31)}


def test_select_sector_spdr_snapshot_as_of_skips_a_filing_after_as_of() -> None:
    sectors = sorted(GICS_SECTOR_NAMES)

    def fake_discover(*, cik: str, client: SecEdgarClient) -> tuple[tuple[str, date], ...]:
        del cik, client
        accessions: list[tuple[str, date]] = []
        for i, _sector in enumerate(sectors):
            accessions.append((f"toolate-{i}", date(2024, 2, 27)))
            accessions.append((f"ok-{i}", date(2023, 11, 1)))
        return tuple(accessions)

    def fake_fetch(
        accession_number: str, *, cik: str, client: SecEdgarClient, bytes_fetcher: object
    ) -> SectorSpdrFilingFetch:
        del cik, client, bytes_fetcher
        kind, index_str = accession_number.split("-")
        index = int(index_str)
        # the "toolate" filing itself reports a period AFTER as_of
        report_period_date = date(2024, 1, 31) if kind == "toolate" else date(2023, 9, 30)
        return _fetch(
            gics_sector=sectors[index],
            series_id=f"S{index}",
            report_period_date=report_period_date,
            accession_number=accession_number,
        )

    snapshot = select_sector_spdr_snapshot_as_of(
        date(2023, 12, 31),
        client=object(),  # type: ignore[arg-type]
        bytes_fetcher=object(),  # type: ignore[arg-type]
        discover_fn=fake_discover,
        fetch_fn=fake_fetch,
    )
    reported_periods = {fetch.filing.report_period_date for fetch in snapshot.filings}
    assert reported_periods == {date(2023, 9, 30)}


def test_select_sector_spdr_snapshot_as_of_fails_closed_when_a_fund_never_resolves() -> None:
    sectors = sorted(GICS_SECTOR_NAMES)

    def fake_discover(*, cik: str, client: SecEdgarClient) -> tuple[tuple[str, date], ...]:
        del cik, client
        # only 10 of 11 funds ever appear as candidates
        return tuple((f"acc-{i}", date(2024, 2, 27)) for i in range(10))

    def fake_fetch(
        accession_number: str, *, cik: str, client: SecEdgarClient, bytes_fetcher: object
    ) -> SectorSpdrFilingFetch:
        del cik, client, bytes_fetcher
        index = int(accession_number.split("-")[1])
        return _fetch(
            gics_sector=sectors[index],
            series_id=f"S{index}",
            report_period_date=date(2023, 12, 31),
            accession_number=accession_number,
        )

    with pytest.raises(SourceAdapterError, match="missing"):
        select_sector_spdr_snapshot_as_of(
            date(2023, 12, 31),
            client=object(),  # type: ignore[arg-type]
            bytes_fetcher=object(),  # type: ignore[arg-type]
            discover_fn=fake_discover,
            fetch_fn=fake_fetch,
        )


# -- discovery / fetch against the existing SecEdgarClient seam --------------


class _RecordedJsonFetcher:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls: list[str] = []

    def get_json(self, url: str, *, headers: Mapping[str, str]) -> tuple[bytes, object]:
        del headers
        self.calls.append(url)
        return b"{}", self._payload


class _RecordedBytesFetcher:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.calls: list[str] = []

    def get_bytes(self, url: str, *, headers: Mapping[str, str]) -> bytes:
        del headers
        self.calls.append(url)
        return self._payload


def _submissions_payload() -> dict[str, object]:
    return {
        "cik": "1064641",
        "filings": {
            "recent": {
                "form": ["NPORT-P", "N-CEN", "NPORT-P"],
                "accessionNumber": [
                    "0001752724-24-043154",
                    "0001752724-24-000001",
                    "0001752724-23-000099",
                ],
                "filingDate": ["2024-02-27", "2024-01-15", "2023-11-27"],
            }
        },
    }


def test_discover_nport_candidate_accessions_filters_form_and_sorts_newest_first() -> None:
    client = SecEdgarClient(_RecordedJsonFetcher(_submissions_payload()), user_agent=UA)
    candidates = discover_nport_candidate_accessions(cik="1064641", client=client)
    assert candidates == (
        ("0001752724-24-043154", date(2024, 2, 27)),
        ("0001752724-23-000099", date(2023, 11, 27)),
    )


def test_fetch_nport_filing_builds_the_correct_archives_url_and_parses() -> None:
    client = SecEdgarClient(_RecordedJsonFetcher(_submissions_payload()), user_agent=UA)
    bytes_fetcher = _RecordedBytesFetcher(_nport_xml())
    fetched = fetch_nport_filing(
        "0001752724-24-043154", cik=CIK, client=client, bytes_fetcher=bytes_fetcher
    )
    assert bytes_fetcher.calls == [
        "https://www.sec.gov/Archives/edgar/data/1064641/000175272424043154/primary_doc.xml"
    ]
    assert fetched.filing.gics_sector == "Information Technology"
    assert fetched.raw_checksum == raw_artifact_checksum(_nport_xml())


# -- build_secondary_sector_mapping: deterministic identity resolution -------


def _holding(name: str) -> SectorSpdrHolding:
    return SectorSpdrHolding(issuer_name=name, asset_category="EC")


def test_build_secondary_sector_mapping_matches_unambiguous_names() -> None:
    snapshot = SectorSpdrSnapshot(
        as_of=date(2023, 12, 31),
        filings=tuple(
            _fetch(
                gics_sector=sector,
                series_id=f"S{i}",
                holdings=(_holding(f"Company {i} Common Inc"),) if sector == "Energy" else (),
            )
            for i, sector in enumerate(sorted(GICS_SECTOR_NAMES))
        ),
    )
    energy_index = sorted(GICS_SECTOR_NAMES).index("Energy")
    resolution = build_secondary_sector_mapping(
        snapshot,
        wikipedia_observations=[("ENRG", f"Company {energy_index} Common Inc")],
    )
    assert resolution.secondary_sectors == {"ENRG": "Energy"}
    assert resolution.unmatched_holdings == ()


def test_build_secondary_sector_mapping_does_not_guess_long_prefix_match() -> None:
    snapshot = SectorSpdrSnapshot(
        as_of=date(2023, 12, 31),
        filings=tuple(
            _fetch(
                gics_sector=sector,
                series_id=f"S{i}",
                holdings=(_holding("Costco"),) if sector == "Consumer Staples" else (),
            )
            for i, sector in enumerate(sorted(GICS_SECTOR_NAMES))
        ),
    )
    resolution = build_secondary_sector_mapping(
        snapshot, wikipedia_observations=[("COST", "Costco Wholesale Corp")]
    )
    assert resolution.secondary_sectors == {}
    assert resolution.unmatched_holdings[0].reason == "NO_MATCH"


def test_build_secondary_sector_mapping_rejects_short_prefix_false_positive() -> None:
    snapshot = SectorSpdrSnapshot(
        as_of=date(2023, 12, 31),
        filings=tuple(
            _fetch(
                gics_sector=sector,
                series_id=f"S{i}",
                holdings=(_holding("Ford"),) if sector == "Consumer Discretionary" else (),
            )
            for i, sector in enumerate(sorted(GICS_SECTOR_NAMES))
        ),
    )
    resolution = build_secondary_sector_mapping(
        snapshot, wikipedia_observations=[("F", "Ford Motor")]
    )
    assert resolution.secondary_sectors == {}
    assert resolution.unmatched_holdings[0].reason == "NO_MATCH"


def test_build_secondary_sector_mapping_excludes_ambiguous_matches() -> None:
    snapshot = SectorSpdrSnapshot(
        as_of=date(2023, 12, 31),
        filings=tuple(
            _fetch(
                gics_sector=sector,
                series_id=f"S{i}",
                holdings=(_holding("Alphabet Inc"),) if sector == "Communication Services" else (),
            )
            for i, sector in enumerate(sorted(GICS_SECTOR_NAMES))
        ),
    )
    resolution = build_secondary_sector_mapping(
        snapshot,
        wikipedia_observations=[("GOOG", "Alphabet Inc"), ("GOOGL", "Alphabet Inc")],
    )
    assert resolution.secondary_sectors == {}
    assert len(resolution.unmatched_holdings) == 1
    unmatched = resolution.unmatched_holdings[0]
    assert unmatched.reason == "AMBIGUOUS_MATCH"
    assert set(unmatched.candidate_tickers) == {"GOOG", "GOOGL"}


def test_build_secondary_sector_mapping_records_a_holding_with_no_wikipedia_match() -> None:
    snapshot = SectorSpdrSnapshot(
        as_of=date(2023, 12, 31),
        filings=tuple(
            _fetch(
                gics_sector=sector,
                series_id=f"S{i}",
                holdings=(_holding("Deere & Co"),) if sector == "Industrials" else (),
            )
            for i, sector in enumerate(sorted(GICS_SECTOR_NAMES))
        ),
    )
    resolution = build_secondary_sector_mapping(
        snapshot, wikipedia_observations=[("DE", "John Deere")]
    )
    assert resolution.secondary_sectors == {}
    assert resolution.unmatched_holdings[0].reason == "NO_MATCH"


# -- end-to-end composition with cross_check_sectors: PASS and FAIL ----------

_ROW_COUNT = 440  # inside [MIN_PLAUSIBLE_CONSTITUENTS, MAX_PLAUSIBLE_CONSTITUENTS]
assert MIN_PLAUSIBLE_CONSTITUENTS <= _ROW_COUNT <= MAX_PLAUSIBLE_CONSTITUENTS


def _ticker_for_index(i: int) -> str:
    letters = []
    n = i
    for _ in range(4):
        n, remainder = divmod(n, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _sector_for_index(i: int) -> str:
    return sorted(GICS_SECTOR_NAMES)[i % 11]


def _wiki_snapshot(*, count: int = _ROW_COUNT) -> WikipediaRevisionSnapshot:
    observations = tuple(
        WikipediaConstituentObservation(
            ticker=_ticker_for_index(i),
            name=f"Company {i} Common Inc",
            gics_sector=_sector_for_index(i),
            gics_sub_industry="Sub-Industry",
        )
        for i in range(count)
    )
    revision = WikipediaRevisionRef(
        page_title="List of S&P 500 companies",
        revision_id=1,
        revision_timestamp=datetime(2023, 12, 31, tzinfo=UTC),
    )
    return WikipediaRevisionSnapshot(
        revision=revision,
        raw_checksum="sha256:" + "a" * 64,
        observations=observations,
    )


def _sector_snapshot_covering(*, fraction: float, agree: bool) -> SectorSpdrSnapshot:
    """One filing per GICS sector, each holding matching some fraction of that
    sector's wiki companies -- deterministic PASS/FAIL fixture builder."""
    filings = []
    for sector_index, sector in enumerate(sorted(GICS_SECTOR_NAMES)):
        company_indices = [i for i in range(_ROW_COUNT) if _sector_for_index(i) == sector]
        take = int(len(company_indices) * fraction)
        claimed_sector = sector if agree else _fund_disagreement_sector(sector)
        holdings = tuple(_holding(f"Company {i} Common Inc") for i in company_indices[:take])
        filings.append(
            _fetch(gics_sector=claimed_sector, series_id=f"S{sector_index}", holdings=holdings)
        )
    # SectorSpdrSnapshot requires each of the 11 GICS sectors represented once;
    # the `claimed_sector` substitution above must still land on all 11.
    return SectorSpdrSnapshot(as_of=date(2023, 12, 31), filings=tuple(filings))


def _fund_disagreement_sector(sector: str) -> str:
    ordered = sorted(GICS_SECTOR_NAMES)
    return ordered[(ordered.index(sector) + 1) % len(ordered)]


def test_end_to_end_sufficient_coverage_and_agreement_is_admitted() -> None:
    wiki = _wiki_snapshot()
    sector_snapshot = _sector_snapshot_covering(fraction=1.0, agree=True)
    resolution = build_secondary_sector_mapping(
        sector_snapshot,
        wikipedia_observations=[(o.ticker, o.name) for o in wiki.observations],
    )
    result = cross_check_sectors(
        wikipedia=wiki,
        secondary_sectors=resolution.secondary_sectors,
        secondary_source_id="sec-edgar-nport-select-sector-spdr",
        as_of=date(2023, 12, 31),
    )
    assert result.corroborated is True
    assert result.coverage_ratio >= MINIMUM_SECTOR_COVERAGE_RATIO
    assert result.agreement_ratio >= MINIMUM_SECTOR_AGREEMENT_RATIO
    assert result.disagreements == ()


def test_end_to_end_insufficient_coverage_fails_closed() -> None:
    """Mirrors the REAL bounded experiment's outcome (2023-12-31, ~0.87
    coverage): identity resolution genuinely cannot reach every ticker, and
    cross_check_sectors must refuse rather than report a partial ratio."""
    wiki = _wiki_snapshot()
    sector_snapshot = _sector_snapshot_covering(fraction=0.5, agree=True)
    resolution = build_secondary_sector_mapping(
        sector_snapshot,
        wikipedia_observations=[(o.ticker, o.name) for o in wiki.observations],
    )
    with pytest.raises(SourceAdapterError, match="coverage"):
        cross_check_sectors(
            wikipedia=wiki,
            secondary_sectors=resolution.secondary_sectors,
            secondary_source_id="sec-edgar-nport-select-sector-spdr",
            as_of=date(2023, 12, 31),
        )


def test_end_to_end_sufficient_coverage_but_disagreement_is_not_corroborated() -> None:
    wiki = _wiki_snapshot()
    sector_snapshot = _sector_snapshot_covering(fraction=1.0, agree=False)
    resolution = build_secondary_sector_mapping(
        sector_snapshot,
        wikipedia_observations=[(o.ticker, o.name) for o in wiki.observations],
    )
    result = cross_check_sectors(
        wikipedia=wiki,
        secondary_sectors=resolution.secondary_sectors,
        secondary_source_id="sec-edgar-nport-select-sector-spdr",
        as_of=date(2023, 12, 31),
    )
    assert result.coverage_ratio >= MINIMUM_SECTOR_COVERAGE_RATIO
    assert result.corroborated is False
    assert len(result.disagreements) > 0


def test_recorded_real_sector_experiment_is_self_authenticating() -> None:
    repo = Path(__file__).resolve().parents[2]
    evidence_path = (
        repo
        / "docs/poi/evidence/dataset-v1/sec-sector-spdr-gics/"
        "experiment-2023-12-31-recheck-v2.json"
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert payload["admission"]["status"] == "NOT_ADMITTED"
    assert payload["independent_source"]["filing_count"] == 11
    result = payload["sector_cross_check"]
    assert result["covered_tickers"] == 417
    assert len(result["uncovered_tickers"]) == 86
    assert len(result["agreements"]) == 417
    assert result["corroborated"] is False

    for filing in payload["independent_source"]["filings"]:
        raw = (repo / filing["raw_artifact_path"]).read_bytes()
        assert "sha256:" + hashlib.sha256(raw).hexdigest() == filing["raw_checksum"]
    wiki = payload["wikipedia_source"]
    wiki_raw = (repo / wiki["raw_artifact_path"]).read_bytes()
    assert "sha256:" + hashlib.sha256(wiki_raw).hexdigest() == wiki["raw_checksum"]

    expected = payload.pop("evidence_checksum")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert expected == "sha256:" + hashlib.sha256(canonical).hexdigest()
