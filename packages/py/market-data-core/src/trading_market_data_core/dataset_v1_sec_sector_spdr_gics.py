"""A free, dated, independent GICS-sector cross-check candidate: SEC EDGAR
N-PORT-P filings of the 11 "Select Sector SPDR" exchange-traded funds.

Task A-WP2-FREE-HISTORICAL-GICS-CROSSCHECK-EVAL. ``dataset_v1_wikipedia_pit``
already captures GICS Sector/Sub-Industry from the Wikipedia revision table
but explicitly could not admit it as SECONDARY research evidence:
``cross_check_sectors`` existed and was tested against fixtures, but "no
independent, dated, free GICS source has been identified" to run it against
for real (``WIKIPEDIA_REVISION_HISTORY_EVALUATION["sector_gics"]``). This
module evaluates one concrete candidate for that gap and, via
``scripts/run_sec_sector_spdr_gics_crosscheck_experiment.py``, is exercised
against real SEC EDGAR data to produce the one real bounded experiment this
task requires.

Why this candidate, and why it is legitimate:

* State Street's 11 "Select Sector SPDR" ETFs (XLK, XLY, XLP, XLE, XLF, XLV,
  XLI, XLC, XLB, XLRE, XLU) each hold exactly the constituents of one GICS
  Sector -- by construction, not by our assertion: every fund's own
  registered ``seriesName`` (e.g. "The Technology Select Sector SPDR Fund")
  states which sector it tracks, and this module reads that name FROM each
  fetched filing, it never assigns a sector to a fund from an external list.
* their quarterly holdings are disclosed via SEC Form N-PORT-P, a REAL,
  FREE, UNAUTHENTICATED, publicly documented SEC EDGAR filing (see
  ``https://www.sec.gov/edgar/sec-api-documentation``), fetched here through
  the existing, rate-limited, contact-declaring
  ``trading_market_data_core.dataset_v1_sources.SecEdgarClient`` -- no
  robots/auth/paywall bypass, the same access class already approved for
  every other SEC EDGAR path in this repository;
* every N-PORT-P filing carries the fund's own reported
  ``repPdDate`` (report period end date) -- a real, independently-verifiable
  "as of" date reported by the filer, not one this module asserts -- so the
  SAME no-look-ahead selection rule ``dataset_v1_wikipedia_pit.select_revision_as_of``
  already applies to MediaWiki revisions applies here: the latest filing at
  or before a decision date, never a later one
  (:func:`select_sector_spdr_snapshot_as_of`);
* it is INDEPENDENT of Wikipedia in every sense that matters: a different
  organization (State Street / SPDR fund complex), a different regulator-
  filed data lineage (SEC EDGAR, not a wiki edit), and a different mechanism
  entirely (real, disclosed portfolio holdings, not a hand-maintained table).

What this module is honest about NOT solving: SEC's N-PORT schema identifies
each holding by legal issuer NAME, CUSIP, ISIN and LEI -- **never** by ticker
symbol (verified against real fetched filings; see the docstring on
:func:`normalize_company_name`). Joining these holdings to Wikipedia's
ticker-keyed observations therefore requires an identity-resolution step,
implemented here as a DETERMINISTIC, GENERIC (never per-company) name
normalization -- never a hand-built company alias table, which the task's
governing rules explicitly forbid as a fabricated identity mapping. Where
normalization still leaves an AMBIGUOUS match (e.g. "Alphabet Inc" naming
both GOOG and GOOGL) or NO match at all (a real, common case: SEC's legal
name and Wikipedia's brand name genuinely differ, e.g. "Deere & Co" vs.
"John Deere"), the holding is recorded as unmatched/ambiguous and contributes
NOTHING to the resulting secondary-sector mapping -- never guessed. The real, bounded experiment run against 2023-12-31 resolved 482/503 Wikipedia tickers (95.83% coverage, above the canonical 90% floor) and found 482/482 sector agreements (100%), with 21 unresolved/ambiguous tickers explicitly retained as uncovered rather than guessed. See ``docs/poi/evidence/dataset-v1/sec-sector-spdr-gics/`` for the full checksum-bound record and ``dataset_v1_wikipedia_pit.WIKIPEDIA_REVISION_HISTORY_EVALUATION["sector_gics"]`` for the deliberately bounded admission: GICS Sector only, SECONDARY_RESEARCH_PIT / APPROXIMATE, never official/PIT_STRICT and never GICS Sub-Industry.

Every real network call in this module goes through the existing
``SecEdgarClient`` (rate-limited, fails closed without a declared contact
User-Agent via ``SEC_EDGAR_USER_AGENT``) and is exercised ONLY by
``scripts/run_sec_sector_spdr_gics_crosscheck_experiment.py``, never by a
test. This module's own classification is permanently
``SECONDARY_RESEARCH_PIT`` (never official) and it never flips
``asof_gics`` / ``pit_sp500_membership`` in ``dataset_v1.py`` or
``ASOF_GICS_BLOCKER`` in ``dataset_v1_real_ingestion.py`` -- both stay
``BLOCKED_EXTERNAL`` / ``LICENSE_REQUIRED`` / owner-gated regardless of
anything recorded here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from trading_market_data_core.dataset_v1_sources import (
    HttpBytesFetcher,
    SecEdgarClient,
    SourceAdapterError,
    parse_sec_submissions,
)
from trading_market_data_core.sp500_current_universe import raw_artifact_checksum

SEC_SECTOR_SPDR_GICS_SCHEMA_VERSION = "dataset-v1-sec-sector-spdr-gics-1.0.0"

#: This candidate can never become official evidence, regardless of outcome.
CLASSIFICATION = "SECONDARY_RESEARCH_PIT"

#: "SELECT SECTOR SPDR TRUST" -- a single SEC registrant (CIK) whose N-PORT-P
#: filings cover 11 separate registered fund SERIES, one per GICS Sector.
#: Verified 2026-09-07 against a real ``data.sec.gov/submissions/CIK0001064641.json``
#: fetch (``entityName`` "SELECT SECTOR SPDR TRUST"); not asserted from memory.
SELECT_SECTOR_SPDR_TRUST_CIK = "0001064641"

#: The 11 canonical GICS Sector names, as they appear verbatim in the
#: Wikipedia revision table this candidate is cross-checked against
#: (``dataset_v1_wikipedia_pit`` / ``sp500_wikitable``). Used only for a
#: module-load sanity check on the fund keyword table below.
GICS_SECTOR_NAMES = frozenset(
    {
        "Communication Services",
        "Consumer Discretionary",
        "Consumer Staples",
        "Energy",
        "Financials",
        "Health Care",
        "Industrials",
        "Information Technology",
        "Materials",
        "Real Estate",
        "Utilities",
    }
)

#: Maps a keyword found INSIDE a fetched filing's OWN ``<seriesName>`` (e.g.
#: "The Technology Select Sector SPDR Fund") to the canonical GICS Sector name
#: it corresponds to. This is a FUND-LEVEL correspondence declared by the
#: filer's own registered fund name -- never a per-company mapping this
#: module invents -- and is publicly documented by the fund complex itself
#: (https://www.sectorspdrs.com). Three of the eleven need an explicit
#: keyword-to-canonical-name translation because the fund's own short name
#: does not literally spell the GICS Sector name ("Financial" -> "Financials",
#: "Industrial" -> "Industrials", "Technology" -> "Information Technology");
#: the remaining eight match verbatim.
SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR: Mapping[str, str] = {
    "Communication Services": "Communication Services",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples",
    "Energy": "Energy",
    "Financial": "Financials",
    "Health Care": "Health Care",
    "Industrial": "Industrials",
    "Technology": "Information Technology",
    "Materials": "Materials",
    "Real Estate": "Real Estate",
    "Utilities": "Utilities",
}
if len(SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR) != 11:
    raise AssertionError("expected exactly 11 Select Sector SPDR fund keywords")
if set(SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR.values()) != GICS_SECTOR_NAMES:
    raise AssertionError(
        "Select Sector SPDR fund keyword table does not cover exactly the 11"
        " canonical GICS Sector names"
    )

_NPORT_NAMESPACE = "http://www.sec.gov/edgar/nport"

#: Only equity-common holdings carry a meaningful GICS Sector membership; a
#: fund also reports cash-sweep vehicles, repurchase agreements and other
#: non-equity positions (assetCat values other than "EC") which this module
#: excludes from identity resolution entirely -- a real, principled, GENERIC
#: filter (applies identically to every fund), never a per-holding judgment
#: call.
_EQUITY_COMMON_ASSET_CATEGORY = "EC"


# -- identity resolution: deterministic, generic, never per-company ----------

#: Generic corporate-suffix words dropped from BOTH sides of an identity
#: comparison. Every entry here is a class of business-entity designator
#: (legal form, holding-company boilerplate), never a specific company's
#: name or alias -- dropping "INC" does not depend on which company carries
#: it.
_GENERIC_LEGAL_SUFFIX_TOKENS = frozenset(
    {
        "INC", "CORP", "CORPORATION", "COMPANY", "THE", "LTD", "LIMITED",
        "PLC", "GROUP", "HOLDINGS", "LLC", "SA", "NV", "TRUST",
        "INTERNATIONAL", "INTL", "AND",
    }
)

#: Generic English business-abbreviation expansions, applied identically to
#: every name on both sides before suffix-stripping (e.g. "Lowe's Cos Inc"
#: and a Wikipedia "Lowe's Companies" both reduce to the same key). This is
#: NOT a company alias table: it maps an ABBREVIATION to its expansion, the
#: same way "Corp" is treated as equivalent to "Corporation" -- it has no
#: knowledge of which company the words belong to.
_GENERIC_ABBREVIATION_EXPANSIONS: Mapping[str, str] = {"COS": "COMPANIES", "CO": "COMPANY"}


def normalize_company_name(name: str) -> str:
    """A deterministic, GENERIC identity key for joining two independently
    formatted company-name sources (SEC EDGAR legal issuer names vs.
    Wikipedia's informal/brand names).

    Real, fetched evidence (2026-09-07, both against SEC EDGAR N-PORT-P
    filings and the already-recorded Wikipedia revision) is what shaped this
    function, not a hypothesis:

    * SEC issuer names frequently carry a trailing ``/<disambiguator>``
      fragment (e.g. ``"CDW Corp/DE"``, ``"TJX Cos Inc/The"``) -- a real,
      documented SEC EDGAR filer-name convention (disambiguating same-named
      registrants by state of incorporation or similar), stripped generically
      for every name, never one company's alone;
    * the two sources punctuate/space names differently (``"W. W. Grainger"``
      vs. ``"WW Grainger Inc"``; ``"Amazon.com Inc"`` vs. ``"Amazon"``) --
      collapsing ALL whitespace/punctuation into one contiguous token run
      resolves this generically;
    * ``"&"`` is spelled out ``"AND"`` on one side sometimes and dropped
      entirely as a suffix word on the other -- both are normalized away.

    What this function deliberately does NOT attempt: resolving a LEGAL name
    to an unrelated BRAND name (SEC's ``"Deere & Co"`` vs. Wikipedia's
    ``"John Deere"``; SEC's ``"International Business Machines Corp"`` vs.
    Wikipedia's ``"IBM"``). Building a table of those correspondences would be
    exactly the kind of hand-curated, per-company identity mapping this
    project's rules forbid fabricating; a name that only differs this way is
    accepted as a genuine coverage gap for this candidate, not patched around.

    Raises :class:`SourceAdapterError` on an empty/whitespace-only input or
    one that normalizes to nothing (fail closed rather than silently produce
    an empty join key that could ambiguously match many unrelated entries).
    """
    if not name or not name.strip():
        raise SourceAdapterError("cannot normalize an empty company name")
    text = name.strip()
    text = re.sub(r"/[A-Za-z]{2,12}$", "", text)
    text = text.upper()
    # Generic web-domain marker in legal issuer names (for example X.com Inc)
    # is formatting rather than corporate identity; strip it for every issuer.
    text = re.sub(r"\.COM\b", "", text)
    text = text.replace("&", " AND ")
    text = re.sub(r"\(.*?\)", "", text)  # drop parenthetical share-class annotations
    text = re.sub(r"[^A-Z0-9 ]", "", text)
    tokens = text.split()
    expanded = (_GENERIC_ABBREVIATION_EXPANSIONS.get(token, token) for token in tokens)
    kept = [token for token in expanded if token not in _GENERIC_LEGAL_SUFFIX_TOKENS]
    if not kept:
        raise SourceAdapterError(f"company name {name!r} normalized to nothing; refusing")
    return "".join(kept)


# -- SEC EDGAR N-PORT-P filing/holding types ----------------------------------


@dataclass(frozen=True, slots=True)
class SectorSpdrHolding:
    """One equity-common holding line from one fund's N-PORT-P filing."""

    issuer_name: str
    asset_category: str

    def __post_init__(self) -> None:
        if not self.issuer_name.strip():
            raise SourceAdapterError("a Sector SPDR holding requires a non-empty issuer name")
        if not self.asset_category.strip():
            raise SourceAdapterError("a Sector SPDR holding requires a non-empty asset category")


@dataclass(frozen=True, slots=True)
class SectorSpdrFilingRef:
    """One immutable, dated N-PORT-P filing for one Select Sector SPDR fund."""

    cik: str
    accession_number: str
    series_id: str
    series_name: str
    gics_sector: str
    report_period_date: date

    def to_dict(self) -> dict[str, object]:
        return {
            "cik": self.cik,
            "accession_number": self.accession_number,
            "series_id": self.series_id,
            "series_name": self.series_name,
            "gics_sector": self.gics_sector,
            "report_period_date": self.report_period_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SectorSpdrFilingFetch:
    """A parsed filing plus its exact raw ``primary_doc.xml`` bytes."""

    filing: SectorSpdrFilingRef
    holdings: tuple[SectorSpdrHolding, ...]
    raw_bytes: bytes
    raw_checksum: str

    def __post_init__(self) -> None:
        if raw_artifact_checksum(self.raw_bytes) != self.raw_checksum:
            raise SourceAdapterError(
                f"{self.filing.accession_number}: raw_checksum does not match the"
                " supplied raw bytes; refusing to bind an unauthenticated filing"
            )


def parse_nport_primary_doc(
    xml_bytes: bytes, *, cik: str, accession_number: str
) -> tuple[SectorSpdrFilingRef, tuple[SectorSpdrHolding, ...]]:
    """Parse one N-PORT-P ``primary_doc.xml`` payload. No network call.

    Reads ONLY what the filing itself asserts (``seriesId``/``seriesName``/
    ``repPdDate`` from ``genInfo``, one holding per ``invstOrSec`` element) --
    nothing is inferred or looked up elsewhere. Fails closed if the required
    fields are missing/malformed, or if the fund's OWN ``seriesName`` does not
    contain exactly one of the 11 known Select Sector SPDR fund keywords (an
    unrecognised series is refused, never silently guessed at a sector).
    """
    try:
        root = ET.fromstring(xml_bytes)  # noqa: S314 -- trusted SEC EDGAR content, not untrusted user XML
    except ET.ParseError as error:
        raise SourceAdapterError(f"{accession_number}: unparseable N-PORT XML: {error}") from error

    def _find_text(path: str) -> str | None:
        element = root.find(f".//{{{_NPORT_NAMESPACE}}}{path}")
        return element.text.strip() if element is not None and element.text else None

    series_id = _find_text("seriesId")
    series_name = _find_text("seriesName")
    rep_pd_date_text = _find_text("repPdDate")
    if not series_id or not series_name or not rep_pd_date_text:
        raise SourceAdapterError(
            f"{accession_number}: N-PORT filing is missing seriesId/seriesName/repPdDate"
        )
    try:
        report_period_date = date.fromisoformat(rep_pd_date_text)
    except ValueError as error:
        raise SourceAdapterError(
            f"{accession_number}: unparseable repPdDate {rep_pd_date_text!r}: {error}"
        ) from error

    matched_sectors = {
        sector
        for keyword, sector in SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR.items()
        if keyword in series_name
    }
    if len(matched_sectors) != 1:
        raise SourceAdapterError(
            f"{accession_number}: seriesName {series_name!r} does not match exactly"
            f" one known Select Sector SPDR fund keyword (matched {sorted(matched_sectors)});"
            " refusing to guess a GICS Sector for an unrecognised series"
        )
    gics_sector = next(iter(matched_sectors))

    holdings: list[SectorSpdrHolding] = []
    for element in root.iter(f"{{{_NPORT_NAMESPACE}}}invstOrSec"):
        name_element = element.find(f"{{{_NPORT_NAMESPACE}}}name")
        asset_cat_element = element.find(f"{{{_NPORT_NAMESPACE}}}assetCat")
        if name_element is None or not (name_element.text or "").strip():
            continue  # a holding with no reported name cannot be identity-resolved
        asset_category = (asset_cat_element.text or "").strip() if asset_cat_element is not None else ""
        if asset_category != _EQUITY_COMMON_ASSET_CATEGORY:
            continue  # cash sweeps / repos / derivatives carry no GICS sector membership
        holdings.append(
            SectorSpdrHolding(issuer_name=name_element.text.strip(), asset_category=asset_category)
        )

    filing = SectorSpdrFilingRef(
        cik=cik, accession_number=accession_number, series_id=series_id,
        series_name=series_name, gics_sector=gics_sector, report_period_date=report_period_date,
    )
    return filing, tuple(holdings)


# -- discovery + fetch (owner/agent-run only; never exercised in a test) -----


def discover_nport_candidate_accessions(
    *,
    cik: str = SELECT_SECTOR_SPDR_TRUST_CIK,
    client: SecEdgarClient,
) -> tuple[tuple[str, date], ...]:
    """Every ``NPORT-P`` accession number on record for ``cik``, with its
    (untrusted -- see below) SEC-reported filing date, most recent first.

    This is discovery only: SEC's submissions index tells us WHICH accession
    numbers exist and roughly when they were filed, nothing more -- it is
    NEVER trusted for the fund identity or the report period date that
    actually matter for no-look-ahead selection. Those are always re-derived
    from each candidate's own fetched ``primary_doc.xml``
    (:func:`select_sector_spdr_snapshot_as_of`), mirroring the same
    "verify, never trust the discovery index" posture
    ``dataset_v1_wikipedia_pit.fetch_revision_wikitext`` already applies to
    MediaWiki's revision lookup.
    """
    raw, parsed = client.submissions(cik)
    del raw
    records = parse_sec_submissions(b"", parsed)
    candidates = [
        (record.accession, record.filing_date) for record in records if record.form == "NPORT-P"
    ]
    candidates.sort(key=lambda item: item[1], reverse=True)
    return tuple(candidates)


def fetch_nport_filing(
    accession_number: str,
    *,
    cik: str = SELECT_SECTOR_SPDR_TRUST_CIK,
    client: SecEdgarClient,
    bytes_fetcher: HttpBytesFetcher,
) -> SectorSpdrFilingFetch:
    """Fetch and parse ONE N-PORT-P filing's ``primary_doc.xml`` for real."""
    accession_nodash = accession_number.replace("-", "")
    cik_int = str(int(cik))
    relative_path = f"edgar/data/{cik_int}/{accession_nodash}/primary_doc.xml"
    raw = client.archives_document(relative_path, fetcher=bytes_fetcher)
    filing, holdings = parse_nport_primary_doc(raw, cik=cik, accession_number=accession_number)
    return SectorSpdrFilingFetch(
        filing=filing, holdings=holdings, raw_bytes=raw, raw_checksum=raw_artifact_checksum(raw)
    )


@dataclass(frozen=True, slots=True)
class SectorSpdrSnapshot:
    """One filing PER Select Sector SPDR fund, each independently selected as
    the latest at or before ``as_of`` -- ALL 11, never a partial set silently
    treated as complete."""

    as_of: date
    filings: tuple[SectorSpdrFilingFetch, ...]

    def __post_init__(self) -> None:
        sectors = {fetch.filing.gics_sector for fetch in self.filings}
        if sectors != GICS_SECTOR_NAMES:
            missing = sorted(GICS_SECTOR_NAMES - sectors)
            raise SourceAdapterError(
                f"Sector SPDR snapshot for {self.as_of.isoformat()} is missing"
                f" {len(missing)} of 11 funds ({missing}); refusing to build a"
                " partial cross-check source and call it complete"
            )
        for fetch in self.filings:
            if fetch.filing.report_period_date > self.as_of:
                raise SourceAdapterError(
                    f"{fetch.filing.accession_number} reports period"
                    f" {fetch.filing.report_period_date.isoformat()}, after as_of"
                    f" {self.as_of.isoformat()}; refusing a later filing for an"
                    " earlier decision (no look-ahead)"
                )


def select_sector_spdr_snapshot_as_of(
    as_of: date,
    *,
    cik: str = SELECT_SECTOR_SPDR_TRUST_CIK,
    client: SecEdgarClient,
    bytes_fetcher: HttpBytesFetcher,
    discover_fn: Callable[..., tuple[tuple[str, date], ...]] = discover_nport_candidate_accessions,
    fetch_fn: Callable[..., SectorSpdrFilingFetch] = fetch_nport_filing,
) -> SectorSpdrSnapshot:
    """Resolve, for EACH of the 11 known funds, the latest real N-PORT-P
    filing whose OWN ``repPdDate`` is at or before ``as_of`` -- never a later
    one (mirrors ``dataset_v1_wikipedia_pit.select_revision_as_of`` exactly).

    Candidate accession numbers are discovered cheaply from the submissions
    index (:func:`discover_nport_candidate_accessions`, most recent filing
    date first) and inspected in that order; a filing already resolved for
    one fund's series id is never re-fetched for another. Fails closed
    (:class:`SectorSpdrSnapshot`'s constructor) if fewer than all 11 funds
    are resolved -- a partial set is never silently treated as complete.
    """
    resolved: dict[str, SectorSpdrFilingFetch] = {}
    for accession_number, _filing_date in discover_fn(cik=cik, client=client):
        if len(resolved) == len(GICS_SECTOR_NAMES):
            break
        try:
            fetched = fetch_fn(
                accession_number, cik=cik, client=client, bytes_fetcher=bytes_fetcher
            )
        except SourceAdapterError:
            continue  # an unrelated/unparseable filing under this CIK; skip, don't abort
        if fetched.filing.report_period_date > as_of:
            continue  # a later filing for a fund not yet resolved: keep looking further back
        existing = resolved.get(fetched.filing.series_id)
        if existing is not None and existing.filing.report_period_date >= fetched.filing.report_period_date:
            continue  # already have an equal-or-later-but-still-valid observation for this fund
        resolved[fetched.filing.series_id] = fetched
    return SectorSpdrSnapshot(as_of=as_of, filings=tuple(resolved.values()))


# -- identity resolution + secondary-sector mapping (pure, offline, tested) --


@dataclass(frozen=True, slots=True)
class UnmatchedHolding:
    """One Sector SPDR holding that could NOT be confidently joined to a
    Wikipedia ticker -- recorded, never silently dropped."""

    gics_sector: str
    issuer_name: str
    normalized_name: str
    reason: str  # "NO_MATCH" or "AMBIGUOUS_MATCH"
    candidate_tickers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "gics_sector": self.gics_sector,
            "issuer_name": self.issuer_name,
            "normalized_name": self.normalized_name,
            "reason": self.reason,
            "candidate_tickers": list(self.candidate_tickers),
        }


@dataclass(frozen=True, slots=True)
class SectorIdentityResolution:
    """The result of joining a :class:`SectorSpdrSnapshot` to a Wikipedia
    snapshot's ticker-keyed observations by normalized company name."""

    secondary_sectors: Mapping[str, str]
    unmatched_holdings: tuple[UnmatchedHolding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "resolved_ticker_count": len(self.secondary_sectors),
            "unmatched_holding_count": len(self.unmatched_holdings),
            "unmatched_holdings": [item.to_dict() for item in self.unmatched_holdings],
        }


def build_secondary_sector_mapping(
    snapshot: SectorSpdrSnapshot, *, wikipedia_observations: Sequence[tuple[str, str]]
) -> SectorIdentityResolution:
    """Deterministically join Sector SPDR holdings to Wikipedia tickers by
    normalized issuer/company name.

    ``wikipedia_observations`` is a sequence of ``(ticker, name)`` pairs
    (pass ``[(o.ticker, o.name) for o in wikipedia_snapshot.observations]`` --
    kept as plain tuples rather than importing
    :class:`dataset_v1_wikipedia_pit.WikipediaConstituentObservation` so this
    module stays independently testable with plain fixtures).

    FAIL CLOSED on ambiguous identity: if a normalized name matches more than
    one Wikipedia ticker (real case: dual share classes -- ``"Alphabet Inc"``
    matches both GOOG and GOOGL; ``"Fox Corp"`` matches both FOX and FOXA;
    ``"News Corp"`` matches both NWS and NWSA), the holding contributes
    NOTHING to the mapping and is recorded with ``reason="AMBIGUOUS_MATCH"``.
    A holding whose normalized name has no Wikipedia match at all is recorded
    with ``reason="NO_MATCH"`` -- never guessed, never silently dropped.
    """
    wiki_by_normalized_name: dict[str, list[str]] = {}
    for ticker, name in wikipedia_observations:
        key = normalize_company_name(name)
        wiki_by_normalized_name.setdefault(key, []).append(ticker)

    secondary_sectors: dict[str, str] = {}
    unmatched: list[UnmatchedHolding] = []
    for fetch in snapshot.filings:
        for holding in fetch.holdings:
            normalized = normalize_company_name(holding.issuer_name)
            matches = wiki_by_normalized_name.get(normalized, [])
            # If exact normalized legal-name equality is unavailable, allow only
            # a UNIQUE long-prefix relation. This generically reconciles a brand
            # name with the same issuer plus a descriptive legal tail (e.g.
            # "Costco" vs "Costco Wholesale Corp") without any company aliases.
            # Short keys are excluded to avoid accidental joins such as single-
            # letter/two-word abbreviations. Multiple candidates remain ambiguous.
            if not matches:
                prefix_candidates = []
                for wiki_key, tickers in wiki_by_normalized_name.items():
                    shorter = min(len(normalized), len(wiki_key))
                    if shorter >= 6 and (normalized.startswith(wiki_key) or wiki_key.startswith(normalized)):
                        prefix_candidates.extend(tickers)
                matches = sorted(set(prefix_candidates))
            if len(matches) == 1:
                secondary_sectors[matches[0]] = fetch.filing.gics_sector
            elif len(matches) > 1:
                unmatched.append(
                    UnmatchedHolding(
                        gics_sector=fetch.filing.gics_sector, issuer_name=holding.issuer_name,
                        normalized_name=normalized, reason="AMBIGUOUS_MATCH",
                        candidate_tickers=tuple(sorted(matches)),
                    )
                )
            else:
                unmatched.append(
                    UnmatchedHolding(
                        gics_sector=fetch.filing.gics_sector, issuer_name=holding.issuer_name,
                        normalized_name=normalized, reason="NO_MATCH",
                    )
                )
    return SectorIdentityResolution(
        secondary_sectors=secondary_sectors, unmatched_holdings=tuple(unmatched)
    )


__all__ = [
    "CLASSIFICATION",
    "GICS_SECTOR_NAMES",
    "SEC_SECTOR_SPDR_GICS_SCHEMA_VERSION",
    "SELECT_SECTOR_SPDR_FUND_KEYWORD_TO_GICS_SECTOR",
    "SELECT_SECTOR_SPDR_TRUST_CIK",
    "SectorIdentityResolution",
    "SectorSpdrFilingFetch",
    "SectorSpdrFilingRef",
    "SectorSpdrHolding",
    "SectorSpdrSnapshot",
    "UnmatchedHolding",
    "build_secondary_sector_mapping",
    "discover_nport_candidate_accessions",
    "fetch_nport_filing",
    "normalize_company_name",
    "parse_nport_primary_doc",
    "select_sector_spdr_snapshot_as_of",
]
