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
#   scripts/install_ops_assets.sh                      # CHECK (default)
#   scripts/install_ops_assets.sh --apply              # real install
#   scripts/install_ops_assets.sh --apply --no-reload  # skip reload only
#   scripts/install_ops_assets.sh --rehearsal          # REHEARSAL
#
# REHEARSAL is STRUCTURALLY isolated in two independent ways.
#
# 1. It never invokes systemctl or systemd-tmpfiles. Staging the tmpfiles
#    config elsewhere would NOT isolate it — the rule names the ABSOLUTE path
#    /run/ibgateway, so `systemd-tmpfiles --create <scratch>/...` would create
#    and chown the REAL directory. A rehearsal that mutates the thing it is
#    rehearsing is not a rehearsal.
#
# 2. It writes ONLY beneath a private directory that this script creates for
#    itself with `mktemp -d`, 0700, owned by the caller. There is no
#    caller-supplied destination to get wrong: absence of privileged COMMANDS
#    is not containment, because `install -D`, `mkdir -p` and `chmod` are by
#    themselves enough to overwrite a host given a bad root.
#
# The rehearsal directory is PRINTED and deliberately NOT deleted, so the
# owner can inspect exactly what a real apply would have produced.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/systemd"

# -- helpers (defined before any side effect, so they can be unit-tested) -----

fail() {
  echo "ERROR: $*" >&2
  echo ">>> INSTALL INCOMPLETE. Do NOT enable the timer." >&2
  exit 1
}

# /etc/tmpfiles.d/ibgateway.conf is the SINGLE canonical owner of
# /run/ibgateway, so it is also the single source of truth for that
# directory's path, mode and ownership. The installer reads them from the
# config rather than restating them, so the two can never drift apart.
read_tmpfiles_rule() {
  awk '$1 == "d" && NF >= 5 { print $2, $3, $4, $5; exit }' "$1"
}

validate_tmpfiles_rule() {
  local path="$1" mode="$2" user="$3" group="$4"
  [[ "${path}" == "/run/ibgateway" ]] \
    || fail "tmpfiles rule names an unexpected path: ${path}"
  [[ "${mode}" == "0700" ]] \
    || fail "the runtime directory must be 0700 (it holds the inhibit marker); config says ${mode}"
  [[ "${user}" == "opc" && "${group}" == "opc" ]] \
    || fail "the runtime directory must be owned by opc:opc; config says ${user}:${group}"
}

# The privileged tmpfiles application. Isolated in a function so its failure
# semantics can be tested without a privileged install.
apply_tmpfiles_real() {
  local conf="$1" path="$2"
  systemd-tmpfiles --create "${conf}" \
    || fail "systemd-tmpfiles --create failed; ${path} may be missing or wrongly permissioned"
}

# Existence, type, mode and ownership. A directory that is missing, wrongly
# permissioned or owned by someone else is an INCOMPLETE install: it holds the
# inhibit marker, the restart budget and the lock.
verify_runtime_dir() {
  local dir="$1" want_mode="$2" want_user="$3" want_group="$4"
  [[ -e "${dir}" ]] || fail "runtime directory does not exist: ${dir}"
  [[ -d "${dir}" ]] || fail "runtime path exists but is not a directory: ${dir}"
  local mode user group
  mode="$(stat -c '%a' "${dir}")" || fail "cannot stat ${dir}"
  user="$(stat -c '%U' "${dir}")" || fail "cannot stat owner of ${dir}"
  group="$(stat -c '%G' "${dir}")" || fail "cannot stat group of ${dir}"
  [[ "${mode}" == "${want_mode}" || "0${mode}" == "${want_mode}" ]] \
    || fail "runtime directory ${dir} has mode ${mode}, expected ${want_mode}"
  [[ "${user}" == "${want_user}" ]] \
    || fail "runtime directory ${dir} is owned by ${user}, expected ${want_user}"
  [[ "${group}" == "${want_group}" ]] \
    || fail "runtime directory ${dir} has group ${group}, expected ${want_group}"
}

