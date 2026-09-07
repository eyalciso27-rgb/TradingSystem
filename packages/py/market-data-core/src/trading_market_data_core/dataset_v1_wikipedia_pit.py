"""A free, contemporaneous-revision SECONDARY point-in-time research path.

ChatGPT review of the A-WP2 canonical-state reconciliation (2026-09-03) found
that ``ASOF_GICS_BLOCKER`` (``dataset_v1_real_ingestion.py``) drew a
categorical "no free path" conclusion without evaluating one specific
candidate: MediaWiki's own, public, immutable **revision history** for the
"List of S&P 500 companies" article, as distinct from scraping the CURRENT
page (which is what ``sp500_current_universe.py`` does and which carries no
effective/knowledge date at all). A follow-up review (2026-09-06) then found
two gaps in that first pass, both closed here: (1) the path had been proven
only against fixtures, never a real MediaWiki revision -- see
``scripts/run_wikipedia_revision_history_experiment.py`` and the recorded
result under ``docs/poi/evidence/dataset-v1/wikipedia-revision-history/`` for
the real, bounded experiment now performed; (2) the evaluation record claimed
a bounded path for both membership AND GICS/sector while only membership had
an actual independent cross-check -- the claim is now split explicitly (see
"Scope: membership vs. GICS/sector" below) and provenance in
:func:`fetch_revision_wikitext` is independently verified, not trusted.

The distinction that makes this a genuine PIT candidate, not a repeat of the
"free datasets only carry today's values" problem:

* every MediaWiki revision is immutable once saved, and the API returns its
  own ``revid`` and ``timestamp`` alongside the exact wikitext that existed at
  that revision — a real, independently-verifiable knowledge date, not one we
  assert ourselves; :func:`fetch_revision_wikitext` re-derives both from
  MediaWiki's OWN metadata for the exact revision id
  (:func:`_verify_revision_metadata`) rather than trusting a caller's claim,
  so a wrong page or a fabricated/stale timestamp is rejected fail closed
  before it could be bound to real content;
* binding a historical decision date to the *latest revision at or before*
  that date (never a later one — see :func:`select_revision_as_of`) means the
  observation reflects what the page asserted *at the time*, not today's
  page projected backward.

A third review (A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD, same day) then
asked for the one-date experiment to become a real multi-date dataset. Two
things were verified before writing any of that code: (1) whether an
owner-approved ABSOLUTE start/end date for the frozen strategy's full
backtest window already existed anywhere on main -- it does not; only
RELATIVE/proportional temporal structure is frozen (60/20/20 chronological
splits, 60-month-context/12-month-evaluation/12-month-step walk-forward:
``docs/strategies/baseline-trend-breakout-v1.yaml``); and (2) that inventing
one would be exactly the kind of fabricated decision this project's rules
forbid. So this module adds the reusable, deterministic, resumable and
idempotent BATCH machinery (:class:`WikipediaDatasetEntry`,
:class:`WikipediaDatasetStore`, :func:`run_wikipedia_revision_history_batch`,
:func:`build_dataset_manifest`) and its offline tests, but does not fetch a
fabricated set of "the" backtest-window dates: :data:`WINDOW_DEFINITION_REQUIRED`
records that gap precisely, and ``WIKIPEDIA_REVISION_HISTORY_EVALUATION``
carries it under ``"batch_dataset"`` so it cannot be silently dropped. The one
already-recorded real experiment (2023-12-31) is unchanged and is not
regenerated.

Scope: membership vs. GICS/sector. The same table that carries tickers also
carries the GICS Sector / GICS Sub-Industry columns already parsed by
:mod:`sp500_wikitable`, so every :class:`WikipediaRevisionSnapshot` CAPTURES
both. Membership is independently cross-checked by :func:`cross_check_membership`.
As of 2026-09-07, GICS **Sector** is also independently cross-checked for the
bounded 2023-12-31 experiment against all 11 Select Sector SPDR SEC EDGAR
N-PORT-P filings: 482/503 tickers are confidently covered and all 482 agree.
That admits Sector only as ``SECONDARY_RESEARCH_PIT`` / ``APPROXIMATE``; GICS
Sub-Industry and official/PIT_STRICT company-level history remain unadmitted.
:func:`cross_check_sectors` enforces the non-weakenable coverage/agreement gates.

What this module explicitly does NOT do, per the owner decisions and
ADR-0017 (repeated here because it is the entire point of keeping this
separate from the official capability machinery in ``dataset_v1.py`` /
``dataset_v1_real_ingestion.py``):

* it never promotes this evidence to ``OFFICIAL_PUBLIC`` or "official GICS" —
  every snapshot this module produces is permanently
  ``classification == "SECONDARY_RESEARCH_PIT"``;
* it never flips the ``asof_gics`` or ``pit_sp500_membership`` capability
  states in ``dataset_v1.py`` — those stay ``BLOCKED_EXTERNAL`` /
  ``LICENSE_REQUIRED`` for the *official, PIT_STRICT* requirement regardless
  of anything recorded here (see ``ASOF_GICS_BLOCKER`` and
  ``WIKIPEDIA_REVISION_HISTORY_EVALUATION`` below);
* it never accepts a single, uncorroborated revision as usable research
  evidence: :func:`cross_check_membership` is mandatory and every
  disagreement it finds is recorded, never silently dropped;
* it never cross-checks a revision timestamped after the decision date being
  checked: :func:`cross_check_membership` and :func:`cross_check_sectors`
  both re-enforce the same no-look-ahead rule :func:`select_revision_as_of`
  applies at selection time, so a later immutable revision cannot be handed
  to either function directly and labeled/corroborated for an earlier
  ``as_of``;
* it never lets a caller weaken the mandatory corroboration gates: both
  cross-check functions' ``minimum_*_ratio`` overrides are bounded to
  ``[canonical_minimum, 1.0]`` and refuse anything outside that range —
  the parameter exists to tighten a check in a test, never to loosen one;
* it never admits GICS/sector values as corroborated evidence without an
  actual, independent, dated cross-check having been run
  (:func:`cross_check_sectors`) — capturing them is not the same as admitting
  them, and this module never conflates the two;
* it never fabricates a revision, a timestamp or a checksum: every
  constructor here recomputes what it can verify and fails closed
  (:class:`trading_market_data_core.dataset_v1_sources.SourceAdapterError`)
  on anything missing, mismatched, or out of plausible range;
* it never fetches anything during a test run: :func:`fetch_revision_at_or_before`,
  :func:`fetch_revision_metadata` and :func:`fetch_revision_wikitext` are
  owner/agent-run real network calls against MediaWiki's public, documented
  API (no authentication, no bot-protection bypass — this is the same class
  of access already used, and already accepted, for the CURRENT-universe
  refresh in ``sp500_current_universe.py``); unit tests parse recorded
  wikitext/JSON strings and never call them. The one exception is the bounded
  real experiment recorded by ``scripts/run_wikipedia_revision_history_experiment.py``,
  run once outside of CI/tests to produce the evidence this module's claim
  depends on.

Net effect on M3 (see ``docs/roadmaps/MVP_DELIVERY_PLAN.md`` §5): the official
historical S&P membership and official historical GICS gaps remain
WAITING_EXTERNAL / owner-gated, unchanged. A free, legitimate, cross-checked
SECONDARY_RESEARCH_PIT path now exists FOR MEMBERSHIP, backed by one real
recorded experiment, and is usable for ``BacktestQuality.APPROXIMATE``
research (ADR-0017) — it is not blocked, and it does not block the official
gap from staying separately tracked. GICS/sector from this source remains
unevaluated until an independent dated source is found.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_sources import SourceAdapterError, SourceAuthority
from trading_market_data_core.sp500_current_universe import (
    DEFAULT_USER_AGENT,
    MAX_PLAUSIBLE_CONSTITUENTS,
    MIN_PLAUSIBLE_CONSTITUENTS,
    raw_artifact_checksum,
)
from trading_market_data_core.sp500_wikitable import (
    ParseDiagnostics,
    TableParseError,
    parse_wikitext_table,
)

WIKIPEDIA_PIT_SCHEMA_VERSION = "dataset-v1-wikipedia-pit-1.0.0"

#: Constant, never overridable: this evidence class can never become official.
CLASSIFICATION = "SECONDARY_RESEARCH_PIT"

DEFAULT_PAGE_TITLE = "List of S&P 500 companies"

#: ``action=parse`` accepts ``oldid`` in place of ``page``: it returns the
#: identical response shape (``parse.wikitext`` / ``parse.revid`` /
#: ``parse.title``) but for the wikitext AS IT STOOD at that specific,
#: immutable revision rather than the current one.
WIKIPEDIA_REVISION_PARSE_API_URL = (
    "https://en.wikipedia.org/w/api.php"
    "?action=parse&oldid={revision_id}&prop=wikitext|revid&format=json&formatversion=2"
)

#: ``action=query&prop=revisions`` with ``rvdir=older`` and ``rvstart`` finds
#: the most recent revision at or before a timestamp -- exactly the "no
#: look-ahead" selection this module needs, done by the API itself so a
#: caller never has to enumerate history.
WIKIPEDIA_REVISION_LOOKUP_API_URL = (
    "https://en.wikipedia.org/w/api.php"
    "?action=query&prop=revisions&titles={page_title}"
    "&rvlimit=1&rvdir=older&rvstart={rvstart}&rvprop=ids|timestamp"
    "&format=json&formatversion=2"
)

#: ``action=query&prop=revisions&revids=`` returns MediaWiki's OWN record of
#: which page and what timestamp one specific revision id belongs to --
#: independent of, and used to verify, whatever a caller claims. This is what
#: makes provenance verified rather than trusted (see
#: :func:`_verify_revision_metadata`).
WIKIPEDIA_REVISION_METADATA_API_URL = (
    "https://en.wikipedia.org/w/api.php"
    "?action=query&prop=revisions&revids={revision_id}"
    "&rvprop=ids|timestamp&format=json&formatversion=2"
)


@dataclass(frozen=True, slots=True)
class WikipediaRevisionRef:
    """One immutable, timestamped MediaWiki revision. Never invented."""

    page_title: str
    revision_id: int
    revision_timestamp: datetime

    def __post_init__(self) -> None:
        if not self.page_title.strip():
            raise SourceAdapterError("a Wikipedia revision reference requires a page title")
        if self.revision_id <= 0:
            raise SourceAdapterError("a Wikipedia revision id must be a positive integer")
        if self.revision_timestamp.tzinfo is None:
            raise SourceAdapterError(
                "a Wikipedia revision timestamp must be timezone-aware (it is a real,"
                " API-reported instant, never a naive guess)"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "page_title": self.page_title,
            "revision_id": self.revision_id,
            "revision_timestamp": self.revision_timestamp.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WikipediaConstituentObservation:
    """One row's ticker/GICS state as recorded in ONE dated revision.

    SECONDARY_RESEARCH_PIT only. Never official S&P/GICS truth.
    """

    ticker: str
    name: str
    gics_sector: str | None = None
    gics_sub_industry: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "gics_sector": self.gics_sector,
            "gics_sub_industry": self.gics_sub_industry,
        }


@dataclass(frozen=True, slots=True)
class WikipediaRevisionSnapshot:
    """The constituent/GICS table as it read at one specific, dated revision.

    Distinct from :class:`trading_market_data_core.sp500_current_universe.ConstituentSnapshot`
    on purpose: that type structurally REFUSES ``is_point_in_time_history=True``
    (it is the CURRENT-only forward archive). This type is exactly the
    opposite claim -- point-in-time, but SECONDARY and never official -- so it
    is its own type rather than a repurposed field on that one.
    """

    revision: WikipediaRevisionRef
    raw_checksum: str
    observations: tuple[WikipediaConstituentObservation, ...]
    source_authority: str = SourceAuthority.PUBLIC_SECONDARY.value
    classification: str = CLASSIFICATION
    parse_diagnostics: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.classification != CLASSIFICATION:
            raise SourceAdapterError(
                "a Wikipedia revision snapshot can never claim any classification"
                f" other than {CLASSIFICATION!r}"
            )
        if self.source_authority != SourceAuthority.PUBLIC_SECONDARY.value:
            raise SourceAdapterError(
                "a Wikipedia revision snapshot can never claim any source_authority"
                f" other than {SourceAuthority.PUBLIC_SECONDARY.value!r}"
            )
        digest = self.raw_checksum.removeprefix("sha256:")
        if digest == self.raw_checksum or len(digest) != 64:
            raise SourceAdapterError(
                "a Wikipedia revision snapshot requires a real sha256: raw checksum"
            )
        if not self.observations:
            raise SourceAdapterError("a Wikipedia revision snapshot has no observations")
        if not (MIN_PLAUSIBLE_CONSTITUENTS <= len(self.observations) <= MAX_PLAUSIBLE_CONSTITUENTS):
            raise SourceAdapterError(
                f"revision {self.revision.revision_id} carries"
                f" {len(self.observations)} observations, outside the plausible"
                f" range {MIN_PLAUSIBLE_CONSTITUENTS}..{MAX_PLAUSIBLE_CONSTITUENTS}"
                " (fail closed: a parse accident must never be mistaken for an"
                " index)"
            )
        tickers = [observation.ticker for observation in self.observations]
        if len(set(tickers)) != len(tickers):
            raise SourceAdapterError(
                f"revision {self.revision.revision_id} contains duplicate tickers"
            )

    def tickers(self) -> frozenset[str]:
        return frozenset(observation.ticker for observation in self.observations)

    def gics_sector_of(self, ticker: str) -> str | None:
        for observation in self.observations:
            if observation.ticker == ticker:
                return observation.gics_sector
        return None

    def verify_raw_bytes(self, raw_bytes: bytes) -> bool:
        """Re-authenticate this snapshot's checksum claim against real bytes.

        Never trust the stored ``raw_checksum`` label on its own -- this is
        what a later re-verification (e.g. before using an archived
        observation) must call.
        """
        return raw_artifact_checksum(raw_bytes) == self.raw_checksum

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": WIKIPEDIA_PIT_SCHEMA_VERSION,
            "classification": self.classification,
            "source_authority": self.source_authority,
            "revision": self.revision.to_dict(),
            "raw_checksum": self.raw_checksum,
            "observation_count": len(self.observations),
            "observations": [
                o.to_dict() for o in sorted(self.observations, key=lambda o: o.ticker)
            ],
            "parse_diagnostics": list(self.parse_diagnostics),
            "eligibility": {
                "authoritative_official_evidence": False,
                "eligible_for_promotion_without_validation": False,
                "substitute_for_historical_gics": False,
            },
        }


def parse_revision_snapshot(
    wikitext: str,
    *,
    raw_bytes: bytes,
    revision: WikipediaRevisionRef,
) -> WikipediaRevisionSnapshot:
    """Parse one revision's wikitext into a dated, checksum-bound snapshot.

    ``raw_bytes`` is the actual payload bytes the ``wikitext`` string was
    decoded from; the checksum is always recomputed from them, never taken on
    trust from a caller-supplied label -- there is nothing to mismatch because
    this function is the one place that computes it.
    """
    if "{|" not in wikitext:
        raise SourceAdapterError(
            f"revision {revision.revision_id} carries no wikitext table; refusing to parse"
        )
    try:
        rows, diagnostics = parse_wikitext_table(wikitext)
    except TableParseError as error:
        raise SourceAdapterError(
            f"revision {revision.revision_id} wikitext is unreadable: {error}"
        ) from error
    observations = tuple(
        WikipediaConstituentObservation(
            ticker=row.ticker,
            name=row.name,
            gics_sector=row.gics_sector,
            gics_sub_industry=row.gics_sub_industry,
        )
        for row in rows
    )
    diagnostic_lines = diagnostics.lines() if isinstance(diagnostics, ParseDiagnostics) else ()
    return WikipediaRevisionSnapshot(
        revision=revision,
        raw_checksum=raw_artifact_checksum(raw_bytes),
        observations=observations,
        parse_diagnostics=diagnostic_lines,
    )


def select_revision_as_of(
    observations: Sequence[WikipediaRevisionSnapshot], as_of: date
) -> WikipediaRevisionSnapshot:
    """The latest revision observed AT OR BEFORE ``as_of`` -- never a later one.

    This is the no-look-ahead guard: a historical decision on ``as_of`` may
    only see what the page asserted by then. A revision timestamped after
    ``as_of`` is excluded even if it is the "closest" one chronologically --
    using it would be projecting a later revision backward onto an earlier
    decision, exactly the failure mode this whole module exists to avoid.
    """
    eligible = [
        observation
        for observation in observations
        if observation.revision.revision_timestamp.date() <= as_of
    ]
    if not eligible:
        raise SourceAdapterError(
            f"no Wikipedia revision was observed at or before {as_of.isoformat()};"
            " refusing to use a later revision for an earlier decision (no look-ahead)"
        )
    return max(eligible, key=lambda observation: observation.revision.revision_timestamp)


@dataclass(frozen=True, slots=True)
class CrossCheckResult:
    """Agreement/disagreement between the revision snapshot and one independent source.

    Every disagreement is recorded, never silently dropped -- this is what
    makes ``corroborated`` an evidence-bounded claim rather than an assertion.
    """

    as_of: date
    secondary_source_id: str
    corroborated: bool
    agreement_ratio: float
    agreements: tuple[str, ...]
    only_in_wikipedia: tuple[str, ...]
    only_in_secondary: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "secondary_source_id": self.secondary_source_id,
            "corroborated": self.corroborated,
            "agreement_ratio": self.agreement_ratio,
            "agreement_count": len(self.agreements),
            "only_in_wikipedia": list(self.only_in_wikipedia),
            "only_in_secondary": list(self.only_in_secondary),
            "detail": self.detail,
        }


#: Below this agreement ratio the Wikipedia-revision observation is NOT
#: corroborated and must not be used, even for APPROXIMATE research. Set well
#: under 1.0 because independent free sources legitimately disagree at the
#: margin (a same-week add/drop, an inactive/relisted ticker) without either
#: one being wrong; a large disagreement is what must fail closed.
MINIMUM_CROSS_CHECK_AGREEMENT_RATIO = 0.98


def cross_check_membership(
    *,
    wikipedia: WikipediaRevisionSnapshot,
    secondary_tickers: frozenset[str],
    secondary_source_id: str,
    as_of: date,
    minimum_agreement_ratio: float = MINIMUM_CROSS_CHECK_AGREEMENT_RATIO,
) -> CrossCheckResult:
    """Corroborate a revision snapshot against one independent SECONDARY source.

    Mandatory before any Wikipedia-revision evidence is used for research: a
    single, uncorroborated community source is never sufficient on its own
    (this mirrors the two-independent-source rule already enforced on the
    offline historical-source registry,
    ``config/data-sources/sp500-historical-source-registry.json``).

    Fails closed, before any comparison is attempted, on two distinct misuse
    classes:

    * ``minimum_agreement_ratio`` outside ``[MINIMUM_CROSS_CHECK_AGREEMENT_RATIO,
      1.0]`` -- the parameter exists for tests to tighten the gate, never to
      weaken it. A caller passing 0.0 (or any value below the canonical
      minimum) would silently corroborate evidence the canonical threshold
      would have refused; that is rejected outright rather than honoured;
    * ``wikipedia.revision.revision_timestamp`` dated AFTER ``as_of`` -- this
      is the same no-look-ahead rule :func:`select_revision_as_of` enforces
      when choosing a revision; enforcing it again here means a caller cannot
      bypass it by handing this function a later revision directly and
      claiming an earlier decision date for it.
    """
    if not (MINIMUM_CROSS_CHECK_AGREEMENT_RATIO <= minimum_agreement_ratio <= 1.0):
        raise SourceAdapterError(
            f"minimum_agreement_ratio={minimum_agreement_ratio!r} would weaken the"
            " mandatory corroboration gate; it must be between"
            f" MINIMUM_CROSS_CHECK_AGREEMENT_RATIO ({MINIMUM_CROSS_CHECK_AGREEMENT_RATIO})"
            " and 1.0 inclusive (fail closed)"
        )
    if wikipedia.revision.revision_timestamp.date() > as_of:
        raise SourceAdapterError(
            f"revision {wikipedia.revision.revision_id} is timestamped"
            f" {wikipedia.revision.revision_timestamp.date().isoformat()}, after the"
            f" decision date {as_of.isoformat()}; refusing to cross-check or label a"
            " later revision for an earlier decision (no look-ahead)"
        )
    if not secondary_tickers:
        raise SourceAdapterError(
            "cross-check requires a non-empty independent secondary ticker set;"
            " an empty set is not corroboration, it is the absence of a check"
        )
    wiki_tickers = wikipedia.tickers()
    agreements = wiki_tickers & secondary_tickers
    only_wiki = wiki_tickers - secondary_tickers
    only_secondary = secondary_tickers - wiki_tickers
    union = wiki_tickers | secondary_tickers
    ratio = len(agreements) / len(union) if union else 0.0
    corroborated = ratio >= minimum_agreement_ratio
    detail = (
        f"{len(agreements)} agree, {len(only_wiki)} only in Wikipedia revision"
        f" {wikipedia.revision.revision_id}, {len(only_secondary)} only in"
        f" {secondary_source_id} (agreement_ratio={ratio:.4f},"
        f" threshold={minimum_agreement_ratio:.4f})"
    )
    return CrossCheckResult(
        as_of=as_of,
        secondary_source_id=secondary_source_id,
        corroborated=corroborated,
        agreement_ratio=ratio,
        agreements=tuple(sorted(agreements)),
        only_in_wikipedia=tuple(sorted(only_wiki)),
        only_in_secondary=tuple(sorted(only_secondary)),
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class SectorCrossCheckResult:
    """Agreement/disagreement/coverage for GICS Sector between two sources.

    Distinct from :class:`CrossCheckResult` (membership) because sector
    agreement is only meaningful over tickers BOTH sources actually cover --
    unlike membership, where "only in one side" is itself the signal, a
    ticker missing a sector value on one side is a coverage gap, not a
    disagreement, and the two must never be conflated.
    """

    as_of: date
    secondary_source_id: str
    corroborated: bool
    coverage_ratio: float
    agreement_ratio: float
    covered_tickers: int
    agreements: tuple[str, ...]
    disagreements: tuple[tuple[str, str, str], ...]  # (ticker, wiki_sector, secondary_sector)
    uncovered_tickers: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "secondary_source_id": self.secondary_source_id,
            "corroborated": self.corroborated,
            "coverage_ratio": self.coverage_ratio,
            "agreement_ratio": self.agreement_ratio,
            "covered_tickers": self.covered_tickers,
            "agreement_count": len(self.agreements),
            "disagreement_count": len(self.disagreements),
            "disagreements": [list(item) for item in self.disagreements],
            "uncovered_ticker_count": len(self.uncovered_tickers),
            "detail": self.detail,
        }


#: Below this ratio of Wikipedia tickers having a corroborating secondary
#: sector value at all, the check is refused outright (fail closed) rather
#: than reporting a possibly-meaningless ratio over a handful of tickers.
MINIMUM_SECTOR_COVERAGE_RATIO = 0.90

#: Below this ratio, of the COVERED tickers, agreeing on sector, the sector
#: evidence is NOT corroborated. Looser than the membership threshold because
#: sector taxonomies and boundary calls legitimately vary more between two
#: independently-maintained sources than membership does.
MINIMUM_SECTOR_AGREEMENT_RATIO = 0.90


def cross_check_sectors(
    *,
    wikipedia: WikipediaRevisionSnapshot,
    secondary_sectors: Mapping[str, str],
    secondary_source_id: str,
    as_of: date,
    minimum_coverage_ratio: float = MINIMUM_SECTOR_COVERAGE_RATIO,
    minimum_agreement_ratio: float = MINIMUM_SECTOR_AGREEMENT_RATIO,
) -> SectorCrossCheckResult:
    """Corroborate GICS Sector values against one independent SECONDARY source.

    Deterministic, coverage- and anomaly-aware: a ticker the secondary source
    does not cover is recorded as ``uncovered``, never silently treated as
    agreement or disagreement, and every actual disagreement is recorded by
    ticker with both sector values -- never reduced to a single ratio only.

    This is the canonical gate used by both fixtures and the real bounded
    2023-12-31 SEC Select Sector SPDR cross-check. A successful call can admit
    GICS Sector only as SECONDARY historical research evidence when the caller
    also binds a real independent dated source and provenance; the function by
    itself never promotes evidence to official/PIT_STRICT and never covers GICS
    Sub-Industry.

    Fails closed, before any comparison is attempted, on the same misuse
    classes :func:`cross_check_membership` guards against: both ratio
    overrides are refused outside their canonical-to-1.0 range (never
    weakenable), and a revision timestamped after ``as_of`` is refused
    (no look-ahead).
    """
    if not (MINIMUM_SECTOR_COVERAGE_RATIO <= minimum_coverage_ratio <= 1.0):
        raise SourceAdapterError(
            f"minimum_coverage_ratio={minimum_coverage_ratio!r} would weaken the"
            " mandatory sector coverage gate; it must be between"
            f" MINIMUM_SECTOR_COVERAGE_RATIO ({MINIMUM_SECTOR_COVERAGE_RATIO}) and"
            " 1.0 inclusive (fail closed)"
        )
    if not (MINIMUM_SECTOR_AGREEMENT_RATIO <= minimum_agreement_ratio <= 1.0):
        raise SourceAdapterError(
            f"minimum_agreement_ratio={minimum_agreement_ratio!r} would weaken the"
            " mandatory sector corroboration gate; it must be between"
            f" MINIMUM_SECTOR_AGREEMENT_RATIO ({MINIMUM_SECTOR_AGREEMENT_RATIO}) and"
            " 1.0 inclusive (fail closed)"
        )
    if wikipedia.revision.revision_timestamp.date() > as_of:
        raise SourceAdapterError(
            f"revision {wikipedia.revision.revision_id} is timestamped"
            f" {wikipedia.revision.revision_timestamp.date().isoformat()}, after the"
            f" decision date {as_of.isoformat()}; refusing to cross-check or label a"
            " later revision for an earlier decision (no look-ahead)"
        )
    if not secondary_sectors:
        raise SourceAdapterError(
            "sector cross-check requires a non-empty independent secondary"
            " sector mapping; an empty mapping is not corroboration, it is the"
            " absence of a check"
        )
    wiki_tickers = wikipedia.tickers()
    covered = sorted(t for t in wiki_tickers if t in secondary_sectors)
    uncovered = sorted(t for t in wiki_tickers if t not in secondary_sectors)
    coverage_ratio = len(covered) / len(wiki_tickers) if wiki_tickers else 0.0
    if coverage_ratio < minimum_coverage_ratio:
        raise SourceAdapterError(
            f"sector cross-check coverage {coverage_ratio:.4f} against"
            f" {secondary_source_id} is below the minimum"
            f" {minimum_coverage_ratio:.4f} ({len(covered)}/{len(wiki_tickers)}"
            " tickers covered); refusing to report a ratio over too few"
            " covered tickers (fail closed)"
        )
    agreements: list[str] = []
    disagreements: list[tuple[str, str, str]] = []
    for ticker in covered:
        wiki_sector = wikipedia.gics_sector_of(ticker)
        secondary_sector = secondary_sectors[ticker]
        if wiki_sector is not None and wiki_sector.strip().casefold() == (
            secondary_sector.strip().casefold()
        ):
            agreements.append(ticker)
        else:
            disagreements.append((ticker, wiki_sector or "", secondary_sector))
    agreement_ratio = len(agreements) / len(covered) if covered else 0.0
    corroborated = agreement_ratio >= minimum_agreement_ratio
    detail = (
        f"{len(agreements)}/{len(covered)} covered tickers agree on sector"
        f" (coverage={coverage_ratio:.4f}, agreement_ratio={agreement_ratio:.4f},"
        f" thresholds coverage>={minimum_coverage_ratio:.4f}"
        f" agreement>={minimum_agreement_ratio:.4f}); {len(uncovered)} ticker(s)"
        f" not covered by {secondary_source_id}"
    )
    return SectorCrossCheckResult(
        as_of=as_of,
        secondary_source_id=secondary_source_id,
        corroborated=corroborated,
        coverage_ratio=coverage_ratio,
        agreement_ratio=agreement_ratio,
        covered_tickers=len(covered),
        agreements=tuple(agreements),
        disagreements=tuple(disagreements),
        uncovered_tickers=tuple(uncovered),
        detail=detail,
    )


# -- the REAL fetch path (owner host only; never exercised in CI) ------------


def _fetch_json(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
    """One validated HTTPS GET against MediaWiki's public API.

    No authentication, no bot-protection bypass -- this is the documented,
    ToS-permitted access path, the same class already used (and already
    accepted) for the CURRENT-universe refresh.
    """
    import urllib.error
    import urllib.request

    if not url.lower().startswith("https://"):
        raise SourceAdapterError("the revision source must be fetched over HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = int(response.status)
            content_type = str(response.headers.get("Content-Type", ""))
            raw = bytes(response.read())
    except urllib.error.HTTPError as error:
        raise SourceAdapterError(f"revision source returned HTTP {error.code}") from error
    except OSError as error:
        raise SourceAdapterError(f"revision source unreachable: {error}") from error
    if status != 200:
        raise SourceAdapterError(f"revision source returned HTTP {status}")
    if "json" not in content_type.lower():
        raise SourceAdapterError(f"unexpected content type {content_type!r}; refusing to parse")
    return raw


def fetch_revision_at_or_before(
    as_of: date,
    *,
    page_title: str = DEFAULT_PAGE_TITLE,
    timeout_seconds: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> WikipediaRevisionRef:
    """Ask MediaWiki itself for the latest revision at or before ``as_of``.

    Owner-host only; unit tests construct :class:`WikipediaRevisionRef`
    directly and never call this. The lookup (``rvdir=older`` /
    ``rvstart=<as_of end-of-day UTC>``) is exactly the no-look-ahead selection
    :func:`select_revision_as_of` re-enforces locally when several revisions
    have already been fetched.
    """
    import urllib.parse

    rvstart = datetime.combine(as_of, datetime.max.time(), tzinfo=UTC).isoformat()
    url = WIKIPEDIA_REVISION_LOOKUP_API_URL.format(
        page_title=urllib.parse.quote(page_title.replace(" ", "_")),
        rvstart=urllib.parse.quote(rvstart),
    )
    raw = _fetch_json(url, timeout_seconds=timeout_seconds, user_agent=user_agent)
    import json as _json

    payload = _json.loads(raw.decode("utf-8"))
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages or not pages[0].get("revisions"):
        raise SourceAdapterError(
            f"no MediaWiki revision found for {page_title!r} at or before {as_of.isoformat()}"
        )
    revision = pages[0]["revisions"][0]
    page = pages[0]
    return WikipediaRevisionRef(
        # MediaWiki normalizes the requested title (underscores, redirects);
        # the page object's own "title" is what the revision actually belongs
        # to, so it -- not the caller's input spelling -- is what gets bound.
        page_title=str(page.get("title", page_title)),
        revision_id=int(revision["revid"]),
        revision_timestamp=datetime.fromisoformat(
            str(revision["timestamp"]).replace("Z", "+00:00")
        ),
    )


def fetch_revision_metadata(
    revision_id: int,
    *,
    timeout_seconds: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> bytes:
    """Fetch MediaWiki's own page/timestamp record for one revision id.

    Owner-host only. Kept separate from :func:`fetch_revision_wikitext` so
    the verification logic in :func:`_verify_revision_metadata` can be unit
    tested against a fabricated payload without any network call.
    """
    url = WIKIPEDIA_REVISION_METADATA_API_URL.format(revision_id=revision_id)
    return _fetch_json(url, timeout_seconds=timeout_seconds, user_agent=user_agent)


def _verify_revision_metadata(
    claimed: WikipediaRevisionRef, metadata_raw: bytes
) -> WikipediaRevisionRef:
    """Extract MediaWiki's own page/timestamp record and fail closed on conflict.

    ``claimed`` is whatever a caller (or a previous fetch step) asserts about
    one revision id. ``metadata_raw`` is the raw bytes of a
    ``action=query&prop=revisions&revids=<id>`` response for that SAME id,
    fetched independently. This function trusts neither the caller's
    ``page_title`` nor its ``revision_timestamp`` on their own -- it recomputes
    both from MediaWiki's metadata and returns the VERIFIED reference, raising
    if the metadata conflicts with what was claimed. This is what stops a
    revision id being paired with the wrong page identity or a fabricated/
    stale timestamp before either is bound to real content.
    """
    import json as _json

    payload = _json.loads(metadata_raw.decode("utf-8"))
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages:
        raise SourceAdapterError(
            f"MediaWiki returned no page for revision {claimed.revision_id};"
            " refusing to bind unverified metadata"
        )
    page = pages[0]
    revisions = page.get("revisions") or []
    if not revisions:
        raise SourceAdapterError(
            f"MediaWiki returned no revision metadata for revision"
            f" {claimed.revision_id}; refusing to bind unverified metadata"
        )
    revision_payload = revisions[0]
    actual_revid = int(revision_payload["revid"])
    if actual_revid != claimed.revision_id:
        raise SourceAdapterError(
            f"MediaWiki metadata reports revid {actual_revid}, not the requested"
            f" {claimed.revision_id}; refusing to bind"
        )
    actual_title = str(page["title"])
    claimed_title = claimed.page_title.replace("_", " ").strip().casefold()
    if actual_title.replace("_", " ").strip().casefold() != claimed_title:
        raise SourceAdapterError(
            f"revision {claimed.revision_id} belongs to page {actual_title!r},"
            f" not the claimed {claimed.page_title!r}; refusing to bind the"
            " wrong page's identity to this revision"
        )
    actual_timestamp = datetime.fromisoformat(
        str(revision_payload["timestamp"]).replace("Z", "+00:00")
    )
    if actual_timestamp != claimed.revision_timestamp:
        raise SourceAdapterError(
            f"revision {claimed.revision_id} was saved at"
            f" {actual_timestamp.isoformat()}, not the claimed"
            f" {claimed.revision_timestamp.isoformat()}; refusing to bind a"
            " fabricated or stale timestamp"
        )
    return WikipediaRevisionRef(
        page_title=actual_title, revision_id=actual_revid, revision_timestamp=actual_timestamp
    )


@dataclass(frozen=True, slots=True)
class RawWikipediaRevisionFetch:
    """A parsed revision snapshot plus the exact raw API response bytes.

    Mirrors :class:`trading_market_data_core.sp500_current_universe.RawConstituentRefresh`:
    ``snapshot.raw_checksum`` authenticates a claim, this pairs it with the
    actual bytes so a caller (e.g. an evidence-recording script) can persist
    genuine immutable raw provenance rather than re-deriving it.
    """

    snapshot: WikipediaRevisionSnapshot
    raw_bytes: bytes


def fetch_revision_wikitext(
    revision: WikipediaRevisionRef,
    *,
    timeout_seconds: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> RawWikipediaRevisionFetch:
    """Fetch and parse the exact wikitext of one immutable revision.

    The page identity and revision timestamp bound to the returned snapshot
    are VERIFIED against MediaWiki's own metadata for this exact revision id
    (:func:`_verify_revision_metadata`) before anything is bound -- ``revision``
    is a claim, used only to select which revision id to fetch; what actually
    gets bound is what MediaWiki itself reports. A caller (or a stale/forged
    reference) asserting the wrong page or the wrong timestamp for a real
    revision id is rejected here, fail closed, rather than silently trusted.

    Returns the raw API response bytes alongside the parsed snapshot
    (:class:`RawWikipediaRevisionFetch`) so a caller can retain genuine
    immutable raw provenance, not just the checksum claim.

    Owner-host only; unit tests call :func:`parse_revision_snapshot` directly
    against a recorded wikitext string and never call this.
    """
    import json as _json

    metadata_raw = fetch_revision_metadata(
        revision.revision_id, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    verified_revision = _verify_revision_metadata(revision, metadata_raw)

    url = WIKIPEDIA_REVISION_PARSE_API_URL.format(revision_id=verified_revision.revision_id)
    raw = _fetch_json(url, timeout_seconds=timeout_seconds, user_agent=user_agent)
    payload = _json.loads(raw.decode("utf-8"))
    parse = payload.get("parse")
    if not isinstance(parse, dict):
        raise SourceAdapterError(
            f"revision {verified_revision.revision_id} payload carries no MediaWiki"
            " parse result"
        )
    wikitext = parse.get("wikitext")
    if not isinstance(wikitext, str):
        raise SourceAdapterError(
            f"revision {verified_revision.revision_id} payload carries no wikitext"
        )
    if int(parse.get("revid", verified_revision.revision_id)) != verified_revision.revision_id:
        raise SourceAdapterError(
            "MediaWiki returned a different revid than requested; refusing to bind"
            " the wrong revision's content to this timestamp"
        )
    snapshot = parse_revision_snapshot(wikitext, raw_bytes=raw, revision=verified_revision)
    return RawWikipediaRevisionFetch(snapshot=snapshot, raw_bytes=raw)


# -- the WINDOW_DEFINITION_REQUIRED gap (checked, not invented) --------------

#: Whether an owner-approved ABSOLUTE start/end date for the frozen
#: ``baseline-trend-breakout-v1`` strategy's full historical backtest window
#: exists anywhere on main was independently re-verified for
#: A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD (2026-09-06). It does not.
#: ``docs/strategies/baseline-trend-breakout-v1.yaml`` freezes only
#: RELATIVE/proportional temporal structure -- ``splits.train`` (60%,
#: chronological), ``splits.validation`` (20%, chronological, immediately
#: follows Train), ``splits.test_oos`` (final 20%, chronological, locked) and
#: ``splits.walk_forward`` (60-month context / 12-month evaluation / 12-month
#: step) -- with no calendar anchor, and
#: ``docs/strategies/STRATEGY_FREEZE_TEMPLATE.md`` section 10 ("Data splits
#: and validation") leaves the Train/Validation/Test-OOS window date fields
#: empty. Without an owner-approved anchor there is no non-arbitrary way to
#: choose which real historical ``as_of`` dates constitute "the full backtest
#: window", a trading-calendar cadence, or a history depth -- so none are
#: invented here. This is a precise evidence/configuration gap, not
#: permission to fabricate one, and it does not block the dependency-safe
#: batch MACHINERY below, only the FULL-WINDOW empirical dataset.
WINDOW_DEFINITION_REQUIRED: dict[str, object] = {
    "status": "WINDOW_DEFINITION_REQUIRED",
    "gap": (
        "No owner-approved ABSOLUTE calendar start/end date for the frozen"
        " baseline-trend-breakout-v1 strategy's full historical backtest"
        " window exists anywhere on main (re-verified"
        " 2026-09-06, A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD)."
        " docs/strategies/baseline-trend-breakout-v1.yaml defines only"
        " RELATIVE/proportional temporal structure: splits.train (60%,"
        " chronological), splits.validation (20%, chronological, immediately"
        " follows Train), splits.test_oos (final 20%, chronological, locked"
        " until final evaluation) and splits.walk_forward (60-month context /"
        " 12-month evaluation / 12-month step) -- none anchored to a calendar"
        " date. docs/strategies/STRATEGY_FREEZE_TEMPLATE.md section 10 (Data"
        " splits and validation) leaves the Train/Validation/Test-OOS window"
        " date fields empty for this strategy."
    ),
    "what_this_task_completed_instead": (
        "The reusable, deterministic, resumable and idempotent batch-build"
        " machinery (WikipediaDatasetEntry, WikipediaDatasetStore,"
        " run_wikipedia_revision_history_batch, build_dataset_manifest, and"
        " scripts/run_wikipedia_revision_history_batch.py) plus offline,"
        " fixture-based tests proving it is resumable, idempotent and"
        " fail-closed against a later revision entering through a retry,"
        " resume, or hand-imported dataset entry. The single already-recorded"
        " real experiment (2023-12-31) is preserved unchanged, not"
        " regenerated. No additional real MediaWiki fetch was performed by"
        " this task: there is no owner-approved date, date range, or cadence"
        " to fetch that would not itself be an invented choice."
    ),
    "unblocks_when": (
        "the owner (or an approved ADR) supplies an absolute start date and"
        " end date for the frozen strategy's full backtest window, anchoring"
        " the existing relative 60/20/20 + 60-month/12-month walk-forward"
        " specification to real calendar dates and the canonical"
        " trading_domain.calendar decision-date grid. Once that exists, the"
        " machinery built here runs unmodified to build the full-window"
        " dataset."
    ),
    "does_not_block": (
        "This gap blocks only the FULL-WINDOW empirical research dataset. It"
        " does not block or weaken the single recorded 2023-12-31 experiment,"
        " the batch machinery/tests built in this task, the official/licensed"
        " ASOF_GICS_BLOCKER or pit_sp500_membership gaps (unchanged,"
        " owner-gated), TRADING_READINESS, or Paper/Live authorization"
        " (unaffected either way -- ADR-0017)."
    ),
}


# -- deterministic, resumable, idempotent multi-date BATCH machinery ---------
#
# Everything below builds MULTIPLE dated MEMBERSHIP research observations
# on top of the single-revision machinery above, using the SAME no-look-ahead
# and mandatory-cross-check rules -- it never relaxes either. It does not,
# by itself, choose which as_of dates to fetch: a caller (owner/agent, or
# scripts/run_wikipedia_revision_history_batch.py) supplies the dates
# explicitly, exactly because WINDOW_DEFINITION_REQUIRED above means there is
# no canonical window to derive them from automatically.

WIKIPEDIA_DATASET_ENTRY_SCHEMA_VERSION = "dataset-v1-wikipedia-pit-entry-1.0.0"
DATASET_MANIFEST_SCHEMA_VERSION = "dataset-v1-wikipedia-pit-manifest-1.0.0"

BATCH_OUTCOME_ADMITTED = "ADMITTED"
BATCH_OUTCOME_RESUMED = "RESUMED_ALREADY_PRESENT"
BATCH_OUTCOME_REFUSED_NOT_CORROBORATED = "REFUSED_NOT_CORROBORATED"
BATCH_OUTCOME_FAILED = "FAILED"

_VALID_BATCH_OUTCOMES = frozenset(
    {
        BATCH_OUTCOME_ADMITTED,
        BATCH_OUTCOME_RESUMED,
        BATCH_OUTCOME_REFUSED_NOT_CORROBORATED,
        BATCH_OUTCOME_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class SecondaryCsvMatch:
    """One deterministic selection from the independent secondary source's
    own ``date,tickers`` history, at or before one decision date.

    Bundles the SAME three things :func:`secondary_csv_tickers_at_or_before`
    derives from one parse -- the effective date it actually matched, the
    exact raw row text (so its own checksum can be computed and later
    re-verified), and the resulting ticker set -- so no caller ever recomputes
    one from a stale copy of another.
    """

    matched_date: date
    matched_line: str
    tickers: frozenset[str]


def secondary_csv_tickers_at_or_before(csv_bytes: bytes, as_of: date) -> SecondaryCsvMatch:
    """The fja05680/sp500-style CSV's own latest ``date,tickers`` row at or
    before ``as_of`` -- never a later one.

    The SAME no-look-ahead rule :func:`select_revision_as_of` applies to
    MediaWiki revisions, applied here to the independent secondary source:
    picking the nearest-but-later row would project a future secondary
    observation onto an earlier decision. Pure, deterministic and offline --
    this never fetches anything itself, so it is exactly as safe to call at
    dataset-BUILD time (to select the cross-check ticker set) as it is at
    dataset-LOAD time (to independently re-derive and verify a previously
    admitted entry's secondary provenance against its own retained raw bytes;
    see :meth:`WikipediaDatasetStore.load`).
    """
    text = csv_bytes.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip().lower() != "date,tickers":
        raise SourceAdapterError(
            "secondary CSV does not carry the expected 'date,tickers' header"
            f" (saw: {lines[0]!r} if any); refusing to parse"
        )
    reader = csv.reader(lines)
    next(reader)  # header
    best_date: date | None = None
    best_line: str | None = None
    for index, row in enumerate(reader, start=1):
        if not row:
            continue
        row_date = date.fromisoformat(row[0])
        if row_date <= as_of and (best_date is None or row_date > best_date):
            best_date = row_date
            best_line = lines[index]
    if best_date is None or best_line is None:
        raise SourceAdapterError(
            f"secondary CSV carries no row at or before {as_of.isoformat()};"
            " refusing to use a later row for an earlier decision (no look-ahead)"
        )
    tickers_field = best_line.split(",", 1)[1].strip().strip('"')
    tickers = frozenset(t for t in tickers_field.split(",") if t)
    return SecondaryCsvMatch(matched_date=best_date, matched_line=best_line, tickers=tickers)


@dataclass(frozen=True, slots=True)
class SecondaryProvenance:
    """Immutable provenance binding one admitted entry to the EXACT
    independent secondary-source artifact and row that corroborated it.

    Unlike a MediaWiki revision (immutable once saved), the community CSV at
    ``source_url`` lives on a mutable branch and can change or disappear
    later. Reproducing this cross-check later therefore requires the
    RETAINED raw bytes this entry was actually built from, not merely a URL
    -- ``full_file_checksum`` authenticates the content retained at
    ``raw_artifact_path`` (:meth:`WikipediaDatasetStore` writes and re-verifies
    it exactly as it does the primary MediaWiki raw sidecar), and
    ``matched_row_checksum`` authenticates the specific row selected for
    ``as_of`` within it.
    """

    source_id: str
    source_url: str
    fetched_at: datetime
    full_file_checksum: str
    raw_artifact_path: str
    matched_row_date: date
    matched_row_checksum: str

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise SourceAdapterError("secondary provenance requires a non-empty source_id")
        if not self.source_url.lower().startswith("https://"):
            raise SourceAdapterError(
                "secondary provenance source_url must be fetched over HTTPS"
            )
        if self.fetched_at.tzinfo is None:
            raise SourceAdapterError(
                "secondary provenance fetched_at must be timezone-aware (a real,"
                " recorded instant, never a naive guess)"
            )
        for label, checksum in (
            ("full_file_checksum", self.full_file_checksum),
            ("matched_row_checksum", self.matched_row_checksum),
        ):
            digest = checksum.removeprefix("sha256:")
            if digest == checksum or len(digest) != 64:
                raise SourceAdapterError(
                    f"secondary provenance {label} must be a real sha256: digest"
                )
        if not self.raw_artifact_path.strip():
            raise SourceAdapterError(
                "secondary provenance requires a non-empty raw_artifact_path"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at.isoformat(),
            "full_file_checksum": self.full_file_checksum,
            "raw_artifact_path": self.raw_artifact_path,
            "matched_row_date": self.matched_row_date.isoformat(),
            "matched_row_checksum": self.matched_row_checksum,
        }


@dataclass(frozen=True, slots=True)
class WikipediaDatasetEntry:
    """One ADMITTED, dated MEMBERSHIP research observation in the batch dataset.

    Deliberately stricter than :class:`WikipediaRevisionSnapshot` alone: this
    type can only be constructed once a snapshot has ALREADY been bound to a
    requested ``as_of`` date (no look-ahead) AND cross-checked and found
    ``corroborated`` against an independent secondary source. An
    uncorroborated cross-check, an as_of/cross_check mismatch, or a revision
    dated after ``as_of`` is refused fail closed by the constructor itself --
    never by caller discipline -- which is what makes this type usable as the
    "this may be persisted as an admitted dataset entry" gate throughout the
    rest of this module (:class:`WikipediaDatasetStore`,
    :func:`run_wikipedia_revision_history_batch`).
    """

    as_of: date
    snapshot: WikipediaRevisionSnapshot
    cross_check: CrossCheckResult
    #: When THIS process fetched/observed the revision -- distinct from
    #: ``snapshot.revision.revision_timestamp`` (when MediaWiki saved it).
    #: Deliberately excluded from :meth:`compute_checksum` (see there): two
    #: fetches of the very same immutable revision, made at different real
    #: times, must be treated as the SAME admitted observation, not a conflict.
    fetch_observed_at: datetime
    #: Path to the immutable raw MediaWiki API response bytes this snapshot
    #: was parsed from (see :class:`WikipediaDatasetStore`).
    raw_provenance_path: str
    #: Provenance binding to the EXACT independent secondary-source artifact
    #: and row that corroborated this entry's cross-check (see
    #: :class:`SecondaryProvenance`) -- required, never optional: an entry
    #: with no independent secondary binding is not admissible research
    #: evidence, only a Wikipedia-only assertion.
    secondary_provenance: SecondaryProvenance

    def __post_init__(self) -> None:
        if self.snapshot.revision.revision_timestamp.date() > self.as_of:
            raise SourceAdapterError(
                f"revision {self.snapshot.revision.revision_id} is timestamped"
                f" {self.snapshot.revision.revision_timestamp.date().isoformat()},"
                f" after as_of {self.as_of.isoformat()}; refusing to admit a"
                " later revision for an earlier decision date (no look-ahead)"
            )
        if self.cross_check.as_of != self.as_of:
            raise SourceAdapterError(
                f"cross-check as_of {self.cross_check.as_of.isoformat()} does not"
                f" match entry as_of {self.as_of.isoformat()}; refusing to bind a"
                " mismatched cross-check result"
            )
        if not self.cross_check.corroborated:
            raise SourceAdapterError(
                f"cross-check for as_of {self.as_of.isoformat()} is not"
                f" corroborated ({self.cross_check.detail}); only corroborated"
                " observations may be admitted to the research dataset"
            )
        if self.fetch_observed_at.tzinfo is None:
            raise SourceAdapterError(
                "fetch_observed_at must be timezone-aware (a real, recorded"
                " instant, never a naive guess)"
            )
        if not self.raw_provenance_path.strip():
            raise SourceAdapterError(
                "a dataset entry requires a non-empty raw_provenance_path"
            )
        if self.secondary_provenance.source_id != self.cross_check.secondary_source_id:
            raise SourceAdapterError(
                f"secondary provenance source_id"
                f" {self.secondary_provenance.source_id!r} does not match the"
                f" cross-check's own secondary_source_id"
                f" {self.cross_check.secondary_source_id!r}; refusing to bind a"
                " mismatched secondary provenance record"
            )
        if self.secondary_provenance.matched_row_date > self.as_of:
            raise SourceAdapterError(
                f"secondary source's matched row is dated"
                f" {self.secondary_provenance.matched_row_date.isoformat()}, after"
                f" as_of {self.as_of.isoformat()}; refusing to admit a later"
                " secondary observation for an earlier decision date (no"
                " look-ahead)"
            )

    def body(self) -> dict[str, object]:
        """The checksummed body -- deliberately excludes ``fetch_observed_at``
        and ``secondary_provenance.fetched_at``.

        See ``fetch_observed_at``'s docstring: two fetches of the identical
        immutable revision (or, here, of the identical independent secondary
        artifact) at different real times must produce the identical
        checksum, so a crash-recovery retry (fetch again, get the same
        content, call :meth:`WikipediaDatasetStore.accept` again) is
        recognised as an idempotent no-op rather than a spurious conflict.
        Every other secondary-provenance field (source id/url, checksums,
        matched row date) IS part of the checksummed body: those identify
        WHAT was used, not WHEN it happened to be fetched.
        """
        return {
            "schema_version": WIKIPEDIA_DATASET_ENTRY_SCHEMA_VERSION,
            "classification": CLASSIFICATION,
            "backtest_quality": "APPROXIMATE",
            "as_of": self.as_of.isoformat(),
            "raw_provenance_path": self.raw_provenance_path,
            "snapshot": self.snapshot.to_dict(),
            "cross_check": {
                "as_of": self.cross_check.as_of.isoformat(),
                "secondary_source_id": self.cross_check.secondary_source_id,
                "corroborated": self.cross_check.corroborated,
                "agreement_ratio": self.cross_check.agreement_ratio,
                "agreements": list(self.cross_check.agreements),
                "only_in_wikipedia": list(self.cross_check.only_in_wikipedia),
                "only_in_secondary": list(self.cross_check.only_in_secondary),
                "detail": self.cross_check.detail,
            },
            "secondary_provenance": {
                "source_id": self.secondary_provenance.source_id,
                "source_url": self.secondary_provenance.source_url,
                "full_file_checksum": self.secondary_provenance.full_file_checksum,
                "raw_artifact_path": self.secondary_provenance.raw_artifact_path,
                "matched_row_date": self.secondary_provenance.matched_row_date.isoformat(),
                "matched_row_checksum": self.secondary_provenance.matched_row_checksum,
            },
            "gics_sector_status": "CAPTURED_UNEVALUATED_NOT_ADMITTED",
            "eligibility": {
                "authoritative_official_evidence": False,
                "eligible_for_promotion_without_validation": False,
                "substitute_for_historical_gics": False,
            },
        }

    def compute_checksum(self) -> str:
        serialized = json.dumps(
            self.body(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return "sha256:" + hashlib.sha256(serialized.encode("ascii")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        payload = self.body()
        payload["fetch_observed_at"] = self.fetch_observed_at.isoformat()
        secondary_provenance_payload = payload["secondary_provenance"]
        assert isinstance(secondary_provenance_payload, dict)
        secondary_provenance_payload["fetched_at"] = (
            self.secondary_provenance.fetched_at.isoformat()
        )
        payload["entry_checksum"] = self.compute_checksum()
        return payload


def _snapshot_from_dict(payload: Mapping[str, object]) -> WikipediaRevisionSnapshot:
    """Rebuild a :class:`WikipediaRevisionSnapshot` from its own ``to_dict()``.

    Reconstruction re-runs every fail-closed check
    :class:`WikipediaRevisionSnapshot` already enforces (classification,
    source authority, checksum shape, plausible count, no duplicate tickers)
    -- a hand-edited or corrupted file cannot bypass any of them by being
    loaded back in rather than freshly parsed.
    """
    revision_payload = payload["revision"]
    assert isinstance(revision_payload, Mapping)
    revision = WikipediaRevisionRef(
        page_title=str(revision_payload["page_title"]),
        revision_id=int(str(revision_payload["revision_id"])),
        revision_timestamp=datetime.fromisoformat(str(revision_payload["revision_timestamp"])),
    )
    raw_observations = payload.get("observations", ())
    assert isinstance(raw_observations, Sequence)
    observations = tuple(
        WikipediaConstituentObservation(
            ticker=str(o["ticker"]),
            name=str(o.get("name", "")),
            gics_sector=str(o["gics_sector"]) if o.get("gics_sector") else None,
            gics_sub_industry=(
                str(o["gics_sub_industry"]) if o.get("gics_sub_industry") else None
            ),
        )
        for o in raw_observations
    )
    raw_diagnostics = payload.get("parse_diagnostics", ())
    assert isinstance(raw_diagnostics, Sequence)
    return WikipediaRevisionSnapshot(
        revision=revision,
        raw_checksum=str(payload["raw_checksum"]),
        observations=observations,
        parse_diagnostics=tuple(str(d) for d in raw_diagnostics),
    )


def _cross_check_from_dict(
    payload: Mapping[str, object], *, wikipedia: WikipediaRevisionSnapshot, as_of: date
) -> CrossCheckResult:
    """Re-derive a :class:`CrossCheckResult`, never trust the persisted verdict.

    ``payload["corroborated"]`` and ``payload["agreement_ratio"]`` are NOT
    read: a hand-edited or externally "imported" file could otherwise assert
    ``corroborated: true`` with a ratio below
    :data:`MINIMUM_CROSS_CHECK_AGREEMENT_RATIO`, or an ``agreements`` /
    ``only_in_wikipedia`` / ``only_in_secondary`` split inconsistent with the
    ACTUAL bound ``wikipedia`` snapshot, and (since the whole file's
    ``entry_checksum`` is computed FROM this same payload) a self-consistent
    forgery would otherwise authenticate itself.

    Instead, only the raw ticker PARTITION is read from the persisted dict.
    ``agreements | only_in_wikipedia`` reconstructs the claimed Wikipedia
    ticker set, which MUST equal ``wikipedia.tickers()`` -- the real,
    independently-reconstructed snapshot's own ticker set -- or the file is
    refused outright as inconsistent with the snapshot it claims to
    accompany. ``agreements | only_in_secondary`` reconstructs the
    independent secondary source's ticker set as it was recorded at
    admission time, and :func:`cross_check_membership` is called on it for
    real, at the canonical (never weakened) threshold: ``corroborated`` and
    ``agreement_ratio`` in the RETURNED :class:`CrossCheckResult` are always
    freshly computed here, never copied from the file.
    """
    raw_agreements = payload.get("agreements", ())
    raw_only_wikipedia = payload.get("only_in_wikipedia", ())
    raw_only_secondary = payload.get("only_in_secondary", ())
    assert isinstance(raw_agreements, Sequence)
    assert isinstance(raw_only_wikipedia, Sequence)
    assert isinstance(raw_only_secondary, Sequence)
    agreements = frozenset(str(t) for t in raw_agreements)
    only_in_wikipedia = frozenset(str(t) for t in raw_only_wikipedia)
    only_in_secondary = frozenset(str(t) for t in raw_only_secondary)
    claimed_wikipedia_tickers = agreements | only_in_wikipedia
    if claimed_wikipedia_tickers != wikipedia.tickers():
        raise SourceAdapterError(
            "persisted cross-check ticker partition (agreements +"
            " only_in_wikipedia) does not match the bound snapshot's own"
            " ticker set; refusing to trust an inconsistent or forged"
            " cross-check record"
        )
    secondary_tickers = agreements | only_in_secondary
    secondary_source_id = str(payload["secondary_source_id"])
    return cross_check_membership(
        wikipedia=wikipedia, secondary_tickers=secondary_tickers,
        secondary_source_id=secondary_source_id, as_of=as_of,
    )


def _secondary_provenance_from_dict(payload: Mapping[str, object]) -> SecondaryProvenance:
    """Rebuild a :class:`SecondaryProvenance`, re-running its constructor checks."""
    return SecondaryProvenance(
        source_id=str(payload["source_id"]),
        source_url=str(payload["source_url"]),
        fetched_at=datetime.fromisoformat(str(payload["fetched_at"])),
        full_file_checksum=str(payload["full_file_checksum"]),
        raw_artifact_path=str(payload["raw_artifact_path"]),
        matched_row_date=date.fromisoformat(str(payload["matched_row_date"])),
        matched_row_checksum=str(payload["matched_row_checksum"]),
    )


def _entry_from_dict(payload: Mapping[str, object]) -> WikipediaDatasetEntry:
    """Rebuild a :class:`WikipediaDatasetEntry`, re-running every constructor check.

    This is what stops a hand-edited or externally "imported" JSON file --
    e.g. one claiming a revision timestamped after its own ``as_of``, a
    cross-check that is not ``corroborated``, or a self-asserted
    ``corroborated: true`` at a below-threshold or fabricated ratio -- from
    ever becoming a usable dataset entry merely by being placed on disk and
    loaded: every guard in :meth:`WikipediaDatasetEntry.__post_init__` runs
    again here, on load, not only at original construction time, and the
    cross-check itself is RE-DERIVED (:func:`_cross_check_from_dict`), never
    trusted from the file. The secondary provenance binding is reconstructed
    too, but only :meth:`WikipediaDatasetStore.load` -- which has the retained
    raw secondary artifact to check it against -- can verify it is genuine
    rather than merely well-shaped; that is done there, not here.
    """
    snapshot_payload = payload["snapshot"]
    cross_check_payload = payload["cross_check"]
    secondary_provenance_payload = payload["secondary_provenance"]
    assert isinstance(snapshot_payload, Mapping)
    assert isinstance(cross_check_payload, Mapping)
    assert isinstance(secondary_provenance_payload, Mapping)
    as_of = date.fromisoformat(str(payload["as_of"]))
    snapshot = _snapshot_from_dict(snapshot_payload)
    return WikipediaDatasetEntry(
        as_of=as_of,
        snapshot=snapshot,
        cross_check=_cross_check_from_dict(cross_check_payload, wikipedia=snapshot, as_of=as_of),
        fetch_observed_at=datetime.fromisoformat(str(payload["fetch_observed_at"])),
        raw_provenance_path=str(payload["raw_provenance_path"]),
        secondary_provenance=_secondary_provenance_from_dict(secondary_provenance_payload),
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (fsync-then-rename-then-dir-fsync).

    A small, deliberate duplicate of
    ``sp500_current_universe._atomic_write_bytes`` (same package, but a
    private helper there) rather than a cross-module reach into another
    module's internals -- the same duplication choice already made between
    this module's real fetch helpers and
    ``scripts/run_wikipedia_revision_history_experiment.py``'s.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _flock(lock_path: Path) -> Iterator[None]:
    """Serialize a read-check-write sequence across processes AND threads.

    Mirrors ``sp500_current_universe.UniverseStore._locked``: an
    exclusively-held ``fcntl.flock`` on a dedicated lock file blocks a second
    writer rather than failing it outright, and is released automatically if
    this process dies while holding it.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class WikipediaDatasetAcceptResult:
    """The outcome of one :meth:`WikipediaDatasetStore.accept` call."""

    as_of: date
    entry_path: Path
    raw_path: Path
    secondary_raw_path: Path
    already_written: bool


