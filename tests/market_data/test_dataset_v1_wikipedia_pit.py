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
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_real_ingestion import ASOF_GICS_BLOCKER
from trading_market_data_core.dataset_v1_sources import SourceAdapterError, SourceAuthority
from trading_market_data_core.dataset_v1_wikipedia_pit import (
    BATCH_OUTCOME_ADMITTED,
    BATCH_OUTCOME_FAILED,
    BATCH_OUTCOME_REFUSED_NOT_CORROBORATED,
    BATCH_OUTCOME_RESUMED,
    CLASSIFICATION,
    MINIMUM_CROSS_CHECK_AGREEMENT_RATIO,
    MINIMUM_SECTOR_AGREEMENT_RATIO,
    WIKIPEDIA_REVISION_HISTORY_EVALUATION,
    WIKIPEDIA_REVISION_LOOKUP_API_URL,
    WIKIPEDIA_REVISION_PARSE_API_URL,
    WINDOW_DEFINITION_REQUIRED,
    CrossCheckResult,
    RawWikipediaRevisionFetch,
    SecondaryCsvMatch,
    SecondaryProvenance,
    SectorCrossCheckResult,
    WikipediaConstituentObservation,
    WikipediaDatasetEntry,
    WikipediaDatasetStore,
    WikipediaRevisionRef,
    WikipediaRevisionSnapshot,
    _verify_revision_metadata,
    build_dataset_manifest,
    cross_check_membership,
    cross_check_sectors,
    parse_revision_snapshot,
    run_wikipedia_revision_history_batch,
    secondary_csv_tickers_at_or_before,
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


def test_cross_check_membership_refuses_a_later_revision_for_an_earlier_as_of() -> None:
    """A later immutable revision must never be corroborated for an earlier decision."""
    snapshot = _snapshot(revision_id=1, timestamp=REV_B_TS)  # 2020-06-15
    earlier_as_of = date(2020, 1, 15)  # strictly before the revision's own timestamp
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        cross_check_membership(
            wikipedia=snapshot,
            secondary_tickers=snapshot.tickers(),
            secondary_source_id="fja05680-sp500-github",
            as_of=earlier_as_of,
        )


def test_cross_check_membership_allows_as_of_on_the_revision_date() -> None:
    """The boundary case (same calendar day) must not be refused."""
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    result = cross_check_membership(
        wikipedia=snapshot,
        secondary_tickers=snapshot.tickers(),
        secondary_source_id="fja05680-sp500-github",
        as_of=REV_A_TS.date(),
    )
    assert result.corroborated is True


@pytest.mark.parametrize("weakened_ratio", [0.0, -1.0, 0.5, 0.979999])
def test_cross_check_membership_refuses_a_weakened_threshold(weakened_ratio: float) -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="weaken"):
        cross_check_membership(
            wikipedia=snapshot,
            secondary_tickers=snapshot.tickers(),
            secondary_source_id="fja05680-sp500-github",
            as_of=date(2020, 1, 15),
            minimum_agreement_ratio=weakened_ratio,
        )


def test_cross_check_membership_refuses_a_threshold_above_one() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    with pytest.raises(SourceAdapterError, match="weaken"):
        cross_check_membership(
            wikipedia=snapshot,
            secondary_tickers=snapshot.tickers(),
            secondary_source_id="fja05680-sp500-github",
            as_of=date(2020, 1, 15),
            minimum_agreement_ratio=1.5,
        )


def test_cross_check_membership_allows_tightening_the_threshold() -> None:
    """The override may only ever make the gate stricter, never looser."""
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    result = cross_check_membership(
        wikipedia=snapshot,
        secondary_tickers=snapshot.tickers(),
        secondary_source_id="fja05680-sp500-github",
        as_of=date(2020, 1, 15),
        minimum_agreement_ratio=1.0,
    )
    assert result.corroborated is True


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


def test_cross_check_sectors_refuses_a_later_revision_for_an_earlier_as_of() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_B_TS)  # 2020-06-15
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        cross_check_sectors(
            wikipedia=snapshot,
            secondary_sectors=secondary_sectors,
            secondary_source_id="fixture-secondary-gics",
            as_of=date(2020, 1, 15),
        )


@pytest.mark.parametrize("weakened_ratio", [0.0, -1.0, 0.5, 0.899999])
def test_cross_check_sectors_refuses_a_weakened_coverage_threshold(weakened_ratio: float) -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    with pytest.raises(SourceAdapterError, match="weaken"):
        cross_check_sectors(
            wikipedia=snapshot,
            secondary_sectors=secondary_sectors,
            secondary_source_id="fixture-secondary-gics",
            as_of=date(2020, 1, 15),
            minimum_coverage_ratio=weakened_ratio,
        )


@pytest.mark.parametrize("weakened_ratio", [0.0, -1.0, 0.5, 0.899999])
def test_cross_check_sectors_refuses_a_weakened_agreement_threshold(weakened_ratio: float) -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    with pytest.raises(SourceAdapterError, match="weaken"):
        cross_check_sectors(
            wikipedia=snapshot,
            secondary_sectors=secondary_sectors,
            secondary_source_id="fixture-secondary-gics",
            as_of=date(2020, 1, 15),
            minimum_agreement_ratio=weakened_ratio,
        )


@pytest.mark.parametrize(
    ("kwarg", "value"),
    [("minimum_coverage_ratio", 1.5), ("minimum_agreement_ratio", 1.5)],
)
def test_cross_check_sectors_refuses_a_threshold_above_one(kwarg: str, value: float) -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    secondary_sectors = {
        o.ticker: o.gics_sector for o in snapshot.observations if o.gics_sector
    }
    with pytest.raises(SourceAdapterError, match="weaken"):
        cross_check_sectors(
            wikipedia=snapshot,
            secondary_sectors=secondary_sectors,
            secondary_source_id="fixture-secondary-gics",
            as_of=date(2020, 1, 15),
            **{kwarg: value},
        )


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


