#!/usr/bin/env bash
# Owner-host runtime remediation for the IB Gateway operational contract.
#
#   --check    (DEFAULT) read-only. Collects, validates, prints the plan.
#   --apply    executes the plan transactionally, with rollback.
#   --verify   proves the resulting state against a PERSISTED pre-apply baseline.
#
# WHAT THIS NEVER DOES, IN ANY MODE:
#   * never starts, stops, restarts or relogins IB Gateway;
#   * never enables the watchdog timer;
#   * never runs `systemctl --now` anything;
#   * never touches firewall / firewalld / nftables / OCI security lists;
#   * never touches SSH or VNC configuration;
#   * never reads or prints an IBC config file, a credential or an account id;
#   * never prints Java command-line arguments.
#
# PHASE ORDER IS THE SAFETY PROPERTY:
#
#   COLLECT -> VALIDATE_ALL -> BUILD_PLAN -> [EXECUTE] -> POST_COLLECT -> STATE
#
# Nothing is copied, moved, renamed or written until COLLECT, VALIDATE_ALL and
# BUILD_PLAN have all completed. A tool that validates as it goes has already
# changed the host by the time it discovers it should not have started.
#
# The running Gateway is deliberately left alone. Its process is owned by a
# user session rather than the canonical unit, and correcting that needs a
# restart — an OWNER-AUTHORIZED action under
# docs/runbooks/CHAOS_RECOVERY_DRILLS.md. This tool makes the next start
# correct and proves the current one untouched.
set -euo pipefail

# -- final states --------------------------------------------------------------
STATE_FAILED_PRECHECK="FAILED_PRECHECK"
STATE_FAILED_ROLLED_BACK="FAILED_APPLY_ROLLED_BACK"
STATE_FAILED_PARTIAL="FAILED_APPLY_PARTIAL_ROLLBACK"
STATE_REMEDIATED="REMEDIATED_RESTART_REQUIRED"
STATE_VERIFIED="VERIFIED_COMPLETE"
STATE_NO_CHANGES="NO_CHANGES_REQUIRED"

GATEWAY_UNIT="ibgateway-ibc.service"
WATCHDOG_UNIT="ibgateway-watchdog.service"
WATCHDOG_TIMER="ibgateway-watchdog.timer"
LEGACY_UNIT="ibgateway.service"
STALE_WATCHDOG="/opt/trading/scripts/ib_gateway_watchdog.py"
DROPIN_SUBDIR="/etc/systemd/system/${GATEWAY_UNIT}.d"

MODE="check"
REPO_ROOT="/home/opc/projects/TradingSystem"
SYSROOT=""
EVIDENCE_DIR=""
FINAL_STATE=""

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}"; }

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
CONTRACT_CLI="${REPO_ROOT}/scripts/systemd_contract.py"
CANONICAL_WATCHDOG="${REPO_ROOT}/scripts/ib_gateway_watchdog.py"
INSTALLER="${REPO_ROOT}/scripts/install_ops_assets.sh"
DROPIN_DIR="${SYSROOT}${DROPIN_SUBDIR}"
SYS_STALE_WATCHDOG="${SYSROOT}${STALE_WATCHDOG}"
#: Baseline and transaction state live here. Root-owned, 0700.
STATE_DIR="${SYSROOT}/var/lib/ibgateway-remediation"
BASELINE_FILE="${STATE_DIR}/gateway-baseline.env"
MANIFEST=""
BACKUP_DIR=""

FINDINGS=()
PLAN=()
PRECHECK_FAILURES=()
NEEDS_RELOAD=0
DAEMON_RELOADS=0
MUTATIONS_DONE=0

record() {
  local severity="$1" check="$2" detail="$3"
  FINDINGS+=("${severity}|${check}|${detail}")
  printf '  %-6s %-34s %s\n' "${severity}" "${check}" "${detail}"
}

precheck_fail() {
  PRECHECK_FAILURES+=("$1")
  record FAIL precheck "$1"
}

