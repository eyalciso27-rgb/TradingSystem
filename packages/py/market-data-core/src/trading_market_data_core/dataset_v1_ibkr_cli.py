"""One-time migration of legacy (untyped) IBKR evidence artifacts.

``POI-IBKR-HIST.json`` and ``POI-IBKR-CORP.json`` were written before the typed
artifact system existed, so a run manifest could not bind them and the whole
IBKR stage contributed nothing to the run ledger.

This command upgrades such a file IN PLACE by adding typing metadata and a
canonical body checksum. It never changes a recorded observation, it requires
the exact legacy shape, and it refuses anything else — so no file can acquire a
type by resembling one.

Usage:
    uv run --no-sync python -m trading_market_data_core.dataset_v1_ibkr_cli \\
        migrate --path docs/poi/evidence/POI-IBKR-HIST.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trading_market_data_core.dataset_v1_ibkr_artifacts import (
    IbkrArtifactError,
    is_legacy_ibkr_evidence,
    load_and_verify_ibkr_evidence,
    migrate_legacy_ibkr_evidence,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOTHING_TO_DO = 3


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def cmd_migrate(path: Path, *, run_id: str | None) -> int:
    if not path.is_file():
        print(f"NOT_FOUND: {path}")
        return EXIT_NOTHING_TO_DO
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"UNREADABLE: {path}: {error}")
        return EXIT_FAILED
    if not isinstance(payload, dict):
        print(f"NOT_AN_OBJECT: {path}")
        return EXIT_FAILED

    if "artifact_type" in payload:
        try:
            artifact = load_and_verify_ibkr_evidence(path)
        except IbkrArtifactError as error:
            print(f"ALREADY_TYPED_BUT_INVALID: {error}")
            return EXIT_FAILED
        print(f"already typed: {path} ({artifact.artifact_type.value})")
        return EXIT_NOTHING_TO_DO

    if is_legacy_ibkr_evidence(payload) is None:
        print(
            f"NOT_MIGRATABLE: {path} does not match the known legacy IBKR shape;"
            " refusing to guess a type. Regenerate it in an owner run."
        )
        return EXIT_FAILED

    artifact = migrate_legacy_ibkr_evidence(path, run_id=run_id)
    checksum = artifact.write(path)
    print(f"migrated: {path}")
    print(f"  artifact_type      : {artifact.artifact_type.value}")
    print(f"  schema_version     : {artifact.schema_version}")
    print(f"  producer           : {artifact.producer}")
    print(f"  capability_scope   : {list(artifact.capability_scope)}")
    print(f"  normalized_checksum: {checksum}")
    return EXIT_OK


def cmd_verify(path: Path) -> int:
    try:
        artifact = load_and_verify_ibkr_evidence(path)
    except IbkrArtifactError as error:
        print(f"IBKR_ARTIFACT_INVALID: {error}")
        return EXIT_FAILED
    print(f"artifact_type      : {artifact.artifact_type.value}")
    print(f"producer           : {artifact.producer}")
    print(f"capability_scope   : {list(artifact.capability_scope)}")
    print(f"normalized_checksum: {artifact.normalized_checksum}")
    for capability, verdict in artifact.capability_states().items():
        print(f"  {capability}: {verdict}")
    return EXIT_OK


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("migrate", "verify"))
    parser.add_argument("--path", type=Path, required=True)
    parser.add_argument("--run-id", default="")
    _force_utf8_stdout()
    args = parser.parse_args(argv[1:])
    if args.command == "migrate":
        return cmd_migrate(args.path, run_id=args.run_id or None)
    return cmd_verify(args.path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
