"""Strategy-freeze validation CLI (Track A / A1).

Loads a machine-readable strategy manifest file and runs one of::

    validate          structural + freeze-invariant validation (fail-closed)
    fingerprint       print the deterministic content fingerprint
    freeze-readiness  report which mandatory owner parameters are missing

It NEVER fills a missing trading value. ``freeze-readiness`` only reports gaps;
a strategy that is not ready stays not ready.

Usage:
    uv run python -m trading_application.strategy_freeze_cli <command> <file.yaml>
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from trading_contracts.enums import AssessmentStatus, CalibrationStatus

from trading_application.strategy_freeze import (
    FreezeError,
    FreezeStatus,
    OwnerParameter,
    StrategyFreezeRecord,
    freeze_readiness,
)
from trading_application.strategy_manifest import (
    ManifestEntry,
    ManifestRole,
    StrategyManifest,
)


class FreezeFileError(Exception):
    """The strategy manifest file is malformed (fail-closed)."""


def _aware(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FreezeFileError(f"{field} must be an ISO-8601 string")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise FreezeFileError(f"{field} must be timezone-aware")
    return stamp


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _load_manifest(data: Any, strategy_id: str) -> StrategyManifest | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise FreezeFileError("fusion_manifest must be a mapping")
    entries_raw = data.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise FreezeFileError("fusion_manifest.entries must be a non-empty list")
    entries: list[ManifestEntry] = []
    for raw in entries_raw:
        if not isinstance(raw, dict):
            raise FreezeFileError("each fusion_manifest entry must be a mapping")
        try:
            role = ManifestRole(str(raw["role"]))
            required_status = (
                AssessmentStatus(str(raw["required_status"]))
                if raw.get("required_status") is not None
                else None
            )
            min_calibration = (
                CalibrationStatus(str(raw["min_calibration"]))
                if raw.get("min_calibration") is not None
                else None
            )
            entries.append(
                ManifestEntry(
                    assessment_key=str(raw["assessment_key"]),
                    role=role,
                    min_score=_decimal(raw.get("min_score")),
                    required_status=required_status,
                    min_calibration=min_calibration,
                    weight=_decimal(raw.get("weight")),
                )
            )
        except KeyError as error:
            raise FreezeFileError(f"fusion_manifest entry missing field {error}") from error
        except ValueError as error:
            raise FreezeFileError(f"invalid fusion_manifest entry: {error}") from error
    return StrategyManifest(
        strategy_id=strategy_id,
        version=str(data.get("version", "")),
        entries=tuple(entries),
    )


def load_freeze_record(path: Path) -> StrategyFreezeRecord:
    """Parse a strategy manifest YAML into a StrategyFreezeRecord (fail-closed)."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise FreezeFileError(f"invalid YAML: {error}") from error
    if not isinstance(data, dict):
        raise FreezeFileError("strategy file must be a mapping at the top level")

    strategy_id = str(data.get("strategy_id", ""))
    try:
        status = FreezeStatus(str(data.get("status", "DRAFT")))
    except ValueError as error:
        raise FreezeFileError(f"invalid status: {error}") from error

    parameters_raw = data.get("parameters") or []
    if not isinstance(parameters_raw, list):
        raise FreezeFileError("parameters must be a list")
    parameters: list[OwnerParameter] = []
    for raw in parameters_raw:
        if not isinstance(raw, dict):
            raise FreezeFileError("each parameter must be a mapping")
        try:
            parameters.append(
                OwnerParameter(
                    key=str(raw["key"]),
                    value=str(raw["value"]),
                    source=str(raw["source"]),
                    supplied_at=_aware(raw.get("supplied_at"), "parameter.supplied_at"),
                )
            )
        except KeyError as error:
            raise FreezeFileError(f"parameter missing field {error}") from error
        except FreezeError as error:
            raise FreezeFileError(str(error)) from error

    evidence_refs = tuple(str(ref) for ref in (data.get("evidence_refs") or []))
    frozen_at = (
        _aware(data.get("frozen_at"), "frozen_at") if data.get("frozen_at") is not None else None
    )
    try:
        return StrategyFreezeRecord(
            strategy_id=strategy_id,
            version_id=str(data.get("version_id", "")),
            status=status,
            parameters=tuple(parameters),
            fusion_manifest=_load_manifest(data.get("fusion_manifest"), strategy_id),
            owner_approval_ref=(
                str(data["owner_approval_ref"])
                if data.get("owner_approval_ref") is not None
                else None
            ),
            frozen_at=frozen_at,
            source_commit=(
                str(data["source_commit"]) if data.get("source_commit") is not None else None
            ),
            superseded_by=(
                str(data["superseded_by"]) if data.get("superseded_by") is not None else None
            ),
            evidence_refs=evidence_refs,
        )
    except FreezeError as error:
        # Re-raise construction-time freeze violations as file errors for the CLI.
        raise FreezeFileError(str(error)) from error


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("validate", "fingerprint", "freeze-readiness")
    )
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv[1:])

    if not args.file.is_file():
        print(f"strategy file not found: {args.file}")
        return 2

    try:
        record = load_freeze_record(args.file)
    except FreezeFileError as error:
        print(f"INVALID: {error}")
        return 1

    if args.command == "validate":
        print(f"OK: {record.strategy_id}@{record.version_id} status={record.status.value}")
        return 0

    if args.command == "fingerprint":
        print(record.fingerprint())
        return 0

    # freeze-readiness
    readiness = freeze_readiness(record)
    print(f"strategy: {readiness.strategy_id}@{readiness.version_id}")
    print(f"fusion_manifest_attached: {readiness.has_fusion_manifest}")
    if readiness.missing_mandatory_parameters:
        print("missing mandatory owner parameters (NOT auto-filled):")
        for key in readiness.missing_mandatory_parameters:
            print(f"  - {key}")
    if readiness.parameters_without_source:
        print("parameters without provenance source:")
        for key in readiness.parameters_without_source:
            print(f"  - {key}")
    if readiness.ready:
        print("READY: all mandatory owner parameters present (owner may freeze)")
        return 0
    print(f"NOT_READY: {'; '.join(readiness.reasons)}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