finish() {
  FINAL_STATE="$1"
  local message="${2:-}"
  echo
  [[ -z "${message}" ]] || echo ">>> ${message}"
  echo ">>> FINAL STATE: ${FINAL_STATE}"
  echo ">>> mutations performed: ${MUTATIONS_DONE}; daemon-reloads: ${DAEMON_RELOADS}"
  write_evidence
  case "${FINAL_STATE}" in
    "${STATE_VERIFIED}"|"${STATE_NO_CHANGES}"|"${STATE_REMEDIATED}") exit 0 ;;
    *) exit 1 ;;
  esac
}

have_systemd() { [[ -z "${SYSROOT}" ]] && command -v systemctl >/dev/null 2>&1; }

show_property() {
  local unit="$1" property="$2"
  if have_systemd; then
    systemctl show "${unit}" -p "${property}" --value 2>/dev/null || true
  elif [[ -f "${SYSROOT}/show/${unit}.${property}" ]]; then
    cat "${SYSROOT}/show/${unit}.${property}"
  fi
}

hash_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
  else
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  fi
}

# =============================================================================
# PHASE 1 — COLLECT (read-only)
# =============================================================================

COLLECTED_DROPINS=()
GATEWAY_PID=""
GATEWAY_START_TICKS=""
GATEWAY_CGROUP=""

collect_gateway_identity() {
  if [[ -n "${SYSROOT}" ]]; then
    GATEWAY_PID="$(cat "${SYSROOT}/gateway.pid" 2>/dev/null || echo none)"
    GATEWAY_START_TICKS="$(cat "${SYSROOT}/gateway.startticks" 2>/dev/null || echo none)"
    GATEWAY_CGROUP="$(cat "${SYSROOT}/gateway.cgroup" 2>/dev/null || echo none)"
    return 0
  fi
  GATEWAY_PID="$(pgrep -f 'ibgateway|IBGateway' 2>/dev/null | head -1 || true)"
  GATEWAY_PID="${GATEWAY_PID:-none}"
  if [[ "${GATEWAY_PID}" == "none" ]]; then
    GATEWAY_START_TICKS="none"; GATEWAY_CGROUP="none"; return 0
  fi
  # Field 22 of /proc/PID/stat is starttime in clock ticks since boot. PID
  # alone is not identity: PIDs are reused, so a restart could present the
  # same number. Ticks + cgroup make reuse detectable.
  GATEWAY_START_TICKS="$(awk '{print $22}' "/proc/${GATEWAY_PID}/stat" 2>/dev/null || echo none)"
  GATEWAY_CGROUP="$(awk -F'::' '{print $2}' "/proc/${GATEWAY_PID}/cgroup" 2>/dev/null | head -1 || echo none)"
  GATEWAY_CGROUP="${GATEWAY_CGROUP:-none}"
}

phase_collect() {
  echo ">>> PHASE 1: COLLECT (read-only)"
  collect_gateway_identity
  record INFO gateway_identity "pid=${GATEWAY_PID} start_ticks=${GATEWAY_START_TICKS} cgroup=${GATEWAY_CGROUP}"

  COLLECTED_DROPINS=()
  if [[ -d "${DROPIN_DIR}" ]]; then
    while IFS= read -r dropin; do
      [[ -n "${dropin}" ]] && COLLECTED_DROPINS+=("${dropin}")
    done < <(find "${DROPIN_DIR}" -maxdepth 1 -name '*.conf' -type f 2>/dev/null | sort)
  fi
  record INFO dropins_found "${#COLLECTED_DROPINS[@]}"

  record INFO gateway_unit_state \
    "ActiveState=$(show_property "${GATEWAY_UNIT}" ActiveState):SubState=$(show_property "${GATEWAY_UNIT}" SubState):MainPID=$(show_property "${GATEWAY_UNIT}" MainPID)"
  record INFO legacy_unit_state "$(show_property "${LEGACY_UNIT}" UnitFileState)"
  record INFO timer_state \
    "UnitFileState=$(show_property "${WATCHDOG_TIMER}" UnitFileState):ActiveState=$(show_property "${WATCHDOG_TIMER}" ActiveState)"
  record INFO stale_opt_watchdog \
    "$([[ -e "${SYS_STALE_WATCHDOG}" ]] && echo present || echo absent)"
  echo
}