# -- rehearsal containment ----------------------------------------------------
#
# A rehearsal that avoids systemctl and systemd-tmpfiles is still not isolated:
# it runs `install -D`, `mkdir -p` and `chmod`. With an unconstrained
# destination root those three are enough to overwrite the real host —
# `--dest-root=/` would write straight into /etc/systemd/system and chmod the
# real /run/ibgateway. Absence of privileged COMMANDS is not containment; the
# destination has to be constrained too.

#: Written into a directory this installer created as a rehearsal root. We
#: refuse to write into an existing directory that does not carry it, so a
#: mistyped root can never turn into "install into someone else's directory".
REHEARSAL_MARKER=".ops-rehearsal-root"

#: Trees a rehearsal must never resolve into, however it was spelled.
SYSTEM_TREES=(
  /etc /run /usr /bin /sbin /boot /dev /proc /sys /lib /lib64 /var/lib /root /home
)

absolute_path() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s' "${PWD}/$1" ;;
  esac
}

canonicalize() {
  realpath -m -- "$1" 2>/dev/null
}

# Checked on the SUPPLIED path, before canonicalization: realpath resolves
# symlinks silently and would hand back an innocent-looking result for
# `--dest-root=/tmp/link` where link -> /.
reject_symlinked_components() {
  local path="$1" current="" part
  local -a parts=()
  IFS='/' read -ra parts <<<"${path#/}"
  for part in ${parts[@]+"${parts[@]}"}; do
    [[ -n "${part}" ]] || continue
    current="${current}/${part}"
    if [[ -L "${current}" ]]; then
      fail "rehearsal root path component is a symlink: ${current}"
    fi
  done
}

# A symlink anywhere inside the tree is a write-through into whatever it
# points at (`etc -> /etc`, `run -> /run`). Rejected before ANY write.
reject_symlinks_inside() {
  local root="$1" found
  found="$(find "${root}" -type l -print 2>/dev/null | head -1 || true)"
  [[ -z "${found}" ]] || fail "symlink inside the rehearsal root: ${found}"
}

require_private_dir() {
  local dir="$1" mode owner me
  mode="$(stat -c '%a' "${dir}")" || fail "cannot stat ${dir}"
  owner="$(stat -c '%U' "${dir}")" || fail "cannot stat owner of ${dir}"
  [[ "${mode}" == "700" || "${mode}" == "0700" ]] \
    || fail "rehearsal root ${dir} must be 0700, found ${mode}"
  me="$(id -un 2>/dev/null || id -u)"
  [[ "${owner}" == "${me}" ]] \
    || fail "rehearsal root ${dir} must be owned by ${me}, found ${owner}"
}

