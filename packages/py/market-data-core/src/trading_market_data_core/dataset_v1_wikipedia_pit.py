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

Scope: membership vs. GICS/sector. The same table that carries tickers also
carries the GICS Sector / GICS Sub-Industry columns already parsed by
:mod:`sp500_wikitable`, so every :class:`WikipediaRevisionSnapshot` CAPTURES
both. But only **membership** currently has a working independent
cross-check (:func:`cross_check_membership`) and is therefore admissible
SECONDARY research evidence. **GICS/sector-as-recorded is NOT currently
cross-checked** — no independent, dated, free GICS source has been
identified — and stays ``CAPTURED_UNEVALUATED_NOT_ADMITTED``
(``WIKIPEDIA_REVISION_HISTORY_EVALUATION``): it is retained on the snapshot
for future use, never claimed as corroborated evidence today.
:func:`cross_check_sectors` exists, is deterministic, and is tested, so that
claim can change the moment such a source exists — adding the function does
not, by itself, change it.

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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

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

    This exists so GICS/sector-as-recorded from a Wikipedia revision CAN be
    admitted as SECONDARY historical evidence once a real independent,
    dated sector source is available to run it against. Until then, no
    caller in this repository invokes this with anything but a fabricated or
    fixture mapping in a test: ``WIKIPEDIA_REVISION_HISTORY_EVALUATION``
    explicitly records that sector/GICS stays `CAPTURED_UNEVALUATED_NOT_ADMITTED`
    because no such independent dated source has been identified yet -- adding
    this function does not, by itself, change that claim.

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
    "evaluated_at": "2026-09-03",
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
        "status": "CAPTURED_UNEVALUATED_NOT_ADMITTED",
        "explanation": (
            "The same revision table carries GICS Sector / GICS Sub-Industry"
            " columns, so every parsed observation captures them (see"
            " primary_source.observations in the real_experiment evidence file)."
            " But NO independent, dated, free GICS source has been identified to"
            " cross-check them against, so sector/GICS values from this source"
            " are NOT admitted as corroborated SECONDARY historical evidence --"
            " they are retained on the snapshot for future use only."
            " cross_check_sectors (deterministic, coverage- and anomaly-aware,"
            " unit-tested) exists for the moment an independent dated GICS"
            " source is identified; adding that function does not, by itself,"
            " change this status."
        ),
    },
    "usable_for": "BacktestQuality.APPROXIMATE research/backtest of MEMBERSHIP only (ADR-0017)",
    "not_usable_for": (
        "BacktestQuality.PIT_STRICT",
        "the asof_gics capability (stays BLOCKED_EXTERNAL / LICENSE_REQUIRED)",
        "the pit_sp500_membership capability (stays owner-gated on official"
        " S&P DJI announcements)",
        "GICS/sector research evidence (see 'sector_gics': UNEVALUATED)",
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
    "next_bounded_task": (
        "A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD: using this now-merged"
        " and now-real-experiment-validated machinery, fetch and cross-check"
        " real revisions spanning the frozen strategy's full backtest window"
        " (the real_experiment above covers exactly one date) and record the"
        " resulting APPROXIMATE-quality MEMBERSHIP research dataset under"
        " docs/poi/evidence/; separately, identify and validate an independent"
        " dated GICS source before sector/GICS evidence from this path can be"
        " admitted. The official/licensed gap stays separately tracked in"
        " ASOF_GICS_BLOCKER and is not closed by either."
    ),
}


__all__ = [
    "CLASSIFICATION",
    "DEFAULT_PAGE_TITLE",
    "MINIMUM_CROSS_CHECK_AGREEMENT_RATIO",
    "MINIMUM_SECTOR_AGREEMENT_RATIO",
    "MINIMUM_SECTOR_COVERAGE_RATIO",
    "WIKIPEDIA_PIT_SCHEMA_VERSION",
    "WIKIPEDIA_REVISION_HISTORY_EVALUATION",
    "WIKIPEDIA_REVISION_LOOKUP_API_URL",
    "WIKIPEDIA_REVISION_METADATA_API_URL",
    "WIKIPEDIA_REVISION_PARSE_API_URL",
    "CrossCheckResult",
    "RawWikipediaRevisionFetch",
    "SectorCrossCheckResult",
    "WikipediaConstituentObservation",
    "WikipediaRevisionRef",
    "WikipediaRevisionSnapshot",
    "cross_check_membership",
    "cross_check_sectors",
    "fetch_revision_at_or_before",
    "fetch_revision_metadata",
    "fetch_revision_wikitext",
    "parse_revision_snapshot",
    "select_revision_as_of",
]