# =============================================================================
# PHASE 2 — VALIDATE_ALL (read-only; every failure collected, none fatal yet)
# =============================================================================

DROPIN_VERDICTS=()

phase_validate_all() {
  echo ">>> PHASE 2: VALIDATE_ALL (read-only)"

  # The runtime the classification itself depends on. If we cannot classify a
  # drop-in we must not move one: "unknown" is not "harmless".
  [[ -d "${SYSROOT}${REPO_ROOT}" ]] \
    || precheck_fail "repository root missing: ${REPO_ROOT}"
  [[ -x "${SYSROOT}${VENV_PYTHON}" ]] \
    || precheck_fail "pinned interpreter missing or not executable: ${VENV_PYTHON}"
  [[ -r "${SYSROOT}${CONTRACT_CLI}" ]] \
    || precheck_fail "contract module missing: ${CONTRACT_CLI}"
  [[ -r "${SYSROOT}${CANONICAL_WATCHDOG}" ]] \
    || precheck_fail "canonical watchdog script missing: ${CANONICAL_WATCHDOG}"
  [[ -x "${SYSROOT}${INSTALLER}" || -r "${SYSROOT}${INSTALLER}" ]] \
    || precheck_fail "installer missing: ${INSTALLER}"

  DROPIN_VERDICTS=()
  if [[ "${#PRECHECK_FAILURES[@]}" -eq 0 ]]; then
    local dropin verdict code
    for dropin in ${COLLECTED_DROPINS[@]+"${COLLECTED_DROPINS[@]}"}; do
      set +e
      verdict="$(PYTHONDONTWRITEBYTECODE=1 "${SYSROOT}${VENV_PYTHON}" -B \
        "${SYSROOT}${CONTRACT_CLI}" classify "${dropin}" 2>&1)"
      code=$?
      set -e
      case "${code}" in
        0) DROPIN_VERDICTS+=("KEEP|${dropin}"); record OK "dropin:$(basename "${dropin}")" "${verdict}" ;;
        1) DROPIN_VERDICTS+=("QUARANTINE|${dropin}"); record FAIL "dropin:$(basename "${dropin}")" "${verdict}" ;;
        *) precheck_fail "cannot classify $(basename "${dropin}"): ${verdict}" ;;
      esac
    done
  else
    record WARN dropin_classification "skipped: the classification runtime is not usable"
  fi

  if [[ "${#PRECHECK_FAILURES[@]}" -gt 0 ]]; then
    record FAIL validate_all "${#PRECHECK_FAILURES[@]} precheck failure(s); zero mutation permitted"
  else
    record OK validate_all "all prechecks passed"
  fi
  echo
}

# =============================================================================
# PHASE 3 — BUILD_PLAN (read-only)
# =============================================================================

phase_build_plan() {
  echo ">>> PHASE 3: BUILD_PLAN (read-only)"
  PLAN=()
  local entry dropin
  for entry in ${DROPIN_VERDICTS[@]+"${DROPIN_VERDICTS[@]}"}; do
    if [[ "${entry}" == QUARANTINE\|* ]]; then
      dropin="${entry#QUARANTINE|}"
      PLAN+=("quarantine_dropin|${dropin}")
    fi
  done
  [[ -e "${SYS_STALE_WATCHDOG}" ]] && PLAN+=("quarantine_stale|${SYS_STALE_WATCHDOG}")
  PLAN+=("install_units|${INSTALLER}")

  local step
  for step in "${PLAN[@]}"; do
    echo "    PLAN ${step%%|*}  ${step#*|}"
  done
  record INFO plan_steps "${#PLAN[@]}"
  echo
}

# =============================================================================
# PHASE 4 — EXECUTE (the only phase that mutates)
# =============================================================================

