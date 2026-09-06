"""Run and record ONE bounded, real Wikipedia-revision-history PIT experiment.

ChatGPT review of the A-WP2 canonical-state reconciliation found that the
Wikipedia/MediaWiki contemporaneous-revision path
(``trading_market_data_core.dataset_v1_wikipedia_pit``) had been evaluated
only with fixture data: real machinery, but no recorded real observation. The
review required at least one bounded real experiment be performed and
persisted before the categorical "no free path" conclusion could be replaced
with an evidence-bounded "available" one.

This script performs exactly that, over the public, unauthenticated,
documented APIs only (no login, no bot-protection bypass, no scraping behind
access controls):

1. asks MediaWiki for the latest revision of "List of S&P 500 companies" at
   or before ``--as-of`` (no look-ahead: never a later revision);
2. fetches and parses that revision's wikitext, with its page identity and
   revision timestamp VERIFIED against MediaWiki's own metadata for that
   revision id (``dataset_v1_wikipedia_pit.fetch_revision_wikitext``);
3. fetches an INDEPENDENT free secondary source (the fja05680/sp500
   community-maintained "Updated" CSV on GitHub -- already recorded as a
   SECONDARY_RESEARCH_PIT candidate in
   ``config/data-sources/sp500-historical-source-registry.json``) and selects
   its own latest row at or before ``--as-of`` (same no-look-ahead rule);
4. cross-checks the two ticker sets (``cross_check_membership``) and records
   agreement/disagreement explicitly;
5. persists an immutable raw sidecar (the exact MediaWiki API response bytes)
   plus a self-authenticating evidence JSON recording page identity, revision
   id, API-reported timestamp, both sources' checksums, and the cross-check
   result.

Scope, stated precisely so it cannot be over-read:

* this is ONE bounded experiment for ONE ``--as-of`` date, not a repeating
  archive -- building the full backtest-window dataset is the separately
  tracked next task (A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD);
* the result is permanently SECONDARY_RESEARCH_PIT, usable only for
  ``BacktestQuality.APPROXIMATE`` research (ADR-0017) -- it never becomes
  official and never flips ``ASOF_GICS_BLOCKER`` / the ``asof_gics`` or
  ``pit_sp500_membership`` capabilities;
* GICS Sector/Sub-Industry values ARE captured in the recorded snapshot
  (the same table carries them) but are explicitly NOT cross-checked here --
  no independent, dated, free GICS source has been identified -- and so are
  recorded ``CAPTURED_UNEVALUATED_NOT_ADMITTED``, never claimed as corroborated
  secondary evidence. ``cross_check_sectors`` exists for when one is;
* the secondary CSV is fetched here to perform THIS bounded cross-check only;
  it does not, by itself, satisfy the stricter multi-source validation the
  historical-source registry requires before that candidate's own
  ``checksum_status`` may read ``VERIFIED`` in that registry file.

Never run in CI or by any test: this makes real network calls.
"""

from __future__ import annotations

import argparse
import csv
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
    MINIMUM_CROSS_CHECK_AGREEMENT_RATIO,
    WIKIPEDIA_PIT_SCHEMA_VERSION,
    cross_check_membership,
    fetch_revision_at_or_before,
    fetch_revision_wikitext,
)

EXPERIMENT_SCHEMA_VERSION = "wikipedia-revision-history-experiment-1.0.0"

DEFAULT_SECONDARY_SOURCE_ID = "fja05680-sp500-github-updated-csv"
DEFAULT_SECONDARY_CSV_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
DEFAULT_EVIDENCE_DIR = Path("docs/poi/evidence/dataset-v1/wikipedia-revision-history")
DEFAULT_USER_AGENT = "TradingSystem-DatasetV1/1.0 (wikipedia-revision-experiment; contact via repo)"


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _fetch_bytes(url: str, *, timeout_seconds: float, user_agent: str) -> bytes:
    """One validated HTTPS GET. No auth, no bypass -- public raw content only."""
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


