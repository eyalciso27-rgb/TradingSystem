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
#   has no submission path at all;
# * never removes the paper database volume, and never runs `down -v`.
#
# Everything secret is resolved internally from the canonical PAPER secret store
# under $SECRETS_ROOT/PAPER: the broker account (ibkr/paper-account-id) and the
# database credentials (postgres/paper-username, postgres/paper-password,
# postgres/paper-database). You export none of them. If the database credentials
# are not there yet, run the one-time bootstrap:
#
#   bash scripts/bootstrap_paper_postgres.sh

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
  # Idempotent: re-applying the paper stack without the probe override is safe
  # whether or not the override was ever applied.
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

# The PostgreSQL credentials are read from the SAME canonical store, by this
# script, into this process only. The owner does not export them.
#
# Requiring them in the environment was the real owner-host regression: the
# documentation said "from the PAPER secret store", the store held only the
# account id, and so a correct, complete, freshly provisioned host could not run
# the probe at all. A fresh SSH shell must be enough.
log "-- loading the paper database credentials from the canonical secret store"
PAPER_CREDENTIALS="$(uv run --no-sync python scripts/load_paper_postgres_env.py)" \
  || fail "the paper database credentials are not available; run the one-time bootstrap first:
    bash scripts/bootstrap_paper_postgres.sh
  (nothing was started, and zero IBKR connections were opened)"
mapfile -t PAPER_CREDENTIAL_LINES <<<"$PAPER_CREDENTIALS"
[ "${#PAPER_CREDENTIAL_LINES[@]}" -eq 3 ] \
  || fail "the paper database credentials are incomplete; not starting anything"
# Exported into THIS process only, for `docker compose` and for the URL builder.
# Never echoed; `set -x` is deliberately not used anywhere in this script.
export PAPER_POSTGRES_USER="${PAPER_CREDENTIAL_LINES[0]}"
export PAPER_POSTGRES_DB="${PAPER_CREDENTIAL_LINES[1]}"
export PAPER_POSTGRES_PASSWORD="${PAPER_CREDENTIAL_LINES[2]}"
unset PAPER_CREDENTIALS PAPER_CREDENTIAL_LINES
PAPER_DB="$PAPER_POSTGRES_DB"
log "   role     : ${PAPER_POSTGRES_USER}"
log "   database : ${PAPER_DB}"

command -v docker >/dev/null 2>&1 || fail "docker is required to reach the paper database"

# -- 3. open the loopback-only publish, briefly ------------------------------
# On a host that has never run the paper stack there is no container, no
# network and no volume. `compose up` creates all three, and PostgreSQL
# initialises the role and database from the credentials just loaded. That is
# the intended first run; it is reported, not silently performed.
if [ -z "$(docker compose "${COMPOSE_FILES[@]}" ps -aq postgres 2>/dev/null)" ]; then
  log "-- first run: creating the paper database (isolated network, persistent volume)"
fi
log "-- opening a temporary loopback database publish on 127.0.0.1:${DB_PORT}"
# Armed BEFORE the mutating call, not after it. `compose up` can recreate the
# container with the published port and still exit non-zero, and a flag set only
# on success would then leave the publish open -- loopback-only, but the stated
# lifecycle is "temporary and always closed". Cleanup is idempotent, so arming
# it early costs nothing when nothing changed.
PROBE_PUBLISH_STARTED=1
if ! PAPER_PROBE_DB_PORT="$DB_PORT" docker compose "${COMPOSE_FILES[@]}" \
     up -d --no-deps postgres >/dev/null; then
  fail "could not start the paper database with the diagnostic publish"
fi

# Wait for it to accept connections, and FAIL if it never does. Exporting
# DATABASE_URL after a timed-out wait recreates the exact owner-host failure
# this round is fixing: configuration present, database absent, discovered only
# after two IBKR sessions are open.
DB_READY=0
for _ in $(seq 1 30); do
  if docker compose "${COMPOSE_FILES[@]}" exec -T postgres \
       pg_isready -U "$PAPER_POSTGRES_USER" -d "$PAPER_DB" >/dev/null 2>&1; then
    DB_READY=1
    break
  fi
  sleep 1
done
[ "$DB_READY" -eq 1 ] \
  || fail "the paper database did not become ready within 30s; not starting the probe (zero IBKR connections were opened)"

# -- 4. run the probe --------------------------------------------------------
# DATABASE_URL is built by SQLAlchemy's own URL.create, which percent-encodes
# every component. Interpolating the credentials into a string here was wrong:
# a legal PostgreSQL password containing @ : / # % or a space produces a URL
# that is malformed, or well-formed and parsed as something else. The secret
# must not have to avoid characters so that our concatenation works.
#
# It is exported to the probe only, never echoed, and `set -x` is deliberately
# not used anywhere in this script.
DATABASE_URL="$(
  PAPER_PROBE_DB_HOST=127.0.0.1 PAPER_PROBE_DB_PORT="$DB_PORT" \
  PAPER_POSTGRES_DB="$PAPER_DB" \
  uv run --no-sync python scripts/build_paper_database_url.py
)" || fail "could not construct the paper database URL"
export DATABASE_URL
export TRADING_OBJECT_STORE="${TRADING_OBJECT_STORE:-var/objects/paper}"

# -- 5. prove the database is actually usable FROM THIS HOST -----------------
# pg_isready ran inside the container: it proves the server accepts connections
# and nothing else. It does not prove the loopback publish is reachable from
# here, that this URL parses, or that these credentials authenticate -- and the
# PostgreSQL image only initialises credentials on an EMPTY data directory, so
# an existing volume can still expect an older password than the environment
# now supplies. Every one of those failures would otherwise surface inside the
# probe, after two IBKR sessions are open.
log "-- verifying the paper database from this host (authenticated SELECT 1)"
if ! uv run --no-sync python scripts/check_paper_probe_database.py; then
  # An authentication failure here almost always means an existing volume was
  # initialised with different credentials. That needs the owner to reconcile
  # the two deliberately. It is NOT repaired by removing the volume: the paper
  # database holds the durable execution history, and `down -v` -- which this
  # script never runs -- would destroy it to fix a configuration mismatch.
  fail "the paper database is not usable from this host; not starting the probe (zero IBKR connections were opened).
  If the database already existed, its stored role/password may differ from the
  canonical secret set. Reconcile them deliberately -- alter the role in the
  running database to match, or point SECRETS_ROOT at the store that describes
  it. Do NOT delete the volume: it holds the durable execution history."
fi

log "-- running the probe (nothing will be submitted)"
set +e
uv run --no-sync python -m trading_execution_agent probe \
  --mode paper --symbol "$SYMBOL"
PROBE_STATUS=$?
set -e

unset DATABASE_URL
log "-- probe exit status: ${PROBE_STATUS}"
exit "$PROBE_STATUS"