begin_transaction() {
  # Collision-resistant, private, root-owned. A predictable name would let an
  # unprivileged local user pre-create or read the backup of a unit file.
  mkdir -p -- "${STATE_DIR}"
  chmod 0700 -- "${STATE_DIR}"
  BACKUP_DIR="$(mktemp -d "${STATE_DIR}/txn-XXXXXXXXXXXX")"
  chmod 0700 -- "${BACKUP_DIR}"
  MANIFEST="${BACKUP_DIR}/manifest.tsv"
  : > "${MANIFEST}"
  chmod 0600 -- "${MANIFEST}"
  echo "  transaction: ${BACKUP_DIR}"
}

# action <TAB> original <TAB> backup <TAB> sha256(original) <TAB> destination
manifest_add() {
  printf '%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" >> "${MANIFEST}"
}

#: Test-only fault injection: make the Nth mutation fail, so rollback can be
#: exercised deterministically. Gated behind the same test-mode flag as
#: --sysroot; without it the variable is ignored entirely.
fault_injected() {
  [[ "${OPS_REMEDIATE_TEST_MODE:-}" == "1" ]] || return 1
  [[ -n "${OPS_REMEDIATE_FAIL_ON_MUTATION:-}" ]] || return 1
  [[ "$((MUTATIONS_DONE + 1))" == "${OPS_REMEDIATE_FAIL_ON_MUTATION}" ]]
}

backup_and_move() {
  local source="$1" destination="$2" backup digest
  if fault_injected; then
    echo "    (test mode: injected failure on mutation $((MUTATIONS_DONE + 1)))" >&2
    return 1
  fi
  backup="${BACKUP_DIR}/$(basename "${source}").bak"
  cp -p -- "${source}" "${backup}"
  chmod 0600 -- "${backup}"
  digest="$(hash_file "${source}")"
  if [[ "$(hash_file "${backup}")" != "${digest}" ]]; then
    echo "ERROR: backup hash mismatch for ${source}" >&2
    return 1
  fi
  manifest_add "move" "${source}" "${backup}" "${digest}" "${destination}"
  mv -- "${source}" "${destination}"
  MUTATIONS_DONE=$((MUTATIONS_DONE + 1))
}

rollback() {
  local restored=0 failed=0 action original backup digest destination
  echo ">>> ROLLBACK"
  # Reverse order: undo the most recent mutation first.
  while IFS=$'\t' read -r action original backup digest destination; do
    [[ -n "${action}" ]] || continue
    # Restore the original AND remove what the move created, so a rolled-back
    # host is byte-identical to the one we started with — not the original
    # plus a stray artefact that systemd would ignore but a reviewer would not.
    if cp -p -- "${backup}" "${original}" 2>/dev/null \
       && [[ "$(hash_file "${original}")" == "${digest}" ]]; then
      if [[ -n "${destination}" && "${destination}" != "${original}" ]]; then
        rm -f -- "${destination}" 2>/dev/null || true
      fi
      restored=$((restored + 1))
      echo "    restored: ${original}"
    else
      failed=$((failed + 1))
      echo "    FAILED to restore: ${original}" >&2
    fi
  done < <(tac "${MANIFEST}" 2>/dev/null || sort -r "${MANIFEST}")
  echo "    restored=${restored} failed=${failed}"
  [[ "${failed}" -eq 0 ]]
}

