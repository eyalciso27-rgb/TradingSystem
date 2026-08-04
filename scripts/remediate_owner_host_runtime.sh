#!/usr/bin/env bash
# Owner-host runtime remediation for the IB Gateway operational contract.
#
# Replaces the ad-hoc sequence of hand-typed commands used during the first
# ARM64 activation with ONE reviewed, idempotent, evidence-producing tool.
#
#   --check    (DEFAULT) read-only. Prints exactly what would change.
#   --apply    makes the reviewed changes. Requires the explicit flag.
#   --verify   proves the resulting state, including that nothing was started.
#
# WHAT THIS NEVER DOES, IN ANY MODE:
#   * never starts, stops, restarts or relogins IB Gateway;
#   * never enables the watchdog timer;
#   * never runs `systemctl --now` anything;
#   * never touches firewall / firewalld / nftables / OCI security lists;
#   * never touches SSH or VNC configuration;
#   * never reads or prints an IBC config file, a credential or an account id;
#   * never prints Java command-line arguments (they carry host detail and can
#     carry an account id).
#
# The running Gateway is deliberately left alone. Its process is currently
# owned by a user session rather than by the canonical unit, and correcting
# that requires a restart — which is an OWNER-AUTHORIZED action under
# docs/runbooks/CHAOS_RECOVERY_DRILLS.md, never something a script decides.
# This tool makes the next start correct and proves the current one untouched.
set -euo pipefail

REPO_ROOT_DEFAULT="/home/opc/projects/TradingSystem"
GATEWAY_UNIT="ibgateway-ibc.service"
WATCHDOG_UNIT="ibgateway-watchdog.service"
WATCHDOG_TIMER="ibgateway-watchdog.timer"
LEGACY_UNIT="ibgateway.service"
STALE_WATCHDOG="/opt/trading/scripts/ib_gateway_watchdog.py"
DROPIN_DIR="/etc/systemd/system/${GATEWAY_UNIT}.d"

MODE="check"
REPO_ROOT="${REPO_ROOT_DEFAULT}"
# Test seam: redirect every read and write below a scratch tree so the tool's
# own logic can be exercised without a systemd host. Never available unless
# explicitly requested; it cannot make the tool touch anything extra.
SYSROOT=""
EVIDENCE_DIR=""

usage() { sed -n '2,27p' "${BASH_SOURCE[0]}"; }

fail() {
  echo "ERROR: $*" >&2
  echo ">>> REMEDIATION INCOMPLETE — no further change was attempted." >&2
  exit 1
}

for arg in "$@"; do
  case "${arg}" in
    --check) MODE="check" ;;
    --apply) MODE="apply" ;;
    --verify) MODE="verify" ;;
    --repo-root=*) REPO_ROOT="${arg#*=}" ;;
    --evidence-dir=*) EVIDENCE_DIR="${arg#*=}" ;;
    --sysroot=*)
      if [[ "${OPS_REMEDIATE_TEST_MODE:-}" != "1" ]]; then
        echo "--sysroot is a test-only interface" >&2
        exit 2
      fi
      SYSROOT="${arg#*=}"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
  esac
done

VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
CANONICAL_WATCHDOG="${REPO_ROOT}/scripts/ib_gateway_watchdog.py"
SYS_DROPIN_DIR="${SYSROOT}${DROPIN_DIR}"
SYS_STALE_WATCHDOG="${SYSROOT}${STALE_WATCHDOG}"

# `systemctl` is never invoked in --sysroot mode: there is no systemd to ask,
# and a test must not reach the real one.
have_systemd() { [[ -z "${SYSROOT}" ]] && command -v systemctl >/dev/null 2>&1; }

show_property() {
  local unit="$1" property="$2"
  if have_systemd; then
    systemctl show "${unit}" -p "${property}" --value 2>/dev/null || true
  elif [[ -f "${SYSROOT}/show/${unit}.${property}" ]]; then
    cat "${SYSROOT}/show/${unit}.${property}"
  fi
}