# == A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD: multi-date batch =========
#
# Deterministic, offline proofs for WikipediaDatasetEntry / WikipediaDatasetStore
# / run_wikipedia_revision_history_batch / build_dataset_manifest, and for the
# WINDOW_DEFINITION_REQUIRED gap record. No network call is made anywhere here
# either: fetch functions are always faked/injected.


def _cross_check_for(
    snapshot: WikipediaRevisionSnapshot, *, as_of: date, corroborated: bool = True
) -> CrossCheckResult:
    secondary = snapshot.tickers() if corroborated else frozenset({"ZZZZ", "YYYY", "XXXX"})
    return cross_check_membership(
        wikipedia=snapshot, secondary_tickers=secondary,
        secondary_source_id="fja05680-sp500-github", as_of=as_of,
    )


def _secondary_provenance_for(
    *, as_of: date, source_id: str = "fja05680-sp500-github", matched_row_date: date | None = None,
    fetched_at: datetime = datetime(2026, 9, 6, tzinfo=UTC),
) -> SecondaryProvenance:
    """A syntactically valid :class:`SecondaryProvenance` with fabricated but
    well-shaped checksums.

    For object-level / hand-written-JSON tests where the secondary artifact
    itself is never persisted or re-verified against real bytes (that only
    happens inside :meth:`WikipediaDatasetStore.accept` /
    :meth:`WikipediaDatasetStore.load`, which every test using this helper
    deliberately bypasses -- see ``_entry_with_secondary`` for the
    real-round-trip counterpart).
    """
    return SecondaryProvenance(
        source_id=source_id,
        source_url="https://raw.githubusercontent.com/fja05680/sp500/master/test.csv",
        fetched_at=fetched_at,
        full_file_checksum="sha256:" + "a" * 64,
        raw_artifact_path="secondary.raw.csv",
        matched_row_date=matched_row_date if matched_row_date is not None else as_of,
        matched_row_checksum="sha256:" + "b" * 64,
    )


def _entry_for(
    *, as_of: date, revision_id: int, timestamp: datetime, raw_provenance_path: str = "raw.json"
) -> WikipediaDatasetEntry:
    snapshot = _snapshot(revision_id=revision_id, timestamp=timestamp)
    cross_check = _cross_check_for(snapshot, as_of=as_of)
    return WikipediaDatasetEntry(
        as_of=as_of, snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC),
        raw_provenance_path=raw_provenance_path,
        secondary_provenance=_secondary_provenance_for(as_of=as_of),
    )


def _secondary_csv_bytes(
    tickers: frozenset[str], *, effective_date: date = date(2000, 1, 1)
) -> bytes:
    """A minimal, schema-valid ``date,tickers`` CSV with exactly one row.

    ``effective_date`` defaults far in the past so this one row is always the
    latest one "at or before" any ``as_of`` used across these tests --
    :func:`secondary_csv_tickers_at_or_before` always resolves to it.
    """
    row = f'{effective_date.isoformat()},"{",".join(sorted(tickers))}"'
    return f"date,tickers\n{row}\n".encode()


def _entry_with_secondary(
    *, as_of: date, revision_id: int, timestamp: datetime, corroborated: bool = True,
    raw_provenance_path: str = "raw.json", secondary_tickers: frozenset[str] | None = None,
) -> tuple[WikipediaDatasetEntry, bytes, bytes]:
    """A fully valid, REAL-ROUND-TRIPPABLE entry plus BOTH raw byte blobs
    (MediaWiki wikitext raw bytes, secondary CSV raw bytes) needed to
    exercise :meth:`WikipediaDatasetStore.accept` /
    :meth:`WikipediaDatasetStore.load` for real -- unlike ``_entry_for``,
    whose fabricated secondary checksums are never meant to authenticate real
    bytes.
    """
    wikitext = _wikitext_table()
    raw_bytes = _raw(wikitext)
    snapshot = parse_revision_snapshot(
        wikitext, raw_bytes=raw_bytes, revision=_revision(revision_id, timestamp)
    )
    if secondary_tickers is None:
        secondary_tickers = (
            snapshot.tickers() if corroborated else frozenset({"ZZZZ", "YYYY", "XXXX"})
        )
    secondary_raw = _secondary_csv_bytes(secondary_tickers)
    match = secondary_csv_tickers_at_or_before(secondary_raw, as_of)
    cross_check = cross_check_membership(
        wikipedia=snapshot, secondary_tickers=match.tickers,
        secondary_source_id="fja05680-sp500-github", as_of=as_of,
    )
    secondary_provenance = SecondaryProvenance(
        source_id="fja05680-sp500-github",
        source_url="https://raw.githubusercontent.com/fja05680/sp500/master/test.csv",
        fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        full_file_checksum=raw_artifact_checksum(secondary_raw),
        raw_artifact_path="secondary.raw.csv",
        matched_row_date=match.matched_date,
        matched_row_checksum=raw_artifact_checksum(match.matched_line.encode("utf-8")),
    )
    entry = WikipediaDatasetEntry(
        as_of=as_of, snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC),
        raw_provenance_path=raw_provenance_path, secondary_provenance=secondary_provenance,
    )
    return entry, raw_bytes, secondary_raw


# -- WikipediaDatasetEntry: fail closed --------------------------------------


def test_dataset_entry_refuses_a_revision_after_as_of() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_B_TS)  # 2020-06-15
    earlier = date(2020, 1, 15)
    cross_check = CrossCheckResult(
        as_of=earlier, secondary_source_id="x", corroborated=True, agreement_ratio=1.0,
        agreements=(), only_in_wikipedia=(), only_in_secondary=(), detail="fabricated for test",
    )
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        WikipediaDatasetEntry(
            as_of=earlier, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC), raw_provenance_path="raw.json",
            secondary_provenance=_secondary_provenance_for(as_of=earlier, source_id="x"),
        )


