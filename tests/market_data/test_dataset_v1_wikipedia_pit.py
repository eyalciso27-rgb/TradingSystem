"""Deterministic, offline proofs for the Wikipedia-revision SECONDARY PIT path.

ChatGPT review of the A-WP2 canonical-state reconciliation (2026-09-03) found
that ``ASOF_GICS_BLOCKER`` drew a categorical "no free path exists" conclusion
without evaluating one specific, legitimate candidate: MediaWiki's own,
immutable, timestamped revision history. This module proves the machinery
built to close that gap actually does what it claims:

* every observation is bound to a real revision id, a timezone-aware
  timestamp and a checksum recomputed from the actual bytes (never trusted
  from a label) -- fail closed on any of those being absent or wrong;
* a decision date is answered only by the latest revision AT OR BEFORE it,
  never a later one (no look-ahead / no back-projection);
* cross-checking against an independent secondary source is mandatory, and
  every disagreement it finds is recorded rather than silently dropped;
* the evidence produced can never claim to be official, and never flips the
  official ``asof_gics`` blocker's ``BLOCKED_EXTERNAL`` / ``LICENSE_REQUIRED``
  state.

No network call is made anywhere in this file.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from trading_market_data_core.dataset_v1_real_ingestion import ASOF_GICS_BLOCKER
from trading_market_data_core.dataset_v1_sources import SourceAdapterError, SourceAuthority
from trading_market_data_core.dataset_v1_wikipedia_pit import (
    CLASSIFICATION,
    MINIMUM_CROSS_CHECK_AGREEMENT_RATIO,
    MINIMUM_SECTOR_AGREEMENT_RATIO,
    WIKIPEDIA_REVISION_HISTORY_EVALUATION,
    WIKIPEDIA_REVISION_LOOKUP_API_URL,
    WIKIPEDIA_REVISION_PARSE_API_URL,
    CrossCheckResult,
    RawWikipediaRevisionFetch,
    SectorCrossCheckResult,
    WikipediaConstituentObservation,
    WikipediaRevisionRef,
    WikipediaRevisionSnapshot,
    _verify_revision_metadata,
    cross_check_membership,
    cross_check_sectors,
    parse_revision_snapshot,
    select_revision_as_of,
)
from trading_market_data_core.sp500_current_universe import (
    MAX_PLAUSIBLE_CONSTITUENTS,
    MIN_PLAUSIBLE_CONSTITUENTS,
    raw_artifact_checksum,
)

TABLE_ROW_COUNT = 405  # inside [MIN_PLAUSIBLE_CONSTITUENTS, MAX_PLAUSIBLE_CONSTITUENTS]


def _ticker_for_index(i: int) -> str:
    """A unique, all-uppercase-letters ticker matching the real ticker shape.

    ``TICKER_RE`` in ``sp500_wikitable`` only accepts ``[A-Z][A-Z.\\-]{0,6}``
    -- digits are not a valid ticker character -- so a synthetic
    ``SYM0000``-style ticker would be silently skipped as unreadable. Four
    uppercase letters (26**4 combinations) is ample for the row counts used
    here and stays inside the real 1-7 character shape.
    """
    letters = []
    n = i
    for _ in range(4):
        n, remainder = divmod(n, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _wikitext_table(
    *, count: int = TABLE_ROW_COUNT, reclassify_first_n: int = 0
) -> str:
    """A minimal, schema-valid wikitext constituent table with ``count`` rows.

    The first ``reclassify_first_n`` rows carry a different GICS sector than
    the rest, so two "revisions" built from different ``reclassify_first_n``
    values differ exactly where a test wants them to.
    """
    header = (
        '{| class="wikitable"\n|-\n! Symbol !! Security !! GICS Sector !! GICS Sub-Industry\n'
    )
    rows = []
    for i in range(count):
        ticker = _ticker_for_index(i)
        sector = "Financials" if i < reclassify_first_n else "Information Technology"
        rows.append(
            f"|-\n| {ticker} || Company {i} Inc. || {sector} || Sub-Industry {i}\n"
        )
    return header + "".join(rows) + "|}"


def _raw(wikitext: str) -> bytes:
    return wikitext.encode("utf-8")


def _revision(revision_id: int, timestamp: datetime) -> WikipediaRevisionRef:
    return WikipediaRevisionRef(
        page_title="List of S&P 500 companies",
        revision_id=revision_id,
        revision_timestamp=timestamp,
    )


REV_A_TS = datetime(2020, 1, 15, 12, 0, tzinfo=UTC)
REV_B_TS = datetime(2020, 6, 15, 12, 0, tzinfo=UTC)


def _snapshot(
    *, revision_id: int, timestamp: datetime, reclassify_first_n: int = 0
) -> WikipediaRevisionSnapshot:
    wikitext = _wikitext_table(reclassify_first_n=reclassify_first_n)
    raw = _raw(wikitext)
    return parse_revision_snapshot(
        wikitext, raw_bytes=raw, revision=_revision(revision_id, timestamp)
    )


# -- revision binding and checksum authentication -----------------------------


def test_parse_revision_snapshot_binds_revision_id_timestamp_and_checksum() -> None:
    wikitext = _wikitext_table()
    raw = _raw(wikitext)
    revision = _revision(123456789, REV_A_TS)
    snapshot = parse_revision_snapshot(wikitext, raw_bytes=raw, revision=revision)

    assert snapshot.revision.revision_id == 123456789
    assert snapshot.revision.revision_timestamp == REV_A_TS
    assert snapshot.raw_checksum == raw_artifact_checksum(raw)
    assert snapshot.classification == "SECONDARY_RESEARCH_PIT"
    assert snapshot.source_authority == SourceAuthority.PUBLIC_SECONDARY.value
    assert MIN_PLAUSIBLE_CONSTITUENTS <= len(snapshot.observations) <= MAX_PLAUSIBLE_CONSTITUENTS
    assert "AAAA" in snapshot.tickers()
    assert snapshot.gics_sector_of("AAAA") == "Information Technology"


def test_parse_revision_snapshot_rejects_a_payload_with_no_table() -> None:
    with pytest.raises(SourceAdapterError, match="no wikitext table"):
        parse_revision_snapshot(
            "no table here", raw_bytes=b"no table here", revision=_revision(1, REV_A_TS)
        )


def test_raw_wikipedia_revision_fetch_pairs_snapshot_with_real_bytes() -> None:
    """The wrapper fetch_revision_wikitext returns -- authenticable, not re-derived."""
    wikitext = _wikitext_table()
    raw = _raw(wikitext)
    snapshot = parse_revision_snapshot(wikitext, raw_bytes=raw, revision=_revision(1, REV_A_TS))
    fetched = RawWikipediaRevisionFetch(snapshot=snapshot, raw_bytes=raw)
    assert fetched.snapshot is snapshot
    assert fetched.raw_bytes == raw
    assert snapshot.verify_raw_bytes(fetched.raw_bytes) is True


def test_verify_raw_bytes_authenticates_and_detects_tamper() -> None:
    wikitext = _wikitext_table()
    raw = _raw(wikitext)
    snapshot = parse_revision_snapshot(wikitext, raw_bytes=raw, revision=_revision(1, REV_A_TS))

    assert snapshot.verify_raw_bytes(raw) is True
    assert snapshot.verify_raw_bytes(raw + b"tampered") is False


# -- fail-closed construction ---------------------------------------------------


def _observation(ticker: str = "AAA") -> WikipediaConstituentObservation:
    return WikipediaConstituentObservation(ticker=ticker, name=f"{ticker} Inc.")


def test_snapshot_rejects_too_few_observations() -> None:
    with pytest.raises(SourceAdapterError, match="outside the plausible range"):
        WikipediaRevisionSnapshot(
            revision=_revision(1, REV_A_TS),
            raw_checksum="sha256:" + "a" * 64,
            observations=(_observation("AAA"),),
        )


def test_snapshot_rejects_duplicate_tickers() -> None:
    observations = tuple(
        WikipediaConstituentObservation(ticker=_ticker_for_index(i), name=f"Company {i}")
        for i in range(TABLE_ROW_COUNT)
    )
    # Force a duplicate by repeating the first ticker in place of the last.
    observations = observations[:-1] + (observations[0],)
    with pytest.raises(SourceAdapterError, match="duplicate tickers"):
        WikipediaRevisionSnapshot(
            revision=_revision(1, REV_A_TS),
            raw_checksum="sha256:" + "a" * 64,
            observations=observations,
        )


def test_snapshot_rejects_a_bad_checksum_shape() -> None:
    observations = tuple(
        WikipediaConstituentObservation(ticker=_ticker_for_index(i), name=f"Company {i}")
        for i in range(TABLE_ROW_COUNT)
    )
    with pytest.raises(SourceAdapterError, match="sha256"):
        WikipediaRevisionSnapshot(
            revision=_revision(1, REV_A_TS),
            raw_checksum="not-a-real-checksum",
            observations=observations,
        )


def test_snapshot_can_never_be_constructed_as_official() -> None:
    """The classification/authority fields are constants, not caller choices."""
    observations = tuple(
        WikipediaConstituentObservation(ticker=_ticker_for_index(i), name=f"Company {i}")
        for i in range(TABLE_ROW_COUNT)
    )
    with pytest.raises(SourceAdapterError, match="SECONDARY_RESEARCH_PIT"):
        WikipediaRevisionSnapshot(
            revision=_revision(1, REV_A_TS),
            raw_checksum="sha256:" + "a" * 64,
            observations=observations,
            classification="OFFICIAL_PUBLIC",
        )
    with pytest.raises(SourceAdapterError, match="PUBLIC_SECONDARY"):
        WikipediaRevisionSnapshot(
            revision=_revision(1, REV_A_TS),
            raw_checksum="sha256:" + "a" * 64,
            observations=observations,
            source_authority=SourceAuthority.OFFICIAL_PUBLIC.value,
        )


def test_revision_ref_validates_its_own_fields() -> None:
    with pytest.raises(SourceAdapterError, match="positive integer"):
        WikipediaRevisionRef(page_title="X", revision_id=0, revision_timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="page title"):
        WikipediaRevisionRef(page_title="  ", revision_id=1, revision_timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="timezone-aware"):
        WikipediaRevisionRef(
            page_title="X", revision_id=1, revision_timestamp=datetime(2020, 1, 1)
        )


# -- no look-ahead / no back-projection ----------------------------------------


def test_select_revision_as_of_never_uses_a_later_revision() -> None:
    older = _snapshot(revision_id=1, timestamp=REV_A_TS)
    newer = _snapshot(revision_id=2, timestamp=REV_B_TS, reclassify_first_n=5)
    observations = [older, newer]

    # A decision date between the two revisions must see only the OLDER one.
    picked = select_revision_as_of(observations, date(2020, 3, 1))
    assert picked.revision.revision_id == 1
    assert picked.gics_sector_of("AAAA") == "Information Technology"

    # A decision date after both sees the latest.
    picked_latest = select_revision_as_of(observations, date(2020, 12, 1))
    assert picked_latest.revision.revision_id == 2
    assert picked_latest.gics_sector_of("AAAA") == "Financials"


def test_select_revision_as_of_refuses_when_nothing_qualifies() -> None:
    older = _snapshot(revision_id=1, timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        select_revision_as_of([older], date(2019, 1, 1))


def test_cross_check_uses_the_no_look_ahead_selected_revision() -> None:
    """The full pipeline: select, then cross-check, never the later revision."""
    older = _snapshot(revision_id=1, timestamp=REV_A_TS)
    newer = _snapshot(revision_id=2, timestamp=REV_B_TS, reclassify_first_n=5)
    decision_date = date(2020, 3, 1)  # strictly between REV_A_TS and REV_B_TS

    selected = select_revision_as_of([older, newer], decision_date)
    assert selected.revision.revision_id == 1

    result = cross_check_membership(
        wikipedia=selected,
        secondary_tickers=selected.tickers(),
        secondary_source_id="fja05680-sp500-github",
        as_of=decision_date,
    )
    assert result.corroborated is True
    assert f"Wikipedia revision {selected.revision.revision_id}" in result.detail
    # Never the newer revision's id, even though it exists in the pool.
    assert f"revision {newer.revision.revision_id}" not in result.detail


# -- mandatory independent cross-check -----------------------------------------


def test_cross_check_reports_full_agreement() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    result = cross_check_membership(
        wikipedia=snapshot,
        secondary_tickers=snapshot.tickers(),
        secondary_source_id="fja05680-sp500-github",
        as_of=date(2020, 1, 15),
    )
    assert isinstance(result, CrossCheckResult)
    assert result.corroborated is True
    assert result.agreement_ratio == pytest.approx(1.0)
    assert result.only_in_wikipedia == ()
    assert result.only_in_secondary == ()


def test_cross_check_records_disagreement_without_dropping_it() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    wiki_tickers = snapshot.tickers()
    # Drop enough tickers, and add enough foreign ones, to push the ratio
    # below the corroboration threshold -- a large, real disagreement.
    secondary = frozenset(list(wiki_tickers)[:200]) | frozenset(
        {"FOREIGN1", "FOREIGN2", "FOREIGN3"}
    )
    result = cross_check_membership(
        wikipedia=snapshot,
        secondary_tickers=secondary,
        secondary_source_id="fja05680-sp500-github",
        as_of=date(2020, 1, 15),
    )
    assert result.corroborated is False
    assert result.agreement_ratio < MINIMUM_CROSS_CHECK_AGREEMENT_RATIO
    assert set(result.only_in_secondary) == {"FOREIGN1", "FOREIGN2", "FOREIGN3"}
    assert len(result.only_in_wikipedia) > 0
    # The disagreement is reported in the human-readable detail, not hidden.
    assert "only in Wikipedia" in result.detail
    assert "only in fja05680-sp500-github" in result.detail


def test_cross_check_requires_a_nonempty_secondary_set() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="non-empty"):
        cross_check_membership(
            wikipedia=snapshot,
            secondary_tickers=frozenset(),
            secondary_source_id="fja05680-sp500-github",
            as_of=date(2020, 1, 15),
        )


# -- sector/GICS cross-check: deterministic coverage + anomaly handling -------


def test_cross_check_sectors_reports_full_agreement() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    result = cross_check_sectors(
        wikipedia=snapshot,
        secondary_sectors=secondary_sectors,
        secondary_source_id="fixture-secondary-gics",
        as_of=date(2020, 1, 15),
    )
    assert isinstance(result, SectorCrossCheckResult)
    assert result.corroborated is True
    assert result.coverage_ratio == pytest.approx(1.0)
    assert result.agreement_ratio == pytest.approx(1.0)
    assert result.disagreements == ()
    assert result.uncovered_tickers == ()


def test_cross_check_sectors_records_disagreements_without_dropping_them() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    # Flip enough sectors to push agreement below the threshold.
    flipped = dict(secondary_sectors)
    for ticker in list(flipped)[:100]:
        flipped[ticker] = "Utilities"
    result = cross_check_sectors(
        wikipedia=snapshot,
        secondary_sectors=flipped,
        secondary_source_id="fixture-secondary-gics",
        as_of=date(2020, 1, 15),
    )
    assert result.corroborated is False
    assert result.agreement_ratio < MINIMUM_SECTOR_AGREEMENT_RATIO
    assert len(result.disagreements) == 100
    # Every disagreement carries both sides' values, not just a count.
    first = result.disagreements[0]
    assert first[1] == "Information Technology"
    assert first[2] == "Utilities"


def test_cross_check_sectors_fails_closed_on_insufficient_coverage() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    tiny_coverage = dict(
        list({o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector}.items())[
            :5
        ]
    )
    with pytest.raises(SourceAdapterError, match="coverage"):
        cross_check_sectors(
            wikipedia=snapshot,
            secondary_sectors=tiny_coverage,
            secondary_source_id="fixture-secondary-gics",
            as_of=date(2020, 1, 15),
        )


def test_cross_check_sectors_requires_a_nonempty_secondary_mapping() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="non-empty"):
        cross_check_sectors(
            wikipedia=snapshot,
            secondary_sectors={},
            secondary_source_id="fixture-secondary-gics",
            as_of=date(2020, 1, 15),
        )


def test_cross_check_sectors_records_uncovered_tickers_separately() -> None:
    """A ticker missing from the secondary source is a coverage gap, not a disagreement."""
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    # Drop coverage for a handful of tickers -- still above the coverage floor.
    for ticker in list(secondary_sectors)[:10]:
        del secondary_sectors[ticker]
    result = cross_check_sectors(
        wikipedia=snapshot,
        secondary_sectors=secondary_sectors,
        secondary_source_id="fixture-secondary-gics",
        as_of=date(2020, 1, 15),
    )
    assert len(result.uncovered_tickers) == 10
    # Uncovered tickers are never counted as disagreements.
    uncovered_set = set(result.uncovered_tickers)
    assert not any(ticker in uncovered_set for ticker, _, _ in result.disagreements)


# -- provenance is verified, never trusted -------------------------------------


def _metadata_payload(*, revid: int, title: str, timestamp: str) -> bytes:
    return json.dumps(
        {
            "batchcomplete": True,
            "query": {
                "pages": [
                    {
                        "pageid": 2676045,
                        "ns": 0,
                        "title": title,
                        "revisions": [{"revid": revid, "timestamp": timestamp}],
                    }
                ]
            },
        }
    ).encode("utf-8")


def test_verify_revision_metadata_returns_the_verified_reference() -> None:
    claimed = _revision(1192789360, datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC))
    metadata = _metadata_payload(
        revid=1192789360,
        title="List of S&P 500 companies",
        timestamp="2023-12-31T09:32:39Z",
    )
    verified = _verify_revision_metadata(claimed, metadata)
    assert verified.revision_id == 1192789360
    assert verified.page_title == "List of S&P 500 companies"
    assert verified.revision_timestamp == datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC)


def test_verify_revision_metadata_rejects_a_mismatched_page_title() -> None:
    claimed = _revision(1192789360, datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC))
    metadata = _metadata_payload(
        revid=1192789360,
        title="List of NASDAQ-100 companies",  # a REAL page, wrong page
        timestamp="2023-12-31T09:32:39Z",
    )
    with pytest.raises(SourceAdapterError, match="belongs to page"):
        _verify_revision_metadata(claimed, metadata)


def test_verify_revision_metadata_rejects_a_mismatched_timestamp() -> None:
    claimed = _revision(1192789360, datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC))
    metadata = _metadata_payload(
        revid=1192789360,
        title="List of S&P 500 companies",
        timestamp="1999-01-01T00:00:00Z",  # fabricated/stale timestamp
    )
    with pytest.raises(SourceAdapterError, match="fabricated or stale timestamp"):
        _verify_revision_metadata(claimed, metadata)


def test_verify_revision_metadata_rejects_a_mismatched_revid() -> None:
    claimed = _revision(1192789360, datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC))
    metadata = _metadata_payload(
        revid=999999999,  # MediaWiki reports a different revision entirely
        title="List of S&P 500 companies",
        timestamp="2023-12-31T09:32:39Z",
    )
    with pytest.raises(SourceAdapterError, match="reports revid"):
        _verify_revision_metadata(claimed, metadata)


def test_verify_revision_metadata_fails_closed_on_no_pages() -> None:
    claimed = _revision(1192789360, datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC))
    empty_metadata = json.dumps({"query": {"pages": []}}).encode("utf-8")
    with pytest.raises(SourceAdapterError, match="no page"):
        _verify_revision_metadata(claimed, empty_metadata)


def test_verify_revision_metadata_is_case_and_underscore_insensitive_on_title() -> None:
    """MediaWiki normalizes underscores/spaces; the check must not false-positive on that."""
    claimed = WikipediaRevisionRef(
        page_title="List_of_S&P_500_companies",
        revision_id=1192789360,
        revision_timestamp=datetime(2023, 12, 31, 9, 32, 39, tzinfo=UTC),
    )
    metadata = _metadata_payload(
        revid=1192789360,
        title="List of S&P 500 companies",
        timestamp="2023-12-31T09:32:39Z",
    )
    verified = _verify_revision_metadata(claimed, metadata)
    assert verified.page_title == "List of S&P 500 companies"


# -- the evaluation record never becomes an official claim ---------------------


def test_evaluation_record_stays_secondary_and_non_blocking() -> None:
    evaluation = WIKIPEDIA_REVISION_HISTORY_EVALUATION
    assert evaluation["classification"] == CLASSIFICATION == "SECONDARY_RESEARCH_PIT"
    assert evaluation["state"] != "BLOCKED_EXTERNAL"
    assert evaluation["requires_cross_check"] is True
    eligibility = evaluation["eligibility"]
    assert eligibility == {
        "authoritative_official_evidence": False,
        "eligible_for_promotion_without_validation": False,
        "substitute_for_historical_gics": False,
    }
    assert evaluation["official_licensed_gap_status"] == "DEFERRED_NOT_BLOCKING"
    assert "next_bounded_task" in evaluation


def test_asof_gics_blocker_still_blocked_but_no_longer_categorical() -> None:
    """Finding #2: replace the categorical conclusion, never the official gate."""
    assert ASOF_GICS_BLOCKER["state"] == "BLOCKED_EXTERNAL"
    assert ASOF_GICS_BLOCKER["blocker_class"] == "LICENSE_REQUIRED"
    alternative = ASOF_GICS_BLOCKER["free_secondary_research_alternative"]
    assert alternative is WIKIPEDIA_REVISION_HISTORY_EVALUATION
    sources_reviewed = ASOF_GICS_BLOCKER["sources_reviewed"]
    assert isinstance(sources_reviewed, dict)
    free_secondary_text = sources_reviewed["free_secondary"]
    assert isinstance(free_secondary_text, str)
    assert "contemporaneous-revision" in free_secondary_text
    assert "MediaWiki" in free_secondary_text
    # The gap itself must still read as blocked -- the evaluation informs it,
    # it never quietly resolves it.
    assert "not close" in free_secondary_text.lower()


# -- API URL templates are well-formed (no network call is made here) ---------


def test_revision_api_url_templates_render() -> None:
    parse_url = WIKIPEDIA_REVISION_PARSE_API_URL.format(revision_id=123)
    assert "oldid=123" in parse_url
    assert parse_url.startswith("https://en.wikipedia.org/w/api.php")

    lookup_url = WIKIPEDIA_REVISION_LOOKUP_API_URL.format(
        page_title="List_of_S%26P_500_companies", rvstart="2020-01-15T23%3A59%3A59.999999%2B00%3A00"
    )
    assert "rvdir=older" in lookup_url
    assert lookup_url.startswith("https://en.wikipedia.org/w/api.php")


def test_to_dict_never_claims_official_eligibility() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    payload = snapshot.to_dict()
    assert payload["classification"] == "SECONDARY_RESEARCH_PIT"
    assert payload["eligibility"] == {
        "authoritative_official_evidence": False,
        "eligible_for_promotion_without_validation": False,
        "substitute_for_historical_gics": False,
    }
    revision_payload = payload["revision"]
    assert isinstance(revision_payload, dict)
    assert revision_payload["revision_id"] == 1