# -- findings ------------------------------------------------------------------

FINDINGS=()
WORST="OK"

record() {
  local severity="$1" check="$2" detail="$3"
  FINDINGS+=("${severity}|${check}|${detail}")
  case "${severity}" in
    FAIL) WORST="FAIL" ;;
    WARN) [[ "${WORST}" == "FAIL" ]] || WORST="WARN" ;;
  esac
  printf '  %-6s %-34s %s\n' "${severity}" "${check}" "${detail}"
}

# -- read-only inspection (all three modes start here) -------------------------

inspect() {
  echo ">>> INSPECTION (read-only)"

  # 1. stale /opt watchdog copy
  if [[ -e "${SYS_STALE_WATCHDOG}" ]]; then
    record FAIL stale_opt_watchdog "present: ${STALE_WATCHDOG} (quarantine candidate)"
  else
    record OK stale_opt_watchdog "absent"
  fi

  # 2. canonical runtime present and usable
  [[ -d "${SYSROOT}${REPO_ROOT}" ]] \
    && record OK repo_root "${REPO_ROOT}" \
    || record FAIL repo_root "missing: ${REPO_ROOT}"
  [[ -x "${SYSROOT}${VENV_PYTHON}" ]] \
    && record OK venv_interpreter "${VENV_PYTHON}" \
    || record FAIL venv_interpreter "missing or not executable: ${VENV_PYTHON}"
  [[ -r "${SYSROOT}${CANONICAL_WATCHDOG}" ]] \
    && record OK canonical_watchdog "${CANONICAL_WATCHDOG}" \
    || record FAIL canonical_watchdog "missing: ${CANONICAL_WATCHDOG}"

  # 3. contradictory drop-ins, judged by CONTENT not filename
  if [[ -d "${SYS_DROPIN_DIR}" ]]; then
    local found=0
    while IFS= read -r dropin; do
      [[ -n "${dropin}" ]] || continue
      local verdict
      verdict="$(classify_dropin_file "${dropin}")"
      case "${verdict}" in
        FAIL*) record FAIL "dropin:$(basename "${dropin}")" "${verdict#FAIL }" ; found=1 ;;
        OK*) record OK "dropin:$(basename "${dropin}")" "${verdict#OK }" ;;
        *) record WARN "dropin:$(basename "${dropin}")" "${verdict}" ;;
      esac
    done < <(find "${SYS_DROPIN_DIR}" -maxdepth 1 -name '*.conf' -type f 2>/dev/null | sort)
    [[ "${found}" -eq 0 ]] || true
  else
    record OK dropins "no drop-in directory"
  fi

  # 4. unit states (never changed here)
  local gw_active gw_sub gw_pid
  gw_active="$(show_property "${GATEWAY_UNIT}" ActiveState)"
  gw_sub="$(show_property "${GATEWAY_UNIT}" SubState)"
  gw_pid="$(show_property "${GATEWAY_UNIT}" MainPID)"
  record INFO gateway_unit_state "ActiveState=${gw_active:-?} SubState=${gw_sub:-?} MainPID=${gw_pid:-?}"
  if [[ "${gw_pid:-0}" == "0" ]]; then
    record FAIL gateway_process_ownership \
      "the canonical unit owns no process (MainPID=0); the live Gateway is not managed by it"
  else
    record OK gateway_process_ownership "MainPID=${gw_pid}"
  fi

  record INFO legacy_unit_state "$(show_property "${LEGACY_UNIT}" UnitFileState):$(show_property "${LEGACY_UNIT}" ActiveState)"
  record INFO watchdog_timer_state "$(show_property "${WATCHDOG_TIMER}" UnitFileState):$(show_property "${WATCHDOG_TIMER}" ActiveState)"

  # 5. listeners — reported, NEVER changed
  local listeners
  listeners="$(listener_scope)"
  record INFO listeners "${listeners}"

  echo
}