def test_dataset_entry_refuses_a_cross_check_as_of_mismatch() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    cross_check = _cross_check_for(snapshot, as_of=date(2020, 3, 1))
    with pytest.raises(SourceAdapterError, match="does not"):
        WikipediaDatasetEntry(
            as_of=date(2020, 1, 15), snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC), raw_provenance_path="raw.json",
            secondary_provenance=_secondary_provenance_for(as_of=date(2020, 1, 15)),
        )


def test_dataset_entry_refuses_an_uncorroborated_cross_check() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    as_of = date(2020, 1, 15)
    cross_check = _cross_check_for(snapshot, as_of=as_of, corroborated=False)
    assert cross_check.corroborated is False
    with pytest.raises(SourceAdapterError, match="not.*corroborated"):
        WikipediaDatasetEntry(
            as_of=as_of, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC), raw_provenance_path="raw.json",
            secondary_provenance=_secondary_provenance_for(as_of=as_of),
        )


def test_dataset_entry_refuses_a_naive_fetch_observed_at() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    as_of = date(2020, 1, 15)
    cross_check = _cross_check_for(snapshot, as_of=as_of)
    with pytest.raises(SourceAdapterError, match="timezone-aware"):
        WikipediaDatasetEntry(
            as_of=as_of, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6), raw_provenance_path="raw.json",
            secondary_provenance=_secondary_provenance_for(as_of=as_of),
        )


def test_dataset_entry_refuses_an_empty_raw_provenance_path() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    as_of = date(2020, 1, 15)
    cross_check = _cross_check_for(snapshot, as_of=as_of)
    with pytest.raises(SourceAdapterError, match="raw_provenance_path"):
        WikipediaDatasetEntry(
            as_of=as_of, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC), raw_provenance_path="   ",
            secondary_provenance=_secondary_provenance_for(as_of=as_of),
        )


def test_dataset_entry_refuses_a_secondary_source_id_mismatch() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    as_of = date(2020, 1, 15)
    # secondary_source_id="fja05680-sp500-github" (_cross_check_for's default)
    cross_check = _cross_check_for(snapshot, as_of=as_of)
    with pytest.raises(SourceAdapterError, match="does not match"):
        WikipediaDatasetEntry(
            as_of=as_of, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC), raw_provenance_path="raw.json",
            secondary_provenance=_secondary_provenance_for(
                as_of=as_of, source_id="a-different-source"
            ),
        )


def test_dataset_entry_refuses_a_secondary_matched_row_after_as_of() -> None:
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    as_of = date(2020, 1, 15)
    cross_check = _cross_check_for(snapshot, as_of=as_of)
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        WikipediaDatasetEntry(
            as_of=as_of, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC), raw_provenance_path="raw.json",
            secondary_provenance=_secondary_provenance_for(
                as_of=as_of, matched_row_date=date(2020, 1, 16)
            ),
        )


def test_dataset_entry_checksum_ignores_fetch_observed_at() -> None:
    """Two fetches of the SAME immutable revision (and of the SAME
    independent secondary artifact) at different real times must be
    recognised as the SAME admitted observation (idempotent), never a
    conflict -- this is what makes crash-recovery retries safe."""
    as_of = date(2020, 1, 15)
    snapshot = _snapshot(revision_id=1, timestamp=REV_A_TS)
    cross_check = _cross_check_for(snapshot, as_of=as_of)
    first = WikipediaDatasetEntry(
        as_of=as_of, snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, 10, 0, tzinfo=UTC), raw_provenance_path="raw.json",
        secondary_provenance=_secondary_provenance_for(
            as_of=as_of, fetched_at=datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
        ),
    )
    second = WikipediaDatasetEntry(
        as_of=as_of, snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, 23, 59, tzinfo=UTC), raw_provenance_path="raw.json",
        secondary_provenance=_secondary_provenance_for(
            as_of=as_of, fetched_at=datetime(2026, 9, 6, 23, 59, tzinfo=UTC)
        ),
    )
    assert first.compute_checksum() == second.compute_checksum()


def test_dataset_entry_to_dict_round_trips() -> None:
    entry = _entry_for(revision_id=1, timestamp=REV_A_TS, as_of=date(2020, 1, 15))
    payload = entry.to_dict()
    assert payload["classification"] == "SECONDARY_RESEARCH_PIT"
    assert payload["backtest_quality"] == "APPROXIMATE"
    assert payload["gics_sector_status"] == "CAPTURED_UNEVALUATED_NOT_ADMITTED"
    assert payload["eligibility"] == {
        "authoritative_official_evidence": False,
        "eligible_for_promotion_without_validation": False,
        "substitute_for_historical_gics": False,
    }


# -- WikipediaDatasetStore: resumable, idempotent, write-once, verified-on-load


def test_store_accepts_and_reloads_an_entry(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry, raw_bytes, secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
    )

    assert store.has(as_of) is False
    result = store.accept(entry, raw_bytes, secondary_raw_bytes=secondary_raw)
    assert result.already_written is False
    assert store.has(as_of) is True
    assert store.dates() == (as_of,)

    reloaded = store.load(as_of)
    assert reloaded.compute_checksum() == entry.compute_checksum()
    assert reloaded.snapshot.revision.revision_id == 1