class WikipediaDatasetStore:
    """A deterministic, resumable, idempotent, write-once archive of
    :class:`WikipediaDatasetEntry` records, one per ``as_of`` date.

    Mirrors ``sp500_current_universe.UniverseStore`` deliberately: archived
    entries are WRITE-ONCE per ``as_of`` -- a later call for the same date is
    accepted only when it is semantically byte-identical (an idempotent retry
    after a crash, or a resumed batch run), and any OTHER candidate for a
    date already on record is refused rather than silently rewriting archived
    history. A raw MediaWiki API response is stored once PER REVISION ID
    (``revision-<id>.raw.json``), shared across every ``as_of`` date that
    happens to resolve to the same immutable revision -- consecutive trading
    days between two page edits legitimately do.

    Loading ALWAYS re-validates: :meth:`load` re-runs every
    :class:`WikipediaDatasetEntry` and :class:`WikipediaRevisionSnapshot`
    constructor check and re-verifies the raw sidecar's checksum, so a
    tampered or hand-imported file on disk can never be trusted merely
    because it is present -- exactly the same "verified, not trusted" posture
    :func:`fetch_revision_wikitext` already applies to a live fetch.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _entry_path(self, as_of: date) -> Path:
        return self.root / f"membership-{as_of.isoformat()}.json"

    def _raw_path(self, revision_id: int) -> Path:
        return self.root / f"revision-{revision_id}.raw.json"

    def _entry_lock_path(self, as_of: date) -> Path:
        return self.root / f".membership-{as_of.isoformat()}.lock"

    def _raw_lock_path(self, revision_id: int) -> Path:
        return self.root / f".revision-{revision_id}.lock"

    def _secondary_raw_path(self, full_file_checksum: str) -> Path:
        digest = full_file_checksum.removeprefix("sha256:")
        return self.root / f"secondary-{digest}.raw.csv"

    def _secondary_raw_lock_path(self, full_file_checksum: str) -> Path:
        digest = full_file_checksum.removeprefix("sha256:")
        return self.root / f".secondary-{digest}.lock"

    def has(self, as_of: date) -> bool:
        return self._entry_path(as_of).is_file()

    def dates(self) -> tuple[date, ...]:
        """Every ``as_of`` date currently archived, sorted ascending."""
        if not self.root.is_dir():
            return ()
        found: list[date] = []
        for path in self.root.glob("membership-*.json"):
            try:
                found.append(date.fromisoformat(path.stem.removeprefix("membership-")))
            except ValueError:
                continue  # not a dated entry file; ignore rather than fail closed here
        return tuple(sorted(found))

    def load(self, as_of: date) -> WikipediaDatasetEntry:
        """Load and fully re-validate the entry archived for ``as_of``."""
        path = self._entry_path(as_of)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SourceAdapterError(f"unreadable dataset entry {path}: {error}") from error
        if not isinstance(payload, dict):
            raise SourceAdapterError(f"{path} is not a dataset entry JSON object")
        entry = _entry_from_dict(payload)
        declared = payload.get("entry_checksum")
        recomputed = entry.compute_checksum()
        if not isinstance(declared, str) or not declared:
            raise SourceAdapterError(
                f"{path}: dataset entry carries no entry_checksum; refusing to"
                " trust an unauthenticated dataset entry"
            )
        digest = declared.removeprefix("sha256:")
        if digest == declared or len(digest) != 64:
            raise SourceAdapterError(
                f"{path}: entry_checksum {declared!r} is not a valid sha256:"
                " digest; refusing to trust a malformed checksum"
            )
        if declared != recomputed:
            raise SourceAdapterError(
                f"{path}: dataset entry checksum mismatch (tampered or stale):"
                f" declared {declared}, recomputed {recomputed}"
            )
        raw_path = self._raw_path(entry.snapshot.revision.revision_id)
        if not raw_path.is_file():
            raise SourceAdapterError(
                f"{raw_path}: raw provenance sidecar is missing; refusing to"
                " trust a dataset entry without its immutable raw artifact"
            )
        if not entry.snapshot.verify_raw_bytes(raw_path.read_bytes()):
            raise SourceAdapterError(
                f"{raw_path}: raw provenance no longer matches the recorded"
                " checksum (tampered); refusing to trust this dataset entry"
            )
        self._verify_secondary_provenance(entry)
        return entry

    def _verify_secondary_provenance(self, entry: WikipediaDatasetEntry) -> None:
        """Independently re-derive and verify the secondary-source binding.

        Unlike the primary MediaWiki raw sidecar (checksum-verified only --
        it is never re-parsed on load, because :class:`WikipediaRevisionSnapshot`
        already re-runs every structural check at construction time), the
        secondary artifact IS re-parsed here: ``full_file_checksum`` is
        checked first (missing/tampered retained bytes fail closed
        immediately), then :func:`secondary_csv_tickers_at_or_before` is run
        again against those retained bytes for ``entry.as_of`` and the result
        must agree with what the entry claims on every axis -- the matched
        effective date, that date's own row checksum, and (the point of this
        check) the exact ticker set, which must equal
        ``entry.cross_check.agreements | entry.cross_check.only_in_secondary``.
        A JSON that forged a self-consistent ticker partition (already
        guarded against being inconsistent with the WIKIPEDIA side by
        :func:`_cross_check_from_dict`) can no longer pass this check either,
        because it is now cross-checked against real bytes from the retained,
        checksum-verified independent secondary artifact, not merely internal
        consistency.
        """
        provenance = entry.secondary_provenance
        secondary_raw_path = self._secondary_raw_path(provenance.full_file_checksum)
        if not secondary_raw_path.is_file():
            raise SourceAdapterError(
                f"{secondary_raw_path}: secondary source raw provenance sidecar"
                " is missing; refusing to trust a dataset entry without its"
                " independent secondary-source artifact"
            )
        secondary_raw_bytes = secondary_raw_path.read_bytes()
        if raw_artifact_checksum(secondary_raw_bytes) != provenance.full_file_checksum:
            raise SourceAdapterError(
                f"{secondary_raw_path}: secondary source raw provenance no"
                " longer matches the recorded checksum (tampered); refusing to"
                " trust this dataset entry"
            )
        match = secondary_csv_tickers_at_or_before(secondary_raw_bytes, entry.as_of)
        if match.matched_date != provenance.matched_row_date:
            raise SourceAdapterError(
                f"{secondary_raw_path}: the retained secondary artifact's own"
                f" effective row for as_of {entry.as_of.isoformat()} is dated"
                f" {match.matched_date.isoformat()}, not the recorded"
                f" {provenance.matched_row_date.isoformat()}; refusing to trust"
                " a mismatched secondary provenance binding"
            )
        matched_line_checksum = raw_artifact_checksum(match.matched_line.encode("utf-8"))
        if matched_line_checksum != provenance.matched_row_checksum:
            raise SourceAdapterError(
                f"{secondary_raw_path}: the retained secondary artifact's"
                " matched row no longer matches the recorded"
                " matched_row_checksum (tampered); refusing to trust this"
                " dataset entry"
            )
        reconstructed_secondary_tickers = (
            frozenset(entry.cross_check.agreements) | frozenset(entry.cross_check.only_in_secondary)
        )
        if match.tickers != reconstructed_secondary_tickers:
            raise SourceAdapterError(
                f"{secondary_raw_path}: the cross-check's recorded secondary"
                " ticker partition no longer matches the tickers independently"
                " re-derived from the retained secondary artifact; refusing to"
                " trust an inconsistent or forged cross-check record"
            )

    def entries(self) -> tuple[WikipediaDatasetEntry, ...]:
        """Every archived entry, fully re-validated, sorted by ``as_of``."""
        return tuple(self.load(as_of) for as_of in self.dates())

    def accept(
        self, entry: WikipediaDatasetEntry, raw_bytes: bytes, *, secondary_raw_bytes: bytes
    ) -> WikipediaDatasetAcceptResult:
        """Persist ``entry`` and BOTH its raw provenances, enforcing write-once
        + idempotence.

        Fails closed before writing anything if ``raw_bytes`` does not
        authenticate ``entry.snapshot.raw_checksum``, or if
        ``secondary_raw_bytes`` does not authenticate
        ``entry.secondary_provenance.full_file_checksum``. The two raw
        sidecars are written first -- the primary MediaWiki one keyed by
        revision id, the independent secondary one keyed by its own content
        checksum, each locked independently so two different ``as_of`` dates
        resolving to the same content cannot race each other -- and the entry
        (keyed by ``as_of``, write-once) last -- a crash at any point leaves
        at most an unreferenced raw sidecar, never an entry pointing at a raw
        artifact that was never durably written.
        """
        if not entry.snapshot.verify_raw_bytes(raw_bytes):
            raise SourceAdapterError(
                f"raw bytes supplied for as_of {entry.as_of.isoformat()} do not"
                " match the snapshot's own raw_checksum claim; refusing to"
                " accept unverifiable provenance"
            )
        secondary_raw_checksum = raw_artifact_checksum(secondary_raw_bytes)
        if secondary_raw_checksum != entry.secondary_provenance.full_file_checksum:
            raise SourceAdapterError(
                f"secondary raw bytes supplied for as_of {entry.as_of.isoformat()}"
                " do not match the secondary provenance's own full_file_checksum"
                " claim; refusing to accept unverifiable provenance"
            )
        revision_id = entry.snapshot.revision.revision_id
        secondary_full_file_checksum = entry.secondary_provenance.full_file_checksum
        raw_path = self._raw_path(revision_id)
        secondary_raw_path = self._secondary_raw_path(secondary_full_file_checksum)
        entry_path = self._entry_path(entry.as_of)
        with _flock(self._entry_lock_path(entry.as_of)):
            already_written = False
            if entry_path.is_file():
                try:
                    existing = self.load(entry.as_of)
                except SourceAdapterError as error:
                    raise SourceAdapterError(
                        f"an archived dataset entry already exists at {entry_path}"
                        f" and is unreadable ({error}); refusing to overwrite"
                        " immutable provenance"
                    ) from error
                if existing.compute_checksum() != entry.compute_checksum():
                    raise SourceAdapterError(
                        f"a dataset entry for as_of {entry.as_of.isoformat()} is"
                        f" already archived immutably at {entry_path} (bound to"
                        f" revision {existing.snapshot.revision.revision_id});"
                        " refusing to overwrite it with different content"
                        " (write-once: a new observation needs a new as_of date,"
                        " never a correction of a past one)"
                    )
                already_written = True
            with _flock(self._raw_lock_path(revision_id)):
                if raw_path.is_file():
                    existing_raw = raw_path.read_bytes()
                    if raw_artifact_checksum(existing_raw) != entry.snapshot.raw_checksum:
                        raise SourceAdapterError(
                            f"{raw_path} already exists with different content"
                            " than the freshly supplied raw bytes for the same"
                            " revision id; refusing to overwrite immutable raw"
                            " provenance"
                        )
                else:
                    _atomic_write_bytes(raw_path, raw_bytes)
            with _flock(self._secondary_raw_lock_path(secondary_full_file_checksum)):
                if secondary_raw_path.is_file():
                    existing_secondary_raw = secondary_raw_path.read_bytes()
                    existing_secondary_checksum = raw_artifact_checksum(existing_secondary_raw)
                    if existing_secondary_checksum != secondary_full_file_checksum:
                        raise SourceAdapterError(
                            f"{secondary_raw_path} already exists with different"
                            " content than the freshly supplied secondary raw"
                            " bytes for the same checksum; refusing to overwrite"
                            " immutable secondary raw provenance"
                        )
                else:
                    _atomic_write_bytes(secondary_raw_path, secondary_raw_bytes)
            if not already_written:
                _atomic_write_bytes(
                    entry_path,
                    json.dumps(entry.to_dict(), indent=2, sort_keys=True).encode("utf-8"),
                )
        return WikipediaDatasetAcceptResult(
            as_of=entry.as_of, entry_path=entry_path, raw_path=raw_path,
            secondary_raw_path=secondary_raw_path, already_written=already_written,
        )


@dataclass(frozen=True, slots=True)
class WikipediaDatasetBatchRow:
    """One requested ``as_of`` date's outcome from one batch run.

    Every requested date gets exactly one row, whatever happened to it --
    admitted, already resumed, refused for lacking corroboration, or a hard
    fetch/parse failure. Nothing is silently dropped: a refusal or failure is
    as visible in the returned result as a success.
    """

    as_of: date
    outcome: str
    detail: str
    revision_id: int | None = None
    agreement_ratio: float | None = None

    def __post_init__(self) -> None:
        if self.outcome not in _VALID_BATCH_OUTCOMES:
            raise SourceAdapterError(
                f"unrecognised batch outcome {self.outcome!r}; must be one of"
                f" {sorted(_VALID_BATCH_OUTCOMES)}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "outcome": self.outcome,
            "detail": self.detail,
            "revision_id": self.revision_id,
            "agreement_ratio": self.agreement_ratio,
        }


@dataclass(frozen=True, slots=True)
class WikipediaDatasetBatchRunResult:
    """Every row from one call to :func:`run_wikipedia_revision_history_batch`."""

    rows: tuple[WikipediaDatasetBatchRow, ...]

    def admitted_dates(self) -> tuple[date, ...]:
        return tuple(row.as_of for row in self.rows if row.outcome == BATCH_OUTCOME_ADMITTED)

    def by_outcome(self, outcome: str) -> tuple[WikipediaDatasetBatchRow, ...]:
        return tuple(row for row in self.rows if row.outcome == outcome)

    def to_dict(self) -> dict[str, object]:
        return {"rows": [row.to_dict() for row in self.rows]}


def run_wikipedia_revision_history_batch(
    as_of_dates: Sequence[date],
    *,
    store: WikipediaDatasetStore,
    secondary_match_at_or_before: Callable[[date], SecondaryCsvMatch],
    secondary_source_id: str,
    secondary_source_url: str,
    secondary_raw_bytes: bytes,
    secondary_fetched_at: datetime,
    page_title: str = DEFAULT_PAGE_TITLE,
    fetch_revision_at_or_before_fn: Callable[
        ..., WikipediaRevisionRef
    ] = fetch_revision_at_or_before,
    fetch_revision_wikitext_fn: Callable[
        ..., RawWikipediaRevisionFetch
    ] = fetch_revision_wikitext,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    timeout_seconds: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> WikipediaDatasetBatchRunResult:
    """Fetch, cross-check and archive one MEMBERSHIP observation per requested date.

    Deterministic, resumable and idempotent:

    * dates are processed in SORTED order regardless of the order requested,
      so two calls with the same set (in any order) behave identically;
    * a date already archived in ``store`` is RESUMED, not re-fetched --
      ``fetch_revision_at_or_before_fn``/``fetch_revision_wikitext_fn``/
      ``secondary_match_at_or_before`` are never called for it, so a later
      real-world page edit (or a later secondary-source update) can never
      retroactively replace an already-admitted historical observation
      through a resumed or retried run;
    * a date whose cross-check is not corroborated is refused
      (:data:`BATCH_OUTCOME_REFUSED_NOT_CORROBORATED`) and nothing is written
      for it -- exactly the same mandatory-corroboration gate
      :func:`cross_check_membership` already enforces, never weakened here;
    * a fetch/parse/verification failure for one date
      (:data:`BATCH_OUTCOME_FAILED`) does not abort the whole batch -- the
      remaining requested dates are still attempted, and the failure is
      recorded, never silently swallowed.

    ``secondary_raw_bytes`` is the independent secondary source fetched ONCE
    for the whole run (the community CSV does not vary per date) and is
    archived, checksum-bound, into every admitted entry's
    :class:`SecondaryProvenance` -- unlike a MediaWiki revision, that source
    lives on a mutable branch and can change later, so the cross-check can
    only be reproduced afterwards if the exact bytes it used are retained.

    ``fetch_revision_at_or_before_fn``/``fetch_revision_wikitext_fn``/
    ``secondary_match_at_or_before`` are injectable specifically so this
    function is fully unit-testable without any network call; production
    callers (``scripts/run_wikipedia_revision_history_batch.py``) pass the
    real fetch functions and a real independent-secondary-source lookup.
    """
    if len(set(as_of_dates)) != len(as_of_dates):
        raise SourceAdapterError(
            "duplicate as_of dates requested in one batch run; each date must"
            " appear at most once"
        )
    secondary_full_file_checksum = raw_artifact_checksum(secondary_raw_bytes)
    rows: list[WikipediaDatasetBatchRow] = []
    for as_of in sorted(as_of_dates):
        if store.has(as_of):
            existing = store.load(as_of)
            rows.append(
                WikipediaDatasetBatchRow(
                    as_of=as_of,
                    outcome=BATCH_OUTCOME_RESUMED,
                    detail=(
                        f"already archived at revision"
                        f" {existing.snapshot.revision.revision_id}; resumed,"
                        " not re-fetched"
                    ),
                    revision_id=existing.snapshot.revision.revision_id,
                    agreement_ratio=existing.cross_check.agreement_ratio,
                )
            )
            continue
        try:
            revision_ref = fetch_revision_at_or_before_fn(
                as_of, page_title=page_title, timeout_seconds=timeout_seconds,
                user_agent=user_agent,
            )
            fetched = fetch_revision_wikitext_fn(
                revision_ref, timeout_seconds=timeout_seconds, user_agent=user_agent
            )
            snapshot = fetched.snapshot
            match = secondary_match_at_or_before(as_of)
            cross_check = cross_check_membership(
                wikipedia=snapshot, secondary_tickers=match.tickers,
                secondary_source_id=secondary_source_id, as_of=as_of,
            )
        except SourceAdapterError as error:
            rows.append(
                WikipediaDatasetBatchRow(
                    as_of=as_of, outcome=BATCH_OUTCOME_FAILED, detail=str(error)
                )
            )
            continue
        if not cross_check.corroborated:
            rows.append(
                WikipediaDatasetBatchRow(
                    as_of=as_of,
                    outcome=BATCH_OUTCOME_REFUSED_NOT_CORROBORATED,
                    detail=cross_check.detail,
                    revision_id=snapshot.revision.revision_id,
                    agreement_ratio=cross_check.agreement_ratio,
                )
            )
            continue
        raw_path = store._raw_path(snapshot.revision.revision_id)  # noqa: SLF001
        secondary_provenance = SecondaryProvenance(
            source_id=secondary_source_id,
            source_url=secondary_source_url,
            fetched_at=secondary_fetched_at,
            full_file_checksum=secondary_full_file_checksum,
            raw_artifact_path=str(store._secondary_raw_path(secondary_full_file_checksum)),  # noqa: SLF001
            matched_row_date=match.matched_date,
            matched_row_checksum=raw_artifact_checksum(match.matched_line.encode("utf-8")),
        )
        entry = WikipediaDatasetEntry(
            as_of=as_of, snapshot=snapshot, cross_check=cross_check,
            fetch_observed_at=now(), raw_provenance_path=str(raw_path),
            secondary_provenance=secondary_provenance,
        )
        store.accept(entry, fetched.raw_bytes, secondary_raw_bytes=secondary_raw_bytes)
        rows.append(
            WikipediaDatasetBatchRow(
                as_of=as_of, outcome=BATCH_OUTCOME_ADMITTED, detail=cross_check.detail,
                revision_id=snapshot.revision.revision_id,
                agreement_ratio=cross_check.agreement_ratio,
            )
        )
    return WikipediaDatasetBatchRunResult(rows=tuple(rows))


def build_dataset_manifest(
    entries: Sequence[WikipediaDatasetEntry],
    *,
    requested_as_of_dates: Sequence[date] = (),
    other_rows: Sequence[WikipediaDatasetBatchRow] = (),
) -> WikipediaDatasetManifest:
    """A deterministic, reproducible manifest over a set of admitted entries.

    Pure function of its inputs: re-running it over byte-identical archived
    entries (the write-once/idempotent guarantee :class:`WikipediaDatasetStore`
    already provides) produces an identical ``content_fingerprint`` every
    time -- re-running the batch build never silently changes previously
    recorded snapshot provenance, and this manifest cannot either.

    Both the per-date records and the fingerprint material commit to each
    entry's own ``entry_checksum`` (:meth:`WikipediaDatasetEntry.compute_checksum`)
    -- which already covers the full cross-check ticker partition and the
    secondary-source binding, not merely the summary ``agreement_ratio`` --
    so two admitted entries that differ in ANY evidentiary respect (a
    different agreement/disagreement partition, a different secondary
    artifact, a different matched row) can never collide into the same
    dataset-level fingerprint merely because their ratios happen to match.
    """
    ordered_entries = tuple(sorted(entries, key=lambda entry: entry.as_of))
    covered = tuple(entry.as_of for entry in ordered_entries)
    if len(set(covered)) != len(covered):
        raise SourceAdapterError(
            "dataset manifest cannot be built over duplicate as_of entries"
        )
    requested = tuple(sorted(set(requested_as_of_dates)))
    covered_set = set(covered)
    gaps = tuple(
        f"{requested_date.isoformat()}: requested but not admitted"
        for requested_date in requested
        if requested_date not in covered_set
    )
    ratios = [entry.cross_check.agreement_ratio for entry in ordered_entries]
    disagreement_total = sum(
        len(entry.cross_check.only_in_wikipedia) + len(entry.cross_check.only_in_secondary)
        for entry in ordered_entries
    )
    aggregate: dict[str, object] = {
        "admitted_count": len(ordered_entries),
        "requested_count": len(requested),
        "gap_count": len(gaps),
        "other_row_count": len(other_rows),
        "other_rows_by_outcome": {
            outcome: sum(1 for row in other_rows if row.outcome == outcome)
            for outcome in sorted(_VALID_BATCH_OUTCOMES)
        },
        "mean_agreement_ratio": (sum(ratios) / len(ratios)) if ratios else None,
        "min_agreement_ratio": min(ratios) if ratios else None,
        "disagreement_observation_count_total": disagreement_total,
        "distinct_revision_ids": sorted({e.snapshot.revision.revision_id for e in ordered_entries}),
        "distinct_secondary_source_ids": sorted(
            {e.secondary_provenance.source_id for e in ordered_entries}
        ),
        "distinct_secondary_full_file_checksums": sorted(
            {e.secondary_provenance.full_file_checksum for e in ordered_entries}
        ),
    }
    per_date_records = tuple(
        {
            "as_of": entry.as_of.isoformat(),
            "entry_checksum": entry.compute_checksum(),
            "revision_id": entry.snapshot.revision.revision_id,
            "revision_timestamp": entry.snapshot.revision.revision_timestamp.isoformat(),
            "raw_checksum": entry.snapshot.raw_checksum,
            "raw_provenance_path": entry.raw_provenance_path,
            "secondary_source_id": entry.cross_check.secondary_source_id,
            "secondary_source_url": entry.secondary_provenance.source_url,
            "secondary_full_file_checksum": entry.secondary_provenance.full_file_checksum,
            "secondary_raw_artifact_path": entry.secondary_provenance.raw_artifact_path,
            "secondary_matched_row_date": entry.secondary_provenance.matched_row_date.isoformat(),
            "secondary_matched_row_checksum": entry.secondary_provenance.matched_row_checksum,
            "agreement_ratio": entry.cross_check.agreement_ratio,
            "corroborated": entry.cross_check.corroborated,
            "agreement_count": len(entry.cross_check.agreements),
            "only_in_wikipedia_count": len(entry.cross_check.only_in_wikipedia),
            "only_in_secondary_count": len(entry.cross_check.only_in_secondary),
        }
        for entry in ordered_entries
    )
    fingerprint_material = [
        {
            "as_of": entry.as_of.isoformat(),
            "entry_checksum": entry.compute_checksum(),
            "revision_id": entry.snapshot.revision.revision_id,
            "revision_timestamp": entry.snapshot.revision.revision_timestamp.isoformat(),
            "raw_checksum": entry.snapshot.raw_checksum,
            "secondary_source_id": entry.cross_check.secondary_source_id,
            "secondary_full_file_checksum": entry.secondary_provenance.full_file_checksum,
            "secondary_matched_row_checksum": entry.secondary_provenance.matched_row_checksum,
            "agreement_ratio": entry.cross_check.agreement_ratio,
        }
        for entry in ordered_entries
    ]
    serialized = json.dumps(
        fingerprint_material, sort_keys=True, separators=(",", ":")
    ).encode("ascii", "backslashreplace")
    content_fingerprint = "sha256:" + hashlib.sha256(serialized).hexdigest()
    return WikipediaDatasetManifest(
        schema_version=DATASET_MANIFEST_SCHEMA_VERSION,
        classification=CLASSIFICATION,
        backtest_quality="APPROXIMATE",
        date_coverage=covered,
        requested_dates=requested,
        gaps=gaps,
        entries=per_date_records,
        aggregate=aggregate,
        content_fingerprint=content_fingerprint,
        window_definition_status=WINDOW_DEFINITION_REQUIRED,
    )


@dataclass(frozen=True, slots=True)
class WikipediaDatasetManifest:
    """Dataset-level coverage/aggregate/reproducibility record over a batch run.

    Never claims official evidence or PIT_STRICT quality (``eligibility`` in
    :meth:`to_dict` is always the same all-``False`` record every other type
    in this module carries) and always names the
    :data:`WINDOW_DEFINITION_REQUIRED` gap so a reader of the manifest alone
    -- without also reading this module -- cannot mistake partial date
    coverage for a completed full-window dataset.
    """

    schema_version: str
    classification: str
    backtest_quality: str
    date_coverage: tuple[date, ...]
    requested_dates: tuple[date, ...]
    gaps: tuple[str, ...]
    #: One record per admitted entry (sorted by ``as_of``), carrying that
    #: entry's own ``entry_checksum`` and secondary-source binding -- see
    #: :func:`build_dataset_manifest`.
    entries: tuple[Mapping[str, object], ...]
    aggregate: Mapping[str, object]
    content_fingerprint: str
    window_definition_status: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "classification": self.classification,
            "backtest_quality": self.backtest_quality,
            "date_coverage": [d.isoformat() for d in self.date_coverage],
            "date_coverage_count": len(self.date_coverage),
            "requested_dates": [d.isoformat() for d in self.requested_dates],
            "requested_date_count": len(self.requested_dates),
            "gaps": list(self.gaps),
            "entries": [dict(entry) for entry in self.entries],
            "aggregate": dict(self.aggregate),
            "content_fingerprint": self.content_fingerprint,
            "window_definition_status": dict(self.window_definition_status),
            "eligibility": {
                "authoritative_official_evidence": False,
                "eligible_for_promotion_without_validation": False,
                "substitute_for_historical_gics": False,
            },
        }


# -- the evaluation record (referenced from ASOF_GICS_BLOCKER) ---------------

#: This is a research-path EVALUATION, not a capability PASS. It never flips
#: ``asof_gics`` / ``pit_sp500_membership`` in ``dataset_v1.py``, and it never
#: substitutes for ``ASOF_GICS_BLOCKER`` -- both stay BLOCKED_EXTERNAL /
#: LICENSE_REQUIRED / owner-gated for the OFFICIAL requirement. This record
#: exists so the "no free path at all" conclusion is replaced by an
#: evidence-bounded one: a free, legitimate, cross-checked SECONDARY path DOES
#: exist for MEMBERSHIP and IS usable for APPROXIMATE-quality research, per
#: ADR-0017 -- backed by one real, recorded experiment
#: (``real_experiment`` below), not fixture machinery alone. GICS/sector from
#: this source is explicitly NOT part of that claim (see ``sector_gics``).
WIKIPEDIA_REVISION_HISTORY_EVALUATION: dict[str, object] = {
    "capability": "wikipedia_revision_history_secondary_pit_membership",
    "state": "SECONDARY_RESEARCH_AVAILABLE",
    "scope": "MEMBERSHIP ONLY -- see 'sector_gics' for why GICS/sector is separate",
    "classification": CLASSIFICATION,
    "evaluated_at": "2026-09-07",
    "real_experiment": {
        "performed_at": "2026-09-06",
        "performed_by": "scripts/run_wikipedia_revision_history_experiment.py",
        "evidence_path": (
            "docs/poi/evidence/dataset-v1/wikipedia-revision-history/"
            "experiment-2023-12-31.json"
        ),
        "summary": (
            "One real, bounded experiment was fetched and recorded (not just"
            " fixture-tested): a real MediaWiki revision of 'List of S&P 500"
            " companies' at or before 2023-12-31, with its page identity and"
            " revision timestamp independently verified against MediaWiki's own"
            " metadata for that exact revision id, cross-checked against the"
            " independent fja05680/sp500 community CSV's own row at or before"
            " the same date. See the evidence_path above for the full result"
            " (revision id, timestamps, checksums, cross-check ratio)."
        ),
    },
    "mechanism": (
        "MediaWiki's public, documented revisions API (action=query/parse with"
        " oldid) returns the exact wikitext of 'List of S&P 500 companies' AS IT"
        " STOOD at one specific, immutable, timestamped revision. Binding a"
        " historical decision date to the latest revision at or before that date"
        " (select_revision_as_of; never a later one) yields a genuinely dated"
        " observation of what the page asserted about constituent membership at"
        " that time -- distinct from scraping the CURRENT page"
        " (sp500_current_universe.py), which carries no effective/knowledge date"
        " and would back-project today's classification onto history."
        " fetch_revision_wikitext independently verifies the page identity and"
        " revision timestamp against MediaWiki's own metadata for the exact"
        " revision id before binding either to the fetched content -- neither is"
        " trusted from a caller's claim."
    ),
    "why_secondary_not_official": (
        "Wikipedia is a community-maintained encyclopedia, not S&P Dow Jones"
        " Indices. Editors may have been slow, inconsistent, or wrong when they"
        " updated the table, and there is no publisher guarantee of accuracy or"
        " completeness. A revision's timestamp only proves WHEN the page"
        " asserted a fact, never that the fact was correct at that time."
    ),
    "requires_cross_check": True,
    "cross_check_mechanism": (
        "trading_market_data_core.dataset_v1_wikipedia_pit.cross_check_membership"
        " compares the revision-bound ticker set against an independent secondary"
        " source (the fja05680/sp500 community CSV on GitHub -- also recorded as"
        " a SECONDARY_RESEARCH_PIT candidate in"
        " config/data-sources/sp500-historical-source-registry.json) and records"
        " agreement/disagreement counts explicitly; it never silently drops a"
        " disagreement, and corroborated=False below the agreement threshold"
        " blocks use, it does not merely warn. Exercised for real in"
        " 'real_experiment' above."
    ),
    "sector_gics": {
        "status": "ADMITTED_SECONDARY_RESEARCH_PIT_APPROXIMATE",
        "scope": "GICS Sector only; GICS Sub-Industry remains unadmitted",
        "independent_source_id": "sec-edgar-nport-select-sector-spdr",
        "evidence_path": (
            "docs/poi/evidence/dataset-v1/sec-sector-spdr-gics/"
            "experiment-2023-12-31.json"
        ),
        "coverage_ratio": 0.9582504970178927,
        "agreement_ratio": 1.0,
        "covered_tickers": 482,
        "uncovered_tickers": 21,
        "explanation": (
            "A real bounded 2023-12-31 experiment cross-checked the revision's"
            " GICS Sector values against all 11 Select Sector SPDR funds'"
            " contemporaneous SEC EDGAR N-PORT-P filings. Each raw filing is"
            " retained and SHA-256-bound. Conservative generic identity"
            " resolution covered 482/503 tickers (95.83%), above the canonical"
            " 90% floor, and all 482 covered tickers agreed on sector (100%)."
            " The 21 unresolved/ambiguous tickers remain explicitly uncovered;"
            " none are guessed. This admits sector only as"
            " SECONDARY_RESEARCH_PIT / BacktestQuality.APPROXIMATE; it does not"
            " admit GICS Sub-Industry or close the official/licensed PIT_STRICT"
            " historical GICS requirement."
        ),
    },
    "usable_for": "BacktestQuality.APPROXIMATE research/backtest of MEMBERSHIP and empirically corroborated GICS Sector at recorded dates (ADR-0017)",
    "not_usable_for": (
        "BacktestQuality.PIT_STRICT",
        "the asof_gics capability (stays BLOCKED_EXTERNAL / LICENSE_REQUIRED)",
        "the pit_sp500_membership capability (stays owner-gated on official"
        " S&P DJI announcements)",
        "GICS Sub-Industry historical assignment evidence",
        "final OOS/shadow qualification evidence",
        "any claim of S&P DJI official truth",
    ),
    "eligibility": {
        "authoritative_official_evidence": False,
        "eligible_for_promotion_without_validation": False,
        "substitute_for_historical_gics": False,
    },
    "official_licensed_gap_status": "DEFERRED_NOT_BLOCKING",
    "fetch_mechanism": (
        "fetch_revision_at_or_before / fetch_revision_metadata /"
        " fetch_revision_wikitext in this module -- owner/agent-run, real"
        " network calls against the public MediaWiki API (no authentication, no"
        " bot-protection bypass); unit tests use recorded wikitext/JSON fixtures"
        " and never call these. Exercised for real in 'real_experiment' above."
    ),
    "batch_dataset": {
        "machinery": (
            "WikipediaDatasetEntry / WikipediaDatasetStore /"
            " run_wikipedia_revision_history_batch / build_dataset_manifest"
            " (this module) plus scripts/run_wikipedia_revision_history_batch.py:"
            " a deterministic, resumable, idempotent, write-once batch path"
            " that fetches, independently verifies, cross-checks and archives"
            " one MEMBERSHIP research observation per requested --as-of date,"
            " and a reproducible dataset-level manifest (date coverage, gaps,"
            " per-date agreement, aggregate disagreement counts, and a content"
            " fingerprint that does not change on re-run over identical"
            " archived evidence). Built 2026-09-06"
            " (A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD) and covered by"
            " offline, fixture-based tests, including tests proving a later"
            " revision cannot enter the dataset through a retry, a resumed"
            " run, or a hand-imported entry file."
        ),
        "full_window_status": WINDOW_DEFINITION_REQUIRED,
    },
    "next_bounded_task": (
        "The batch MACHINERY for A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD"
        " is now built and tested (see 'batch_dataset' above); the FULL-WINDOW"
        " empirical dataset itself is NOT built, because no owner-approved"
        " absolute start/end date for the frozen strategy's full backtest"
        " window exists on main to fetch it against (see"
        " WINDOW_DEFINITION_REQUIRED under 'batch_dataset'.'full_window_status')."
        " The real_experiment above still covers exactly one date"
        " (2023-12-31), unchanged. Once the owner (or an approved ADR) supplies"
        " that absolute window, run"
        " scripts/run_wikipedia_revision_history_batch.py over the resulting"
        " calendar decision-date grid to build and record the"
        " APPROXIMATE-quality MEMBERSHIP research dataset under"
        " docs/poi/evidence/; the bounded 2023-12-31 GICS Sector cross-check is"
        " now empirically admitted as SECONDARY_RESEARCH_PIT / APPROXIMATE;"
        " extending it across the eventual full window must reuse the same"
        " provenance and canonical gates. The official/licensed gap stays"
        " separately tracked in"
        " ASOF_GICS_BLOCKER and is not closed by either."
    ),
}


__all__ = [
    "BATCH_OUTCOME_ADMITTED",
    "BATCH_OUTCOME_FAILED",
    "BATCH_OUTCOME_REFUSED_NOT_CORROBORATED",
    "BATCH_OUTCOME_RESUMED",
    "CLASSIFICATION",
    "DATASET_MANIFEST_SCHEMA_VERSION",
    "DEFAULT_PAGE_TITLE",
    "MINIMUM_CROSS_CHECK_AGREEMENT_RATIO",
    "MINIMUM_SECTOR_AGREEMENT_RATIO",
    "MINIMUM_SECTOR_COVERAGE_RATIO",
    "WIKIPEDIA_DATASET_ENTRY_SCHEMA_VERSION",
    "WIKIPEDIA_PIT_SCHEMA_VERSION",
    "WIKIPEDIA_REVISION_HISTORY_EVALUATION",
    "WIKIPEDIA_REVISION_LOOKUP_API_URL",
    "WIKIPEDIA_REVISION_METADATA_API_URL",
    "WIKIPEDIA_REVISION_PARSE_API_URL",
    "WINDOW_DEFINITION_REQUIRED",
    "CrossCheckResult",
    "RawWikipediaRevisionFetch",
    "SecondaryCsvMatch",
    "SecondaryProvenance",
    "SectorCrossCheckResult",
    "WikipediaConstituentObservation",
    "WikipediaDatasetAcceptResult",
    "WikipediaDatasetBatchRow",
    "WikipediaDatasetBatchRunResult",
    "WikipediaDatasetEntry",
    "WikipediaDatasetManifest",
    "WikipediaDatasetStore",
    "WikipediaRevisionRef",
    "WikipediaRevisionSnapshot",
    "build_dataset_manifest",
    "cross_check_membership",
    "cross_check_sectors",
    "fetch_revision_at_or_before",
    "fetch_revision_metadata",
    "fetch_revision_wikitext",
    "parse_revision_snapshot",
    "run_wikipedia_revision_history_batch",
    "secondary_csv_tickers_at_or_before",
    "select_revision_as_of",
]
