#!/usr/bin/env bash
# The canonical way to start the PAPER stack.
#
#   export SECRETS_ROOT=<your canonical secret store>
#   bash scripts/run_paper_stack.sh
#
# This is infrastructure startup only: PostgreSQL, NATS and the scheduler. It
# starts NO execution path, places no order, and cannot. Paper Auto and Live
# remain prohibited.
#
# What it replaces
# ----------------
# The documented command used to be:
#
#   docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml up -d
#
# described as "credentials from the PAPER secret store" -- which it was not.
# Compose interpolates each file independently before merging, so that command
# requires generic POSTGRES_USER and POSTGRES_PASSWORD in the caller's shell for
# deploy/compose.yaml to parse at all, whatever compose.paper.yaml later
# overrides. On a real fresh owner shell it fails before doing anything.
#
# This launcher loads the canonical credentials first and exports both families
# into its own process, so the only thing you set is SECRETS_ROOT.
#
# It never prints a password or a DSN, never writes a secret into the
# repository or a compose file, publishes no port, and removes no volume.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=scripts/lib/paper_env.sh
source "$REPO_ROOT/scripts/lib/paper_env.sh"

log() { printf '%s\n' "$*" >&2; }

fail() {
  log "ERROR: $*"
  exit 2
}

COMPOSE_FILES=(-f deploy/compose.yaml -f deploy/compose.paper.yaml)
WITH_OBSERVABILITY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-observability)
      # Mandatory from Paper Manual onward. It must go through this launcher
      # too: compose.observability.yaml also interpolates the generic
      # POSTGRES_USER/POSTGRES_PASSWORD for the postgres exporter, so running
      # it as a second bare command would fail exactly as the paper stack did.
      COMPOSE_FILES+=(-f deploy/compose.observability.yaml)
      WITH_OBSERVABILITY=1
      shift
      ;;
    --)
      shift
      break
      ;;
    *)
      fail "unknown option: $1 (supported: --with-observability)"
      ;;
  esac
done

command -v docker >/dev/null 2>&1 || fail "docker is required to start the paper stack"

if [ "$WITH_OBSERVABILITY" -eq 1 ]; then
  # A separate secret family, not part of the PAPER PostgreSQL set. Fail with a
  # clear message rather than letting Compose report an interpolation error.
  : "${GRAFANA_ADMIN_USER:?GRAFANA_ADMIN_USER must be provided for the observability stack}"
  : "${GRAFANA_ADMIN_PASSWORD:?GRAFANA_ADMIN_PASSWORD must be provided for the observability stack}"
fi

log "== paper stack (infrastructure only; no execution path) =="
load_paper_environment \
  || fail "the paper database credentials are not available; run the one-time bootstrap first:
    bash scripts/bootstrap_paper_postgres.sh"
log "-- credentials loaded from the canonical secret store"
log "   role     : ${PAPER_POSTGRES_USER}"
log "   database : ${PAPER_POSTGRES_DB}"

# Exactly the documented stack, no ports published, volumes preserved.
docker compose "${COMPOSE_FILES[@]}" up -d
log "-- paper stack is up (nothing in it trades)"
