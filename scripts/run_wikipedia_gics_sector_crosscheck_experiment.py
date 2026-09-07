"""Run and record ONE bounded, real GICS-sector cross-check experiment.

A-WP2-FREE-HISTORICAL-GICS-CROSSCHECK-EVAL. ``dataset_v1_wikipedia_pit.py``
already captures GICS Sector/Sub-Industry from every Wikipedia revision it
parses, but kept that data ``CAPTURED_UNEVALUATED_NOT_ADMITTED`` because no
independent, dated, free GICS source had been identified to cross-check it
against (``cross_check_sectors`` existed, deterministic and tested, but had
never been run against a real independent source).

This script closes exactly ONE narrowly justified instance of that gap, per
the owner instructions ("choose only a narrowly justified historical test
date or classification-change case from source evidence itself" -- never a
fabricated full-window claim):

S&P Dow Jones Indices and MSCI -- the GICS standard's own official publishers
-- jointly announced, via a free, public, unauthenticated press release
(PRNewswire, no login, no paywall, no bot-protection bypass), a September
2018 GICS structure revision broadening "Telecommunication Services" into
"Communication Services". That release NAMES specific companies moving
sectors, e.g.: "certain companies currently classified in the Information
Technology Sector, including Alphabet Inc. and Facebook Inc." and "such as
Netflix Inc. and TripAdvisor Inc." (moving from the Consumer Discretionary
Sector's Internet & Direct Marketing Retail Sub-Industry) and "such as
Comcast Corp[.]" (moving from Consumer Discretionary's Media Industry Group).
See ``GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION`` below for the exact
quoted text this experiment relies on.

This is a genuinely free, dated, NAMED-COMPANY source -- distinct from, and
strictly narrower than, the licensed "company-level GICS assignment series ...
covering the backtest window" that ``ASOF_GICS_BLOCKER`` requires and that
stays LICENSE_REQUIRED. It only ever supports a claim about the SPECIFIC
companies it names, at the SPECIFIC effective date it states -- never a
general, continuous, full-universe GICS cross-check. ``cross_check_sectors``'s
new ``scope_tickers`` parameter exists for exactly this shape of evidence.

What this experiment does, over the public, unauthenticated, documented APIs
only (no login, no bot-protection bypass, no scraping behind access
controls):

1. fetches the real MediaWiki revision of "List of S&P 500 companies" at or
   before ``--before-as-of`` (well before the reclassification's effective
   date) and at or before ``--after-as-of`` (well after it, to give Wikipedia
   editors time to catch up) -- both with page identity and revision
   timestamp VERIFIED against MediaWiki's own metadata
   (``dataset_v1_wikipedia_pit.fetch_revision_wikitext``);
2. fetches the real press release bytes from PRNewswire (the free,
   unauthenticated distribution channel S&P DJI/MSCI themselves used) and
   retains them, checksum-bound, as immutable secondary provenance;
3. cross-checks the BEFORE snapshot's GICS Sector values for the named
   companies against their press-release-stated OLD sector, and the AFTER
   snapshot's values against their press-release-stated NEW sector
   ("Communication Services") -- both via
   ``dataset_v1_wikipedia_pit.cross_check_sectors(..., scope_tickers=...)``,
   scoped to exactly the named tickers, never the full ~500-ticker universe;
4. records BOTH results explicitly, including any disagreement found --
   never silently dropped, and never forced to agree.

Scope, stated precisely so it cannot be over-read:

* this is ONE bounded experiment for ONE known reclassification event, not a
  repeating archive and not a general cross-check capability;
* a result here is permanently SECONDARY_RESEARCH_PIT /
  ``BacktestQuality.APPROXIMATE`` research evidence, bounded to the named
  tickers and this one event -- it never becomes official and never flips
  ``asof_gics`` / ``pit_sp500_membership`` in ``dataset_v1.py``, nor
  ``ASOF_GICS_BLOCKER``'s ``BLOCKED_EXTERNAL`` / ``LICENSE_REQUIRED`` state;
* it does NOT establish a general, continuous, full-universe GICS cross-check
  path -- ``WIKIPEDIA_REVISION_HISTORY_EVALUATION["sector_gics"]`` still
  records the general claim as ``CAPTURED_UNEVALUATED_NOT_ADMITTED``;
* a disagreement found here is recorded as evidence, not hidden -- see the
  module docstring for why (Wikipedia may be slow, inconsistent or wrong).

Never run in CI or by any test: this makes real network calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages/py/market-data-core/src"))
sys.path.insert(0, str(REPO_ROOT / "packages/py/contracts/src"))
sys.path.insert(0, str(REPO_ROOT / "packages/py/domain/src"))

from trading_market_data_core.dataset_v1_wikipedia_pit import (  # noqa: E402
    DEFAULT_PAGE_TITLE,
    MINIMUM_SECTOR_AGREEMENT_RATIO,
    MINIMUM_SECTOR_COVERAGE_RATIO,
    WIKIPEDIA_PIT_SCHEMA_VERSION,
    cross_check_sectors,
    fetch_revision_at_or_before,
    fetch_revision_wikitext,
)

EXPERIMENT_SCHEMA_VERSION = "wikipedia-gics-sector-crosscheck-experiment-1.0.0"

DEFAULT_EVIDENCE_DIR = Path("docs/poi/evidence/dataset-v1/wikipedia-gics-sector-crosscheck")
DEFAULT_USER_AGENT = (
    "TradingSystem-DatasetV1/1.0 (gics-sector-crosscheck-research; contact via repo)"
)

#: The free, public, unauthenticated PRNewswire distribution of S&P DJI/MSCI's
#: own January 2018 announcement of the September 2018 GICS structure
#: revision. No login, no paywall, no bot-protection bypass -- this is the
#: exact page S&P DJI/MSCI themselves used to publish it.
DEFAULT_PRESS_RELEASE_URL = (
    "https://www.prnewswire.com/news-releases/sp-dow-jones-indices-and-msci-announce"
    "-select-list-of-companies-changing-due-to-revisions-to-the-gics-structure-in"
    "-2018-300581770.html"
)
DEFAULT_PRESS_RELEASE_SOURCE_ID = "spdji-msci-gics-2018-communication-services-press-release"
DEFAULT_PRESS_RELEASE_PUBLISHED_AT = date(2018, 1, 11)
DEFAULT_PRESS_RELEASE_EFFECTIVE_AT = date(2018, 9, 28)

#: Hand-curated from the press release's own quoted text (verified against the
#: retained raw bytes at run time -- see ``_verify_quoted_claims``), never
#: invented: which named companies moved FROM which OLD GICS Sector TO
#: "Communication Services", per S&P DJI/MSCI's own free announcement.
#:
#: Naspers Ltd is named in the same sentence as Comcast Corp but is not an
#: S&P 500 constituent (JSE-listed) and carries no NYSE/Nasdaq ticker on the
#: Wikipedia table, so it is excluded from ``scope_tickers`` -- including it
#: would trip the "ambiguous identity" fail-closed check in
#: ``cross_check_sectors`` (a scoped ticker the snapshot cannot corroborate).
GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION: dict[str, dict[str, str]] = {
    "GOOGL": {
        "name": "Alphabet Inc Class A",
        "old_sector": "Information Technology",
        "new_sector": "Communication Services",
        "quoted_press_release_text": (
            "certain companies currently classified in the Information"
            " Technology Sector, including Alphabet Inc. and Facebook Inc."
        ),
    },
    "GOOG": {
        "name": "Alphabet Inc Class C",
        "old_sector": "Information Technology",
        "new_sector": "Communication Services",
        "quoted_press_release_text": (
            "certain companies currently classified in the Information"
            " Technology Sector, including Alphabet Inc. and Facebook Inc."
        ),
    },
    "FB": {
        "name": "Facebook Inc.",
        "old_sector": "Information Technology",
        "new_sector": "Communication Services",
        "quoted_press_release_text": (
            "certain companies currently classified in the Information"
            " Technology Sector, including Alphabet Inc. and Facebook Inc."
        ),
    },
    "NFLX": {
        "name": "Netflix Inc.",
        "old_sector": "Consumer Discretionary",
        "new_sector": "Communication Services",
        "quoted_press_release_text": (
            "select companies from the Internet & Direct Marketing Retail"
            " Sub-Industry such as Netflix Inc. and TripAdvisor Inc."
        ),
    },
    "TRIP": {
        "name": "TripAdvisor Inc.",
        "old_sector": "Consumer Discretionary",
        "new_sector": "Communication Services",
        "quoted_press_release_text": (
            "select companies from the Internet & Direct Marketing Retail"
            " Sub-Industry such as Netflix Inc. and TripAdvisor Inc."
        ),
    },
    "CMCSA": {
        "name": "Comcast Corp.",
        "old_sector": "Consumer Discretionary",
        "new_sector": "Communication Services",
        "quoted_press_release_text": (
            "companies selected from the Consumer Discretionary Sector"
            " currently classified under the Media Industry Group, such as"
            " Comcast Corp and Naspers Ltd"
        ),
    },
}

DEFAULT_BEFORE_AS_OF = date(2018, 6, 30)
DEFAULT_AFTER_AS_OF = date(2019, 1, 15)


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _fetch_bytes(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
    """One validated HTTPS GET. No auth, no bypass -- public raw content only."""
    if not url.lower().startswith("https://"):
        raise ValueError("the press release must be fetched over HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = int(response.status)
            raw = bytes(response.read())
    except urllib.error.HTTPError as error:
        raise ValueError(f"press release source returned HTTP {error.code}") from error
    except OSError as error:
        raise ValueError(f"press release source unreachable: {error}") from error
    if status != 200:
        raise ValueError(f"press release source returned HTTP {status}")
    return raw


def _verify_quoted_claims(press_release_raw: bytes) -> None:
    """Fail closed if the retained press release no longer contains the exact
    text every ``quoted_press_release_text`` entry above claims to quote --
    this is what stops the hand-curated mapping from silently drifting away
    from the actual retained evidence it claims to summarize.

    The retained bytes are raw HTML (e.g. ``&amp;`` for a literal ``&``);
    ``html.unescape`` normalizes entities before matching so the quoted text
    above can stay human-readable rather than HTML-entity-encoded, without
    weakening the check -- it still must be an exact substring of the actual
    retained page text, not a paraphrase.
    """
    import html as _html

    text = _html.unescape(press_release_raw.decode("utf-8", errors="replace"))
    for ticker, claim in GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION.items():
        quoted = claim["quoted_press_release_text"]
        if quoted not in text:
            raise ValueError(
                f"retained press release no longer contains the exact quoted text"
                f" claimed for {ticker!r}: {quoted!r}; refusing to trust a"
                " hand-curated mapping that has drifted from its own retained"
                " source evidence"
            )


def _fetch_and_verify_revision(
    as_of: date,
    *,
    page_title: str,
    evidence_dir: Path,
    user_agent: str,
    timeout_seconds: float,
) -> tuple[object, Path]:
    revision_ref = fetch_revision_at_or_before(
        as_of, page_title=page_title, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    fetched = fetch_revision_wikitext(
        revision_ref, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    snapshot = fetched.snapshot
    raw_sidecar_name = (
        f"{page_title.replace(' ', '_').replace('&', 'and')}"
        f"-rev{snapshot.revision.revision_id}.raw.json"
    )
    raw_sidecar_path = evidence_dir / raw_sidecar_name
    if raw_sidecar_path.exists():
        existing = raw_sidecar_path.read_bytes()
        if _sha256(existing) != snapshot.raw_checksum:
            raise ValueError(
                f"{raw_sidecar_path} already exists with different content than"
                " the freshly-fetched revision; refusing to overwrite immutable"
                " raw provenance"
            )
    else:
        assert snapshot.verify_raw_bytes(fetched.raw_bytes)
        raw_sidecar_path.write_bytes(fetched.raw_bytes)
    return snapshot, raw_sidecar_path


def run_experiment(
    *,
    before_as_of: date,
    after_as_of: date,
    page_title: str,
    press_release_url: str,
    press_release_source_id: str,
    press_release_published_at: date,
    press_release_effective_at: date,
    evidence_dir: Path,
    user_agent: str,
    timeout_seconds: float,
) -> dict[str, object]:
    if before_as_of >= press_release_effective_at:
        raise ValueError(
            f"--before-as-of {before_as_of.isoformat()} must be strictly before the"
            f" press release's stated effective date {press_release_effective_at.isoformat()}"
        )
    if after_as_of <= press_release_effective_at:
        raise ValueError(
            f"--after-as-of {after_as_of.isoformat()} must be strictly after the"
            f" press release's stated effective date {press_release_effective_at.isoformat()}"
        )
    performed_at = datetime.now(tz=UTC)
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # -- 1: the two primary, revision-bound Wikipedia observations ----------
    before_snapshot, before_raw_path = _fetch_and_verify_revision(
        before_as_of, page_title=page_title, evidence_dir=evidence_dir,
        user_agent=user_agent, timeout_seconds=timeout_seconds,
    )
    after_snapshot, after_raw_path = _fetch_and_verify_revision(
        after_as_of, page_title=page_title, evidence_dir=evidence_dir,
        user_agent=user_agent, timeout_seconds=timeout_seconds,
    )

    # -- 2: the free, dated, official-publisher press release ---------------
    press_release_raw = _fetch_bytes(
        press_release_url, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    _verify_quoted_claims(press_release_raw)
    press_release_checksum = _sha256(press_release_raw)
    press_release_raw_path = evidence_dir / "spdji-msci-gics-2018-communication-services.raw.html"
    if press_release_raw_path.exists():
        existing = press_release_raw_path.read_bytes()
        if _sha256(existing) != press_release_checksum:
            raise ValueError(
                f"{press_release_raw_path} already exists with different content"
                " than the freshly-fetched press release; refusing to overwrite"
                " immutable secondary raw provenance"
            )
    else:
        press_release_raw_path.write_bytes(press_release_raw)

    scope_tickers = frozenset(GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION)

    # -- 3: the two scoped, mandatory cross-checks ---------------------------
    before_secondary_sectors = {
        ticker: claim["old_sector"]
        for ticker, claim in GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION.items()
    }
    after_secondary_sectors = {
        ticker: claim["new_sector"]
        for ticker, claim in GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION.items()
    }
    before_cross_check = cross_check_sectors(
        wikipedia=before_snapshot,
        secondary_sectors=before_secondary_sectors,
        secondary_source_id=press_release_source_id,
        as_of=before_as_of,
        scope_tickers=scope_tickers,
    )
    after_cross_check = cross_check_sectors(
        wikipedia=after_snapshot,
        secondary_sectors=after_secondary_sectors,
        secondary_source_id=press_release_source_id,
        as_of=after_as_of,
        scope_tickers=scope_tickers,
    )

    # -- 4: the persisted, self-authenticating evidence record ---------------
    body: dict[str, object] = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "wikipedia_pit_schema_version": WIKIPEDIA_PIT_SCHEMA_VERSION,
        "artifact_type": "wikipedia_gics_sector_crosscheck_experiment",
        "classification": "SECONDARY_RESEARCH_PIT",
        "scope": (
            "ONE bounded real GICS-sector reclassification-event cross-check,"
            " scoped to the 6 named tickers in"
            " GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION; not a"
            " repeating archive and not a general full-universe GICS"
            " cross-check capability"
        ),
        "reclassification_event": {
            "name": "2018 GICS Communication Services Sector structure revision",
            "announced_at": "2017-11-15",
            "press_release_published_at": press_release_published_at.isoformat(),
            "press_release_effective_at": press_release_effective_at.isoformat(),
            "named_reclassifications": GICS_2018_COMMUNICATION_SERVICES_RECLASSIFICATION,
        },
        "press_release_source": {
            "source_id": press_release_source_id,
            "source_authority": "OFFICIAL_PUBLIC",
            "source_authority_note": (
                "The press release is S&P DJI/MSCI's own free, public,"
                " unauthenticated announcement of specific named company"
                " reclassifications -- an official-publisher document, but"
                " narrowly scoped to named companies at one event, never a"
                " substitute for the licensed continuous company-level GICS"
                " assignment series ASOF_GICS_BLOCKER requires."
            ),
            "source_url": press_release_url,
            "full_file_checksum": press_release_checksum,
            "raw_artifact_path": str(press_release_raw_path),
            "full_file_byte_length": len(press_release_raw),
        },
        "before_transition": {
            "requested_as_of": before_as_of.isoformat(),
            "revision_id": before_snapshot.revision.revision_id,
            "revision_timestamp": before_snapshot.revision.revision_timestamp.isoformat(),
            "raw_checksum": before_snapshot.raw_checksum,
            "raw_provenance_path": str(before_raw_path),
            "sector_cross_check": before_cross_check.to_dict(),
        },
        "after_transition": {
            "requested_as_of": after_as_of.isoformat(),
            "revision_id": after_snapshot.revision.revision_id,
            "revision_timestamp": after_snapshot.revision.revision_timestamp.isoformat(),
            "raw_checksum": after_snapshot.raw_checksum,
            "raw_provenance_path": str(after_raw_path),
            "sector_cross_check": after_cross_check.to_dict(),
        },
        "cross_check_thresholds": {
            "minimum_coverage_ratio": MINIMUM_SECTOR_COVERAGE_RATIO,
            "minimum_agreement_ratio": MINIMUM_SECTOR_AGREEMENT_RATIO,
        },
        "performed_at": performed_at.isoformat(),
        "eligibility": {
            "authoritative_official_evidence": False,
            "eligible_for_promotion_without_validation": False,
            "substitute_for_historical_gics": False,
        },
        "official_licensed_gap_status": "DEFERRED_NOT_BLOCKING",
        "usable_for": (
            "BacktestQuality.APPROXIMATE research, bounded to the 6 named"
            " tickers and this one 2018 reclassification event only"
        ),
        "not_usable_for": [
            "BacktestQuality.PIT_STRICT",
            "the asof_gics capability (stays BLOCKED_EXTERNAL / LICENSE_REQUIRED)",
            "a general, continuous, full-universe GICS/sector cross-check claim",
            "any classification-change case other than the one named here",
            "final OOS/shadow qualification evidence",
            "any claim of S&P DJI official truth about a DIFFERENT company or date",
        ],
    }
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
        "ascii", "backslashreplace"
    )
    body["evidence_checksum"] = _sha256(serialized)
    return body


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--before-as-of", type=date.fromisoformat, default=DEFAULT_BEFORE_AS_OF)
    parser.add_argument("--after-as-of", type=date.fromisoformat, default=DEFAULT_AFTER_AS_OF)
    parser.add_argument("--page-title", default=DEFAULT_PAGE_TITLE)
    parser.add_argument("--press-release-url", default=DEFAULT_PRESS_RELEASE_URL)
    parser.add_argument("--press-release-source-id", default=DEFAULT_PRESS_RELEASE_SOURCE_ID)
    parser.add_argument(
        "--press-release-published-at", type=date.fromisoformat,
        default=DEFAULT_PRESS_RELEASE_PUBLISHED_AT,
    )
    parser.add_argument(
        "--press-release-effective-at", type=date.fromisoformat,
        default=DEFAULT_PRESS_RELEASE_EFFECTIVE_AT,
    )
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--output", type=Path, default=None,
        help=(
            "evidence JSON path (default: <evidence-dir>/"
            "gics-sector-crosscheck-2018-communication-services.json)"
        ),
    )
    args = parser.parse_args(argv[1:])

    try:
        body = run_experiment(
            before_as_of=args.before_as_of,
            after_as_of=args.after_as_of,
            page_title=args.page_title,
            press_release_url=args.press_release_url,
            press_release_source_id=args.press_release_source_id,
            press_release_published_at=args.press_release_published_at,
            press_release_effective_at=args.press_release_effective_at,
            evidence_dir=args.evidence_dir,
            user_agent=args.user_agent,
            timeout_seconds=args.timeout_seconds,
        )
    except (ValueError, OSError) as error:
        print(f"EXPERIMENT FAILED (fail closed): {error}", file=sys.stderr)
        return 1

    output = args.output or (
        args.evidence_dir / "gics-sector-crosscheck-2018-communication-services.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    before_check = body["before_transition"]["sector_cross_check"]  # type: ignore[index]
    after_check = body["after_transition"]["sector_cross_check"]  # type: ignore[index]
    assert isinstance(before_check, dict)
    assert isinstance(after_check, dict)
    print(f"evidence written: {output}")
    print(
        f"before-transition corroborated: {before_check['corroborated']}"
        f" (ratio={before_check['agreement_ratio']},"
        f" disagreements={before_check['disagreement_count']})"
    )
    print(
        f"after-transition corroborated: {after_check['corroborated']}"
        f" (ratio={after_check['agreement_ratio']},"
        f" disagreements={after_check['disagreement_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
