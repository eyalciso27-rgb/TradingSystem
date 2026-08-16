"""Validate Dataset V1 evidence/readiness without contacting external services."""

import argparse
from pathlib import Path

from trading_market_data_core.dataset_v1 import DatasetCapabilityMatrix

DEFAULT_EVIDENCE = Path("docs/poi/evidence/POI-IBKR-HIST.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ibkr-evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="fail unless every Dataset V1 capability is empirically verified",
    )
    args = parser.parse_args()

    matrix = DatasetCapabilityMatrix.from_ibkr_historical_evidence(args.ibkr_evidence)
    for name, state in sorted(matrix.states.items()):
        print(f"{name}: {state.value}")
    print(f"evidence_checksum: {matrix.evidence_checksum}")
    print(f"full_dataset_contract_ready: {matrix.full_dataset_contract_ready}")

    if args.require_full:
        matrix.assert_full_dataset_contract_ready()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
