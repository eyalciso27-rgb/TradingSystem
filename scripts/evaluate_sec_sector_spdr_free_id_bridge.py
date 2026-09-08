#!/usr/bin/env python3
"""Deterministic offline feasibility evaluation for a free stable-ID bridge.

Reads only the immutable SEC N-PORT-P artifacts already retained for the
2023-12-31 Sector-SPDR experiment. It inventories CUSIP/ISIN/LEI coverage and
records why the evaluated free public registries do not provide a dated,
fail-closed identifier-to-Wikipedia-ticker bridge. No network call is made.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1_sec_sector_spdr_gics import parse_nport_primary_doc

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "docs/poi/evidence/dataset-v1/sec-sector-spdr-gics/2023-12-31"
OUT = ROOT / "docs/poi/evidence/dataset-v1/sec-sector-spdr-gics/free-id-bridge-eval-2023-12-31.json"
CIK = "0001064641"


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_evaluation() -> dict[str, object]:
    files = sorted(RAW_DIR.glob("*.xml"))
    if len(files) != 11:
        raise RuntimeError(f"expected 11 retained N-PORT XML files, found {len(files)}")
    counts = {"cusip": 0, "isin": 0, "lei": 0}
    unique: dict[str, set[str]] = {"cusip": set(), "isin": set(), "lei": set()}
    total = 0
    artifacts = []
    for path in files:
        raw = path.read_bytes()
        match = re.search(r"(\d{10}-\d{2}-\d{6})\.xml$", path.name)
        if not match:
            raise RuntimeError(f"cannot derive accession from {path.name}")
        filing, holdings = parse_nport_primary_doc(raw, cik=CIK, accession_number=match.group(1))
        total += len(holdings)
        for holding in holdings:
            for field in counts:
                value = getattr(holding, field)
                if value:
                    counts[field] += 1
                    unique[field].add(value)
        artifacts.append({
            "path": str(path.relative_to(ROOT)),
            "accession_number": filing.accession_number,
            "report_period_date": filing.report_period_date.isoformat(),
            "sha256": sha256(raw),
            "equity_common_holdings": len(holdings),
        })
    inventory = {
        key: {"present": counts[key], "total": total, "coverage_ratio": counts[key] / total,
              "unique_values": len(unique[key])}
        for key in counts
    }
    return {
        "schema_version": "dataset-v1-sec-sector-free-id-bridge-eval-1.0.0",
        "artifact_type": "FREE_STABLE_IDENTIFIER_BRIDGE_EVALUATION",
        "performed_at": datetime.now(UTC).isoformat(),
        "requested_as_of": "2023-12-31",
        "input_canonical_sector_result": {
            "status": "NOT_ADMITTED", "covered": 417, "total": 503,
            "coverage_ratio": 417 / 503, "coverage_floor": 0.90,
            "agreements": 417, "agreement_total": 417,
        },
        "identifier_inventory": {"equity_common_holdings": total, **inventory},
        "retained_sec_artifacts": artifacts,
        "evaluated_free_bridges": [
            {
                "source": "SEC Official List of Section 13(f) Securities",
                "authority": "U.S. Securities and Exchange Commission",
                "url": "https://www.sec.gov/rules-regulations/staff-guidance/official-list-section-13f-securities",
                "date_semantics": (
                    "quarterly lists are dated and can be selected without look-ahead"
                ),
                "fields_relevant_to_bridge": ["CUSIP", "issuer name", "issuer description"],
                "result": (
                    "NO_TICKER_FIELD; does not advance legal-name matching "
                    "to a dated Wikipedia ticker"
                ),
            },
            {
                "source": "SEC Company Tickers",
                "authority": "U.S. Securities and Exchange Commission",
                "url": "https://www.sec.gov/file/company-tickers",
                "date_semantics": (
                    "published current company/ticker dataset; no effective-dated "
                    "2023-12-31 symbol validity is asserted"
                ),
                "fields_relevant_to_bridge": ["CIK", "ticker", "company title"],
                "result": (
                    "NO_CUSIP_ISIN_LEI_KEY and no historical-validity binding; "
                    "unsafe for this PIT bridge"
                ),
            },
            {
                "source": "GLEIF ISIN-to-LEI relationship data",
                "authority": "Global Legal Entity Identifier Foundation / ANNA certified mapping",
                "url": "https://www.gleif.org/en/lei-data/lei-mapping",
                "date_semantics": (
                    "daily open relationship files exist, but map ISIN to LEI "
                    "rather than to exchange ticker"
                ),
                "fields_relevant_to_bridge": ["ISIN", "LEI"],
                "result": (
                    "IDENTIFIER_TO_IDENTIFIER_ONLY; no dated ticker link to the "
                    "Wikipedia constituent key"
                ),
            },
        ],
        "bridge_result": "NO_FREE_ID_BRIDGE_FOUND",
        "admission": {
            "status": "NOT_ADMITTED",
            "reason": (
                "stable identifiers are present, but no evaluated legitimate free "
                "dated source closes identifier-to-ticker identity without guessing "
                "or current-value back-projection"
            ),
            "coverage_floor_unchanged": 0.90,
            "canonical_sector_result_unchanged": "417/503 (82.90%)",
            "official_licensed_gics_gap": "DEFERRED_BLOCKED_EXTERNAL",
        },
        "safety": {"guessed_mappings": 0, "company_alias_table": False,
                   "paper_auto_enabled": False, "live_enabled": False},
    }


def main() -> None:
    result = build_evaluation()
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(OUT), "bridge_result": result["bridge_result"],
                      "inventory": result["identifier_inventory"]}, indent=2))


if __name__ == "__main__":
    main()