def test_store_accept_is_idempotent_on_retry(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    wikitext = _wikitext_table()
    raw_bytes = _raw(wikitext)
    snapshot = parse_revision_snapshot(
        wikitext, raw_bytes=raw_bytes, revision=_revision(1, REV_A_TS)
    )
    secondary_raw = _secondary_csv_bytes(snapshot.tickers())
    match = secondary_csv_tickers_at_or_before(secondary_raw, as_of)
    cross_check = cross_check_membership(
        wikipedia=snapshot, secondary_tickers=match.tickers,
        secondary_source_id="fja05680-sp500-github", as_of=as_of,
    )
    # Two SEPARATE entry objects for the identical real observation, built as
    # if by two different fetch attempts (different fetch_observed_at /
    # secondary fetched_at) -- the crash-recovery-retry scenario.
    first = WikipediaDatasetEntry(
        as_of=as_of, snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, 10, 0, tzinfo=UTC),
        raw_provenance_path=str(store._raw_path(1)),  # noqa: SLF001
        secondary_provenance=SecondaryProvenance(
            source_id="fja05680-sp500-github",
            source_url="https://raw.githubusercontent.com/fja05680/sp500/master/test.csv",
            fetched_at=datetime(2026, 9, 6, 10, 0, tzinfo=UTC),
            full_file_checksum=raw_artifact_checksum(secondary_raw),
            raw_artifact_path=str(store._secondary_raw_path(raw_artifact_checksum(secondary_raw))),  # noqa: SLF001
            matched_row_date=match.matched_date,
            matched_row_checksum=raw_artifact_checksum(match.matched_line.encode("utf-8")),
        ),
    )
    second = WikipediaDatasetEntry(
        as_of=as_of, snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, 23, 59, tzinfo=UTC),
        raw_provenance_path=str(store._raw_path(1)),  # noqa: SLF001
        secondary_provenance=SecondaryProvenance(
            source_id="fja05680-sp500-github",
            source_url="https://raw.githubusercontent.com/fja05680/sp500/master/test.csv",
            fetched_at=datetime(2026, 9, 6, 23, 59, tzinfo=UTC),
            full_file_checksum=raw_artifact_checksum(secondary_raw),
            raw_artifact_path=str(store._secondary_raw_path(raw_artifact_checksum(secondary_raw))),  # noqa: SLF001
            matched_row_date=match.matched_date,
            matched_row_checksum=raw_artifact_checksum(match.matched_line.encode("utf-8")),
        ),
    )

    first_result = store.accept(first, raw_bytes, secondary_raw_bytes=secondary_raw)
    assert first_result.already_written is False
    second_result = store.accept(second, raw_bytes, secondary_raw_bytes=secondary_raw)
    assert second_result.already_written is True
    assert store.dates() == (as_of,)
    # The FIRST fetch's observation time is what survives -- a retry never
    # silently updates previously recorded provenance.
    reloaded = store.load(as_of)
    assert reloaded.fetch_observed_at == datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    assert reloaded.secondary_provenance.fetched_at == datetime(2026, 9, 6, 10, 0, tzinfo=UTC)


def test_store_accept_refuses_a_conflicting_candidate_for_the_same_as_of(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    original, original_raw, original_secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
        raw_provenance_path=str(store._raw_path(1)),  # noqa: SLF001
    )
    store.accept(original, original_raw, secondary_raw_bytes=original_secondary_raw)

    # A DIFFERENT revision (different content) for the SAME as_of date --
    # with its OWN validly-bound (not fabricated) secondary provenance, so
    # the only thing under test is the write-once entry-level conflict, not
    # a spurious secondary-checksum mismatch.
    conflicting_wikitext = _wikitext_table(reclassify_first_n=5)
    conflicting_raw = _raw(conflicting_wikitext)
    conflicting_snapshot = parse_revision_snapshot(
        conflicting_wikitext, raw_bytes=conflicting_raw, revision=_revision(2, REV_A_TS)
    )
    conflicting_secondary_raw = _secondary_csv_bytes(conflicting_snapshot.tickers())
    conflicting_match = secondary_csv_tickers_at_or_before(conflicting_secondary_raw, as_of)
    conflicting_cross_check = cross_check_membership(
        wikipedia=conflicting_snapshot, secondary_tickers=conflicting_match.tickers,
        secondary_source_id="fja05680-sp500-github", as_of=as_of,
    )
    conflicting_entry = WikipediaDatasetEntry(
        as_of=as_of, snapshot=conflicting_snapshot, cross_check=conflicting_cross_check,
        fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC),
        raw_provenance_path=str(store._raw_path(2)),  # noqa: SLF001
        secondary_provenance=SecondaryProvenance(
            source_id="fja05680-sp500-github",
            source_url="https://raw.githubusercontent.com/fja05680/sp500/master/test.csv",
            fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
            full_file_checksum=raw_artifact_checksum(conflicting_secondary_raw),
            raw_artifact_path="secondary.raw.csv",
            matched_row_date=conflicting_match.matched_date,
            matched_row_checksum=raw_artifact_checksum(
                conflicting_match.matched_line.encode("utf-8")
            ),
        ),
    )
    with pytest.raises(SourceAdapterError, match="write-once"):
        store.accept(
            conflicting_entry, conflicting_raw, secondary_raw_bytes=conflicting_secondary_raw
        )
    # The original entry must still be exactly what was first archived.
    assert store.load(as_of).snapshot.revision.revision_id == 1


def test_store_accept_refuses_mismatched_secondary_raw_bytes(tmp_path: Path) -> None:
    """``secondary_raw_bytes`` must authenticate the entry's own
    ``full_file_checksum`` claim -- exactly the same fail-closed guard already
    enforced for the primary MediaWiki raw bytes."""
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry, raw_bytes, _secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
    )
    with pytest.raises(SourceAdapterError, match="do not match"):
        store.accept(entry, raw_bytes, secondary_raw_bytes=b"not the real secondary csv bytes")


def test_store_load_detects_tampered_raw_provenance(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry, raw_bytes, secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
    )
    store.accept(entry, raw_bytes, secondary_raw_bytes=secondary_raw)

    store._raw_path(1).write_bytes(raw_bytes + b"tampered")  # noqa: SLF001
    with pytest.raises(SourceAdapterError, match="tampered"):
        store.load(as_of)


def test_store_load_detects_tampered_secondary_raw_provenance(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry, raw_bytes, secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
    )
    store.accept(entry, raw_bytes, secondary_raw_bytes=secondary_raw)

    secondary_path = store._secondary_raw_path(entry.secondary_provenance.full_file_checksum)  # noqa: SLF001
    secondary_path.write_bytes(secondary_raw + b"tampered")
    with pytest.raises(SourceAdapterError, match="tampered"):
        store.load(as_of)


