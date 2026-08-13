"""Dataset validation CLI (Track A / A2 + A4).

Operates on a serialized ``DatasetManifest`` JSON file (see
``dataset_ingestion.serialize_manifest``). Commands::

    fingerprint    print the deterministic content fingerprint + snapshot id
    audit          run the strict no-look-ahead + split-contamination auditor
    coverage       per-series first/last/count coverage report
    provenance     source versions/URIs/checksums
    status         print the build status (never HISTORICALLY_TESTED/OOS)

Fail-closed: a malformed manifest, a look-ahead violation or split contamination
exits non-zero. Nothing is auto-filled or auto-corrected.

Usage:
    uv run python -m trading_application.dataset_cli <command> <manifest.json>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trading_application.dataset_ingestion import (
    IngestionError,
    coverage_report,
    deserialize_manifest,
    provenance_report,
)
from trading_application.dataset_manifest import DatasetError, NoLookaheadAuditor


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("fingerprint", "audit", "coverage", "provenance", "status")
    )
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv[1:])

    if not args.file.is_file():
        print(f"manifest file not found: {args.file}")
        return 2

    try:
        manifest = deserialize_manifest(args.file.read_text(encoding="utf-8"))
    except IngestionError as error:
        print(f"INVALID: {error}")
        return 1

    if args.command == "fingerprint":
        print(manifest.fingerprint())
        print(manifest.snapshot_identity())
        return 0

    if args.command == "status":
        print(f"status: {manifest.status.value}")
        return 0

    if args.command == "coverage":
        report = coverage_report(manifest.records)
        for series_id, (first, last, count) in sorted(report.per_series.items()):
            print(f"{series_id}: {first} .. {last} ({count} records)")
        return 0

    if args.command == "provenance":
        for source in provenance_report(manifest.sources).sources:
            print(f"{source['source_id']}@{source['version']} {source['checksum']} {source['uri']}")
        return 0

    # audit
    auditor = NoLookaheadAuditor()
    try:
        auditor.assert_no_lookahead(manifest)
        auditor.assert_no_split_contamination(manifest)
    except DatasetError as error:
        print(f"AUDIT_FAILED: {error}")
        return 1
    print(
        f"AUDIT_OK: {manifest.dataset_id}@{manifest.dataset_version}"
        f" ({len(manifest.records)} records, status={manifest.status.value})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