phase_execute() {
  echo ">>> PHASE 4: EXECUTE"
  begin_transaction

  local step kind target
  for step in "${PLAN[@]}"; do
    kind="${step%%|*}"; target="${step#*|}"
    case "${kind}" in
      quarantine_dropin)
        if ! backup_and_move "${target}" "${target}.quarantined"; then
          rollback && finish "${STATE_FAILED_ROLLED_BACK}" "quarantine failed" \
                   || finish "${STATE_FAILED_PARTIAL}" "quarantine failed AND rollback incomplete"
        fi
        NEEDS_RELOAD=1
        echo "    quarantined drop-in: $(basename "${target}")"
        ;;
      quarantine_stale)
        # Only once the canonical replacement is proven present.
        if [[ ! -r "${SYSROOT}${CANONICAL_WATCHDOG}" ]]; then
          rollback && finish "${STATE_FAILED_ROLLED_BACK}" "canonical watchdog missing" \
                   || finish "${STATE_FAILED_PARTIAL}" "canonical watchdog missing AND rollback incomplete"
        fi
        if ! backup_and_move "${target}" "${target}.quarantined"; then
          rollback && finish "${STATE_FAILED_ROLLED_BACK}" "stale quarantine failed" \
                   || finish "${STATE_FAILED_PARTIAL}" "stale quarantine failed AND rollback incomplete"
        fi
        echo "    quarantined stale watchdog copy (not deleted)"
        ;;
      install_units)
        if [[ -n "${SYSROOT}" ]]; then
          echo "    (test mode: installer not invoked)"
        elif ! "${INSTALLER}" --apply --no-reload; then
          rollback && finish "${STATE_FAILED_ROLLED_BACK}" "installer failed" \
                   || finish "${STATE_FAILED_PARTIAL}" "installer failed AND rollback incomplete"
        else
          NEEDS_RELOAD=1
          MUTATIONS_DONE=$((MUTATIONS_DONE + 1))
        fi
        ;;
    esac
  done

  # EXACTLY ONCE, and only if something changed. Quarantining a drop-in
  # changes the EFFECTIVE unit even when the base unit file is byte-identical,
  # so a reload keyed only on "did the unit file change" would leave systemd
  # serving the old, contradicted contract.
  if [[ "${NEEDS_RELOAD}" -eq 1 ]]; then
    if [[ -n "${SYSROOT}" ]]; then
      echo "    (test mode: daemon-reload not invoked)"
    else
      systemctl daemon-reload || {
        rollback && finish "${STATE_FAILED_ROLLED_BACK}" "daemon-reload failed" \
                 || finish "${STATE_FAILED_PARTIAL}" "daemon-reload failed AND rollback incomplete"
      }
    fi
    DAEMON_RELOADS=1
    echo "    daemon-reload: 1 (drop-in and/or unit changed)"
  else
    echo "    daemon-reload: 0 (nothing changed)"
  fi
  echo
}

# =============================================================================
# baseline persistence (item 6)
# =============================================================================

persist_baseline() {
  mkdir -p -- "${STATE_DIR}"
  chmod 0700 -- "${STATE_DIR}"
  {
    echo "schema=1"
    echo "captured_phase=pre_apply"
    echo "gateway_pid=${GATEWAY_PID}"
    echo "gateway_start_ticks=${GATEWAY_START_TICKS}"
    echo "gateway_cgroup=${GATEWAY_CGROUP}"
  } > "${BASELINE_FILE}"
  chmod 0600 -- "${BASELINE_FILE}"
  echo "  baseline persisted: ${BASELINE_FILE}"
}

# =============================================================================
# PHASE 5 — POST_COLLECT + VERIFY
# =============================================================================

