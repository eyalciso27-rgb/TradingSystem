#!/usr/bin/env bash
# Install the operational hardening assets (systemd units, tmpfiles, logrotate).
#
# DEFAULT MODE IS CHECK / DRY-RUN. It prints exactly what WOULD change and
# changes nothing. Applying requires an explicit --apply.
#
# WHAT THIS NEVER DOES, in any mode:
#   * never starts, stops, restarts or relogins IB Gateway;
#   * never enables a unit (enabling is an owner decision, done by hand);
#   * never touches firewall / firewalld / nftables / OCI security lists;
#   * never touches SSH or VNC configuration;
#   * never reads or writes an IBC config file or any credential.
#
# Those exclusions are deliberate. Network exposure and remote access are
# owner decisions with blast radius far beyond this repository, and a script
# that silently "fixed" a firewall could cut off the only way back into the
# host. This installer places files; the owner decides what runs.
#
# Usage:
#   scripts/install_ops_assets.sh                 # CHECK (default): show a diff
#   scripts/install_ops_assets.sh --apply         # copy files, daemon-reload
#   scripts/install_ops_assets.sh --apply --no-reload
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/systemd"

APPLY=0
RELOAD=1
# Prefix for every destination path. Empty means the real system paths.
# Used to REHEARSE an apply into a scratch tree (and by the test suite); it
# cannot make the script touch anything it would not otherwise touch.
DEST_ROOT=""
for arg in "$@"; do
  case "${arg}" in
    --apply) APPLY=1 ;;
    --no-reload) RELOAD=0 ;;
    --dest-root=*) DEST_ROOT="${arg#*=}" ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

# src::dst::mode
ASSETS=(
  "ibgateway-ibc.service::/etc/systemd/system/ibgateway-ibc.service::0644"
  "ibgateway-watchdog.service::/etc/systemd/system/ibgateway-watchdog.service::0644"
  "ibgateway-watchdog.timer::/etc/systemd/system/ibgateway-watchdog.timer::0644"
  "ibgateway-tmpfiles.conf::/etc/tmpfiles.d/ibgateway.conf::0644"
  "ibgateway-logrotate.conf::/etc/logrotate.d/ibgateway::0644"
)

if [[ "${APPLY}" -eq 1 ]]; then
  echo ">>> APPLY MODE — files will be written."
else
  echo ">>> CHECK / DRY-RUN MODE — nothing will be changed."
  echo ">>> Re-run with --apply to write these files."
fi
echo

changed=0
for entry in "${ASSETS[@]}"; do
  src="${SYSTEMD_SRC}/${entry%%::*}"
  rest="${entry#*::}"
  dst="${DEST_ROOT}${rest%%::*}"
  mode="${rest#*::}"

  if [[ ! -f "${src}" ]]; then
    echo "MISSING SOURCE: ${src}" >&2
    exit 1
  fi

  if [[ -f "${dst}" ]] && diff -q "${src}" "${dst}" >/dev/null 2>&1; then
    echo "unchanged : ${dst}"
    continue
  fi

  changed=$((changed + 1))
  if [[ -f "${dst}" ]]; then
    echo "WOULD UPDATE: ${dst} (mode ${mode})"
    diff -u "${dst}" "${src}" || true
  else
    echo "WOULD CREATE: ${dst} (mode ${mode})"
  fi

  if [[ "${APPLY}" -eq 1 ]]; then
    install -D -m "${mode}" "${src}" "${dst}"
    echo "installed : ${dst}"
  fi
done

echo
if [[ "${APPLY}" -eq 1 ]]; then
  if [[ "${RELOAD}" -eq 1 && "${changed}" -gt 0 ]]; then
    systemctl daemon-reload
    echo "systemd daemon-reload done"
    # Creates /run/ibgateway 0700 now, without starting anything.
    #
    # NO '|| true' HERE, AND NEVER AGAIN. /etc/tmpfiles.d/ibgateway.conf is the
    # single canonical owner of /run/ibgateway; neither unit declares
    # RuntimeDirectory. If this command fails, the directory that holds the
    # lock, the restart budget and the order-lifecycle marker may not exist at
    # the declared 0700 — and the watchdog units are configured to fail closed
    # rather than run without it. Reporting "tmpfiles applied" in that state
    # would be a false success on a security-relevant step.
    if ! systemd-tmpfiles --create "${DEST_ROOT}/etc/tmpfiles.d/ibgateway.conf"; then
      echo "ERROR: systemd-tmpfiles --create FAILED." >&2
      echo "       /run/ibgateway may be missing or have the wrong mode." >&2
      echo "       The install is INCOMPLETE. Do NOT enable the timer." >&2
      exit 1
    fi
    echo "tmpfiles applied"
  fi
  echo ">>> Files installed. NOTHING was enabled or started."
else
  echo ">>> ${changed} file(s) would change. Nothing was written."
fi

cat <<'NEXT'

NEXT STEPS (owner decisions, performed by hand — deliberately not automated):

  1. Review the units:      systemctl cat ibgateway-ibc.service
  2. Audit the host:        uv run --no-sync python scripts/host_operations_audit.py
  3. Read-only watchdog:    uv run --no-sync python scripts/ib_gateway_watchdog.py --status
  4. Enable the timer ONLY when you accept passive supervision:
                            systemctl enable --now ibgateway-watchdog.timer

  The Gateway service is NOT enabled by this script and the watchdog will NOT
  start or restart it: every unattended process action is gated on a complete
  read-only proof that the account is CLEAN, and a down Gateway can never
  provide that proof. Recovery is an owner-authorized action under
  docs/runbooks/CHAOS_RECOVERY_DRILLS.md.

  Port exposure (4002 / 5901) and OCI network security are NOT changed here.
  See docs/runbooks/HOST_PORT_SECURITY.md.
NEXT
