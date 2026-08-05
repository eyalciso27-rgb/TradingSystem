#!/usr/bin/env bash
# Owner-host runtime remediation — entry point.
#
#   --check                (DEFAULT) read-only: collect, validate, plan.
#   --apply                execute the plan transactionally, with rollback.
#   --verify               prove the contract against the persisted baseline.
#   --verify-post-restart  prove canonical ownership AFTER the owner-authorized
#                          restart, without demanding the old pre-restart PID.
#
# WHAT THIS NEVER DOES, IN ANY MODE:
#   * never starts, stops, restarts or relogins IB Gateway;
#   * never enables the watchdog timer;
#   * never runs any `--now` unit operation;
#   * never touches firewall / firewalld / nftables / OCI security lists;
#   * never touches SSH or VNC configuration;
#   * never reads or prints an IBC config file, a credential or an account id;
#   * never prints Java command-line arguments.
#
# The engine is scripts/owner_host_remediation.py, run with the PINNED venv
# interpreter. It lives in Python because the contract now requires recording
# and restoring ownership and mode, a machine-readable plan, an injectable
# command adapter and proof of the EFFECTIVE systemd properties — none of which
# shell can express in a way a test can hold.
set -euo pipefail

REPO_ROOT="/home/opc/projects/TradingSystem"
for arg in "$@"; do
  case "${arg}" in
    --repo-root=*) REPO_ROOT="${arg#*=}" ;;
  esac
done

VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
ENGINE="${REPO_ROOT}/scripts/owner_host_remediation.py"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "ERROR: pinned interpreter missing or not executable: ${VENV_PYTHON}" >&2
  echo ">>> FINAL STATE: FAILED_PRECHECK" >&2
  exit 1
fi
if [[ ! -r "${ENGINE}" ]]; then
  echo "ERROR: remediation engine missing: ${ENGINE}" >&2
  echo ">>> FINAL STATE: FAILED_PRECHECK" >&2
  exit 1
fi

# -B / PYTHONDONTWRITEBYTECODE: a read-only CHECK must not leave __pycache__
# behind on the host it claims not to touch.
PYTHONDONTWRITEBYTECODE=1 exec "${VENV_PYTHON}" -B "${ENGINE}" "$@"