phase_verify() {
  echo ">>> PHASE 5: POST_COLLECT + VERIFY (fresh collection)"

  # VERIFY NEVER SELF-BASELINES. Comparing a value to itself proves nothing;
  # the baseline must have been written before the first mutation.
  if [[ ! -r "${BASELINE_FILE}" ]]; then
    record FAIL baseline_present "no persisted pre-apply baseline at ${BASELINE_FILE}"
    finish "${STATE_FAILED_PRECHECK}" \
      "VERIFY requires a baseline written by --apply; it must never create its own"
  fi
  local base_pid base_ticks base_cgroup
  base_pid="$(awk -F= '$1=="gateway_pid"{print $2}' "${BASELINE_FILE}")"
  base_ticks="$(awk -F= '$1=="gateway_start_ticks"{print $2}' "${BASELINE_FILE}")"
  base_cgroup="$(awk -F= '$1=="gateway_cgroup"{print $2}' "${BASELINE_FILE}")"
  record INFO baseline_loaded "pid=${base_pid} start_ticks=${base_ticks}"

  # Fresh collection — not the values gathered before EXECUTE.
  collect_gateway_identity

  local ok=1
  if [[ "${base_pid}" == "${GATEWAY_PID}" && "${base_ticks}" == "${GATEWAY_START_TICKS}" \
        && "${base_cgroup}" == "${GATEWAY_CGROUP}" ]]; then
    record OK gateway_untouched "pid, start ticks and cgroup all unchanged"
  else
    record FAIL gateway_untouched "the Gateway process identity CHANGED"
    ok=0
  fi

  local timer_file timer_active service_active
  timer_file="$(show_property "${WATCHDOG_TIMER}" UnitFileState)"
  timer_active="$(show_property "${WATCHDOG_TIMER}" ActiveState)"
  service_active="$(show_property "${WATCHDOG_UNIT}" ActiveState)"
  if [[ "${timer_file}" == "enabled" ]]; then
    record FAIL timer_disabled "timer UnitFileState=${timer_file}"; ok=0
  else
    record OK timer_disabled "${timer_file:-not-enabled}"
  fi
  if [[ "${timer_active}" == "active" ]]; then
    record FAIL timer_inactive "timer ActiveState=active"; ok=0
  else
    record OK timer_inactive "${timer_active:-inactive}"
  fi
  if [[ "${service_active}" == "active" ]]; then
    record FAIL watchdog_service_inactive "watchdog service ActiveState=active"; ok=0
  else
    record OK watchdog_service_inactive "${service_active:-inactive}"
  fi

  if [[ -e "${SYS_STALE_WATCHDOG}" ]]; then
    record FAIL stale_removed "stale copy still present"; ok=0
  else
    record OK stale_removed "absent or quarantined"
  fi
  echo
  [[ "${ok}" -eq 1 ]]
}

# =============================================================================
# evidence
# =============================================================================

write_evidence() {
  [[ -n "${EVIDENCE_DIR}" ]] || return 0
  mkdir -p -- "${EVIDENCE_DIR}"
  chmod 0700 -- "${EVIDENCE_DIR}"
  local report="${EVIDENCE_DIR}/remediation-${MODE}.txt"
  {
    echo "mode=${MODE}"
    echo "final_state=${FINAL_STATE}"
    echo "mutations=${MUTATIONS_DONE}"
    echo "daemon_reloads=${DAEMON_RELOADS}"
    local finding
    for finding in ${FINDINGS[@]+"${FINDINGS[@]}"}; do echo "${finding}"; done
  } > "${report}"
  chmod 0600 -- "${report}"
  echo ">>> evidence: ${report} (0600, sanitized)"
}

# =============================================================================
# main
# =============================================================================

echo ">>> owner-host runtime remediation — mode: ${MODE^^}"
echo ">>> the running Gateway is NEVER started, stopped, restarted or relogged in."
echo

if [[ "${MODE}" == "verify" ]]; then
  phase_collect
  if phase_verify; then
    finish "${STATE_VERIFIED}"
  fi
  finish "${STATE_FAILED_PRECHECK}" "verification found unresolved failures"
fi

phase_collect
phase_validate_all

if [[ "${#PRECHECK_FAILURES[@]}" -gt 0 ]]; then
  # ZERO MUTATION. No transaction is opened, no backup directory is created,
  # no baseline is written, no file is touched.
  finish "${STATE_FAILED_PRECHECK}" \
    "${#PRECHECK_FAILURES[@]} precheck failure(s); nothing was changed"
fi

phase_build_plan

if [[ "${MODE}" == "check" ]]; then
  echo ">>> CHECK ONLY — nothing was changed."
  if [[ "${#PLAN[@]}" -le 1 ]]; then
    finish "${STATE_NO_CHANGES}"
  fi
  finish "${STATE_REMEDIATED}" "an --apply would perform ${#PLAN[@]} planned step(s)"
fi

# APPLY
persist_baseline
phase_execute

if phase_verify; then
  if [[ "${MUTATIONS_DONE}" -eq 0 ]]; then
    finish "${STATE_NO_CHANGES}"
  fi
  finish "${STATE_REMEDIATED}" \
    "the contract is in place; the RUNNING Gateway still belongs to its old session — an owner-authorized restart is required"
fi
finish "${STATE_FAILED_PRECHECK}" "post-apply verification failed"