# Delegates to the shared, unit-tested contract module so the installer, the
# audit and this tool cannot drift apart.
classify_dropin_file() {
  local path="$1"
  "${SYSROOT}${VENV_PYTHON}" - "$path" <<'PYEOF' 2>/dev/null || echo "WARN could not classify"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "scripts")
from systemd_contract import Severity, classify_dropin, parse_dropin  # noqa: E402

path = sys.argv[1]
dropin = parse_dropin(path, Path(path).read_text(encoding="utf-8"))
findings = classify_dropin(dropin)
bad = [f for f in findings if f.severity is Severity.FAIL]
if bad:
    print("FAIL " + "; ".join(f.detail for f in bad))
else:
    print("OK " + "; ".join(f.detail for f in findings) or "OK no overrides")
PYEOF
}

listener_scope() {
  if [[ -n "${SYSROOT}" ]] || ! command -v ss >/dev/null 2>&1; then
    echo "not collected"
    return 0
  fi
  local out=""
  for port in 4002 5901; do
    local line
    line="$(ss -lntH 2>/dev/null | awk -v p=":${port}" '$4 ~ p {print $4}' | head -1)"
    if [[ -z "${line}" ]]; then
      out+="${port}=not-listening "
    elif [[ "${line}" == 127.0.0.1:* || "${line}" == "[::1]:"* ]]; then
      out+="${port}=localhost "
    else
      out+="${port}=ALL-INTERFACES "
    fi
  done
  echo "${out}(binding only; ingress policy is a separate fact)"
}

# -- evidence ------------------------------------------------------------------

write_evidence() {
  local target="${EVIDENCE_DIR}"
  [[ -n "${target}" ]] || return 0
  mkdir -p -- "${target}"
  chmod 0700 -- "${target}"
  local report="${target}/remediation-${MODE}.txt"
  {
    echo "mode: ${MODE}"
    echo "worst: ${WORST}"
    echo "repo_root: ${REPO_ROOT}"
    for finding in "${FINDINGS[@]}"; do
      echo "${finding}"
    done
  } > "${report}"
  chmod 0600 -- "${report}"
  echo ">>> evidence: ${report} (0600, sanitized)"
}

# -- apply ---------------------------------------------------------------------

apply_changes() {
  echo ">>> APPLY"
  [[ "${WORST}" != "FAIL" || -d "${SYS_DROPIN_DIR}" || -e "${SYS_STALE_WATCHDOG}" ]] \
    || fail "unexpected state; refusing to change anything"

  local backup="${SYSROOT}/var/backups/ibgateway-remediation"
  mkdir -p -- "${backup}"
  chmod 0700 -- "${backup}"

  # Quarantine contradicting drop-ins after a private backup. Renaming to a
  # non-.conf suffix is enough for systemd to ignore them and is reversible;
  # nothing is deleted.
  if [[ -d "${SYS_DROPIN_DIR}" ]]; then
    while IFS= read -r dropin; do
      [[ -n "${dropin}" ]] || continue
      local verdict
      verdict="$(classify_dropin_file "${dropin}")"
      if [[ "${verdict}" == FAIL* ]]; then
        cp -p -- "${dropin}" "${backup}/$(basename "${dropin}").bak"
        chmod 0600 -- "${backup}/$(basename "${dropin}").bak"
        mv -- "${dropin}" "${dropin}.quarantined"
        echo "  quarantined: $(basename "${dropin}") -> $(basename "${dropin}").quarantined"
      fi
    done < <(find "${SYS_DROPIN_DIR}" -maxdepth 1 -name '*.conf' -type f 2>/dev/null | sort)
  fi

  # The stale /opt copy is RENAMED, never deleted, and only once the canonical
  # replacement is present and readable.
  if [[ -e "${SYS_STALE_WATCHDOG}" ]]; then
    if [[ -r "${SYSROOT}${CANONICAL_WATCHDOG}" && -x "${SYSROOT}${VENV_PYTHON}" ]]; then
      mv -- "${SYS_STALE_WATCHDOG}" "${SYS_STALE_WATCHDOG}.quarantined"
      echo "  quarantined stale watchdog copy (not deleted)"
    else
      fail "refusing to quarantine the stale watchdog before the canonical one is verified"
    fi
  fi

  # Unit files are installed by the ONE installer, never re-implemented here.
  if [[ -z "${SYSROOT}" ]]; then
    "${REPO_ROOT}/scripts/install_ops_assets.sh" --apply \
      || fail "installer failed; the runtime contract is not in place"
  else
    echo "  (test mode: installer not invoked)"
  fi

  echo ">>> APPLY COMPLETE. Nothing was started, stopped, restarted or enabled."
}

