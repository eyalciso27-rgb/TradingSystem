"""Run one real, bounded historical sector cross-check using SEC N-PORT-P.

This script is owner/agent-run only; tests never make network calls. It binds the
existing 2023-12-31 MediaWiki revision evidence to the 11 Select Sector SPDR
N-PORT-P filings on SEC EDGAR, retains every raw XML with SHA-256, resolves
issuer identity conservatively, then applies the canonical sector coverage and
agreement gates. The result is SECONDARY_RESEARCH_PIT / APPROXIMATE only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[1]
for rel in (
    "packages/py/market-data-core/src",
    "packages/py/contracts/src",
    "packages/py/domain/src",
):
    sys.path.insert(0, str(REPO_ROOT / rel))

from trading_market_data_core.dataset_v1_sec_sector_spdr_gics import (  # noqa: E402
    build_secondary_sector_mapping,
    select_sector_spdr_snapshot_as_of,
)
from trading_market_data_core.dataset_v1_sources import (  # noqa: E402
    SecEdgarClient,
    UrllibHttpFetcher,
)
from trading_market_data_core.dataset_v1_wikipedia_pit import (  # noqa: E402
    WikipediaConstituentObservation,
    WikipediaRevisionRef,
    WikipediaRevisionSnapshot,
    cross_check_sectors,
)

AS_OF = date(2023, 12, 31)
WIKI_EVIDENCE = (
    REPO_ROOT / "docs/poi/evidence/dataset-v1/wikipedia-revision-history/experiment-2023-12-31.json"
)
OUT_ROOT = REPO_ROOT / "docs/poi/evidence/dataset-v1/sec-sector-spdr-gics"
RAW_ROOT = OUT_ROOT / AS_OF.isoformat()
RESULT_PATH = OUT_ROOT / f"experiment-{AS_OF.isoformat()}.json"
SCHEMA_VERSION = "sec-sector-spdr-gics-crosscheck-experiment-1.0.0"
SECONDARY_SOURCE_ID = "sec-edgar-nport-select-sector-spdr"


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _write_immutable(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise RuntimeError(f"immutable evidence conflict at {path}")
        return
    path.write_bytes(raw)


def _load_wikipedia_snapshot() -> tuple[WikipediaRevisionSnapshot, dict[str, object]]:
    evidence = json.loads(WIKI_EVIDENCE.read_text(encoding="utf-8"))
    primary = evidence["primary_source"]
    if evidence["requested_as_of"] != AS_OF.isoformat():
        raise RuntimeError("Wikipedia evidence date does not match experiment date")
    raw_path = REPO_ROOT / str(primary["raw_provenance_path"])
    raw = raw_path.read_bytes()
    if _sha256(raw) != primary["raw_checksum"]:
        raise RuntimeError("Wikipedia raw provenance checksum mismatch")
    revision = WikipediaRevisionRef(
        page_title=str(primary["page_title"]),
        revision_id=int(primary["revision_id"]),
        revision_timestamp=datetime.fromisoformat(str(primary["revision_timestamp"])).astimezone(
            UTC
        ),
    )
    observations = tuple(
        WikipediaConstituentObservation(
            ticker=str(row["ticker"]),
            name=str(row["name"]),
            gics_sector=str(row["gics_sector"]),
            gics_sub_industry=str(row["gics_sub_industry"]),
        )
        for row in primary["observations"]
    )
    return WikipediaRevisionSnapshot(
        revision=revision, raw_checksum=str(primary["raw_checksum"]), observations=observations
    ), evidence


def main() -> int:
    performed_at = datetime.now(tz=UTC)
    wikipedia, wiki_evidence = _load_wikipedia_snapshot()
    primary = cast(dict[str, Any], wiki_evidence["primary_source"])
    user_agent = (
        os.environ.get("SEC_EDGAR_USER_AGENT")
        or "TradingSystem DatasetV1 GICS experiment eyalciso27@gmail.com"
    )
    fetcher = UrllibHttpFetcher()
    client = SecEdgarClient(fetcher, user_agent=user_agent)
    snapshot = select_sector_spdr_snapshot_as_of(AS_OF, client=client, bytes_fetcher=fetcher)
    filing_records: list[dict[str, object]] = []
    for fetched in sorted(snapshot.filings, key=lambda item: item.filing.gics_sector):
        filing = fetched.filing
        filename = f"{filing.gics_sector.replace(' ', '_')}-{filing.accession_number}.xml"
        raw_path = RAW_ROOT / filename
        _write_immutable(raw_path, fetched.raw_bytes)
        if _sha256(raw_path.read_bytes()) != fetched.raw_checksum:
            raise RuntimeError(f"retained SEC checksum mismatch for {raw_path}")
        filing_records.append(
            {
                **filing.to_dict(),
                "holding_count": len(fetched.holdings),
                "raw_artifact_path": str(raw_path.relative_to(REPO_ROOT)).replace("\\", "/"),
                "raw_checksum": fetched.raw_checksum,
            }
        )

    resolution = build_secondary_sector_mapping(
        snapshot,
        wikipedia_observations=[(item.ticker, item.name) for item in wikipedia.observations],
    )
    cross_check = cross_check_sectors(
        wikipedia=wikipedia,
        secondary_sectors=resolution.secondary_sectors,
        secondary_source_id=SECONDARY_SOURCE_ID,
        as_of=AS_OF,
    )
    body: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "historical_sector_crosscheck_experiment",
        "classification": "SECONDARY_RESEARCH_PIT",
        "backtest_quality": "APPROXIMATE",
        "scope": (
            "GICS Sector only for one bounded historical as-of date; "
            "GICS Sub-Industry is not cross-checked"
        ),
        "requested_as_of": AS_OF.isoformat(),
        "performed_at": performed_at.isoformat(),
        "wikipedia_source": {
            "evidence_path": str(WIKI_EVIDENCE.relative_to(REPO_ROOT)).replace("\\", "/"),
            "evidence_checksum": wiki_evidence["evidence_checksum"],
            "revision_id": primary["revision_id"],
            "revision_timestamp": primary["revision_timestamp"],
            "raw_artifact_path": primary["raw_provenance_path"],
            "raw_checksum": primary["raw_checksum"],
            "observation_count": len(wikipedia.observations),
        },
        "independent_source": {
            "source_id": SECONDARY_SOURCE_ID,
            "source_authority": "OFFICIAL_PUBLIC_REGULATORY_FILINGS",
            "source": "SEC EDGAR N-PORT-P filings for all 11 Select Sector SPDR funds",
            "as_of": AS_OF.isoformat(),
            "filing_count": len(filing_records),
            "filings": filing_records,
        },
        "identity_resolution": resolution.to_dict(),
        "sector_cross_check": cross_check.to_dict(),
        "admission": {
            "status": "ADMITTED_SECONDARY_RESEARCH_PIT_APPROXIMATE"
            if cross_check.corroborated
            else "REFUSED_NOT_CORROBORATED",
            "usable_for": "BacktestQuality.APPROXIMATE historical GICS Sector research only",
            "not_usable_for": [
                "BacktestQuality.PIT_STRICT",
                "official/licensed GICS truth",
                "GICS Sub-Industry historical assignment",
                "final OOS/shadow qualification evidence",
                "PAPER_AUTO or LIVE authorization",
            ],
            "official_licensed_gap_status": "DEFERRED_NOT_BLOCKING",
        },
    }
    body["evidence_checksum"] = _sha256(_canonical_bytes(body))
    rendered = (json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )
    _write_immutable(RESULT_PATH, rendered)
    print(f"evidence={RESULT_PATH.relative_to(REPO_ROOT)}")
    print(f"filings={len(filing_records)}")
    coverage_detail = f"{cross_check.covered_tickers}/{len(wikipedia.observations)}"
    print(f"coverage={cross_check.coverage_ratio:.6f} ({coverage_detail})")
    agreement_detail = f"{len(cross_check.agreements)}/{cross_check.covered_tickers}"
    print(f"agreement={cross_check.agreement_ratio:.6f} ({agreement_detail})")
    print(f"uncovered={len(cross_check.uncovered_tickers)}")
    print(f"corroborated={cross_check.corroborated}")
    print(f"checksum={body['evidence_checksum']}")
    return 0 if cross_check.corroborated else 2


if __name__ == "__main__":
    raise SystemExit(main())
