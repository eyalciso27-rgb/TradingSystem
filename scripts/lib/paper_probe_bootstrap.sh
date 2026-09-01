# shellcheck shell=bash
#
# The ONE owner-safe Paper DB/object-store bootstrap, shared by BOTH the
# read-only execution probe and the protected-bracket proof. Source it; do not
# execute it.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/paper_env.sh"
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/paper_probe_bootstrap.sh"
#   trap 'paper_probe_close_db' EXIT
#   paper_probe_open_db "$REPO_ROOT" || fail "..."
#
# Why this exists
# ---------------
# The Paper stack deliberately publishes PostgreSQL nowhere, and a fresh owner
# SSH shell holds only SECRETS_ROOT. Both owner commands (the probe and the
# proof) therefore need the SAME sequence: load the credentials from the
# canonical store, open a LOOPBACK-ONLY publish, prove the database authenticates
# FROM THIS HOST, bring the schema to head, and always close the publish again.
# Two copies of that sequence would drift; this is the single copy.
#
# What it never does
# ------------------
# * never prints the DSN, the database password or the broker account id;
# * never publishes PostgreSQL beyond 127.0.0.1;
# * never runs `down -v`, removes a volume, prunes, or resets the database --
#   the Paper database holds the durable execution history;
# * never opens a broker socket (that is the caller's command, after this).

: "${PAPER_PROBE_DB_PORT_DEFAULT:=55432}"

# Set to 1 the instant the publish is ATTEMPTED, so the caller's EXIT trap always
# closes it -- armed before the mutating call, never after, because `compose up`
# can recreate the container with the published port and still exit non-zero.
PAPER_PROBE_PUBLISH_STARTED=0

_paper_probe_log() { printf '%s\n' "$*" >&2; }

# The canonical Paper stack files PLUS the loopback-publish override, in order.
_paper_probe_compose_files() {
  printf '%s\n' \
    -f deploy/compose.yaml \
    -f deploy/compose.paper.yaml \
    -f deploy/compose.paper-probe.yaml
}

# Close the temporary loopback publish. Idempotent, and safe whether or not the
# publish was ever opened; intended for the caller's EXIT trap so it runs on
# success AND on failure. Never removes the volume.
paper_probe_close_db() {
  if [ "${PAPER_PROBE_PUBLISH_STARTED:-0}" -eq 1 ]; then
    _paper_probe_log "-- closing the temporary loopback database publish"
    # Re-apply the paper stack WITHOUT the probe override, which removes the
    # published port. A bare compose command in the owner shell would fail
    # interpolation (it needs the generic POSTGRES_* the loader exports), so the
    # fallback points at the canonical helper, never at `down`/`down -v`.
    if ! docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
         up -d --no-deps postgres >/dev/null 2>&1; then
      _paper_probe_log "WARNING: could not close the temporary publish; run:"
      _paper_probe_log "  bash scripts/close_paper_probe_publish.sh"
    fi
  fi
}

