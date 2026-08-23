# shellcheck shell=bash
#
# The ONE owner-safe Paper environment loader. Source it; do not execute it.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/paper_env.sh"
#   load_paper_environment || fail "..."
#
# Why this exists as a library rather than as lines inside one script
# -------------------------------------------------------------------
# There must be exactly one answer to "how does Paper get its PostgreSQL
# credentials?", and it must be the canonical secret store. Before this file
# there were three answers -- the probe read the store, normal Paper startup
# expected the owner to export generic POSTGRES_* by hand, and CI supplied
# ci-syntax-check values -- which is three implementations free to drift apart,
# and they did.
#
# The generic aliases are the subtle part
# ---------------------------------------
# Compose interpolates each file INDEPENDENTLY, before merging them. So
#
#     docker compose -f deploy/compose.yaml -f deploy/compose.paper.yaml ...
#
# must first interpolate deploy/compose.yaml, which requires POSTGRES_USER and
# POSTGRES_PASSWORD via `:?`. compose.paper.yaml overrides those keys with
# PAPER_POSTGRES_* -- but an override cannot rescue a base file that already
# failed to interpolate. Loading only the PAPER_* family therefore leaves the
# very fresh-shell contract we are trying to prove still broken.
#
# So the loader exports both families from the same canonical values. That does
# not weaken the base file's no-default-credentials policy: `:?` still fails
# closed, and the values still come from the secret store rather than from a
# default or from a committed .env.

# Resolve the canonical PAPER credentials and export everything Compose needs.
#
# Returns non-zero (rather than exiting) so the caller decides how to fail; a
# library that kills its caller's process cannot be tested.
load_paper_environment() {
  local repo_root credentials
  local -a lines

  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || return 1

  if [ -z "${SECRETS_ROOT:-}" ]; then
    echo "SECRETS_ROOT must point at the canonical secret store" >&2
    return 1
  fi

  credentials="$(cd "$repo_root" && uv run --no-sync python scripts/load_paper_postgres_env.py)" \
    || return 1
  mapfile -t lines <<<"$credentials"
  if [ "${#lines[@]}" -ne 3 ]; then
    echo "the paper database credentials are incomplete" >&2
    return 1
  fi

  # The PAPER family: what compose.paper.yaml and the URL builder consume.
  export PAPER_POSTGRES_USER="${lines[0]}"
  export PAPER_POSTGRES_DB="${lines[1]}"
  export PAPER_POSTGRES_PASSWORD="${lines[2]}"

  # The generic family: what deploy/compose.yaml requires in order to
  # interpolate at all. Same values, same source, one resolution.
  export POSTGRES_USER="$PAPER_POSTGRES_USER"
  export POSTGRES_PASSWORD="$PAPER_POSTGRES_PASSWORD"
  export POSTGRES_DB="$PAPER_POSTGRES_DB"
}

# The canonical Paper stack files, in order. One definition, so the launcher,
# the probe wrapper and the config check cannot drift.
paper_compose_files() {
  printf '%s\n' -f deploy/compose.yaml -f deploy/compose.paper.yaml
}
