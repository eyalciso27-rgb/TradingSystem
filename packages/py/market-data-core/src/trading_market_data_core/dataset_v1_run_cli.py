"""Dataset V1 run orchestration CLI (owner host).

Commands::

    new-id      print a fresh run id
    stage       record one stage outcome for the current run
    manifest    assemble + AUTHENTICATE the run manifest from recorded stages
    report      capability report built ONLY from the current run's manifest

Why this exists: the real owner run had its SEC corporate-action stage FAIL, yet
a ``corporate_actions.json`` left by the PREVIOUS run was still discovered by a
directory glob and treated as available. Readiness is now derived from the run
manifest, so a failed stage cannot inherit stale evidence and historical
artifacts can sit beside current ones without contaminating anything.

Usage:
    uv run --no-sync python -m trading_market_data_core.dataset_v1_run_cli \\
        <command> [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1 import (
    DATASET_V1_REQUIRED_CAPABILITIES,
    CapabilityState,
    DatasetCapabilityMatrix,
)
from trading_market_data_core.dataset_v1_artifacts import (
    ArtifactError,
    ArtifactType,
    load_and_verify_crosswalk,
)
from trading_market_data_core.dataset_v1_builder import BUILDER_VERSION
from trading_market_data_core.dataset_v1_ibkr_artifacts import (
    IbkrArtifactError,
    load_and_verify_ibkr_evidence,
)
from trading_market_data_core.dataset_v1_qualification import (
    QUALIFICATION_SCHEMA_VERSION,
    QualificationError,
    capability_states_from_evidence,
    load_and_verify_evidence,
)
from trading_market_data_core.dataset_v1_run import (
    RUN_MANIFEST_SCHEMA_VERSION,
    CurrentRunStatus,
    RunArtifact,
    RunManifest,
    StageOutcome,
    StageRecord,
    current_run_status,
    file_checksum,
    git_sha,
    load_and_verify_run_manifest,
    new_run_id,
    verify_manifest_artifacts,
)

DEFAULT_RUN_ROOT = Path("docs/poi/evidence/dataset-v1-runs")

#: Which stage is responsible for each qualifiable capability. Used to report
#: CURRENT_RUN_FAILED vs NO_CURRENT_EVIDENCE precisely.
STAGE_BY_CAPABILITY = {
    "delisted_security_history": "sec_delisted",
    "corporate_actions": "sec_corporate_actions",
    "pit_sp500_membership": "sp500_membership",
    "asof_gics": "asof_gics_discovery",
    # The six IBKR historical capabilities come from POI-IBKR-HIST.json. That
    # file persists between runs, so without this mapping a run in which the
    # IBKR stage never executed would still report CURRENT_RUN_EVIDENCE for
    # them - the same stale-evidence defect, on the IBKR path.
    "bid_ask_midpoint": "ibkr_historical",
    "connectivity_paper_du_gate": "ibkr_historical",
    "contract_identity_conid": "ibkr_historical",
    "daily_trades_rth_schema": "ibkr_historical",
    "head_timestamp_coverage": "ibkr_historical",
    "symbol_change_discovery": "ibkr_historical",
}


def _force_utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _stages_dir(run_root: Path, run_id: str) -> Path:
    return run_root / run_id / "stages"


def cmd_new_id(args: argparse.Namespace) -> int:
    del args
    print(new_run_id())
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    """Record one stage outcome. Artifacts are listed only if they exist."""
    try:
        outcome = StageOutcome(args.outcome.upper())
    except ValueError:
        print(f"UNKNOWN_OUTCOME: {args.outcome}")
        return 2
    artifacts: list[str] = []
    if outcome.may_publish:
        for name in args.artifact or []:
            path = Path(name)
            if path.is_file():
                artifacts.append(str(path))
    directory = _stages_dir(args.run_root, args.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": args.stage_name,
        "outcome": outcome.value,
        "detail": args.detail,
        "artifacts": artifacts,
    }
    (directory / f"{args.stage_name}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"stage recorded: {args.stage_name}={outcome.value} artifacts={artifacts}")
    return 0


def _load_stages(run_root: Path, run_id: str) -> list[StageRecord]:
    directory = _stages_dir(run_root, run_id)
    records: list[StageRecord] = []
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        records.append(
            StageRecord(
                name=str(payload["name"]),
                outcome=StageOutcome(str(payload["outcome"])),
                detail=str(payload.get("detail", "")),
                artifacts=tuple(str(a) for a in payload.get("artifacts", ())),
            )
        )
    return records


def cmd_manifest(args: argparse.Namespace) -> int:
    """Assemble and authenticate the manifest for THIS run."""
    stages = _load_stages(args.run_root, args.run_id)
    artifacts: list[RunArtifact] = []
    failures: list[str] = []
    for stage in stages:
        if stage.outcome is StageOutcome.FAILED:
            failures.append(f"{stage.name}: {stage.detail or 'stage failed'}")
        if not stage.outcome.may_publish:
            continue
        for name in stage.artifacts:
            path = Path(name)
            if not path.is_file():
                failures.append(f"{stage.name}: declared artifact missing {path}")
                continue
            try:
                from trading_market_data_core.dataset_v1_artifacts import load_artifact

                artifact_type, _ = load_artifact(path)
            except ArtifactError as error:
                failures.append(f"{stage.name}: {error}")
                continue
            source_digest: str | None = None
            if artifact_type in (
                ArtifactType.IBKR_HISTORICAL_EVIDENCE,
                ArtifactType.IBKR_CORPORATE_EVIDENCE,
            ):
                # A typed IBKR artifact must authenticate before it is bound;
                # an untyped legacy file is refused here, never guessed.
                try:
                    load_and_verify_ibkr_evidence(path)
                except IbkrArtifactError as error:
                    failures.append(f"{stage.name}: {error}")
                    continue
            if artifact_type is ArtifactType.QUALIFICATION_EVIDENCE:
                try:
                    evidence = load_and_verify_evidence(path)
                except QualificationError as error:
                    failures.append(f"{stage.name}: {error}")
                    continue
                source_digest = evidence.source.raw_checksum
            artifacts.append(
                RunArtifact(
                    name=path.stem, artifact_type=artifact_type, path=str(path),
                    file_checksum=file_checksum(path),
                    source_artifact_checksum=source_digest, stage=stage.name,
                )
            )

    now = datetime.now(tz=UTC)
    started = (
        datetime.fromisoformat(args.started_at) if args.started_at else now
    )
    manifest = RunManifest(
        run_id=args.run_id,
        started_at=started,
        completed_at=now,
        git_sha=git_sha(Path.cwd()),
        owner_host_mode=args.mode,
        versions={
            "qualification_schema": QUALIFICATION_SCHEMA_VERSION,
            "builder": BUILDER_VERSION,
            "run_manifest_schema": RUN_MANIFEST_SCHEMA_VERSION,
        },
        stages=tuple(stages),
        artifacts=tuple(artifacts),
        failure_reasons=tuple(failures),
    )
    path = args.out or (args.run_root / args.run_id / "run-manifest.json")
    checksum = manifest.write(path)
    print(f"run_id            : {manifest.run_id}")
    print(f"git_sha           : {manifest.git_sha}")
    print(f"stages            : {len(stages)}")
    print(f"artifacts bound   : {len(artifacts)}")
    for reason in manifest.failure_reasons:
        print(f"  failure: {reason}")
    print(f"run manifest      : {path}")
    print(f"manifest checksum : {checksum}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Capability report from the CURRENT run's manifest only."""
    try:
        manifest = load_and_verify_run_manifest(args.manifest)
    except ArtifactError as error:
        print(f"RUN_MANIFEST_INVALID: {error}")
        return 2
    findings = verify_manifest_artifacts(manifest, root=Path.cwd())
    if findings:
        for finding in findings:
            print(f"ARTIFACT_FINDING: {finding}")
        return 2

    # ONLY manifest-bound evidence may reach promotion. No directory glob.
    evidences = []
    for artifact in manifest.artifacts_of_type(ArtifactType.QUALIFICATION_EVIDENCE):
        try:
            evidences.append(load_and_verify_evidence(Path(artifact.path)))
        except QualificationError as error:
            print(f"EVIDENCE_INVALID {artifact.path}: {error}")
            return 2
    for artifact in manifest.artifacts_of_type(ArtifactType.IDENTITY_CROSSWALK):
        try:
            load_and_verify_crosswalk(Path(artifact.path))
        except ArtifactError as error:
            print(f"CROSSWALK_INVALID {artifact.path}: {error}")
            return 2

    # CURRENT-RUN IBKR: only artifacts this manifest BOUND may seed the six IBKR
    # capabilities. A stage that ran but whose artifact was rejected binds
    # nothing, and therefore contributes nothing current.
    base_states: dict[str, CapabilityState] = {}
    for artifact in manifest.artifacts_of_type(ArtifactType.IBKR_HISTORICAL_EVIDENCE):
        try:
            matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(Path(artifact.path))
        except ValueError as error:
            print(f"IBKR_EVIDENCE_INVALID {artifact.path}: {error}")
            return 2
        base_states.update(matrix.states)

    # GLOBAL (historical) truth is read separately and is NEVER reported as
    # current-run evidence. A prior owner run's verified capability stays known
    # even when today's refresh failed.
    global_states: dict[str, CapabilityState] = {}
    if args.ibkr_evidence.is_file():
        try:
            global_states.update(
                DatasetCapabilityMatrix.from_ibkr_historical_evidence(
                    args.ibkr_evidence
                ).states
            )
        except ValueError:
            global_states = {}

    states = capability_states_from_evidence(evidences, base_states=base_states)
    rows: dict[str, dict[str, str]] = {}
    for name in sorted(states):
        status = (
            current_run_status(manifest, name, stage_by_capability=STAGE_BY_CAPABILITY)
            if name in STAGE_BY_CAPABILITY
            else CurrentRunStatus.NO_CURRENT_EVIDENCE
        )
        evidence = next((e for e in evidences if e.capability == name), None)
        global_state = global_states.get(name, states[name])
        rows[name] = {
            # what THIS run established
            "capability_state": states[name].value,
            "current_run": status.value,
            "result": evidence.result.value if evidence is not None else "NONE",
            # what is known historically; never conflated with the above
            "global_state": global_state.value,
        }

    ready = all(
        states.get(name) is CapabilityState.EMPIRICALLY_VERIFIED
        for name in DATASET_V1_REQUIRED_CAPABILITIES
    )

    # SOURCE EXECUTION vs ARTIFACT AUTHENTICATION are different questions. The
    # owner report said "IBKR PASS" while the IBKR artifacts had failed to
    # authenticate, which read as though the whole IBKR path had succeeded.
    stage_rows: dict[str, dict[str, object]] = {}
    for stage in sorted(manifest.stages, key=lambda item: item.name):
        bound = [a for a in manifest.artifacts if a.stage == stage.name]
        declared = len(stage.artifacts)
        if not stage.outcome.may_publish:
            artifact_status = "NOT_APPLICABLE"
        elif declared and not bound:
            artifact_status = "FAILED_AUTHENTICATION"
        elif declared and len(bound) < declared:
            artifact_status = "PARTIALLY_BOUND"
        elif bound:
            artifact_status = "BOUND"
        else:
            artifact_status = "NO_ARTIFACTS"
        stage_rows[stage.name] = {
            "execution_status": stage.outcome.value,
            "artifact_status": artifact_status,
            "artifacts_declared": declared,
            "artifacts_bound": len(bound),
            "current_run_evidence": bool(bound),
        }

    payload = {
        "stages": stage_rows,
        "run_id": manifest.run_id,
        "manifest_checksum": manifest.checksum,
        "capabilities": rows,
        "full_dataset_contract_ready": ready,
        "failure_reasons": list(manifest.failure_reasons),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ready else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("new-id", "stage", "manifest", "report"))
    parser.add_argument("--run-id", default="")
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--stage-name", default="")
    parser.add_argument("--outcome", default="NOT_RUN")
    parser.add_argument("--detail", default="")
    parser.add_argument("--artifact", action="append", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--mode", default="owner-host")
    parser.add_argument("--started-at", default="")
    parser.add_argument(
        "--ibkr-evidence", type=Path, default=Path("docs/poi/evidence/POI-IBKR-HIST.json")
    )
    _force_utf8_stdout()
    args = parser.parse_args(argv[1:])

    if args.command == "new-id":
        return cmd_new_id(args)
    if args.command in ("stage", "manifest") and not args.run_id:
        print("RUN_ID_REQUIRED: pass --run-id")
        return 2
    if args.command == "stage":
        if not args.stage_name:
            print("STAGE_NAME_REQUIRED: pass --stage-name")
            return 2
        return cmd_stage(args)
    if args.command == "manifest":
        return cmd_manifest(args)
    if args.manifest is None:
        print("MANIFEST_REQUIRED: pass --manifest <run-manifest.json>")
        return 2
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