def test_store_load_refuses_a_missing_secondary_raw_sidecar(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry, raw_bytes, secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
    )
    store.accept(entry, raw_bytes, secondary_raw_bytes=secondary_raw)

    secondary_path = store._secondary_raw_path(entry.secondary_provenance.full_file_checksum)  # noqa: SLF001
    secondary_path.unlink()
    with pytest.raises(SourceAdapterError, match="secondary source raw provenance sidecar"):
        store.load(as_of)


def test_store_load_refuses_a_secondary_source_that_has_drifted(tmp_path: Path) -> None:
    """The entry's declared ``matched_row_date`` must equal what
    :func:`secondary_csv_tickers_at_or_before` actually re-derives from the
    RETAINED secondary bytes for this ``as_of`` -- not merely be internally
    well-shaped. Built via ``dataclasses.replace`` on the already-validated
    object (recomputing a genuinely matching ``entry_checksum`` for the
    mutated content) so this test isolates the independent re-derivation
    check from the separately-covered stale/mismatched-checksum guard."""
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry, raw_bytes, secondary_raw = _entry_with_secondary(
        as_of=as_of, revision_id=1, timestamp=REV_A_TS,
    )
    store.accept(entry, raw_bytes, secondary_raw_bytes=secondary_raw)

    tampered = replace(
        entry,
        secondary_provenance=replace(
            entry.secondary_provenance, matched_row_date=date(1999, 1, 1)
        ),
    )
    entry_path = store._entry_path(as_of)  # noqa: SLF001
    entry_path.write_text(json.dumps(tampered.to_dict()), encoding="utf-8")
    with pytest.raises(SourceAdapterError, match="effective row"):
        store.load(as_of)


def test_store_load_refuses_a_hand_imported_entry_with_a_later_revision(tmp_path: Path) -> None:
    """A file placed directly on disk (never through accept()) claiming a
    revision timestamped AFTER its own as_of must be refused on load -- the
    no-look-ahead guard is enforced by reconstruction, not by trusting
    whichever process wrote the file."""
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    # Build a validly-admitted entry first, purely to get a realistic body
    # shape, then hand-edit the as_of down so the bound revision (dated
    # REV_B_TS, 2020-06-15) is now AFTER the claimed as_of -- exactly what an
    # externally "imported" file might (incorrectly) assert.
    snapshot = _snapshot(revision_id=1, timestamp=REV_B_TS)
    cross_check = _cross_check_for(snapshot, as_of=date(2020, 6, 15))
    legitimate = WikipediaDatasetEntry(
        as_of=date(2020, 6, 15), snapshot=snapshot, cross_check=cross_check,
        fetch_observed_at=datetime(2026, 9, 6, tzinfo=UTC),
        raw_provenance_path=str(store._raw_path(1)),  # noqa: SLF001
        secondary_provenance=_secondary_provenance_for(as_of=date(2020, 6, 15)),
    )
    payload = legitimate.to_dict()
    payload["as_of"] = as_of.isoformat()  # hand-tamper: pretend this is an EARLIER date
    cross_check_payload = payload["cross_check"]
    assert isinstance(cross_check_payload, dict)
    cross_check_payload["as_of"] = as_of.isoformat()
    entry_path = tmp_path / f"membership-{as_of.isoformat()}.json"
    entry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceAdapterError, match="no look-ahead"):
        store.load(as_of)


def _write_hand_tampered_entry(
    tmp_path: Path, *, as_of: date, mutate_cross_check: Callable[[dict[str, object]], None]
) -> None:
    """Write a legitimately-shaped entry file with only the cross_check block
    hand-tampered by ``mutate_cross_check`` -- everything else (schema,
    snapshot, checksum-shape fields) stays exactly what a real ``accept()``
    would have produced, so only the tampered field is under test."""
    entry = _entry_for(revision_id=1, timestamp=REV_A_TS, as_of=as_of)
    payload = entry.to_dict()
    cross_check_payload = payload["cross_check"]
    assert isinstance(cross_check_payload, dict)
    mutate_cross_check(cross_check_payload)
    entry_path = tmp_path / f"membership-{as_of.isoformat()}.json"
    entry_path.write_text(json.dumps(payload), encoding="utf-8")


def test_store_load_refuses_a_forged_corroborated_flag_with_a_low_real_ratio(
    tmp_path: Path,
) -> None:
    """The JSON claims ``corroborated: true`` / ``agreement_ratio: 1.0``, but
    the persisted ticker PARTITION -- once independently recomputed against
    the bound snapshot -- proves only a tiny secondary overlap. The forged
    boolean/ratio must never be trusted; the RECOMPUTED ratio governs, and it
    must fail the canonical, non-weakenable threshold."""
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)

    def _mutate(cross_check_payload: dict[str, object]) -> None:
        agreements = cross_check_payload["agreements"]
        assert isinstance(agreements, list)
        # Move all but one ticker from "agreements" into "only_in_wikipedia"
        # -- the union with only_in_wikipedia (still equal to the real
        # snapshot's own ticker set) is preserved, so the snapshot-consistency
        # check passes, but the RECOMPUTED secondary overlap collapses to
        # essentially nothing.
        kept = agreements[:1]
        moved = agreements[1:]
        cross_check_payload["agreements"] = kept
        cross_check_payload["only_in_wikipedia"] = moved
        cross_check_payload["only_in_secondary"] = []
        # The lie: claim full corroboration anyway.
        cross_check_payload["corroborated"] = True
        cross_check_payload["agreement_ratio"] = 1.0
        cross_check_payload["detail"] = "forged: claims full agreement"

    _write_hand_tampered_entry(tmp_path, as_of=as_of, mutate_cross_check=_mutate)
    with pytest.raises(SourceAdapterError, match="not.*corroborated"):
        store.load(as_of)


