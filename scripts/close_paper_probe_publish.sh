#!/usr/bin/env bash
# Owner-safe recovery: close the temporary loopback database publish the probe
# opens, by re-applying the paper postgres service WITHOUT the probe override.
#
#   export SECRETS_ROOT=<your canonical secret store>
#   bash scripts/close_paper_probe_publish.sh
#
# When to run it
# --------------
# scripts/run_execution_probe_paper.sh opens a 127.0.0.1-only publish of the
# paper database for the duration of a run and closes it again on exit. If that
# teardown itself fails, the wrapper points here rather than at a bare
# `docker compose` command -- because a bare command requires the generic
# POSTGRES_* variables, and a fresh owner shell has only SECRETS_ROOT. Round 5
# proved that bare command fails interpolation.
#
# What it does, and does not do
# -----------------------------
# * loads BOTH credential families from the canonical store, into this process;
# * re-applies base + paper compose WITHOUT compose.paper-probe.yaml, which
#   removes the published port;
# * `up -d --no-deps postgres`, so the volume and its data are preserved;
# * never runs down, down -v, volume rm or volume prune;
# * never prints the password or the DSN;
# * starts no execution path. Paper Auto and Live remain prohibited.

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

command -v docker >/dev/null 2>&1 || fail "docker is required"

load_paper_environment \
  || fail "the paper database credentials are not available; run the one-time bootstrap first:
    bash scripts/bootstrap_paper_postgres.sh"

log "-- re-applying the paper database without the temporary publish"
# Deliberately base + paper only. The probe override (compose.paper-probe.yaml)
# is what publishes the port, so re-applying without it removes the publish.
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
  up -d --no-deps postgres
log "-- done; the temporary publish is closed and the volume is untouched"