def _secondary_row_at_or_before(
    csv_bytes: bytes, as_of: date
) -> tuple[date, str, str]:
    """The secondary CSV's own latest ``date,tickers`` row at or before ``as_of``.

    Returns ``(matched_date, matched_line, tickers_field)``. Mirrors the same
    no-look-ahead rule as :func:`select_revision_as_of`: a row dated after
    ``as_of`` is never selected, even if it is the nearest one.
    """
    text = csv_bytes.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip().lower() != "date,tickers":
        raise ValueError(
            f"secondary CSV does not carry the expected 'date,tickers' header"
            f" (saw: {lines[0]!r} if any); refusing to parse"
        )
    reader = csv.reader(lines)
    next(reader)  # header
    best_date: date | None = None
    best_line_index: int | None = None
    for index, row in enumerate(reader, start=1):
        if not row:
            continue
        row_date = date.fromisoformat(row[0])
        if row_date <= as_of and (best_date is None or row_date > best_date):
            best_date = row_date
            best_line_index = index
    if best_date is None or best_line_index is None:
        raise ValueError(
            f"secondary CSV carries no row at or before {as_of.isoformat()};"
            " refusing to use a later row for an earlier decision (no look-ahead)"
        )
    matched_line = lines[best_line_index]
    tickers_field = matched_line.split(",", 1)[1].strip().strip('"')
    return best_date, matched_line, tickers_field


