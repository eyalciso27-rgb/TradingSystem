"""Build a real MULTI-DATE Wikipedia-revision SECONDARY PIT MEMBERSHIP dataset.

A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD extends the single, bounded
real experiment recorded by ``scripts/run_wikipedia_revision_history_experiment.py``
(``--as-of 2023-12-31``) into a deterministic, resumable and idempotent BATCH
path, using the same machinery
(``trading_market_data_core.dataset_v1_wikipedia_pit``):

1. for each ``--as-of`` date requested (explicitly, one per ``--as-of`` flag,
   or one per line in a ``--as-of-file``), ask MediaWiki for the latest
   revision of "List of S&P 500 companies" at or before that date (no
   look-ahead: never a later revision) and fetch+verify its wikitext exactly
   as the single-date experiment does;
2. cross-check each admitted snapshot against the SAME independent free
   secondary source (the fja05680/sp500 community-maintained CSV on GitHub,
   fetched ONCE for the whole run), using ``cross_check_membership`` --
   corroboration is mandatory, never weakened;
3. archive every corroborated observation, INCLUDING the exact independent
   secondary-source artifact and row that corroborated it (checksum-bound
   ``SecondaryProvenance`` -- unlike a MediaWiki revision, that community CSV
   lives on a mutable branch and can change later, so reproducing the
   cross-check afterwards needs the retained bytes, not just the URL), in a
   ``trading_market_data_core.dataset_v1_wikipedia_pit.WikipediaDatasetStore``
   under ``--dataset-dir``: write-once per ``as_of``, resumable (a date
   already archived is never re-fetched), and idempotent (re-running this
   script with the same dates and the same immutable MediaWiki/secondary-CSV
   content is a no-op, not a duplicate or a silent rewrite);
4. build and write a deterministic dataset-level manifest (coverage, gaps,
   per-date agreement, each entry's own cryptographic checksum and secondary
   binding, aggregate disagreement/anomaly counts, and a content fingerprint
   that does not change on re-run over identical archived evidence) via
   ``build_dataset_manifest``.

WHAT DATES TO PASS IS DELIBERATELY NOT DECIDED BY THIS SCRIPT. No
owner-approved ABSOLUTE start/end date for the frozen strategy's full
backtest window exists on main as of this task (see
``trading_market_data_core.dataset_v1_wikipedia_pit.WINDOW_DEFINITION_REQUIRED``);
inventing a cadence or a window here would be exactly the kind of fabricated
decision this project's rules forbid. This script is the reusable MACHINERY:
an owner/agent supplies the dates explicitly once an absolute window exists.

Scope, stated precisely so it cannot be over-read (identical to the single-
date experiment's scope, restated because it applies date-by-date here too):

* every admitted observation is permanently ``SECONDARY_RESEARCH_PIT`` /
  ``BacktestQuality.APPROXIMATE`` MEMBERSHIP research evidence -- never
  official S&P DJI truth, never ``PIT_STRICT``, and it never flips
  ``ASOF_GICS_BLOCKER`` or the ``asof_gics`` / ``pit_sp500_membership``
  capabilities;
* GICS Sector/Sub-Industry values ARE captured in each admitted snapshot but
  are NOT cross-checked here and stay ``CAPTURED_UNEVALUATED_NOT_ADMITTED``;
* a date whose cross-check does not meet the mandatory agreement threshold is
  refused, not silently dropped -- it is recorded in this run's summary and
  in the manifest's aggregate counts, but nothing is archived for it.

Never run in CI or by any test: this makes real network calls. Owner/agent-run
only, exactly like ``scripts/run_wikipedia_revision_history_experiment.py``.
"""

from __future__ import annotations

import argparse
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

from trading_market_data_core.dataset_v1_sources import SourceAdapterError  # noqa: E402
from trading_market_data_core.dataset_v1_wikipedia_pit import (  # noqa: E402
    BATCH_OUTCOME_ADMITTED,
    BATCH_OUTCOME_RESUMED,
    DEFAULT_PAGE_TITLE,
    WINDOW_DEFINITION_REQUIRED,
    SecondaryCsvMatch,
    WikipediaDatasetStore,
    build_dataset_manifest,
    run_wikipedia_revision_history_batch,
    secondary_csv_tickers_at_or_before,
)

DEFAULT_SECONDARY_SOURCE_ID = "fja05680-sp500-github-updated-csv"
DEFAULT_SECONDARY_CSV_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
DEFAULT_DATASET_DIR = Path("docs/poi/evidence/dataset-v1/wikipedia-revision-history/dataset")
DEFAULT_MANIFEST_PATH = Path(
    "docs/poi/evidence/dataset-v1/wikipedia-revision-history/dataset-manifest.json"
)
DEFAULT_USER_AGENT = "TradingSystem-DatasetV1/1.0 (wikipedia-revision-batch; contact via repo)"


