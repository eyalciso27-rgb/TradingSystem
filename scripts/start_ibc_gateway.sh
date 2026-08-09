#!/usr/bin/env bash
#
# start_ibc_gateway.sh — launch IB Gateway (Paper) through IBC, reading
# credentials ONLY from a secure config file outside the repo.
#
# Hard safety rules:
#   * PAPER ONLY: the config's TradingMode must be "paper"; a "live" config
#     is refused fail-closed. There is no --live path.
#   * NO CREDENTIALS ON CLI/ENV: the IB login id and password live only in the
#     IBC config file (chmod 600). They are never passed on the command line
#     (so they never appear in `ps`) and never exported into a visible
#     environment variable, and this script never prints the config.
#   * NO ORDERS: this only starts the Gateway process; it places nothing.
#
# Run as the owner (opc). Real login (incl. IBKR Mobile 2FA) is performed by
# IBC/Gateway; this script does not automate or bypass 2FA.
#
# RECOVERY EXIT-CODE CONTRACT (paired with the systemd unit):
#   * The launcher runs ibcstart.sh in the FOREGROUND as a child and waits, so
#     it can classify how the Gateway ended and pick an exit code that drives
#     systemd's BOUNDED, FAIL-CLOSED recovery — instead of exec-ing and losing
#     that control.
#   * exit 78 (EXIT_FAIL_CLOSED) = a KNOWN fail-closed condition (invalid
#     config, non-paper, missing Java/files, OR a 2FA-timeout / auth-failure
#     detected in the IBC log). The unit lists 78 in RestartPreventExitStatus,
#     so systemd NEVER restarts on it. No 2FA/auth restart loop can form.
#   * exit 0 = an EXPLICIT owner/systemd stop ONLY (trapped SIGTERM/SIGINT);
#     systemd treats a requested stop as success and does not restart. A clean
#     but UNREQUESTED Gateway exit (e.g. an Auto Logoff) is NOT exit 0 — it is
#     converted to a recoverable code so recovery still happens.
#   * any other termination, INCLUDING a clean rc=0 that was not an owner stop,
#     is an UNEXPECTED/unrequested exit; systemd performs bounded recovery
#     (Restart=on-failure, <=3 starts / 15 min, then fails closed).
#   Native scheduled IB Gateway Auto Restart is handled INSIDE IBC (the process
#   does not exit), so it never reaches this contract.
#
# On every (re)start the launcher rewrites the broker-session token in
# /run/ibgateway so the ExecutionAgent's reconciliation latch invalidates any
# pre-restart CLEAN proof: no new entries until a fresh reconciliation.
#
set -euo pipefail

# Sentinel exit code for every KNOWN fail-closed condition. It MUST match the
# unit's RestartPreventExitStatus= so these never trigger an automatic restart.
readonly EXIT_FAIL_CLOSED=78

IBC_PATH="${IBC_PATH:-/opt/ibc}"
IBC_INI="${IBC_INI:-/home/opc/.config/ibc/config.ini}"
# Runtime dir (tmpfiles.d-owned) that holds cross-restart safety state, incl.
# the broker-session token the ExecutionAgent's reconciliation latch keys on.
RUN_DIR="${IBGATEWAY_RUN_DIR:-/run/ibgateway}"
GATEWAY_SESSION_FILE="${GATEWAY_SESSION_FILE:-${RUN_DIR}/gateway-session}"
# IB Gateway standalone major-version directory (e.g. ~/Jts/ibgateway/1048).
TWS_MAJOR_VRSN="${TWS_MAJOR_VRSN:-1048}"
# IB Gateway installation + settings tree (IBC --tws-path / --tws-settings-path).
TWS_PATH="${TWS_PATH:-/home/opc/Jts}"
# Directory holding the bundled Gateway Java (IBC --java-path). Derived from the
# canonical install tree + major version so there is no second, independently
# pinned Java path to drift: ~/Jts/ibgateway/<vrsn>/jre/bin. IBC otherwise
# probes a stale unpack dir and fails with "Can't find suitable Java".
JAVA_PATH="${JAVA_PATH:-${TWS_PATH}/ibgateway/${TWS_MAJOR_VRSN}/jre/bin}"
LOG_PATH="${IBC_LOG_PATH:-/home/opc/ibc/logs}"

log() { printf '[start-ibc] %s\n' "$*"; }
# Every die is a KNOWN fail-closed condition -> sentinel 78 -> no auto-restart.
die() { printf '[start-ibc] ERROR: %s\n' "$*" >&2; exit "${EXIT_FAIL_CLOSED}"; }

