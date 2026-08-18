"""Dataset V1 real snapshot attempt (owner host).

Attempts a DatasetSnapshot build from whatever REAL, authenticated evidence
exists, and reports the readiness the builder gate computes. It never forces a
readiness level: with any required capability still blocked, the gate stays
below ``BACKTEST_ELIGIBLE`` and the blockers are listed.

Evidence bindings are recomputed from the artifact bytes, so a decorative or
stale checksum cannot authorize a build.

Usage:
    uv run --no-sync python -m trading_market_data_core.dataset_v1_snapshot_cli \\
        --evidence-dir docs/poi/evidence/dataset-v1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from trading_market_data_core.dataset_v1 import (
    DATASET_V1_REQUIRED_CAPABILITIES,
    CapabilityState,
    DatasetCapabilityMatrix,
    DatasetV1,
)
from trading_market_data_core.dataset_v1_artifacts import (
    ArtifactError,
    ArtifactScan,
    ArtifactType,
    load_and_verify_crosswalk,
    scan_evidence_dir,
    verify_identity_binding,
)
from trading_market_data_core.dataset_v1_builder import (
    AdjustmentPolicy,
    BuildConfig,
    BuilderError,
    EvidenceBinding,
    ReadinessLevel,
    SourceBinding,
    build_snapshot,
    compute_readiness,
)
from trading_market_data_core.dataset_v1_qualification import (
    QualificationError,
    capability_states_from_evidence,
    load_and_verify_evidence,
)
from trading_market_data_core.dataset_v1_run import (
    load_and_verify_run_manifest,
    verify_manifest_artifacts,
)

DEFAULT_EVIDENCE_DIR = Path("docs/poi/evidence/dataset-v1")
DEFAULT_IBKR_EVIDENCE = Path("docs/poi/evidence/POI-IBKR-HIST.json")


def _force_utf8_stdout() -> None:
    """A console codepage must never abort a qualification run mid-way.

    Without this, a single non-ASCII character in a report raises
    UnicodeEncodeError on a non-UTF-8 console and the run dies after doing the
    real work but before writing its evidence.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _scan_from_manifest(manifest: object) -> ArtifactScan:
    """An ArtifactScan containing exactly what the current run produced."""
    from trading_market_data_core.dataset_v1_run import RunManifest

    assert isinstance(manifest, RunManifest)
    grouped: dict[ArtifactType, list[Path]] = {}
    for artifact in manifest.artifacts:
        grouped.setdefault(artifact.artifact_type, []).append(Path(artifact.path))
    return ArtifactScan(by_type={k: tuple(sorted(v)) for k, v in grouped.items()})


