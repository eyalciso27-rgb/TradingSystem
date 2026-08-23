#!/usr/bin/env bash
# One-time, owner-invoked bootstrap of the PAPER PostgreSQL credentials.
#
#   export SECRETS_ROOT=<your canonical secret store>
#   bash scripts/bootstrap_paper_postgres.sh
#
# Creates $SECRETS_ROOT/PAPER/postgres/{paper-username,paper-password,paper-database}
# if and only if none of them exists yet. Run it again and it changes nothing.
#
# It is a separate command on purpose. Generating credentials as a side effect
# of starting the probe would mean that a probe run could silently replace the
# password of an already-initialised PostgreSQL volume -- which the PostgreSQL
# image will not re-apply -- leaving a database nobody can authenticate against
# and whose data nobody can reach without the old password.
#
# It never prints the password, never prints a DSN, never rotates an existing
# credential, and never touches the database or its volume.

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

: "${SECRETS_ROOT:?SECRETS_ROOT must point at the canonical secret store -- the same store that holds PAPER/ibkr/paper-account-id}"

exec uv run --no-sync python scripts/bootstrap_paper_postgres.py "$@"
