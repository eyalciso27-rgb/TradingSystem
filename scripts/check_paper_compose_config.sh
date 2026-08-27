#!/usr/bin/env bash
# Prove the PAPER compose stack interpolates from the canonical secret store
# ALONE -- the real owner contract -- rather than from credentials someone
# happened to have in their shell.
#
#   export SECRETS_ROOT=<a store holding the PAPER PostgreSQL set>
#   bash scripts/check_paper_compose_config.sh
#
# Run it from an environment with NO POSTGRES_* and NO PAPER_POSTGRES_*. That is
# the whole point: the previous CI validation injected both families itself, so
# it proved only that Paper compose parses when handed test credentials, which
# is not the runtime contract and masked a real fresh-shell failure.
#
# It parses only. It starts nothing, pulls nothing and needs no network.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck source=scripts/lib/paper_env.sh
source "$REPO_ROOT/scripts/lib/paper_env.sh"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

load_paper_environment \
  || fail "could not load the canonical paper credentials; nothing was validated"

# The normal paper stack, and the probe variant with the loopback-only publish.
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml config -q \
  || fail "the paper stack does not interpolate from the canonical secret store"
docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml \
               -f deploy/compose.paper-probe.yaml config -q \
  || fail "the paper probe stack does not interpolate from the canonical secret store"

printf 'PAPER_COMPOSE_CONFIG_OK\n'
