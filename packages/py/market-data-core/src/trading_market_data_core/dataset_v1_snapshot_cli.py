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


def _file_checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--ibkr-evidence", type=Path, default=DEFAULT_IBKR_EVIDENCE)
    parser.add_argument("--index-id", default="SP500")
    parser.add_argument("--as-of", default="")
    parser.add_argument("--out", type=Path, default=None)
    _force_utf8_stdout()
    args = parser.parse_args(argv[1:])

    as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(tz=UTC).date()
    known_as_of = datetime.now(tz=UTC)

    # 1. load + AUTHENTICATE every evidence artifact
    evidences = []
    bindings: list[EvidenceBinding] = []
    sources: list[SourceBinding] = []
    if args.evidence_dir.is_dir():
        for path in sorted(args.evidence_dir.glob("*.json")):
            if path.name == "identity-crosswalk.json":
                continue  # a report, not capability evidence
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

    # 2. seed the six IBKR capabilities from their own authenticated evidence
    base_states: dict[str, CapabilityState] = {}
    if args.ibkr_evidence.is_file():
        try:
            matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(args.ibkr_evidence)
        except ValueError as error:
            print(f"IBKR_EVIDENCE_INVALID: {error}")
            return 2
        base_states = dict(matrix.states)
        # The six IBKR capabilities are claimed EMPIRICALLY_VERIFIED from THIS
        # artifact, so it must be bound and re-verified like any other evidence.
        # Without the binding the builder correctly refuses the claim.
        bindings.append(
            EvidenceBinding(
                name="POI-IBKR-HIST",
                path=args.ibkr_evidence,
                checksum=_file_checksum(args.ibkr_evidence),
            )
        )
        sources.append(
            SourceBinding(
                name="ibkr-paper-historical",
                version="POI-IBKR-HIST",
                checksum=_file_checksum(args.ibkr_evidence),
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