def test_store_load_refuses_a_cross_check_partition_inconsistent_with_the_snapshot(
    tmp_path: Path,
) -> None:
    """``agreements | only_in_wikipedia`` must equal the bound snapshot's own
    ticker set; a JSON that changes this partition to something that no
    longer reconstructs the snapshot's real tickers (e.g. injecting a ticker
    the snapshot never had) is refused outright, before any ratio is even
    computed."""
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)

    def _mutate(cross_check_payload: dict[str, object]) -> None:
        only_in_wikipedia = cross_check_payload["only_in_wikipedia"]
        assert isinstance(only_in_wikipedia, list)
        # Inject a ticker the real snapshot never contained.
        cross_check_payload["only_in_wikipedia"] = [*only_in_wikipedia, "FORGEDTICKER"]

    _write_hand_tampered_entry(tmp_path, as_of=as_of, mutate_cross_check=_mutate)
    with pytest.raises(SourceAdapterError, match="does not match the bound snapshot"):
        store.load(as_of)


# -- entry_checksum is mandatory and must be a genuine, matching sha256 -----


def test_store_load_refuses_a_missing_entry_checksum(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry = _entry_for(revision_id=1, timestamp=REV_A_TS, as_of=as_of)
    payload = entry.to_dict()
    del payload["entry_checksum"]
    entry_path = tmp_path / f"membership-{as_of.isoformat()}.json"
    entry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceAdapterError, match="no entry_checksum"):
        store.load(as_of)


def test_store_load_refuses_an_empty_entry_checksum(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry = _entry_for(revision_id=1, timestamp=REV_A_TS, as_of=as_of)
    payload = entry.to_dict()
    payload["entry_checksum"] = ""
    entry_path = tmp_path / f"membership-{as_of.isoformat()}.json"
    entry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceAdapterError, match="no entry_checksum"):
        store.load(as_of)


def test_store_load_refuses_a_malformed_entry_checksum(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry = _entry_for(revision_id=1, timestamp=REV_A_TS, as_of=as_of)
    payload = entry.to_dict()
    payload["entry_checksum"] = "not-a-real-checksum"
    entry_path = tmp_path / f"membership-{as_of.isoformat()}.json"
    entry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceAdapterError, match="not a valid sha256"):
        store.load(as_of)


def test_store_load_refuses_a_mismatched_entry_checksum(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 1, 15)
    entry = _entry_for(revision_id=1, timestamp=REV_A_TS, as_of=as_of)
    payload = entry.to_dict()
    payload["entry_checksum"] = "sha256:" + "a" * 64
    entry_path = tmp_path / f"membership-{as_of.isoformat()}.json"
    entry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SourceAdapterError, match="checksum mismatch"):
        store.load(as_of)