# Baseline byte offsets of every IBC log that already existed BEFORE this
# launcher started IBC. Classification reads ONLY content appended (or newly
# created / rotated) during THIS invocation, so a stale 2FA/auth marker from a
# previous run can never classify the current termination.
declare -A _LOG_BASELINE=()

# capture_log_baseline — record each existing *.log size just before launch.
capture_log_baseline() {
  _LOG_BASELINE=()
  local f size
  shopt -s nullglob
  for f in "${LOG_PATH}"/*.log; do
    size="$(stat -c '%s' "${f}" 2>/dev/null || echo 0)"
    _LOG_BASELINE["${f}"]="${size}"
  done
  shopt -u nullglob
}

# _current_invocation_log_text — concatenate only the log bytes produced during
# THIS invocation: for a pre-existing file, the bytes after its baseline offset;
# for a file created or rotated-in during this run, the whole file (baseline 0);
# for an in-place truncation (now shorter than baseline), the whole current
# file. Read-only; never printed.
_current_invocation_log_text() {
  local f base size
  shopt -s nullglob
  for f in "${LOG_PATH}"/*.log; do
    [ -r "${f}" ] || continue
    base="${_LOG_BASELINE["${f}"]:-0}"
    size="$(stat -c '%s' "${f}" 2>/dev/null || echo 0)"
    if [ "${size}" -lt "${base}" ]; then
      base=0  # truncated/rotated in place -> the whole current file is new
    fi
    if [ "${size}" -gt "${base}" ]; then
      tail -c "+$((base + 1))" "${f}" 2>/dev/null || true
      printf '\n'
    fi
  done
  shopt -u nullglob
}

# classify_ibc_log — classify ONLY this invocation's IBC log output for a
# TERMINAL fail-closed login condition. Prints FAILCLOSED (2FA timeout or
# authentication failure) or UNKNOWN. The phrases mirror the watchdog's
# parse_login_marker (TWO_FA_TIMEOUT / LOGIN_FAILED) so the two never drift; a
# regression test cross-checks them. Read-only; never prints credentials.
classify_ibc_log() {
  local lowered
  lowered="$(_current_invocation_log_text | tr '[:upper:]' '[:lower:]')"
  if printf '%s' "${lowered}" | grep -q 'second factor authentication' \
    && printf '%s' "${lowered}" | grep -Eq 'timed out|timeout'; then
    echo "FAILCLOSED"
    return 0
  fi
  if printf '%s' "${lowered}" | grep -Eq 'login failed|invalid username or password'; then
    echo "FAILCLOSED"
    return 0
  fi
  echo "UNKNOWN"
}

# recovery_exit_code <stopped> <child_rc> — the single source of truth for the
# exit code that drives systemd's bounded, fail-closed recovery. Echoes the code
# (does not exit) so it is unit-testable:
#   * stopped=1 (owner systemctl stop / trapped TERM/INT)      -> 0   (no restart)
#   * this invocation's log shows a fail-closed login condition -> 78  (no restart)
#   * ANY other unrequested termination, INCLUDING a clean rc=0 -> 1   (bounded
#     recovery) — an Auto Logoff / clean unrequested exit must come back, only an
#     explicit owner stop stays down; the sentinel 78 is never surfaced here.
recovery_exit_code() {
  local stopped="$1" child_rc="$2"
  if [ "${stopped}" -eq 1 ]; then
    echo 0
    return 0
  fi
  if [ "$(classify_ibc_log)" = "FAILCLOSED" ]; then
    echo "${EXIT_FAIL_CLOSED}"
    return 0
  fi
  if [ "${child_rc}" -eq 0 ] || [ "${child_rc}" -eq "${EXIT_FAIL_CLOSED}" ]; then
    echo 1
    return 0
  fi
  echo "${child_rc}"
}

validate_config() {
  [ -f "${IBC_INI}" ] || die "IBC config not found: ${IBC_INI} (see runbook)"

  # Permissions must be exactly 600 and owned by the running user — never
  # world/group readable, because it contains the IB password.
  local perms owner
  perms="$(stat -c '%a' "${IBC_INI}")"
  owner="$(stat -c '%U' "${IBC_INI}")"
  [ "${perms}" = "600" ] || die "config ${IBC_INI} must be chmod 600 (found ${perms})"
  [ "${owner}" = "$(id -un)" ] || die "config ${IBC_INI} must be owned by $(id -un)"

  # PAPER ONLY. Refuse anything that is not an explicit paper trading mode.
  # We grep only for the mode line; the file is never printed.
  grep -Eiq '^[[:space:]]*TradingMode[[:space:]]*=[[:space:]]*paper[[:space:]]*$' \
    "${IBC_INI}" || die "TradingMode must be 'paper' in ${IBC_INI}; refusing"
  if grep -Eiq '^[[:space:]]*TradingMode[[:space:]]*=[[:space:]]*live' "${IBC_INI}"; then
    die "a LIVE TradingMode is present in ${IBC_INI}; refusing (paper only)"
  fi

  # A login id must be present (value not printed); an empty one means the
  # owner has not finished secure setup.
  grep -Eq '^[[:space:]]*IbLoginId[[:space:]]*=[[:space:]]*[^[:space:]]+' \
    "${IBC_INI}" || die "IbLoginId is empty in ${IBC_INI}; complete secure setup"
}

main() {
  [ -d "${IBC_PATH}" ] || die "IBC not installed at ${IBC_PATH} (run install_ibc_gateway.sh)"

  # Run the REAL foreground IBC service script, scripts/ibcstart.sh. IBC's
  # gatewaystart.sh stays in the foreground ONLY when its first argument is
  # "-inline"; otherwise it spawns an xterm in the background and returns, so
  # under systemd Type=simple the unit's main process would exit at once and
  # the service would report "Deactivated successfully." while nothing runs.
  local entry="${IBC_PATH}/scripts/ibcstart.sh"
  [ -x "${entry}" ] || die "IBC entrypoint not found/executable: ${entry}"

  # Fail closed BEFORE launching IBC if the bundled Gateway Java is missing —
  # otherwise IBC probes a stale unpack directory and dies with "Can't find
  # suitable Java installation", leaving the unit failed.
  [ -x "${JAVA_PATH}/java" ] || \
    die "Gateway Java not found/executable: ${JAVA_PATH}/java"

  validate_config
  mkdir -p "${LOG_PATH}"
  log "starting IB Gateway (PAPER) via IBC ${TWS_MAJOR_VRSN}; credentials read"
  log "from ${IBC_INI} (chmod 600); never shown, never on the command line."
  log "Gateway Java: ${JAVA_PATH}/java"

  # Snapshot the logs NOW so fail-closed classification sees only this run.
  capture_log_baseline

  # Rewrite the broker-session token: a NEW session invalidates any pre-restart
  # account-safety CLEAN, so the ExecutionAgent's reconciliation latch blocks
  # new entries until a fresh CLEAN for this session. Best-effort: only if the
  # tmpfiles.d-owned runtime dir exists (it is never created here).
  if [ -d "${RUN_DIR}" ]; then
    printf 'gw-%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$$" \
      > "${GATEWAY_SESSION_FILE}" 2>/dev/null \
      && log "broker session: ${GATEWAY_SESSION_FILE} (reconciliation required)" \
      || log "WARN: could not write ${GATEWAY_SESSION_FILE}"
  fi

  # Run IBC in the FOREGROUND as a child (deliberately NOT exec) so that when it
  # ends we can classify HOW it ended and choose an exit code that drives
  # systemd's bounded, fail-closed recovery. Credentials come from the config
  # (--user/--pw are never passed). --on2fatimeout=exit (NOT restart): on a 2FA
  # timeout IBC exits rather than self-restarting; blind recovery is forbidden.
  local stopped=0 child_pid child_rc
  stop_child() {
    stopped=1
    [ -n "${child_pid:-}" ] && kill -TERM "${child_pid}" 2>/dev/null || true
  }
  trap stop_child TERM INT

  set +e
  "${entry}" "${TWS_MAJOR_VRSN}" \
    --gateway \
    "--tws-path=${TWS_PATH}" \
    "--tws-settings-path=${TWS_PATH}" \
    "--ibc-path=${IBC_PATH}" \
    "--ibc-ini=${IBC_INI}" \
    "--java-path=${JAVA_PATH}" \
    --mode=paper \
    --on2fatimeout=exit &
  child_pid=$!
  wait "${child_pid}"
  child_rc=$?
  set -e

  local code
  code="$(recovery_exit_code "${stopped}" "${child_rc}")"
  if [ "${code}" -eq 0 ]; then
    log "owner stop; Gateway shut down — no automatic restart"
  elif [ "${code}" -eq "${EXIT_FAIL_CLOSED}" ]; then
    log "fail-closed login condition (2FA timeout / auth failure); not restarting"
  else
    log "Gateway terminated unrequested (rc=${code}); systemd bounded recovery will restart"
  fi
  exit "${code}"
}

# Only run when EXECUTED, not when SOURCED (tests source this file to exercise
# classify_ibc_log / the recovery decision without launching IBC).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  main "$@"
fi