def _fetch_bytes(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
    """One validated HTTPS GET. No auth, no bypass -- public raw content only.

    Deliberately duplicated from
    ``scripts/run_wikipedia_revision_history_experiment.py`` rather than
    imported -- the same small-helper duplication already made there between
    that script and ``dataset_v1_wikipedia_pit``'s own ``_fetch_json``.
    """
    if not url.lower().startswith("https://"):
        raise ValueError("the secondary source must be fetched over HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            status = int(response.status)
            raw = bytes(response.read())
    except urllib.error.HTTPError as error:
        raise ValueError(f"secondary source returned HTTP {error.code}") from error
    except OSError as error:
        raise ValueError(f"secondary source unreachable: {error}") from error
    if status != 200:
        raise ValueError(f"secondary source returned HTTP {status}")
    return raw


def _parse_as_of_dates(args: argparse.Namespace) -> list[date]:
    dates: list[date] = list(args.as_of or ())
    if args.as_of_file is not None:
        for line in args.as_of_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                dates.append(date.fromisoformat(stripped))
    if not dates:
        raise ValueError("at least one --as-of date (or a non-empty --as-of-file) is required")
    if len(set(dates)) != len(dates):
        raise ValueError("duplicate as_of dates requested; each date must appear once")
    return dates


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--as-of", type=date.fromisoformat, action="append",
        help="ISO date to fetch/cross-check/archive; may be repeated. No cadence is"
        " inferred -- pass every date explicitly.",
    )
    parser.add_argument(
        "--as-of-file", type=Path, default=None,
        help="a text file with one ISO date per line (blank lines and '#' comments ignored)",
    )
    parser.add_argument("--page-title", default=DEFAULT_PAGE_TITLE)
    parser.add_argument("--secondary-source-id", default=DEFAULT_SECONDARY_SOURCE_ID)
    parser.add_argument("--secondary-csv-url", default=DEFAULT_SECONDARY_CSV_URL)
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv[1:])

    try:
        as_of_dates = _parse_as_of_dates(args)
    except ValueError as error:
        print(f"BATCH FAILED (fail closed): {error}", file=sys.stderr)
        return 1

    store = WikipediaDatasetStore(args.dataset_dir)

    # Fetched ONCE per run, up front: every requested date's cross-check reads
    # the SAME immutable secondary-CSV bytes, so a run is fully reproducible
    # from one real fetch rather than re-fetching per date. The exact bytes
    # are archived and checksum-bound into every admitted entry's
    # SecondaryProvenance (WikipediaDatasetStore.accept) -- unlike a MediaWiki
    # revision, this CSV lives on a mutable branch and can change later, so
    # reproducing the cross-check afterwards requires the retained bytes, not
    # just the URL.
    secondary_fetched_at = datetime.now(tz=UTC)
    try:
        secondary_raw = _fetch_bytes(
            args.secondary_csv_url, timeout_seconds=args.timeout_seconds,
            user_agent=args.user_agent,
        )
    except ValueError as error:
        print(f"BATCH FAILED (fail closed): {error}", file=sys.stderr)
        return 1

    def _secondary_match_at_or_before(as_of: date) -> SecondaryCsvMatch:
        return secondary_csv_tickers_at_or_before(secondary_raw, as_of)

    try:
        # fetch_revision_at_or_before_fn / fetch_revision_wikitext_fn are left
        # at their defaults -- the real MediaWiki fetch path
        # (trading_market_data_core.dataset_v1_wikipedia_pit
        # .fetch_revision_at_or_before / .fetch_revision_wikitext). Tests
        # inject fakes instead; this script is the real, owner/agent-run path.
        result = run_wikipedia_revision_history_batch(
            as_of_dates, store=store,
            secondary_match_at_or_before=_secondary_match_at_or_before,
            secondary_source_id=args.secondary_source_id,
            secondary_source_url=args.secondary_csv_url,
            secondary_raw_bytes=secondary_raw,
            secondary_fetched_at=secondary_fetched_at,
            page_title=args.page_title,
            now=lambda: datetime.now(tz=UTC), timeout_seconds=args.timeout_seconds,
            user_agent=args.user_agent,
        )
    except SourceAdapterError as error:
        print(f"BATCH FAILED (fail closed): {error}", file=sys.stderr)
        return 1

    other_rows = tuple(row for row in result.rows if row.outcome != BATCH_OUTCOME_ADMITTED)
    manifest = build_dataset_manifest(
        store.entries(), requested_as_of_dates=as_of_dates, other_rows=other_rows,
    )
    args.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"dataset dir: {args.dataset_dir}")
    print(f"manifest written: {args.manifest_path}")
    for row in result.rows:
        print(f"  {row.as_of.isoformat()}: {row.outcome} ({row.detail})")
    print(f"admitted: {len(result.admitted_dates())} / requested: {len(as_of_dates)}")
    print(f"content_fingerprint: {manifest.content_fingerprint}")
    print(f"window_definition_status: {WINDOW_DEFINITION_REQUIRED['status']}")

    failed_or_refused = [
        row for row in result.rows
        if row.outcome not in (BATCH_OUTCOME_ADMITTED, BATCH_OUTCOME_RESUMED)
    ]
    return 2 if failed_or_refused else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
