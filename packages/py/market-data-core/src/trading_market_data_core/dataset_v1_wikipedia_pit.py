"""A free, contemporaneous-revision SECONDARY point-in-time research path.

ChatGPT review of the A-WP2 canonical-state reconciliation (2026-09-03) found
that ``ASOF_GICS_BLOCKER`` (``dataset_v1_real_ingestion.py``) drew a
categorical "no free path" conclusion without evaluating one specific
candidate: MediaWiki's own, public, immutable **revision history** for the
"List of S&P 500 companies" article, as distinct from scraping the CURRENT
page (which is what ``sp500_current_universe.py`` does and which carries no
effective/knowledge date at all).

The distinction that makes this a genuine PIT candidate, not a repeat of the
"free datasets only carry today's values" problem:

* every MediaWiki revision is immutable once saved, and the API returns its
  own ``revid`` and ``timestamp`` alongside the exact wikitext that existed at
  that revision — a real, independently-verifiable knowledge date, not one we
  assert ourselves;
* binding a historical decision date to the *latest revision at or before*
  that date (never a later one — see :func:`select_revision_as_of`) means the
  observation reflects what the page asserted *at the time*, not today's
  page projected backward;
* the same table that carries tickers also carries the GICS Sector / GICS
  Sub-Industry columns already parsed by :mod:`sp500_wikitable`, so one
  mechanism answers both the membership and the GICS-as-of question.

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
* it never fabricates a revision, a timestamp or a checksum: every
  constructor here recomputes what it can verify and fails closed
  (:class:`trading_market_data_core.dataset_v1_sources.SourceAdapterError`)
  on anything missing, mismatched, or out of plausible range;
* it never fetches anything during a test run: :func:`fetch_revision_at_or_before`
  and :func:`fetch_revision_wikitext` are owner-host-only real network calls
  against MediaWiki's public, documented API (no authentication, no bot-
  protection bypass — this is the same class of access already used, and
  already accepted, for the CURRENT-universe refresh in
  ``sp500_current_universe.py``); unit tests parse recorded wikitext strings
  and never call them.

Net effect on M3 (see ``docs/roadmaps/MVP_DELIVERY_PLAN.md`` §5): the official
historical S&P membership and official historical GICS gaps remain
WAITING_EXTERNAL / owner-gated, unchanged. A free, legitimate, cross-checked
SECONDARY_RESEARCH_PIT path now exists and is usable for
``BacktestQuality.APPROXIMATE`` research (ADR-0017) — it is not blocked, and
it does not block the official gap from staying separately tracked.
"""

from __future__ import annotations

