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
set -euo pipefail

IBC_PATH="${IBC_PATH:-/opt/ibc}"
IBC_INI="${IBC_INI:-/home/opc/.config/ibc/config.ini}"
# IB Gateway standalone major-version directory (e.g. ~/Jts/ibgateway/1048).
TWS_MAJOR_VRSN="${TWS_MAJOR_VRSN:-1048}"
LOG_PATH="${IBC_LOG_PATH:-/home/opc/ibc/logs}"

log() { printf '[start-ibc] %s\n' "$*"; }
die() { printf '[start-ibc] ERROR: %s\n' "$*" >&2; exit 1; }

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
  local entry="${IBC_PATH}/gatewaystart.sh"
  [ -x "${entry}" ] || die "IBC entrypoint not found/executable: ${entry}"

  validate_config
  mkdir -p "${LOG_PATH}"
  log "starting IB Gateway (PAPER) via IBC ${TWS_MAJOR_VRSN}; credentials read"
  log "from ${IBC_INI} (chmod 600); never shown, never on the command line."

  # Credentials come from the config (--user/--pw are deliberately NOT passed).
  # exec so systemd tracks the real process; IBC handles login + 2FA itself.
  exec "${entry}" "${TWS_MAJOR_VRSN}" \
    --gateway \
    --mode=paper \
    "--config=${IBC_INI}"
}

main "$@"
