"""Dataset V1 real-source qualification CLI (owner-run).

Commands::

    discover     report which real historical sources are configured/available
    criteria     print the acceptance criteria for each qualifiable capability
    verify       load and AUTHENTICATE an evidence artifact (fail-closed)
    report       summarise capability states from supplied evidence artifacts

This CLI never fabricates evidence and never contacts a paid/live service by
itself: real evidence is produced on the owner host by running a configured
source adapter, and this tool verifies and reports it. With no configured source
every capability stays BLOCKED_EXTERNAL.

Secrets are never read or printed here; a source is declared through a config
file that contains only non-secret identity/metadata.

Usage:
    uv run --no-sync python -m trading_market_data_core.dataset_v1_qualification_cli \\
        <command> [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trading_market_data_core.dataset_v1 import CapabilityState, DatasetCapabilityMatrix
from trading_market_data_core.dataset_v1_artifacts import (
    ArtifactError,
    ArtifactType,
    load_and_verify_crosswalk,
    load_artifact,
    scan_evidence_dir,
)
from trading_market_data_core.dataset_v1_qualification import (
    CAPABILITY_CRITERIA,
    QUALIFIABLE_CAPABILITIES,
    QualificationError,
    QualificationRun,
    load_and_verify_evidence,
)

#: Where an owner-configured real source would declare itself (non-secret).
DEFAULT_SOURCE_CONFIG = Path("config/data-sources/dataset-v1-sources.yaml")
DEFAULT_EVIDENCE_DIR = Path("docs/poi/evidence/dataset-v1")
#: The already-authenticated IBKR historical evidence (six proven capabilities).
DEFAULT_IBKR_EVIDENCE = Path("docs/poi/evidence/POI-IBKR-HIST.json")


def _discover(config_path: Path, evidence_dir: Path) -> tuple[list[str], int]:
    """Report configured sources and existing evidence. Fabricates nothing."""
    notes: list[str] = []
    if config_path.is_file():
        notes.append(f"source config present: {config_path}")
    else:
        notes.append(
            f"no source config at {config_path}: no real historical source is declared"
        )
    scan = scan_evidence_dir(evidence_dir)
    found = scan.promotable
    for artifact_type in sorted(scan.by_type):
        paths = scan.by_type[artifact_type]
        notes.append(f"{len(paths)} {artifact_type.value} artifact(s) in {evidence_dir}")
    for path, reason in scan.unclassified:
        notes.append(f"UNCLASSIFIED {path}: {reason}")
    if not scan.by_type:
        notes.append(f"no artifacts in {evidence_dir}")
    for capability in QUALIFIABLE_CAPABILITIES:
        artifact = evidence_dir / f"{capability}.json"
        notes.append(
            f"{capability}: {'evidence present' if artifact.is_file() else 'NO EVIDENCE'}"
        )
    return notes, len(found)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("discover", "criteria", "verify", "report"))
    parser.add_argument("--config", type=Path, default=DEFAULT_SOURCE_CONFIG)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--evidence", type=Path, default=None)
    parser.add_argument("--ibkr-evidence", type=Path, default=DEFAULT_IBKR_EVIDENCE)
    parser.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv[1:])

    if args.command == "criteria":
        for capability in QUALIFIABLE_CAPABILITIES:
            criteria = CAPABILITY_CRITERIA[capability]
            print(f"{capability}:")
            print(f"  historical changes required : {criteria.minimum_historical_changes}")
            print(f"  effective dates required    : {criteria.requires_effective_dates}")
            print(f"  knowledge time required     : {criteria.requires_known_at}")
            print(f"  stable identity required    : {criteria.requires_stable_identity}")
            print(f"  required proofs             : {list(criteria.required_proofs)}")
        return 0

    if args.command == "discover":
        notes, _ = _discover(args.config, args.evidence_dir)
        for note in notes:
            print(note)
        return 0

    if args.command == "verify":
        if args.evidence is None:
            print("verify requires --evidence <path>")
            return 2
        # Dispatch on the artifact's REAL type. Parsing an auxiliary report
        # with the qualification schema is what produced the false
        # "evidence is missing a sha256 normalized_checksum" failure.
        try:
            artifact_type, _ = load_artifact(args.evidence)
        except ArtifactError as error:
            print(f"ARTIFACT_INVALID: {error}")
            return 1
        if artifact_type is not ArtifactType.QUALIFICATION_EVIDENCE:
            if artifact_type is ArtifactType.IDENTITY_CROSSWALK:
                try:
                    crosswalk = load_and_verify_crosswalk(args.evidence)
                except ArtifactError as error:
                    print(f"CROSSWALK_INVALID: {error}")
                    return 1
                print(f"artifact_type        : {artifact_type.value}")
                print(f"checksum verified    : {crosswalk.checksum}")
                print(f"identity clean       : {crosswalk.clean}")
                print("promotable           : no (auxiliary artifact)")
                return 0
            print(f"artifact_type        : {artifact_type.value}")
            print("promotable           : no (auxiliary artifact); not parsed as evidence")
            return 0
        try:
            evidence = load_and_verify_evidence(args.evidence)
        except QualificationError as error:
            print(f"EVIDENCE_INVALID: {error}")
            return 1
        print(f"capability          : {evidence.capability}")
        print(f"source              : {evidence.source.name}@{evidence.source.version}")
        print(f"source kind         : {evidence.source.kind.value}")
        print(f"result              : {evidence.result.value}")
        print(f"promotion_eligible  : {evidence.promotion_eligible}")
        print(f"checksum verified   : {evidence.normalized_checksum}")
        for reason in evidence.blocked_reasons:
            print(f"  blocked: {reason}")
        return 0 if evidence.promotion_eligible else 1

    # report
    #
    # Readiness is NEVER computed from a directory glob. The real owner run had
    # a FAILED corporate-action stage while a previous run's artifact was still
    # on disk, and directory-wide discovery treated it as available. Current
    # readiness comes from the authenticated run manifest, so this command
    # refuses to answer without one.
    if args.manifest is None:
        print(
            "READINESS_REQUIRES_A_RUN_MANIFEST: current readiness is computed from"
            " the authenticated run manifest, not from the evidence directory."
        )
        print(
            "  run: uv run --no-sync python -m"
            " trading_market_data_core.dataset_v1_run_cli report --manifest <run-manifest.json>"
        )
        print(
            "  (this command still supports 'discover', 'criteria' and 'verify'"
            " for inspecting individual artifacts)"
        )
        return 2

    from trading_market_data_core.dataset_v1_run_cli import cmd_report

    return cmd_report(args)


def _legacy_directory_report(args: argparse.Namespace) -> int:
    """Retained for inspection only; never used to decide current readiness."""
    notes, _ = _discover(args.config, args.evidence_dir)
    evidences = []
    scan = scan_evidence_dir(args.evidence_dir)
    # ONLY qualification_evidence may reach the promotion path. Auxiliary
    # artifacts are still verified, just with their own schema.
    for path in scan.promotable:
        try:
            evidences.append(load_and_verify_evidence(path))
        except QualificationError as error:
            print(f"EVIDENCE_INVALID {path}: {error}")
            return 1
    for path in scan.paths(ArtifactType.IDENTITY_CROSSWALK):
        try:
            crosswalk = load_and_verify_crosswalk(path)
        except ArtifactError as error:
            print(f"CROSSWALK_INVALID {path}: {error}")
            return 1
        notes.append(
            f"identity crosswalk verified: {path} (clean={crosswalk.clean},"
            f" checksum={crosswalk.checksum})"
        )
    for path, reason in scan.unclassified:
        print(f"ARTIFACT_UNCLASSIFIED {path}: {reason}")
        return 1
    # Seed the six IBKR historical capabilities from their own AUTHENTICATED
    # evidence so this report tells the truth about the whole required set
    # instead of under-claiming cells this harness does not govern.
    base_states: dict[str, CapabilityState] = {}
    if args.ibkr_evidence.is_file():
        try:
            matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(args.ibkr_evidence)
        except ValueError as error:
            print(f"IBKR_EVIDENCE_INVALID: {error}")
            return 1
        base_states = dict(matrix.states)
        notes.append(f"IBKR historical evidence verified: {args.ibkr_evidence}")
    else:
        notes.append(f"no IBKR historical evidence at {args.ibkr_evidence}")

    run = QualificationRun(
        evidences=tuple(evidences), discovery_notes=tuple(notes), base_states=base_states
    )
    payload = run.to_dict()
    print(json.dumps(payload, indent=2, sort_keys=True))
    # Non-zero while the dataset contract is not genuinely ready, so an owner run
    # cannot be mistaken for success.
    return 0 if payload["full_dataset_contract_ready"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