# Prepare the Paper database and object store for a run. On success, exports
# DATABASE_URL (built by SQLAlchemy, never interpolated) and TRADING_OBJECT_STORE.
# Returns non-zero on ANY failure so the caller fails closed BEFORE opening a
# broker socket. Never deletes data.
paper_probe_open_db() {
  local repo_root="$1"
  local db_port="${PAPER_PROBE_DB_PORT:-$PAPER_PROBE_DB_PORT_DEFAULT}"
  local -a compose_files
  mapfile -t compose_files < <(_paper_probe_compose_files)

  # -- local preconditions, before anything is started ----------------------
  if [ -z "${SECRETS_ROOT:-}" ]; then
    _paper_probe_log "ERROR: SECRETS_ROOT must point at the canonical secret store (the paper account id is read from \$SECRETS_ROOT/PAPER/ibkr/paper-account-id; never export the account id itself)"
    return 1
  fi
  if [ ! -f "$SECRETS_ROOT/PAPER/ibkr/paper-account-id" ]; then
    _paper_probe_log "ERROR: the canonical paper account secret is missing at \$SECRETS_ROOT/PAPER/ibkr/paper-account-id"
    return 1
  fi
  if [ ! -s "$SECRETS_ROOT/PAPER/ibkr/paper-account-id" ]; then
    _paper_probe_log "ERROR: the canonical paper account secret is empty"
    return 1
  fi

  # The PostgreSQL credentials come from the SAME canonical store, loaded into
  # this process only; the owner exports none of them. The loader also exports
  # the generic POSTGRES_* family compose.yaml needs to interpolate at all.
  _paper_probe_log "-- loading the paper database credentials from the canonical secret store"
  load_paper_environment || {
    _paper_probe_log "ERROR: the paper database credentials are not available; run the one-time bootstrap first:
    bash scripts/bootstrap_paper_postgres.sh
  (nothing was started, and zero IBKR connections were opened)"
    return 1
  }
  export PAPER_DB="$PAPER_POSTGRES_DB"
  _paper_probe_log "   role     : ${PAPER_POSTGRES_USER}"
  _paper_probe_log "   database : ${PAPER_DB}"

  command -v docker >/dev/null 2>&1 || {
    _paper_probe_log "ERROR: docker is required to reach the paper database"
    return 1
  }

  # -- open the loopback-only publish, briefly ------------------------------
  if [ -z "$(docker compose "${compose_files[@]}" ps -aq postgres 2>/dev/null)" ]; then
    _paper_probe_log "-- first run: creating the paper database (isolated network, persistent volume)"
  fi
  _paper_probe_log "-- opening a temporary loopback database publish on 127.0.0.1:${db_port}"
  PAPER_PROBE_PUBLISH_STARTED=1
  if ! PAPER_PROBE_DB_PORT="$db_port" docker compose "${compose_files[@]}" \
       up -d --no-deps postgres >/dev/null; then
    _paper_probe_log "ERROR: could not start the paper database with the diagnostic publish"
    return 1
  fi

  # Wait for it to accept connections, and FAIL if it never does.
  local db_ready=0
  for _ in $(seq 1 30); do
    if docker compose "${compose_files[@]}" exec -T postgres \
         pg_isready -U "$PAPER_POSTGRES_USER" -d "$PAPER_DB" >/dev/null 2>&1; then
      db_ready=1
      break
    fi
    sleep 1
  done
  if [ "$db_ready" -ne 1 ]; then
    _paper_probe_log "ERROR: the paper database did not become ready within 30s; not continuing (zero IBKR connections were opened)"
    return 1
  fi

  # -- DATABASE_URL built by SQLAlchemy (percent-encoded), never echoed -------
  local url
  url="$(
    PAPER_PROBE_DB_HOST=127.0.0.1 PAPER_PROBE_DB_PORT="$db_port" \
    PAPER_POSTGRES_DB="$PAPER_DB" \
    uv run --no-sync python scripts/build_paper_database_url.py
  )" || {
    _paper_probe_log "ERROR: could not construct the paper database URL"
    return 1
  }
  export DATABASE_URL="$url"
  export TRADING_OBJECT_STORE="${TRADING_OBJECT_STORE:-var/objects/paper}"

  # -- prove the database is usable FROM THIS HOST (authenticated SELECT 1) ---
  _paper_probe_log "-- verifying the paper database from this host (authenticated SELECT 1)"
  if ! uv run --no-sync python scripts/check_paper_probe_database.py; then
    _paper_probe_log "ERROR: the paper database is not usable from this host; not continuing (zero IBKR connections were opened).
  If the database already existed, its stored role/password may differ from the
  canonical secret set. Reconcile them deliberately -- do NOT delete the volume:
  it holds the durable execution history."
    return 1
  fi

  # -- schema to head, still before any broker socket (alembic upgrade only) --
  _paper_probe_log "-- preparing the paper database schema (alembic upgrade head)"
  if ! uv run --no-sync python scripts/prepare_paper_database.py; then
    _paper_probe_log "ERROR: the paper database schema could not be prepared or verified; not continuing (zero IBKR connections were opened).
  Nothing was reset and no data was removed."
    return 1
  fi
}