def run_experiment(
    *,
    as_of: date,
    page_title: str,
    secondary_source_id: str,
    secondary_csv_url: str,
    evidence_dir: Path,
    user_agent: str,
    timeout_seconds: float,
) -> dict[str, object]:
    performed_at = datetime.now(tz=UTC)

    # -- 1/2: the primary, revision-bound Wikipedia observation --------------
    revision_ref = fetch_revision_at_or_before(
        as_of, page_title=page_title, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    fetched = fetch_revision_wikitext(
        revision_ref, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    snapshot = fetched.snapshot

    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw_sidecar_name = (
        f"{page_title.replace(' ', '_').replace('&', 'and')}"
        f"-rev{snapshot.revision.revision_id}.raw.json"
    )
    raw_sidecar_path = evidence_dir / raw_sidecar_name
    # Refetching the SAME revision id is idempotent (immutable revisions), so a
    # byte-identical re-run is a no-op; a conflicting write is refused rather
    # than silently overwritten -- write-once, same as the rest of the archive
    # machinery in this package.
    if raw_sidecar_path.exists():
        existing = raw_sidecar_path.read_bytes()
        if _sha256(existing) != snapshot.raw_checksum:
            raise ValueError(
                f"{raw_sidecar_path} already exists with different content than"
                " the freshly-fetched revision; refusing to overwrite immutable"
                " raw provenance"
            )
    else:
        # The exact bytes the MediaWiki API returned -- authenticated by
        # snapshot.verify_raw_bytes(), never re-derived or reconstructed.
        assert snapshot.verify_raw_bytes(fetched.raw_bytes)
        raw_sidecar_path.write_bytes(fetched.raw_bytes)

    # -- 3: the independent secondary source ---------------------------------
    secondary_raw = _fetch_bytes(
        secondary_csv_url, timeout_seconds=timeout_seconds, user_agent=user_agent
    )
    secondary_full_checksum = _sha256(secondary_raw)
    matched_date, matched_line, tickers_field = _secondary_row_at_or_before(secondary_raw, as_of)
    secondary_tickers = frozenset(t for t in tickers_field.split(",") if t)
    matched_row_checksum = _sha256(matched_line.encode("utf-8"))

    # -- 4: the mandatory cross-check ----------------------------------------
    cross_check = cross_check_membership(
        wikipedia=snapshot,
        secondary_tickers=secondary_tickers,
        secondary_source_id=secondary_source_id,
        as_of=as_of,
    )

    # -- 5: the persisted, self-authenticating evidence record --------------
    body: dict[str, object] = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "wikipedia_pit_schema_version": WIKIPEDIA_PIT_SCHEMA_VERSION,
        "artifact_type": "wikipedia_revision_history_experiment",
        "classification": "SECONDARY_RESEARCH_PIT",
        "scope": "ONE bounded real experiment for one --as-of date; not a repeating archive",
        "requested_as_of": as_of.isoformat(),
        "performed_at": performed_at.isoformat(),
        "primary_source": {
            "source_id": "wikipedia-mediawiki-revision-history",
            "source_authority": "PUBLIC_SECONDARY",
            "page_title": snapshot.revision.page_title,
            "revision_id": snapshot.revision.revision_id,
            "revision_timestamp": snapshot.revision.revision_timestamp.isoformat(),
            "raw_checksum": snapshot.raw_checksum,
            "raw_provenance_path": str(raw_sidecar_path),
            "observation_count": len(snapshot.observations),
            "observations": [
                o.to_dict() for o in sorted(snapshot.observations, key=lambda o: o.ticker)
            ],
            "parse_diagnostics": list(snapshot.parse_diagnostics),
        },
        "secondary_source": {
            "source_id": secondary_source_id,
            "source_authority": "PUBLIC_SECONDARY",
            "source_url": secondary_csv_url,
            "full_file_checksum": secondary_full_checksum,
            "full_file_byte_length": len(secondary_raw),
            "matched_row_date": matched_date.isoformat(),
            "matched_row_checksum": matched_row_checksum,
            "matched_row_ticker_count": len(secondary_tickers),
            "note": (
                "The full third-party file is not committed to this repository;"
                " only the exact matched row and its own checksum are retained"
                " here, alongside the whole-file checksum so the source can be"
                " re-fetched and independently re-verified against both."
            ),
        },
        "membership_cross_check": cross_check.to_dict(),
        "membership_cross_check_threshold": MINIMUM_CROSS_CHECK_AGREEMENT_RATIO,
        "gics_sector_status": "CAPTURED_UNEVALUATED_NOT_ADMITTED",
        "gics_sector_note": (
            "GICS Sector/Sub-Industry values ARE present in primary_source."
            "observations (the same table carries them), but are NOT"
            " cross-checked by this experiment: no independent, dated, free"
            " GICS source has been identified. They are therefore captured for"
            " future use only and are NOT admitted as corroborated SECONDARY"
            " historical evidence. dataset_v1_wikipedia_pit.cross_check_sectors"
            " exists and is deterministic/tested for when a source is found."
        ),
        "eligibility": {
            "authoritative_official_evidence": False,
            "eligible_for_promotion_without_validation": False,
            "substitute_for_historical_gics": False,
        },
        "official_licensed_gap_status": "DEFERRED_NOT_BLOCKING",
        "not_usable_for": [
            "BacktestQuality.PIT_STRICT",
            "the asof_gics capability (stays BLOCKED_EXTERNAL / LICENSE_REQUIRED)",
            "the pit_sp500_membership capability (stays owner-gated on official"
            " S&P DJI announcements)",
            "any claim of S&P DJI official truth",
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
    parser.add_argument(
        "--as-of", type=date.fromisoformat, required=True,
        help="ISO date; the experiment uses the latest revision/row at or before this date",
    )
    parser.add_argument("--page-title", default=DEFAULT_PAGE_TITLE)
    parser.add_argument("--secondary-source-id", default=DEFAULT_SECONDARY_SOURCE_ID)
    parser.add_argument("--secondary-csv-url", default=DEFAULT_SECONDARY_CSV_URL)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--output", type=Path, default=None,
        help="evidence JSON path (default: <evidence-dir>/experiment-<as-of>.json)",
    )
    args = parser.parse_args(argv[1:])

    try:
        body = run_experiment(
            as_of=args.as_of,
            page_title=args.page_title,
            secondary_source_id=args.secondary_source_id,
            secondary_csv_url=args.secondary_csv_url,
            evidence_dir=args.evidence_dir,
            user_agent=args.user_agent,
            timeout_seconds=args.timeout_seconds,
        )
    except (ValueError, OSError) as error:
        print(f"EXPERIMENT FAILED (fail closed): {error}", file=sys.stderr)
        return 1

    output = args.output or (args.evidence_dir / f"experiment-{args.as_of.isoformat()}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    cross_check = body["membership_cross_check"]
    assert isinstance(cross_check, dict)
    print(f"evidence written: {output}")
    print(f"primary revision: {body['primary_source']['revision_id']}")  # type: ignore[index]
    print(
        f"membership corroborated: {cross_check['corroborated']}"
        f" (ratio={cross_check['agreement_ratio']})"
    )
    print("gics_sector_status: CAPTURED_UNEVALUATED_NOT_ADMITTED")
    return 0 if cross_check["corroborated"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