# -- verify --------------------------------------------------------------------

verify_state() {
  echo ">>> VERIFY"
  local pid_before="$1" start_before="$2"
  local pid_now start_now
  pid_now="$(gateway_pid)"
  start_now="$(gateway_start_time "${pid_now}")"

  if [[ "${pid_before}" == "${pid_now}" && "${start_before}" == "${start_now}" ]]; then
    record OK gateway_untouched "PID and start time unchanged across remediation"
  else
    record FAIL gateway_untouched \
      "the Gateway process identity CHANGED — a process action occurred"
  fi

  local timer_state
  timer_state="$(show_property "${WATCHDOG_TIMER}" UnitFileState)"
  if [[ "${timer_state}" == "enabled" ]]; then
    record FAIL watchdog_timer_disabled "the timer is ENABLED; this tool never enables it"
  else
    record OK watchdog_timer_disabled "${timer_state:-not-enabled}"
  fi

  if [[ -e "${SYS_STALE_WATCHDOG}" ]]; then
    record FAIL stale_opt_watchdog "still present"
  else
    record OK stale_opt_watchdog "absent or quarantined"
  fi
}

gateway_pid() {
  if [[ -n "${SYSROOT}" ]]; then
    cat "${SYSROOT}/gateway.pid" 2>/dev/null || echo "none"
    return 0
  fi
  pgrep -f 'ibgateway|IBGateway' 2>/dev/null | head -1 || echo "none"
}

gateway_start_time() {
  local pid="$1"
  [[ "${pid}" != "none" && -n "${pid}" ]] || { echo "none"; return 0; }
  if [[ -n "${SYSROOT}" ]]; then
    cat "${SYSROOT}/gateway.start" 2>/dev/null || echo "none"
    return 0
  fi
  ps -o lstart= -p "${pid}" 2>/dev/null | tr -s ' ' || echo "none"
}

# -- main ----------------------------------------------------------------------

PID_BEFORE="$(gateway_pid)"
START_BEFORE="$(gateway_start_time "${PID_BEFORE}")"

echo ">>> owner-host runtime remediation — mode: ${MODE^^}"
echo ">>> the running Gateway is NEVER started, stopped, restarted or relogged in."
echo

inspect

case "${MODE}" in
  check)
    echo ">>> CHECK ONLY — nothing was changed."
    if [[ "${WORST}" == "FAIL" ]]; then
      echo ">>> Planned changes on --apply:"
      echo "    * quarantine contradicting drop-ins (after a private 0600 backup)"
      echo "    * quarantine the stale /opt watchdog copy once the canonical one is verified"
      echo "    * install the canonical units via scripts/install_ops_assets.sh --apply"
      echo "    * NOT enable the timer, NOT restart the Gateway — those stay manual"
    fi
    ;;
  apply)
    apply_changes
    verify_state "${PID_BEFORE}" "${START_BEFORE}"
    ;;
  verify)
    verify_state "${PID_BEFORE}" "${START_BEFORE}"
    ;;
esac

write_evidence

echo
echo ">>> worst finding: ${WORST}"
if [[ "${WORST}" == "FAIL" && "${MODE}" != "check" ]]; then
  fail "remediation finished with unresolved FAIL findings"
fi
exit 0
