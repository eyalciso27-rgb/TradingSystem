#!/usr/bin/env bash
# The canonical owner-safe way to run the PROTECTED-BRACKET PROOF against PAPER.
#
#   bash scripts/run_protected_bracket_proof_paper.sh [SYMBOL] [ARM_PHRASE]
#
# Dry run (default) -- places NOTHING, shows what would be sent:
#   bash scripts/run_protected_bracket_proof_paper.sh AAPL
#
# Armed real Paper proof -- places ONE 1-share bracket, proves both legs,
# cancels both legs and reconciles:
#   bash scripts/run_protected_bracket_proof_paper.sh AAPL yes-place-a-real-paper-bracket
#
# What this solves
# ----------------
# The Python proof needs the same Paper DB/object-store environment the probe
# needs, and a fresh owner SSH shell has only SECRETS_ROOT. That whole lifecycle
# -- secret loading, the LOOPBACK-ONLY publish, the authenticated preflight,
# DATABASE_URL construction, schema-to-head, and guaranteed cleanup -- lives in
# scripts/lib/paper_probe_bootstrap.sh, the SAME implementation the read-only
# probe uses, so the two owner commands cannot drift.
#
# What it never does
# ------------------
# * never prints the DSN, the database password or the broker account id;
# * never publishes PostgreSQL beyond 127.0.0.1;
# * never runs `down -v`, removes a volume, prunes, or resets the database;
# * never touches Live -- the Paper port 4002 is the only port allowed;
# * never enables Paper Auto; production authorization stays DenyAll -- only the
#   Python proof's own scoped, single-use, Paper-only authority can submit, and
#   only when armed with the exact phrase;
# * never market-flattens: a residual position is reported for a manual flatten.

set -Eeuo pipefail

SYMBOL="${1:-AAPL}"
ARM_ARG="${2:-}"
# The exact phrase the Python proof requires to arm a real order. A bare flag is
# never enough; the whole phrase must be typed, so arming is a deliberate act.
ARM_PHRASE="yes-place-a-real-paper-bracket"
# Paper only, always. The Live API port (4001) is refused; the adapter refuses
# it too, but this is an earlier, louder refusal.
PORT="${TRADING_IBKR_PORT:-4002}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

log() { printf '%s\n' "$*" >&2; }
fail() {
  log "ERROR: $*"
  exit 2
}

# -- cheap safety gates FIRST: no bootstrap, no socket, until these pass -------
[ "$PORT" = "4002" ] \
  || fail "Paper port 4002 only; refusing port ${PORT} (Live is never allowed)"

# Arming: ONLY the exact phrase arms a real order. Anything else non-empty is a
# refusal (never a silent dry run); empty is the dry run.
EXECUTE_ARGS=()
MODE="DRY RUN (places nothing)"
if [ -n "$ARM_ARG" ]; then
  [ "$ARM_ARG" = "$ARM_PHRASE" ] \
    || fail "the second argument must be exactly the arming phrase to place a real order; refusing (nothing was started)"
  EXECUTE_ARGS=(--execute "$ARM_PHRASE")
  MODE="ARMED -- will place ONE real 1-share Paper bracket"
fi

# shellcheck source=scripts/lib/paper_env.sh
source "$REPO_ROOT/scripts/lib/paper_env.sh"
# shellcheck source=scripts/lib/paper_probe_bootstrap.sh
source "$REPO_ROOT/scripts/lib/paper_probe_bootstrap.sh"

# The temporary loopback publish is always closed -- on success AND on failure.
trap 'paper_probe_close_db' EXIT

log "== protected-bracket proof (PAPER) : ${MODE} =="
log "repository : $(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
log "branch     : $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'unknown')"
log "symbol     : ${SYMBOL}  port : ${PORT}"

# -- verify the repo runtime Python is 3.13, through uv (never system python) --
PYVER="$(uv run --no-sync python -c 'import sys; print("%d.%d" % sys.version_info[:2])')" \
  || fail "could not determine the repo runtime Python via uv"
[ "$PYVER" = "3.13" ] \
  || fail "the repo runtime Python must be 3.13 (uv reported ${PYVER})"

# -- prepare the Paper DB + object store (shared, owner-safe, fail-closed) -----
paper_probe_open_db "$REPO_ROOT" \
  || fail "the paper environment could not be prepared; nothing was submitted (zero IBKR connections were opened)"

# -- run the proof ------------------------------------------------------------
# Dry run passes NO --execute, so the scoped authority denies and nothing is
# submitted. Armed passes --execute with the exact phrase only. The Python proof
# itself enforces qty 1, cancels both legs and reconciles, and never flattens.
log "-- running the protected-bracket proof"
set +e
uv run --no-sync python -m scripts.poi_protected_bracket_proof \
  --symbol "$SYMBOL" --port "$PORT" "${EXECUTE_ARGS[@]}"
PROOF_STATUS=$?
set -e

unset DATABASE_URL
log "-- proof exit status: ${PROOF_STATUS}"
exit "$PROOF_STATUS"
