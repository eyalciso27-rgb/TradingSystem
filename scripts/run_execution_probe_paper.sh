#!/usr/bin/env bash
# The canonical owner-safe way to run the execution probe against PAPER.
#
#   bash scripts/run_execution_probe_paper.sh [SYMBOL]
#
# What this solves
# ----------------
# The probe needs two things that do not normally meet: the IB Gateway, which is
# reachable only on the host at 127.0.0.1:4002 (both the adapter and the
# market-data session refuse anything else), and the paper PostgreSQL, which the
# paper stack deliberately publishes nowhere.
#
# So this script opens a LOOPBACK-ONLY publish of the paper database for the
# duration of the run and closes it again afterwards. It is never left open, and
# it is never bound to 0.0.0.0. That whole DB/env lifecycle -- secret loading,
# the loopback publish, the authenticated preflight, DATABASE_URL construction,
# schema-to-head and cleanup -- lives in scripts/lib/paper_probe_bootstrap.sh,
# shared with the protected-bracket proof so the two cannot drift.
#
# What it never does
# ------------------
# * never prints the DSN, the database password or the broker account id;
# * never publishes PostgreSQL beyond 127.0.0.1;
# * never writes a secret into the repository or into a compose file;
# * never submits an order -- the production authority denies, and the probe
#   has no submission path at all;
# * never removes the paper database volume, and never runs `down -v`.
#
# Everything secret is resolved internally from the canonical PAPER secret store
# under $SECRETS_ROOT/PAPER. You export none of it. If the database credentials
# are not there yet, run the one-time bootstrap:
#
#   bash scripts/bootstrap_paper_postgres.sh

set -Eeuo pipefail

SYMBOL="${1:-AAPL}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=scripts/lib/paper_env.sh
source "$REPO_ROOT/scripts/lib/paper_env.sh"
# shellcheck source=scripts/lib/paper_probe_bootstrap.sh
source "$REPO_ROOT/scripts/lib/paper_probe_bootstrap.sh"

log() { printf '%s\n' "$*" >&2; }
fail() {
  log "ERROR: $*"
  exit 2
}

# The temporary loopback publish is always closed -- on success AND on failure.
trap 'paper_probe_close_db' EXIT

# -- build/version evidence, so the artifact says what was actually run -------
log "== execution probe (PAPER, read-only, no submission) =="
log "repository : $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
log "branch     : $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null; then
  log "WARNING    : the working tree has uncommitted changes"
fi
log "symbol     : $SYMBOL"

# -- prepare the Paper DB + object store (shared, owner-safe, fail-closed) -----
paper_probe_open_db "$REPO_ROOT" \
  || fail "the paper environment could not be prepared; not starting the probe (zero IBKR connections were opened)"

# -- run the probe (nothing will be submitted) --------------------------------
log "-- running the probe (nothing will be submitted)"
set +e
uv run --no-sync python -m trading_execution_agent probe \
  --mode paper --symbol "$SYMBOL"
PROBE_STATUS=$?
set -e

unset DATABASE_URL
log "-- probe exit status: ${PROBE_STATUS}"
exit "$PROBE_STATUS"