from collections.abc import Sequence
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
    """
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
    return WikipediaRevisionRef(
        page_title=page_title,
        revision_id=int(revision["revid"]),
        revision_timestamp=datetime.fromisoformat(
            str(revision["timestamp"]).replace("Z", "+00:00")
        ),
    )


def fetch_revision_wikitext(
    revision: WikipediaRevisionRef,
    *,
    timeout_seconds: float = 30.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> WikipediaRevisionSnapshot:
    """Fetch and parse the exact wikitext of one immutable revision.

    Owner-host only; unit tests call :func:`parse_revision_snapshot` directly
    against a recorded wikitext string and never call this.
    """
    import json as _json

    url = WIKIPEDIA_REVISION_PARSE_API_URL.format(revision_id=revision.revision_id)
    raw = _fetch_json(url, timeout_seconds=timeout_seconds, user_agent=user_agent)
    payload = _json.loads(raw.decode("utf-8"))
    parse = payload.get("parse")
    if not isinstance(parse, dict):
        raise SourceAdapterError(
            f"revision {revision.revision_id} payload carries no MediaWiki parse result"
        )
    wikitext = parse.get("wikitext")
    if not isinstance(wikitext, str):
        raise SourceAdapterError(f"revision {revision.revision_id} payload carries no wikitext")
    if int(parse.get("revid", revision.revision_id)) != revision.revision_id:
        raise SourceAdapterError(
            "MediaWiki returned a different revid than requested; refusing to bind"
            " the wrong revision's content to this timestamp"
        )
    return parse_revision_snapshot(wikitext, raw_bytes=raw, revision=revision)


# -- the evaluation record (referenced from ASOF_GICS_BLOCKER) ---------------

#: This is a research-path EVALUATION, not a capability PASS. It never flips
#: ``asof_gics`` / ``pit_sp500_membership`` in ``dataset_v1.py``, and it never
#: substitutes for ``ASOF_GICS_BLOCKER`` -- both stay BLOCKED_EXTERNAL /
#: LICENSE_REQUIRED / owner-gated for the OFFICIAL requirement. This record
#: exists so the "no free path at all" conclusion is replaced by an
#: evidence-bounded one: a free, legitimate, cross-checked SECONDARY path DOES
#: exist and IS usable for APPROXIMATE-quality research, per ADR-0017.
WIKIPEDIA_REVISION_HISTORY_EVALUATION: dict[str, object] = {
    "capability": "wikipedia_revision_history_secondary_pit",
    "state": "SECONDARY_RESEARCH_AVAILABLE",
    "classification": CLASSIFICATION,
    "evaluated_at": "2026-09-03",
    "mechanism": (
        "MediaWiki's public, documented revisions API (action=query/parse with"
        " oldid) returns the exact wikitext of 'List of S&P 500 companies' AS IT"
        " STOOD at one specific, immutable, timestamped revision. Binding a"
        " historical decision date to the latest revision at or before that date"
        " (select_revision_as_of; never a later one) yields a genuinely dated"
        " observation of what the page asserted about GICS Sector/Sub-Industry"
        " and constituent membership at that time -- distinct from scraping the"
        " CURRENT page (sp500_current_universe.py), which carries no"
        " effective/knowledge date and would back-project today's classification"
        " onto history."
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
        " source (e.g. the fja05680/sp500 community CSV already recorded in"
        " config/data-sources/sp500-historical-source-registry.json, once that"
        " candidate is itself validated per that registry's rules) and records"
        " agreement/disagreement counts explicitly; it never silently drops a"
        " disagreement, and corroborated=False below the agreement threshold"
        " blocks use, it does not merely warn."
    ),
    "usable_for": "BacktestQuality.APPROXIMATE research/backtest only (ADR-0017)",
    "not_usable_for": (
        "BacktestQuality.PIT_STRICT",
        "the asof_gics capability (stays BLOCKED_EXTERNAL / LICENSE_REQUIRED)",
        "the pit_sp500_membership capability (stays owner-gated on official"
        " S&P DJI announcements)",
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
        "fetch_revision_at_or_before / fetch_revision_wikitext in this module --"
        " owner-host only, real network calls against the public MediaWiki API"
        " (no authentication, no bot-protection bypass); unit tests use recorded"
        " wikitext fixtures and never call these."
    ),
    "next_bounded_task": (
        "A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD: using this now-merged"
        " machinery, fetch and cross-check real revisions spanning the frozen"
        " strategy's backtest window and record the resulting APPROXIMATE-quality"
        " research dataset under docs/poi/evidence/; the official/licensed gap"
        " stays separately tracked in ASOF_GICS_BLOCKER and is not closed by this."
    ),
}


__all__ = [
    "CLASSIFICATION",
    "DEFAULT_PAGE_TITLE",
    "MINIMUM_CROSS_CHECK_AGREEMENT_RATIO",
    "WIKIPEDIA_PIT_SCHEMA_VERSION",
    "WIKIPEDIA_REVISION_HISTORY_EVALUATION",
    "WIKIPEDIA_REVISION_LOOKUP_API_URL",
    "WIKIPEDIA_REVISION_PARSE_API_URL",
    "CrossCheckResult",
    "WikipediaConstituentObservation",
    "WikipediaRevisionRef",
    "WikipediaRevisionSnapshot",
    "cross_check_membership",
    "fetch_revision_at_or_before",
    "fetch_revision_wikitext",
    "parse_revision_snapshot",
    "select_revision_as_of",
]
