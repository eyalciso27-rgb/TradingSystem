#!/usr/bin/env bash
# Dataset V1 — real historical source qualification (owner-run, fail-closed).
#
# ONE command for the owner host. It:
#   1. preflights the toolchain and repository layout;
#   2. DISCOVERS whether a real historical source is configured;
#   3. verifies every existing evidence artifact (recomputing its checksum);
#   4. prints PASS/PARTIAL/FAIL and the capability state for each of the four
#      external Dataset V1 capabilities;
#   5. prints the resulting readiness and the evidence paths/checksums.
#
# It never prints credentials, never fabricates evidence and never promotes a
# capability by itself: promotion happens only through the authenticated
# evidence produced by a configured REAL source.
#
# Exit codes: 0 = dataset contract genuinely ready; 1 = not ready (expected
# while any external cell is blocked); 2 = preflight failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

SOURCE_CONFIG="${DATASET_V1_SOURCE_CONFIG:-config/data-sources/dataset-v1-sources.yaml}"
EVIDENCE_DIR="${DATASET_V1_EVIDENCE_DIR:-docs/poi/evidence/dataset-v1}"
CLI="trading_market_data_core.dataset_v1_qualification_cli"

echo "== Dataset V1 real-source qualification =="
echo "repo         : $REPO_ROOT"
echo "source config: $SOURCE_CONFIG"
echo "evidence dir : $EVIDENCE_DIR"
echo

# -- 1. preflight ------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  echo "PREFLIGHT_FAILED: uv is not installed on this host" >&2
  exit 2
fi
if [ ! -d "packages/py/market-data-core" ]; then
  echo "PREFLIGHT_FAILED: run this from the TradingSystem repository" >&2
  exit 2
fi
echo "preflight: OK"
echo

# -- 2. acceptance criteria (what a real source must prove) ------------------
echo "== acceptance criteria =="
uv run --no-sync python -m "$CLI" criteria
echo

# -- 3. discovery ------------------------------------------------------------
echo "== source discovery =="
uv run --no-sync python -m "$CLI" discover \
  --config "$SOURCE_CONFIG" --evidence-dir "$EVIDENCE_DIR"
echo

# -- 4. verify each evidence artifact individually ---------------------------
if [ -d "$EVIDENCE_DIR" ]; then
  shopt -s nullglob
  for artifact in "$EVIDENCE_DIR"/*.json; do
    echo "== verifying $artifact =="
    # a tampered/stale artifact exits non-zero here; keep going to report all
    uv run --no-sync python -m "$CLI" verify --evidence "$artifact" || true
    sha256sum "$artifact" 2>/dev/null || shasum -a 256 "$artifact" 2>/dev/null || true
    echo
  done
  shopt -u nullglob
fi

# -- 5. consolidated report + readiness --------------------------------------
echo "== capability report =="
set +e
uv run --no-sync python -m "$CLI" report \
  --config "$SOURCE_CONFIG" --evidence-dir "$EVIDENCE_DIR"
STATUS=$?
set -e

echo
if [ "$STATUS" -eq 0 ]; then
  echo "RESULT: full_dataset_contract_ready = TRUE"
  echo "A real DatasetSnapshot may now be built; readiness is still computed by"
  echo "the builder gate (never overridden here)."
else
  echo "RESULT: full_dataset_contract_ready = FALSE (expected while cells are blocked)"
  echo "No snapshot can be BACKTEST_ELIGIBLE until every required capability has"
  echo "real, authenticated evidence."
fi
exit "$STATUS"
