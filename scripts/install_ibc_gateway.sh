#!/usr/bin/env bash
#
# install_ibc_gateway.sh — install the OFFICIAL IbcAlpha/IBC release used to
# auto-start IB Gateway (Paper) on Oracle Linux ARM64.
#
# IBC is pure Java, so the same Linux release runs on ARM64 through the JRE
# that ships with IB Gateway; it drives any current Gateway version (10.48
# included). This script ONLY installs IBC. It never handles credentials,
# never logs in, never starts a session and never contacts IBKR. Run it
# manually on the host, as the owner.
#
# Safety: pinned version + pinned SHA-256 (from the GitHub release asset
# digest) are verified before anything is extracted; a mismatch aborts with
# no install. Nothing secret is read or printed.
#
set -euo pipefail

# --- pinned, verified release -------------------------------------------------
# Version and digest recorded from the official GitHub release
# https://github.com/IbcAlpha/IBC/releases/tag/3.24.1 (asset digest via the
# GitHub API). Update BOTH together when bumping the version.
readonly IBC_VERSION="3.24.1"
readonly IBC_ASSET="IBCLinux-${IBC_VERSION}.zip"
readonly IBC_SHA256="d99ee28cc3539e3843fb00d28dc484c255006b0063d38f0808ac3ef07dd88bb8"
readonly IBC_URL="https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/${IBC_ASSET}"
readonly IBC_ASSET_SIZE_BYTES="283529"

# --- configurable install location (no secrets) ------------------------------
IBC_DEST="${IBC_DEST:-/opt/ibc}"

log() { printf '[install-ibc] %s\n' "$*"; }
die() { printf '[install-ibc] ERROR: %s\n' "$*" >&2; exit 1; }

require_tool() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

main() {
  require_tool curl
  require_tool unzip
  require_tool sha256sum

  log "IBC version : ${IBC_VERSION}"
  log "IBC asset   : ${IBC_ASSET}"
  log "expected sha: ${IBC_SHA256}"
  log "destination : ${IBC_DEST}"

  local workdir
  workdir="$(mktemp -d)"
  # shellcheck disable=SC2064  # expand workdir now for the cleanup trap
  trap "rm -rf '${workdir}'" EXIT

  local archive="${workdir}/${IBC_ASSET}"
  log "downloading official release (no credentials involved)…"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "${archive}" "${IBC_URL}"

  log "verifying SHA-256 before extracting…"
  printf '%s  %s\n' "${IBC_SHA256}" "${archive}" | sha256sum --check --status \
    || die "checksum mismatch — refusing to install (possible tampering)"
  log "checksum OK (${IBC_ASSET_SIZE_BYTES} bytes expected)"

  log "installing to ${IBC_DEST}…"
  mkdir -p "${IBC_DEST}"
  unzip -o -q "${archive}" -d "${IBC_DEST}"

  # Make the IBC shell entrypoints executable; there are no order tools here.
  find "${IBC_DEST}" -maxdepth 2 -name '*.sh' -exec chmod +x {} +

  # Record the provenance next to the install (version + checksum only).
  printf 'version=%s\nasset=%s\nsha256=%s\nsize_bytes=%s\nsource=%s\n' \
    "${IBC_VERSION}" "${IBC_ASSET}" "${IBC_SHA256}" "${IBC_ASSET_SIZE_BYTES}" \
    "${IBC_URL}" > "${IBC_DEST}/INSTALLED_VERSION.txt"

  log "done. IBC ${IBC_VERSION} installed at ${IBC_DEST}."
  log "next: create the secure IBC config (see docs/runbooks/IB_GATEWAY_AUTO_LOGIN.md);"
  log "this script never wrote credentials and never contacted IBKR."
}

main "$@"