def test_store_dates_and_entries_are_sorted(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    for as_of, revision_id, timestamp in (
        (date(2020, 6, 15), 2, REV_B_TS),
        (date(2020, 1, 15), 1, REV_A_TS),
    ):
        entry, raw_bytes, secondary_raw = _entry_with_secondary(
            as_of=as_of, revision_id=revision_id, timestamp=timestamp,
            raw_provenance_path=str(store._raw_path(revision_id)),  # noqa: SLF001
        )
        store.accept(entry, raw_bytes, secondary_raw_bytes=secondary_raw)

    assert store.dates() == (date(2020, 1, 15), date(2020, 6, 15))
    entries = store.entries()
    assert [e.as_of for e in entries] == [date(2020, 1, 15), date(2020, 6, 15)]


# -- run_wikipedia_revision_history_batch: resumable, idempotent, fail-closed


class _FakeFetcher:
    """A deterministic, in-memory stand-in for the real MediaWiki fetch path.

    Each call is counted so tests can prove a RESUMED date never triggers a
    fetch.
    """

    def __init__(self, revisions: dict[date, tuple[int, datetime]]) -> None:
        self._revisions = revisions
        self.lookup_calls: list[date] = []
        self.wikitext_calls: list[int] = []

    def lookup(self, as_of: date, **_kwargs: object) -> WikipediaRevisionRef:
        self.lookup_calls.append(as_of)
        revision_id, timestamp = self._revisions[as_of]
        return WikipediaRevisionRef(
            page_title="List of S&P 500 companies", revision_id=revision_id,
            revision_timestamp=timestamp,
        )

    def wikitext(
        self, revision: WikipediaRevisionRef, **_kwargs: object
    ) -> RawWikipediaRevisionFetch:
        self.wikitext_calls.append(revision.revision_id)
        wikitext = _wikitext_table()
        raw = _raw(wikitext)
        snapshot = parse_revision_snapshot(wikitext, raw_bytes=raw, revision=revision)
        return RawWikipediaRevisionFetch(snapshot=snapshot, raw_bytes=raw)


#: One shared, real (never fabricated) secondary CSV blob with a single row
#: dated far in the past, so "at or before" resolves to it for every ``as_of``
#: these tests use -- exactly what the real script does with one real fetch
#: per run, just without the network call.
_FULL_AGREEMENT_SECONDARY_CSV = _secondary_csv_bytes(
    frozenset(_ticker_for_index(i) for i in range(TABLE_ROW_COUNT))
)
_NO_AGREEMENT_SECONDARY_CSV = _secondary_csv_bytes(frozenset({"FOREIGN1", "FOREIGN2", "FOREIGN3"}))
_SECONDARY_SOURCE_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/test.csv"


def _always_full_agreement() -> Callable[[date], SecondaryCsvMatch]:
    def _match(as_of: date) -> SecondaryCsvMatch:
        return secondary_csv_tickers_at_or_before(_FULL_AGREEMENT_SECONDARY_CSV, as_of)
    return _match


def _no_agreement() -> Callable[[date], SecondaryCsvMatch]:
    def _match(as_of: date) -> SecondaryCsvMatch:
        return secondary_csv_tickers_at_or_before(_NO_AGREEMENT_SECONDARY_CSV, as_of)
    return _match


def test_batch_admits_new_dates(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({date(2020, 1, 15): (1, REV_A_TS)})
    result = run_wikipedia_revision_history_batch(
        [date(2020, 1, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert result.admitted_dates() == (date(2020, 1, 15),)
    assert result.rows[0].outcome == BATCH_OUTCOME_ADMITTED
    assert store.has(date(2020, 1, 15))
    # The archived entry round-trips through load(), including the
    # independent secondary-source binding.
    reloaded = store.load(date(2020, 1, 15))
    assert reloaded.secondary_provenance.source_id == "fja05680-sp500-github"
    assert reloaded.secondary_provenance.full_file_checksum == raw_artifact_checksum(
        _FULL_AGREEMENT_SECONDARY_CSV
    )


def test_batch_resumes_already_archived_dates_without_refetching(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({
        date(2020, 1, 15): (1, REV_A_TS),
        date(2020, 6, 15): (2, REV_B_TS),
    })
    run_wikipedia_revision_history_batch(
        [date(2020, 1, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert fetcher.lookup_calls == [date(2020, 1, 15)]

    # A second, larger batch request that RE-REQUESTS the already-archived
    # date alongside a genuinely new one.
    result = run_wikipedia_revision_history_batch(
        [date(2020, 6, 15), date(2020, 1, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    # The already-archived date was NEVER fetched again.
    assert fetcher.lookup_calls == [date(2020, 1, 15), date(2020, 6, 15)]
    outcomes = {row.as_of: row.outcome for row in result.rows}
    assert outcomes[date(2020, 1, 15)] == BATCH_OUTCOME_RESUMED
    assert outcomes[date(2020, 6, 15)] == BATCH_OUTCOME_ADMITTED
    # Rows are returned in deterministic sorted order regardless of request order.
    assert [row.as_of for row in result.rows] == [date(2020, 1, 15), date(2020, 6, 15)]


def test_batch_resume_survives_a_later_valid_revision_for_the_same_date(tmp_path: Path) -> None:
    """Proves the specific no-look-ahead-through-retry failure mode: even if a
    later fetch WOULD return a different (but still valid, still
    at-or-before) revision for an already-archived date, resuming never lets
    it replace the original archived observation."""
    store = WikipediaDatasetStore(tmp_path)
    as_of = date(2020, 6, 15)
    first_fetcher = _FakeFetcher({as_of: (1, REV_A_TS)})
    run_wikipedia_revision_history_batch(
        [as_of], store=store, secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=first_fetcher.lookup,
        fetch_revision_wikitext_fn=first_fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    archived_revision_id = store.load(as_of).snapshot.revision.revision_id
    assert archived_revision_id == 1

    # A hypothetical retry whose fetcher would now report revision 2 (still
    # <= as_of) for the SAME date -- e.g. the page was edited again between
    # runs, still before the decision date.
    second_fetcher = _FakeFetcher({as_of: (2, REV_B_TS)})
    result = run_wikipedia_revision_history_batch(
        [as_of], store=store, secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=second_fetcher.lookup,
        fetch_revision_wikitext_fn=second_fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert second_fetcher.lookup_calls == []  # never even attempted: resumed first
    assert result.rows[0].outcome == BATCH_OUTCOME_RESUMED
    assert store.load(as_of).snapshot.revision.revision_id == 1  # unchanged


def test_batch_refuses_dates_that_fail_corroboration(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({date(2020, 1, 15): (1, REV_A_TS)})

    result = run_wikipedia_revision_history_batch(
        [date(2020, 1, 15)], store=store, secondary_match_at_or_before=_no_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_NO_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert result.rows[0].outcome == BATCH_OUTCOME_REFUSED_NOT_CORROBORATED
    assert result.admitted_dates() == ()
    assert store.has(date(2020, 1, 15)) is False


def test_batch_records_a_failure_without_aborting_the_rest(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)

    def _lookup(as_of: date, **_kwargs: object) -> WikipediaRevisionRef:
        if as_of == date(2020, 1, 15):
            raise SourceAdapterError("simulated fetch failure")
        return WikipediaRevisionRef(
            page_title="List of S&P 500 companies", revision_id=2, revision_timestamp=REV_B_TS,
        )

    fetcher = _FakeFetcher({date(2020, 6, 15): (2, REV_B_TS)})
    result = run_wikipedia_revision_history_batch(
        [date(2020, 1, 15), date(2020, 6, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=_lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    outcomes = {row.as_of: row.outcome for row in result.rows}
    assert outcomes[date(2020, 1, 15)] == BATCH_OUTCOME_FAILED
    assert outcomes[date(2020, 6, 15)] == BATCH_OUTCOME_ADMITTED


def test_batch_refuses_duplicate_requested_dates(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({date(2020, 1, 15): (1, REV_A_TS)})
    with pytest.raises(SourceAdapterError, match="duplicate"):
        run_wikipedia_revision_history_batch(
            [date(2020, 1, 15), date(2020, 1, 15)], store=store,
            secondary_match_at_or_before=_always_full_agreement(),
            secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
            secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
            secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
            fetch_revision_at_or_before_fn=fetcher.lookup,
            fetch_revision_wikitext_fn=fetcher.wikitext,
            now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        )


# -- build_dataset_manifest: deterministic, reproducible, gap-aware ----------


def test_manifest_is_deterministic_across_rebuilds(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({
        date(2020, 1, 15): (1, REV_A_TS),
        date(2020, 6, 15): (2, REV_B_TS),
    })
    run_wikipedia_revision_history_batch(
        [date(2020, 1, 15), date(2020, 6, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    manifest_1 = build_dataset_manifest(store.entries())
    manifest_2 = build_dataset_manifest(store.entries())
    assert manifest_1.content_fingerprint == manifest_2.content_fingerprint
    assert manifest_1.date_coverage == (date(2020, 1, 15), date(2020, 6, 15))
    assert manifest_1.aggregate["admitted_count"] == 2
    assert manifest_1.aggregate["mean_agreement_ratio"] == pytest.approx(1.0)
    assert manifest_1.aggregate["distinct_secondary_source_ids"] == ["fja05680-sp500-github"]
    assert manifest_1.aggregate["distinct_secondary_full_file_checksums"] == [
        raw_artifact_checksum(_FULL_AGREEMENT_SECONDARY_CSV)
    ]


def test_manifest_records_gaps_for_requested_but_unadmitted_dates(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({date(2020, 1, 15): (1, REV_A_TS)})
    run_wikipedia_revision_history_batch(
        [date(2020, 1, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    manifest = build_dataset_manifest(
        store.entries(), requested_as_of_dates=[date(2020, 1, 15), date(2020, 12, 31)],
    )
    assert len(manifest.gaps) == 1
    assert "2020-12-31" in manifest.gaps[0]


def test_manifest_never_claims_eligibility_and_names_window_gap(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    manifest = build_dataset_manifest(store.entries())
    payload = manifest.to_dict()
    assert payload["classification"] == "SECONDARY_RESEARCH_PIT"
    assert payload["backtest_quality"] == "APPROXIMATE"
    assert payload["eligibility"] == {
        "authoritative_official_evidence": False,
        "eligible_for_promotion_without_validation": False,
        "substitute_for_historical_gics": False,
    }
    window_status = payload["window_definition_status"]
    assert isinstance(window_status, dict)
    assert window_status["status"] == "WINDOW_DEFINITION_REQUIRED"


def test_manifest_entries_carry_checksum_and_secondary_binding(tmp_path: Path) -> None:
    store = WikipediaDatasetStore(tmp_path)
    fetcher = _FakeFetcher({date(2020, 1, 15): (1, REV_A_TS)})
    run_wikipedia_revision_history_batch(
        [date(2020, 1, 15)], store=store,
        secondary_match_at_or_before=_always_full_agreement(),
        secondary_source_id="fja05680-sp500-github", secondary_source_url=_SECONDARY_SOURCE_URL,
        secondary_raw_bytes=_FULL_AGREEMENT_SECONDARY_CSV,
        secondary_fetched_at=datetime(2026, 9, 6, tzinfo=UTC),
        fetch_revision_at_or_before_fn=fetcher.lookup,
        fetch_revision_wikitext_fn=fetcher.wikitext,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )
    entries = store.entries()
    manifest = build_dataset_manifest(entries)
    payload = manifest.to_dict()
    records = payload["entries"]
    assert isinstance(records, list)
    assert len(records) == 1
    record = records[0]
    assert record["entry_checksum"] == entries[0].compute_checksum()
    assert record["secondary_full_file_checksum"] == raw_artifact_checksum(
        _FULL_AGREEMENT_SECONDARY_CSV
    )
    assert record["secondary_source_id"] == "fja05680-sp500-github"
    expected_matched_row_checksum = entries[0].secondary_provenance.matched_row_checksum
    assert record["secondary_matched_row_checksum"] == expected_matched_row_checksum


def test_manifest_fingerprint_distinguishes_different_partitions_with_the_same_ratio(
    tmp_path: Path,
) -> None:
    """Two admitted entries whose ``agreement_ratio`` happens to match but
    whose actual cross-check partition (and secondary artifact) differ must
    NOT collide into the same dataset-level fingerprint -- the fingerprint
    must commit to each entry's own ``entry_checksum``, not merely to the
    lossy summary ratio."""
    entry_a, _, _ = _entry_with_secondary(
        as_of=date(2020, 1, 15), revision_id=1, timestamp=REV_A_TS,
    )
    entry_b, _, _ = _entry_with_secondary(
        as_of=date(2020, 1, 15), revision_id=1, timestamp=REV_A_TS,
    )
    # Both are full agreement (ratio 1.0) against a DIFFERENT independent
    # secondary artifact (different source URL/fetch time is not enough on
    # its own -- change the retained bytes' checksum too).
    entry_b = replace(
        entry_b,
        secondary_provenance=replace(
            entry_b.secondary_provenance,
            full_file_checksum="sha256:" + "9" * 64,
        ),
    )
    assert entry_a.cross_check.agreement_ratio == entry_b.cross_check.agreement_ratio == 1.0
    assert entry_a.compute_checksum() != entry_b.compute_checksum()

    manifest_a = build_dataset_manifest([entry_a])
    manifest_b = build_dataset_manifest([entry_b])
    assert manifest_a.content_fingerprint != manifest_b.content_fingerprint


# -- WINDOW_DEFINITION_REQUIRED: a precise gap, never a fabricated window ----


def test_window_definition_required_is_precise_and_non_fabricating() -> None:
    assert WINDOW_DEFINITION_REQUIRED["status"] == "WINDOW_DEFINITION_REQUIRED"
    gap_text = WINDOW_DEFINITION_REQUIRED["gap"]
    assert isinstance(gap_text, str)
    assert "baseline-trend-breakout-v1" in gap_text
    assert "chronological" in gap_text  # describes the relative split, not a fabricated date
    completed = WINDOW_DEFINITION_REQUIRED["what_this_task_completed_instead"]
    assert isinstance(completed, str)
    assert "2023-12-31" in completed


def test_evaluation_record_references_batch_dataset_and_window_gap() -> None:
    batch_dataset = WIKIPEDIA_REVISION_HISTORY_EVALUATION["batch_dataset"]
    assert isinstance(batch_dataset, dict)
    assert batch_dataset["full_window_status"] is WINDOW_DEFINITION_REQUIRED
    next_task = WIKIPEDIA_REVISION_HISTORY_EVALUATION["next_bounded_task"]
    assert isinstance(next_task, str)
    assert "WINDOW_DEFINITION_REQUIRED" in next_task
    # Must never claim the full-window dataset is complete.
    assert "is NOT built" in next_task or "not built" in next_task.lower()