def _file_checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--ibkr-evidence", type=Path, default=DEFAULT_IBKR_EVIDENCE)
    parser.add_argument("--index-id", default="SP500")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--run-manifest", type=Path, default=None)
    _force_utf8_stdout()
    args = parser.parse_args(argv[1:])

    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(tz=UTC).date()
    known_as_of = datetime.now(tz=UTC)

    # 1. load + AUTHENTICATE every evidence artifact
    evidences = []
    bindings: list[EvidenceBinding] = []
    sources: list[SourceBinding] = []
    # CURRENT-RUN BINDING. With a run manifest the snapshot uses ONLY artifacts
    # that manifest binds, so a stale artifact left in the directory by an
    # earlier run can never be substituted into this build.
    manifest = None
    if args.run_manifest is not None:
        try:
            manifest = load_and_verify_run_manifest(args.run_manifest)
        except ArtifactError as error:
            print(f"RUN_MANIFEST_INVALID: {error}")
            return 2
        findings = verify_manifest_artifacts(manifest, root=Path.cwd())
        if findings:
            for finding in findings:
                print(f"ARTIFACT_FINDING: {finding}")
            return 2
        scan = _scan_from_manifest(manifest)
    else:
        scan = scan_evidence_dir(args.evidence_dir)
    for path, reason in scan.unclassified:
        print(f"ARTIFACT_UNCLASSIFIED {path}: {reason}")
        return 2
    # Type-routed: an auxiliary artifact is never parsed as evidence, and the
    # old filename-based exclusion is gone.
    for path in scan.promotable:
        try:
            evidences.append(load_and_verify_evidence(path))
        except QualificationError as error:
            print(f"EVIDENCE_INVALID {path}: {error}")
            return 2
        bindings.append(
            EvidenceBinding(name=path.stem, path=path, checksum=_file_checksum(path))
        )
        # The SOURCE digest the evidence itself authenticates — the raw
        # artifact the data came from, not the evidence file.
        descriptor = evidences[-1].source
        sources.append(
            SourceBinding(
                name=descriptor.source_id,
                version=descriptor.version,
                checksum=descriptor.raw_checksum or _file_checksum(path),
            )
        )

    # 2. IBKR capabilities may ONLY be seeded from evidence the CURRENT run
    # manifest binds. The real run bound zero artifacts yet the snapshot still
    # listed POI-IBKR-HIST, because this read the file directly. With a manifest
    # there is no direct-file fallback and no global-directory fallback.
    base_states: dict[str, CapabilityState] = {}
    ibkr_paths: list[Path] = []
    if manifest is not None:
        ibkr_paths = [
            Path(artifact.path)
            for artifact in manifest.artifacts_of_type(ArtifactType.IBKR_HISTORICAL_EVIDENCE)
        ]
        if not ibkr_paths:
            print(
                "IBKR_EVIDENCE_NOT_BOUND: the current run manifest binds no IBKR"
                " historical evidence; the snapshot will not use any."
            )
    elif args.ibkr_evidence.is_file():
        # legacy path, only when no run manifest is supplied at all
        ibkr_paths = [args.ibkr_evidence]
    for ibkr_path in ibkr_paths:
        try:
            matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(ibkr_path)
        except ValueError as error:
            print(f"IBKR_EVIDENCE_INVALID: {error}")
            return 2
        base_states.update(matrix.states)
        # The six IBKR capabilities are claimed EMPIRICALLY_VERIFIED from THIS
        # artifact, so it must be bound and re-verified like any other evidence.
        # Without the binding the builder correctly refuses the claim.
        bindings.append(
            EvidenceBinding(
                name=ibkr_path.stem, path=ibkr_path, checksum=_file_checksum(ibkr_path)
            )
        )
        sources.append(
            SourceBinding(
                name="ibkr-paper-historical",
                version=ibkr_path.stem,
                checksum=_file_checksum(ibkr_path),
            )
        )

    # The identity artifact is bound to the build and RE-VERIFIED. A snapshot
    # must not assert identities that its crosswalk never resolved, and a
    # tampered crosswalk must invalidate the build rather than be ignored.
    crosswalk = None
    for path in scan.paths(ArtifactType.IDENTITY_CROSSWALK):
        try:
            crosswalk = load_and_verify_crosswalk(path)
        except ArtifactError as error:
            print(f"CROSSWALK_INVALID {path}: {error}")
            return 2
        bindings.append(
            EvidenceBinding(name=path.stem, path=path, checksum=_file_checksum(path))
        )
        for source in crosswalk.sources:
            sources.append(
                SourceBinding(
                    name=source.name, version=source.version, checksum=source.checksum
                )
            )

    states = capability_states_from_evidence(evidences, base_states=base_states)

    # 3. No reference rows are invented here. A dataset with no real priced
    # universe cannot be BACKTEST_ELIGIBLE, and the gate is what says so.
    dataset = DatasetV1()
    readiness, blockers = compute_readiness(
        states, validation_errors=(), universe_size=0, priced_securities=0
    )

    snapshot_id = None
    fingerprint = None
    try:
        snapshot = build_snapshot(
            dataset,
            BuildConfig(
                as_of=as_of, known_as_of=known_as_of, index_id=args.index_id,
                # RAW: nothing is adjusted here, so no adjustment can be applied twice.
                adjustment_policy=AdjustmentPolicy.RAW,
            ),
            sources=sources,
            evidence=bindings,
            capability_states=states,
        )
        claimed = [identity.security_id for identity in snapshot.security_master]
        if crosswalk is None and manifest is not None:
            produced = manifest.artifacts_of_type(ArtifactType.IDENTITY_CROSSWALK)
            if produced:
                print(
                    "CROSSWALK_NOT_BOUND: the run produced an identity crosswalk"
                    f" ({produced[0].path}) but the snapshot did not bind it;"
                    " refusing to assert SEC-derived identities unbound."
                )
                return 2
        if crosswalk is not None:
            unbound = verify_identity_binding(claimed, crosswalk)
            if unbound:
                print(
                    "SNAPSHOT_NOT_BUILT: security_ids not bound to the identity"
                    f" crosswalk artifact: {list(unbound)}"
                )
                return 2
        snapshot_id = snapshot.manifest.snapshot_id
        fingerprint = snapshot.manifest.fingerprint
        readiness = snapshot.manifest.readiness
    except BuilderError as error:
        print(f"SNAPSHOT_NOT_BUILT: {error}")

    payload = {
        "snapshot_id": snapshot_id,
        "fingerprint": fingerprint,
        "snapshot_readiness": readiness.value,
        "capability_blockers": list(blockers),
        "capability_states": {name: state.value for name, state in sorted(states.items())},
        "evidence": sorted(f"{b.name}={b.checksum}" for b in bindings),
        "identity_crosswalk": crosswalk.checksum if crosswalk is not None else None,
        "run_id": manifest.run_id if manifest is not None else None,
        "run_manifest_checksum": manifest.checksum if manifest is not None else None,
        "artifact_type": ArtifactType.SNAPSHOT_REPORT.value,
        "universe_size": 0,
        "priced_securities": 0,
        "backtest_eligible": readiness is ReadinessLevel.BACKTEST_ELIGIBLE,
        "as_of": as_of.isoformat(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"snapshot report written: {args.out}")

    verified = sum(
        1
        for name in DATASET_V1_REQUIRED_CAPABILITIES
        if states.get(name) is CapabilityState.EMPIRICALLY_VERIFIED
    )
    print(
        f"required capabilities verified: {verified}/{len(DATASET_V1_REQUIRED_CAPABILITIES)}"
    )
    return 0 if readiness is ReadinessLevel.BACKTEST_ELIGIBLE else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
