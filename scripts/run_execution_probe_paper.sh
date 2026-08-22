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
# it is never bound to 0.0.0.0.
#
# What it never does
# ------------------
# * never prints the DSN, the database password or the broker account id;
# * never publishes PostgreSQL beyond 127.0.0.1;
# * never writes a secret into the repository or into a compose file;
# * never submits an order -- the production authority denies, and the probe
#   has no submission path at all.
#
# The broker account is resolved internally from the canonical PAPER secret
# store ($SECRETS_ROOT/PAPER/ibkr/paper-account-id). You do not export it.

set -Eeuo pipefail

SYMBOL="${1:-AAPL}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_FILES=(
  -f deploy/compose.yaml
  -f deploy/compose.paper.yaml
  -f deploy/compose.paper-probe.yaml
)
DB_PORT="${PAPER_PROBE_DB_PORT:-55432}"
PROBE_PUBLISH_STARTED=0

log() { printf '%s\n' "$*" >&2; }

fail() {
  log "ERROR: $*"
  exit 2
}

cleanup() {
  local status=$?
  if [ "$PROBE_PUBLISH_STARTED" -eq 1 ]; then
    log "-- closing the temporary loopback database publish"
    # Re-apply the paper stack WITHOUT the probe override, which removes the
    # published port. Errors here are reported, never hidden.
    if ! docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
         up -d --no-deps postgres >/dev/null 2>&1; then
      log "WARNING: could not close the temporary publish; run:"
      log "  docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml up -d --no-deps postgres"
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

# -- 1. build/version evidence, so the artifact says what was actually run ----
log "== execution probe (PAPER, read-only, no submission) =="
log "repository : $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
log "branch     : $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null; then
  log "WARNING    : the working tree has uncommitted changes"
fi
log "symbol     : $SYMBOL"

# -- 2. local preconditions, before anything is started ----------------------
: "${SECRETS_ROOT:?SECRETS_ROOT must point at the canonical secret store (the paper account id is read from \$SECRETS_ROOT/PAPER/ibkr/paper-account-id; never export the account id itself)}"
[ -f "$SECRETS_ROOT/PAPER/ibkr/paper-account-id" ] \
  || fail "the canonical paper account secret is missing at \$SECRETS_ROOT/PAPER/ibkr/paper-account-id"
[ -s "$SECRETS_ROOT/PAPER/ibkr/paper-account-id" ] \
  || fail "the canonical paper account secret is empty"

: "${PAPER_POSTGRES_USER:?PAPER_POSTGRES_USER must come from the PAPER secret store, as for the paper stack itself}"
: "${PAPER_POSTGRES_PASSWORD:?PAPER_POSTGRES_PASSWORD must come from the PAPER secret store, as for the paper stack itself}"
PAPER_DB="${PAPER_POSTGRES_DB:-trading_paper}"

command -v docker >/dev/null 2>&1 || fail "docker is required to reach the paper database"

# -- 3. open the loopback-only publish, briefly ------------------------------
log "-- opening a temporary loopback database publish on 127.0.0.1:${DB_PORT}"
PAPER_PROBE_DB_PORT="$DB_PORT" docker compose "${COMPOSE_FILES[@]}" \
  up -d --no-deps postgres >/dev/null
PROBE_PUBLISH_STARTED=1

# Wait for it to accept connections rather than assuming.
for _ in $(seq 1 30); do
  if docker compose "${COMPOSE_FILES[@]}" exec -T postgres \
       pg_isready -U "$PAPER_POSTGRES_USER" -d "$PAPER_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# -- 4. run the probe --------------------------------------------------------
# DATABASE_URL is constructed here and exported to the probe only. It is never
# echoed, and `set -x` is deliberately not used anywhere in this script.
export DATABASE_URL="postgresql+psycopg://${PAPER_POSTGRES_USER}:${PAPER_POSTGRES_PASSWORD}@127.0.0.1:${DB_PORT}/${PAPER_DB}"
export TRADING_OBJECT_STORE="${TRADING_OBJECT_STORE:-var/objects/paper}"

log "-- running the probe (nothing will be submitted)"
set +e
uv run --no-sync python -m trading_execution_agent probe \
  --mode paper --symbol "$SYMBOL"
PROBE_STATUS=$?
set -e

unset DATABASE_URL
log "-- probe exit status: ${PROBE_STATUS}"
exit "$PROBE_STATUS"