# Sets REHEARSAL_ROOT. Deliberately NOT a command substitution: `fail` must
# exit the script, and inside $(...) it would only exit a subshell.
validate_rehearsal_root() {
  local supplied="$1" absolute canonical tree
  [[ -n "${supplied}" ]] || fail "rehearsal root must not be empty"
  absolute="$(absolute_path "${supplied}")"
  reject_symlinked_components "${absolute}"

  canonical="$(canonicalize "${absolute}")" \
    || fail "cannot canonicalize rehearsal root: ${supplied}"
  [[ -n "${canonical}" ]] || fail "rehearsal root canonicalizes to nothing: ${supplied}"
  # Traversal is handled here: whatever ../.. spelled, the canonical form is
  # what every containment check below is measured against.
  [[ "${canonical}" != "/" ]] || fail "rehearsal root must not be /"
  for tree in "${SYSTEM_TREES[@]}"; do
    if [[ "${canonical}" == "${tree}" || "${canonical}" == "${tree}"/* ]]; then
      fail "rehearsal root must not be inside a system tree: ${canonical}"
    fi
  done

  if [[ -e "${canonical}" ]]; then
    [[ -d "${canonical}" ]] \
      || fail "rehearsal root exists and is not a directory: ${canonical}"
    # Structure first, ownership second: a hostile tree is rejected even on a
    # filesystem that cannot represent 0700.
    reject_symlinks_inside "${canonical}"
    [[ -f "${canonical}/${REHEARSAL_MARKER}" ]] \
      || fail "refusing to write into an existing directory with no ${REHEARSAL_MARKER}: ${canonical}"
    require_private_dir "${canonical}"
  else
    mkdir -p -- "${canonical}"
    chmod 0700 -- "${canonical}"
    printf 'created by scripts/install_ops_assets.sh\n' \
      > "${canonical}/${REHEARSAL_MARKER}"
    require_private_dir "${canonical}"
  fi
  REHEARSAL_ROOT="${canonical}"
}

# The owner-facing mode: the installer picks its own private directory, so
# there is no root to get wrong. Never deleted — the point is to inspect it.
create_private_rehearsal_root() {
  local root canonical
  root="$(mktemp -d "${TMPDIR:-/tmp}/ops-rehearsal-XXXXXXXX")" \
    || fail "could not create a private rehearsal directory"
  [[ ! -L "${root}" ]] || fail "mktemp returned a symlink: ${root}"
  chmod 0700 -- "${root}"
  canonical="$(canonicalize "${root}")" || fail "cannot canonicalize ${root}"
  [[ "${canonical}" != "/" ]] || fail "refusing to rehearse into /"
  if [[ -n "$(ls -A -- "${canonical}" 2>/dev/null || true)" ]]; then
    fail "a fresh rehearsal root must be empty: ${canonical}"
  fi
  printf 'created by scripts/install_ops_assets.sh\n' \
    > "${canonical}/${REHEARSAL_MARKER}"
  require_private_dir "${canonical}"
  REHEARSAL_ROOT="${canonical}"
}

# Belt and braces for every single write: resolve the destination and prove it
# is still under the root. This catches a symlink created AFTER validation,
# which the scan above cannot.
assert_within_root() {
  local target="$1" canonical
  [[ "${REHEARSAL:-0}" -eq 1 ]] || return 0
  canonical="$(canonicalize "${target}")" || fail "cannot canonicalize ${target}"
  case "${canonical}" in
    "${REHEARSAL_ROOT}" | "${REHEARSAL_ROOT}"/*) ;;
    *) fail "destination escapes the rehearsal root: ${target} -> ${canonical}" ;;
  esac
}

# Test seam: source this file with OPS_INSTALL_LIB_ONLY=1 to get the helpers
# above without running an install. Nothing below this line has executed yet,
# so sourcing is side-effect free. It changes no behaviour of a normal run.
if [[ -n "${OPS_INSTALL_LIB_ONLY:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

# -- arguments ----------------------------------------------------------------

APPLY=0
RELOAD=1
REHEARSAL=0
REHEARSAL_ROOT=""
# Prefix for every destination path. Empty means the real system paths.
DEST_ROOT=""
SUPPLIED_ROOT=""
#: Tracked separately from the value: `--dest-root=` with an EMPTY value must
#: be rejected, not silently upgraded into a private root.
DEST_ROOT_GIVEN=0
for arg in "$@"; do
  case "${arg}" in
    --apply) APPLY=1 ;;
    --no-reload) RELOAD=0 ;;
    --rehearsal) REHEARSAL=1; APPLY=1 ;;
    --dest-root=*)
      # NOT a public interface. A caller-supplied destination is the whole
      # attack surface fixed here, so it exists only for deterministic tests.
      # Owners use --rehearsal, where the installer picks the directory.
      if [[ "${OPS_INSTALL_TEST_MODE:-}" != "1" ]]; then
        echo "--dest-root is a test-only interface; use --rehearsal" >&2
        exit 2
      fi
      SUPPLIED_ROOT="${arg#*=}"
      DEST_ROOT_GIVEN=1
      REHEARSAL=1
      ;;
    -h|--help) sed -n '2,33p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

# Establish and validate the rehearsal root BEFORE anything is written.
if [[ "${REHEARSAL}" -eq 1 ]]; then
  if [[ "${DEST_ROOT_GIVEN}" -eq 1 ]]; then
    validate_rehearsal_root "${SUPPLIED_ROOT}"
  else
    create_private_rehearsal_root
  fi
  DEST_ROOT="${REHEARSAL_ROOT}"
  echo ">>> REHEARSAL ROOT: ${REHEARSAL_ROOT}"
  echo ">>> It is kept for inspection and is NOT deleted on exit."
fi

# Reported state, so the final summary can never overstate what happened.
STATE_RELOAD="not attempted"
STATE_TMPFILES="not attempted"
STATE_RUNTIME_DIR="not attempted"

# src::dst::mode::kind   (kind: unit => a daemon-reload is meaningful)
ASSETS=(
  "ibgateway-ibc.service::/etc/systemd/system/ibgateway-ibc.service::0644::unit"
  "ibgateway-watchdog.service::/etc/systemd/system/ibgateway-watchdog.service::0644::unit"
  "ibgateway-watchdog.timer::/etc/systemd/system/ibgateway-watchdog.timer::0644::unit"
  "ibgateway-tmpfiles.conf::/etc/tmpfiles.d/ibgateway.conf::0644::tmpfiles"
  "ibgateway-logrotate.conf::/etc/logrotate.d/ibgateway::0644::logrotate"
)

TMPFILES_CONF="${DEST_ROOT}/etc/tmpfiles.d/ibgateway.conf"

# -- phase 1: stage the files -------------------------------------------------

if [[ "${APPLY}" -eq 1 ]]; then
  if [[ "${REHEARSAL}" -eq 1 ]]; then
    echo ">>> REHEARSAL MODE — everything is confined to ${DEST_ROOT}"
    echo ">>> The real /etc, /run and systemd are NOT touched."
  else
    echo ">>> APPLY MODE — files will be written."
  fi
else
  echo ">>> CHECK / DRY-RUN MODE — nothing will be changed."
  echo ">>> Re-run with --apply to write these files."
fi
echo

changed=0
units_changed=0
for entry in "${ASSETS[@]}"; do
  src="${SYSTEMD_SRC}/${entry%%::*}"
  rest="${entry#*::}"
  dst="${DEST_ROOT}${rest%%::*}"
  rest="${rest#*::}"
  mode="${rest%%::*}"
  kind="${rest#*::}"

  if [[ ! -f "${src}" ]]; then
    echo "MISSING SOURCE: ${src}" >&2
    exit 1
  fi

  # Before we even READ the destination: prove it resolves inside the root.
  assert_within_root "${dst}"

  if [[ -f "${dst}" ]] && diff -q "${src}" "${dst}" >/dev/null 2>&1; then
    echo "unchanged : ${dst}"
    continue
  fi

  changed=$((changed + 1))
  if [[ "${kind}" == "unit" ]]; then
    units_changed=$((units_changed + 1))
  fi
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
if [[ "${APPLY}" -eq 0 ]]; then
  # CHECK mode is READ-ONLY: no system command runs, nothing is created.
  echo ">>> ${changed} file(s) would change. Nothing was written."
  echo
  echo "A real --apply would additionally:"
  echo "  * systemctl daemon-reload            (only if a unit file changed)"
  echo "  * systemd-tmpfiles --create /etc/tmpfiles.d/ibgateway.conf"
  echo "  * verify /run/ibgateway is a directory, 0700, owned opc:opc"
  echo
  cat <<'CHECKNEXT'
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
CHECKNEXT
  exit 0
fi

# -- phase 2: daemon-reload (unit files only, never in rehearsal) -------------
#
# The decision is computed independently of rehearsal and then reported, so a
# rehearsal still shows what a real apply WOULD do. Otherwise the only way to
# observe this logic would be to run a privileged install.
#
# A daemon-reload matters only for UNIT files: a logrotate or tmpfiles change
# does not make systemd's view of the units stale.
if [[ "${RELOAD}" -eq 0 ]]; then
  reload_decision="SKIP (--no-reload)"
elif [[ "${units_changed}" -eq 0 ]]; then
  reload_decision="SKIP (no unit file changed)"
else
  reload_decision="RELOAD (${units_changed} unit file(s) changed)"
fi

if [[ "${REHEARSAL}" -eq 1 ]]; then
  STATE_RELOAD="SKIPPED (rehearsal — the real systemd is never contacted)"
  echo "daemon reload  : ${STATE_RELOAD}"
  echo "                 a real apply would: ${reload_decision}"
elif [[ "${reload_decision}" == SKIP* ]]; then
  STATE_RELOAD="SKIPPED ${reload_decision#SKIP }"
  echo "daemon reload  : ${STATE_RELOAD}"
else
  systemctl daemon-reload || fail "systemctl daemon-reload failed"
  STATE_RELOAD="DONE (${units_changed} unit file(s) changed)"
  echo "daemon reload  : ${STATE_RELOAD}"
fi

# -- phase 3: the runtime directory -------------------------------------------
#
# MANDATORY on every apply, independently of whether any file changed. The
# previous version nested this under "reload enabled AND changed > 0", so an
# apply where every asset already matched — or an apply with --no-reload —
# reported success while /run/ibgateway could be missing entirely. A tmpfiles
# CONFIG on disk is not a runtime DIRECTORY on the filesystem: /run is
# volatile, so the directory can vanish on reboot while the config still looks
# perfectly installed.

[[ -f "${TMPFILES_CONF}" ]] || fail "tmpfiles config not found: ${TMPFILES_CONF}"
rule="$(read_tmpfiles_rule "${TMPFILES_CONF}")"
[[ -n "${rule}" ]] || fail "no 'd' rule found in ${TMPFILES_CONF}"
read -r rule_path rule_mode rule_user rule_group <<<"${rule}"
validate_tmpfiles_rule "${rule_path}" "${rule_mode}" "${rule_user}" "${rule_group}"

runtime_dir="${DEST_ROOT}${rule_path}"

if [[ "${REHEARSAL}" -eq 1 ]]; then
  # systemd-tmpfiles IS DELIBERATELY NOT INVOKED HERE — see the header.
  # Instead: validate the staged rule (above) and materialise the directory
  # inside the scratch root, then verify it exactly as a real apply would.
  STATE_TMPFILES="VALIDATED (rehearsal — systemd-tmpfiles NOT invoked)"
  echo "tmpfiles config: ${STATE_TMPFILES}"
  # `run -> /run` inside the root would make this mkdir/chmod hit the real
  # /run/ibgateway, so the destination is re-resolved immediately before use.
  assert_within_root "${runtime_dir}"
  mkdir -p "${runtime_dir}"
  assert_within_root "${runtime_dir}"
  chmod "${rule_mode}" "${runtime_dir}"
  # A non-privileged rehearsal cannot chown to opc:opc, so ownership is
  # verified against the invoking user and the REAL owner check is explicitly
  # deferred. Rehearsal must never be mistaken for proof of host ownership.
  # Numeric fallback: a uid with no passwd entry must not abort the rehearsal.
  want_user="$(id -un 2>/dev/null || id -u)"
  want_group="$(id -gn 2>/dev/null || id -g)"
  echo "                 ownership ${rule_user}:${rule_group} is NOT established by a"
  echo "                 rehearsal; only a privileged apply can do that."
else
  apply_tmpfiles_real "${TMPFILES_CONF}" "${rule_path}"
  STATE_TMPFILES="APPLIED"
  echo "tmpfiles config: ${STATE_TMPFILES}"
  want_user="${rule_user}"
  want_group="${rule_group}"
fi

verify_runtime_dir "${runtime_dir}" "${rule_mode}" "${want_user}" "${want_group}"
STATE_RUNTIME_DIR="VERIFIED ${runtime_dir} (mode ${rule_mode}, owner ${want_user}:${want_group})"
echo "runtime dir    : ${STATE_RUNTIME_DIR}"

# -- summary ------------------------------------------------------------------

echo
echo "SUMMARY"
echo "  files copied      : ${changed}"
echo "  daemon reload     : ${STATE_RELOAD}"
echo "  tmpfiles config   : ${STATE_TMPFILES}"
echo "  runtime directory : ${STATE_RUNTIME_DIR}"
if [[ "${REHEARSAL}" -eq 1 ]]; then
  echo "  install           : REHEARSAL COMPLETE — the real host is unchanged"
  echo
  cat <<'REHEARSALNEXT'
This was a REHEARSAL. Nothing was installed on the host, no systemd command
ran, and the real /run/ibgateway was neither created nor inspected. Do NOT
enable any timer on the basis of this run — re-run without --dest-root, as
the service user, to perform the real install.
REHEARSALNEXT
  exit 0
fi

echo "  install           : COMPLETE — NOTHING was enabled or started"
echo
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
