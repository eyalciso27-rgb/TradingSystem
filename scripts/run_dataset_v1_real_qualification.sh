#!/usr/bin/env bash
# Dataset V1 — real historical source qualification (owner-run, fail-closed).
#
# ONE command for the owner host. It:
#   1. preflights the toolchain and repository layout;
#   2. runs IBKR, SEC and (when configured) S&P ingestion + qualification;
#   3. verifies every artifact against ITS OWN schema (recomputing checksums);
#   4. prints PASS/PARTIAL/FAIL and the capability state for each of the four
#      external Dataset V1 capabilities;
#   5. prints the resulting readiness and the evidence paths/checksums.
#
# It never prints credentials, never fabricates evidence and never promotes a
# capability by itself: promotion happens only through the authenticated
# evidence produced by a configured REAL source.
#
# SCOPE NOTE: this runner DOES exercise the approved real sources. The SEC
# User-Agent check is a GATE, not a result; what each source actually produced
# is reported under SOURCE EXECUTION and comes from real exit codes.
#
# Artifacts are verified BY TYPE: an auxiliary artifact (identity crosswalk,
# coverage report) is never parsed with the qualification-evidence schema.
#
# The probe helpers below are pure and side-effect free so they can be sourced
# and tested; main() runs only when this file is executed directly.
#
# Exit codes: 0 = dataset contract genuinely ready; 1 = not ready (expected
# while any external cell is blocked); 2 = preflight failure.

# NOTE: `set -euo pipefail` and the repo-root resolution live inside main(), not
# at file scope. Sourcing this file must not mutate the caller's shell options
# (`-e` would abort the caller on the first probe that legitimately returns
# non-zero) and must not depend on any external binary being on PATH.

SOURCE_CONFIG="${DATASET_V1_SOURCE_CONFIG:-config/data-sources/dataset-v1-sources.yaml}"
IB_HOST="${IB_GATEWAY_HOST:-127.0.0.1}"
IB_PORT="${IB_GATEWAY_PORT:-4002}"   # PAPER only; 4001 (live) is never used
IB_PROBE_TIMEOUT="${IB_GATEWAY_PROBE_TIMEOUT:-3}"
EVIDENCE_DIR="${DATASET_V1_EVIDENCE_DIR:-docs/poi/evidence/dataset-v1}"
CLI="trading_market_data_core.dataset_v1_qualification_cli"
INGEST_CLI="trading_market_data_core.dataset_v1_ingest_cli"
SNAPSHOT_CLI="trading_market_data_core.dataset_v1_snapshot_cli"

# -- real-source execution knobs (all bounded; nothing here is a bulk crawl) --
RUN_IBKR="${DATASET_V1_RUN_IBKR:-1}"
IB_ACCOUNT_ALIAS="${IB_ACCOUNT_ALIAS:-paper-poi}"
IBKR_CORPORATE_ARTIFACT="${DATASET_V1_IBKR_CORPORATE_ARTIFACT:-docs/poi/evidence/POI-IBKR-CORP.json}"
#: one quarter of SEC's official catalogue + a capped number of filers
SEC_YEAR="${DATASET_V1_SEC_YEAR:-2023}"
SEC_QUARTER="${DATASET_V1_SEC_QUARTER:-1}"
SEC_LIMIT="${DATASET_V1_SEC_LIMIT:-5}"
SP_MANIFEST="${DATASET_V1_SP_MANIFEST:-config/data-sources/sp500-announcements.json}"
SP_DOCUMENTS="${DATASET_V1_SP_DOCUMENTS:-config/data-sources/sp500-documents}"
SP_PRODUCTION_START="${DATASET_V1_SP_PRODUCTION_START:-}"
SP_PRODUCTION_END="${DATASET_V1_SP_PRODUCTION_END:-}"
SNAPSHOT_OUT="${DATASET_V1_SNAPSHOT_OUT:-docs/poi/evidence/dataset-v1-snapshot-report.json}"
RUN_CLI="trading_market_data_core.dataset_v1_run_cli"
RUN_ROOT="${DATASET_V1_RUN_ROOT:-docs/poi/evidence/dataset-v1-runs}"
IBKR_CLI="trading_market_data_core.dataset_v1_ibkr_cli"

# -- TCP preflight ------------------------------------------------------------
# Each probe returns: 0 = port open, 1 = port closed/unreachable, 2 = this probe
# is UNAVAILABLE on this host (which is NOT evidence about the port).
#
# A missing tool must never be reported as an unreachable Gateway, so the probes
# are layered cheapest-and-most-available first and only a genuine connection
# result short-circuits the chain.

_probe_bash() {
    # Bash's own /dev/tcp: no external dependency at all. `timeout` is used when
    # available so a filtered port cannot hang the preflight, but its absence
    # must not disable the probe — the builtin path works with an empty PATH.
    local host="$1" port="$2" timeout_s="$3" err rc
    if command -v timeout >/dev/null 2>&1 && command -v bash >/dev/null 2>&1; then
        err="$(LC_ALL=C timeout "$timeout_s" bash -c 'exec 3<>/dev/tcp/$0/$1' \
            "$host" "$port" 2>&1)" && return 0
        rc=$?
    else
        err="$( (LC_ALL=C exec 3<>"/dev/tcp/$host/$port") 2>&1 )" && return 0
        rc=$?
    fi
    # 124 = timeout(1) fired: the port did not accept a connection in time.
    if [ "$rc" -eq 124 ]; then
        return 1
    fi
    case "$err" in
        *"Connection refused"*|*"onnection timed out"*|*"o route to host"*|*nreachable*)
            return 1 ;;
        *)
            # e.g. a bash built with --disable-net-redirections: says nothing
            # about the port, so defer to the next probe.
            return 2 ;;
    esac
}

_probe_python() {
    # Python's stdlib socket: no third-party package required. The inline
    # program exits ONLY 0 (open) or 1 (closed), so any other status means the
    # interpreter never ran it — a broken shim (e.g. the Windows Store python3
    # stub, which exits 49) says nothing about the port and must report
    # UNAVAILABLE rather than a false "closed".
    local host="$1" port="$2" timeout_s="$3" python_bin rc
    python_bin="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
    [ -n "$python_bin" ] || return 2
    "$python_bin" -c '
import socket, sys
try:
    with socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=float(sys.argv[3])):
        sys.exit(0)
except OSError:
    sys.exit(1)
' "$host" "$port" "$timeout_s" >/dev/null 2>&1
    rc=$?
    case "$rc" in
        0) return 0 ;;
        1) return 1 ;;
        *) return 2 ;;
    esac
}

_probe_nc() {
    # nc(1) reports 0 for a successful connection and 1 for a refused one; any
    # other status (usage error, unsupported flag) is a tooling problem, not a
    # verdict about the port.
    local host="$1" port="$2" timeout_s="$3" rc
    command -v nc >/dev/null 2>&1 || return 2
    nc -z -w "$timeout_s" "$host" "$port" >/dev/null 2>&1
    rc=$?
    case "$rc" in
        0) return 0 ;;
        1) return 1 ;;
        *) return 2 ;;
    esac
}

tcp_probe() {
    # 0 = open, 1 = closed/unreachable, 2 = no usable probe on this host.
    local host="$1" port="$2" timeout_s="${3:-3}" probe rc
    for probe in _probe_bash _probe_python _probe_nc; do
        "$probe" "$host" "$port" "$timeout_s" && return 0
        rc=$?
        [ "$rc" -eq 2 ] || return "$rc"
    done
    return 2
}

ibkr_preflight_status() {
    # Prints the status token. Returns 2 only for the hard live-port block.
    local host="$1" port="$2" timeout_s="${3:-3}" rc
    if [ "$port" = "4001" ]; then
        echo "LIVE_PORT_REJECTED"
        return 2
    fi
    tcp_probe "$host" "$port" "$timeout_s" && { echo "GATEWAY_REACHABLE"; return 0; }
    rc=$?
    if [ "$rc" -eq 1 ]; then
        echo "GATEWAY_UNREACHABLE"
    else
        # No probe could run. Report the tooling gap honestly instead of
        # blaming the Gateway for a missing binary.
        echo "PREFLIGHT_TOOL_UNAVAILABLE"
    fi
    return 0
}

classify_execution() {
    # Map an ingestion CLI exit code to an honest SOURCE EXECUTION status.
    # These describe what the run ACHIEVED. Configuration state is never
    # reported here: "the User-Agent is set" is not "SEC was qualified".
    case "${1:-}" in
        0) echo "PASS" ;;
        3) echo "PARTIAL" ;;
        4) echo "NOT_AVAILABLE" ;;
        2) echo "FAIL" ;;   # source could not be read
        *) echo "FAIL" ;;
    esac
}

run_stage() {
    # run_stage <label> <command...>; echoes the status, preserves the code.
    local label="$1"; shift
    local rc=0
    echo "== $label ==" >&2
    "$@" >&2 || rc=$?
    classify_execution "$rc"
}

record_stage() {
    # record_stage <cli> <run_id> <run_root> <stage> <status> [artifact...]
    # Maps a SOURCE EXECUTION status to a run-manifest stage outcome. A FAILED
    # stage records NO artifacts, which is what prevents a previous run's file
    # from being bound into this run.
    local cli="$1" run_id="$2" run_root="$3" stage="$4" status="$5"
    shift 5
    local outcome
    case "$status" in
        PASS) outcome="SUCCEEDED" ;;
        PARTIAL) outcome="PARTIAL" ;;
        NOT_AVAILABLE) outcome="NOT_AVAILABLE" ;;
        NOT_RUN) outcome="NOT_RUN" ;;
        *) outcome="FAILED" ;;
    esac
    local -a artifact_args=()
    local artifact
    for artifact in "$@"; do
        artifact_args+=(--artifact "$artifact")
    done
    uv run --no-sync python -m "$cli" stage --run-id "$run_id" --run-root "$run_root" \
        --stage-name "$stage" --outcome "$outcome" "${artifact_args[@]+"${artifact_args[@]}"}" \
        >/dev/null || true
}

main() {
    set -euo pipefail

    local REPO_ROOT
    REPO_ROOT="$(cd "${BASH_SOURCE[0]%/*}/.." && pwd)"
    cd "$REPO_ROOT"

    echo "== Dataset V1 real-source qualification =="
    echo "repo         : $REPO_ROOT"
    echo "source config: $SOURCE_CONFIG"
    echo "evidence dir : $EVIDENCE_DIR"
    echo

    # -- 1. preflight ---------------------------------------------------------
    if ! command -v uv >/dev/null 2>&1; then
        echo "PREFLIGHT_FAILED: uv is not installed on this host" >&2
        exit 2
    fi
    if [ ! -d "packages/py/market-data-core" ]; then
        echo "PREFLIGHT_FAILED: run this from the TradingSystem repository" >&2
        exit 2
    fi
    echo "preflight: OK"

    # -- RUN ISOLATION -------------------------------------------------------
    # Every run gets its own id and staging area. Stages write into staging and
    # publish only on success, so a FAILED stage cannot inherit a previous
    # run's artifact. Readiness is computed from this run's manifest, never
    # from a directory glob.
    local RUN_ID RUN_STARTED
    RUN_ID="$(uv run --no-sync python -m "$RUN_CLI" new-id)"
    RUN_STARTED="$(date -u +%Y-%m-%dT%H:%M:%S+00:00 2>/dev/null || echo "")"
    echo "run_id: $RUN_ID"

    # One-time, EXPLICIT migration of committed pre-typing IBKR evidence so the
    # manifest can bind it. A no-op once migrated, so normal operation stays a
    # single command with no cleanup and no manual steps.
    local legacy
    for legacy in "docs/poi/evidence/POI-IBKR-HIST.json" "$IBKR_CORPORATE_ARTIFACT"; do
        if [ -f "$legacy" ]; then
            uv run --no-sync python -m "$IBKR_CLI" migrate --path "$legacy" || true
        fi
    done
    echo

    # -- 1b. IBKR (OWNER_PRIMARY) preflight -----------------------------------
    # Paper-only: port 4002. The live API port is never contacted. Read-only;
    # there is no order surface anywhere in this path.
    local IBKR_STATUS
    if ! IBKR_STATUS="$(ibkr_preflight_status "$IB_HOST" "$IB_PORT" "$IB_PROBE_TIMEOUT")"; then
        echo "PREFLIGHT_FAILED: port 4001 is the LIVE API port and is never allowed" >&2
        exit 2
    fi
    echo "IBKR paper gateway ($IB_HOST:$IB_PORT): $IBKR_STATUS"
    case "$IBKR_STATUS" in
        GATEWAY_REACHABLE)
            echo "  -> run the read-only historical qualification to refresh POI-IBKR-HIST:"
            echo "     uv run --no-sync python -m trading_broker_ibkr.poi_historical_qualification \\"
            echo "       --host $IB_HOST --port $IB_PORT --account-alias paper-poi \\"
            echo "       --out docs/poi/evidence/POI-IBKR-HIST.json"
            ;;
        PREFLIGHT_TOOL_UNAVAILABLE)
            echo "  -> no TCP probe available on this host (no bash /dev/tcp, python, or nc)."
            echo "     This says NOTHING about the Gateway; it is a tooling gap, not a"
            echo "     reachability result. Install one, or check manually: ss -ltnp | grep $IB_PORT"
            ;;
    esac

    # -- 1c. SEC EDGAR (OFFICIAL_PUBLIC) access-rule preflight ----------------
    # Configuration is only the GATE. Whether SEC was actually exercised is
    # decided below by the ingestion stages, and reported as SOURCE EXECUTION.
    local SEC_CONFIGURED="NO"
    if [ -n "${SEC_EDGAR_USER_AGENT:-}" ]; then
        SEC_CONFIGURED="YES"
    else
        echo "SEC EDGAR: SEC_EDGAR_USER_AGENT is not set."
        echo "  SEC automated access requires a real contact, e.g.:"
        echo "  export SEC_EDGAR_USER_AGENT='Your Name your.email@example.com'"
        echo "  Without it the SEC client fails closed and SEC stays NOT_RUN."
    fi
    echo "SEC EDGAR user-agent configured: $SEC_CONFIGURED (configuration is a gate,"
    echo "  not a qualification result)"
    echo

    # -- 2. acceptance criteria (what a real source must prove) ---------------
    echo "== acceptance criteria =="
    uv run --no-sync python -m "$CLI" criteria
    echo

    # -- 3. discovery ---------------------------------------------------------
    echo "== source discovery =="
    uv run --no-sync python -m "$CLI" discover \
        --config "$SOURCE_CONFIG" --evidence-dir "$EVIDENCE_DIR"
    echo

    # -- 3b. REAL SOURCE EXECUTION -------------------------------------------
    # Everything below actually exercises the approved sources, and EVERY stage
    # registers with the run manifest. A stage that cannot run reports
    # NOT_RUN/NOT_AVAILABLE and binds nothing; it never reports PASS.
    local IBKR_EXEC="NOT_RUN" SEC_EXEC="NOT_RUN" SP_EXEC="NOT_RUN" GICS_EXEC="NOT_RUN"
    local CROSSWALK_EXEC="NOT_RUN" SEC_CORP_EXEC="NOT_RUN"

    if [ "$IBKR_STATUS" = "GATEWAY_REACHABLE" ] && [ "$RUN_IBKR" = "1" ]; then
        local hist_rc=0 corp_rc=0
        echo "== IBKR read-only historical qualification ==" >&2
        uv run --no-sync python -m trading_broker_ibkr.poi_historical_qualification \
            --host "$IB_HOST" --port "$IB_PORT" --account-alias "$IB_ACCOUNT_ALIAS" \
            --out docs/poi/evidence/POI-IBKR-HIST.json >&2 || hist_rc=$?
        echo "== IBKR read-only corporate-action qualification ==" >&2
        uv run --no-sync python -m trading_broker_ibkr.poi_corporate_actions_qualification \
            --host "$IB_HOST" --port "$IB_PORT" --account-alias "$IB_ACCOUNT_ALIAS" \
            --out "$IBKR_CORPORATE_ARTIFACT" >&2 || corp_rc=$?
        if [ "$hist_rc" -eq 0 ]; then IBKR_EXEC="PASS"; else IBKR_EXEC="PARTIAL"; fi
        if [ "$corp_rc" -eq 0 ]; then SEC_CORP_EXEC="PASS"; else SEC_CORP_EXEC="PARTIAL"; fi
    fi
    # Each IBKR artifact is registered under its OWN stage, so a rejected
    # artifact cannot be mistaken for a bound one.
    record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "ibkr_historical" "$IBKR_EXEC" \
        "docs/poi/evidence/POI-IBKR-HIST.json"
    record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "ibkr_corporate" "$SEC_CORP_EXEC" \
        "$IBKR_CORPORATE_ARTIFACT"

    if [ "$SEC_CONFIGURED" = "YES" ]; then
        SEC_EXEC="$(run_stage "SEC delisting ingestion + qualification" \
            uv run --no-sync python -m "$INGEST_CLI" sec-delisting \
            --evidence-dir "$EVIDENCE_DIR" --year "$SEC_YEAR" --quarter "$SEC_QUARTER" \
            --limit "$SEC_LIMIT" --run-id "$RUN_ID" --run-root "$RUN_ROOT")"
        record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "sec_delisted" "$SEC_EXEC" \
            "$EVIDENCE_DIR/delisted_security_history.json"

        local sec_corp
        sec_corp="$(run_stage "SEC corporate-action ingestion + qualification" \
            uv run --no-sync python -m "$INGEST_CLI" sec-corporate \
            --evidence-dir "$EVIDENCE_DIR" --year "$SEC_YEAR" --quarter "$SEC_QUARTER" \
            --limit "$SEC_LIMIT" --ibkr-artifact "$IBKR_CORPORATE_ARTIFACT" \
            --run-id "$RUN_ID" --run-root "$RUN_ROOT")"
        record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "sec_corporate_actions" \
            "$sec_corp" "$EVIDENCE_DIR/corporate_actions.json"

        CROSSWALK_EXEC="$(run_stage "identity crosswalk" \
            uv run --no-sync python -m "$INGEST_CLI" crosswalk \
            --evidence-dir "$EVIDENCE_DIR" --year "$SEC_YEAR" --quarter "$SEC_QUARTER" \
            --limit "$SEC_LIMIT" --run-id "$RUN_ID" --run-root "$RUN_ROOT")"
        record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "identity_crosswalk" \
            "$CROSSWALK_EXEC" "$EVIDENCE_DIR/identity-crosswalk.json"

        # the weaker of the two SEC stages is the honest SEC status
        if [ "$sec_corp" = "FAIL" ] || [ "$SEC_EXEC" = "FAIL" ]; then
            SEC_EXEC="FAIL"
        elif [ "$sec_corp" = "PARTIAL" ] || [ "$SEC_EXEC" = "PARTIAL" ]; then
            SEC_EXEC="PARTIAL"
        fi
    else
        record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "sec_delisted" "NOT_RUN"
        record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "sec_corporate_actions" "NOT_RUN"
        record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "identity_crosswalk" "NOT_RUN"
    fi

    SP_EXEC="$(run_stage "S&P official membership ingestion" \
        uv run --no-sync python -m "$INGEST_CLI" sp-membership \
        --evidence-dir "$EVIDENCE_DIR" --manifest "$SP_MANIFEST" \
        --documents-dir "$SP_DOCUMENTS" --run-id "$RUN_ID" --run-root "$RUN_ROOT" \
        --production-start "$SP_PRODUCTION_START" --production-end "$SP_PRODUCTION_END")"
    record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "sp500_membership" "$SP_EXEC" \
        "$EVIDENCE_DIR/pit_sp500_membership.json"

    GICS_EXEC="$(run_stage "as-of GICS free-source discovery" \
        uv run --no-sync python -m "$INGEST_CLI" gics)"
    record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "asof_gics_discovery" "$GICS_EXEC"
    echo

    # -- 4. verify each artifact BY ITS OWN TYPE ------------------------------
    # The CLI classifies each file and applies the MATCHING schema. An auxiliary
    # artifact (identity crosswalk, coverage report) is verified with its own
    # rules and is never parsed as qualification evidence - that mismatch is
    # what produced the false "missing a sha256 normalized_checksum" failure on
    # the owner host.
    if [ -d "$EVIDENCE_DIR" ]; then
        shopt -s nullglob
        local artifact
        for artifact in "$EVIDENCE_DIR"/*.json; do
            echo "== verifying $artifact =="
            # a tampered/stale artifact exits non-zero here; keep going to report all
            uv run --no-sync python -m "$CLI" verify --evidence "$artifact" || true
            sha256sum "$artifact" 2>/dev/null || shasum -a 256 "$artifact" 2>/dev/null || true
            echo
        done
        shopt -u nullglob
    fi

    # -- 5. run manifest + CURRENT-RUN capability report -----------------------
    echo "== run manifest =="
    local RUN_MANIFEST="$RUN_ROOT/$RUN_ID/run-manifest.json"
    uv run --no-sync python -m "$RUN_CLI" manifest --run-id "$RUN_ID" \
        --run-root "$RUN_ROOT" --started-at "$RUN_STARTED" || true
    echo

    echo "== capability report (CURRENT RUN ONLY) =="
    local STATUS
    set +e
    uv run --no-sync python -m "$RUN_CLI" report --manifest "$RUN_MANIFEST"
    STATUS=$?
    set -e

    echo
    echo "== SOURCE EXECUTION =="
    echo "(what each source ACTUALLY produced on this run; configuration state is"
    echo " never reported as a qualification result)"
    printf '%-28s %s\n' "IBKR" "$IBKR_EXEC"
    printf '%-28s %s\n' "SEC EDGAR" "$SEC_EXEC"
    printf '%-28s %s\n' "S&P PUBLIC ANNOUNCEMENTS" "$SP_EXEC"
    printf '%-28s %s\n' "FREE SECONDARY" "NOT_USED"
    printf '%-28s %s\n' "IDENTITY CROSSWALK" "$CROSSWALK_EXEC"
    printf '%-28s %s\n' "AS-OF GICS DISCOVERY" "$GICS_EXEC"
    echo
    echo "run_id             $RUN_ID"
    echo "run_manifest       $RUN_ROOT/$RUN_ID/run-manifest.json"
    echo "gateway_preflight  $IBKR_STATUS"
    echo "evidence_dir       $EVIDENCE_DIR"
    echo
    if [ -d "$EVIDENCE_DIR" ]; then
        echo "evidence checksums:"
        shopt -s nullglob
        local seal
        for seal in "$EVIDENCE_DIR"/*.json; do
            sha256sum "$seal" 2>/dev/null || shasum -a 256 "$seal" 2>/dev/null || true
        done
        shopt -u nullglob
        echo
    fi

    # -- 6. real snapshot attempt --------------------------------------------
    echo "== snapshot attempt =="
    set +e
    uv run --no-sync python -m "$SNAPSHOT_CLI" \
        --evidence-dir "$EVIDENCE_DIR" --run-manifest "$RUN_MANIFEST" \
        --out "$SNAPSHOT_OUT"
    local SNAP_RC=$?
    set -e
    local SNAP_STATUS="PARTIAL"
    if [ "$SNAP_RC" -eq 0 ]; then SNAP_STATUS="PASS"; fi
    if [ "$SNAP_RC" -ge 2 ]; then SNAP_STATUS="FAIL"; fi
    record_stage "$RUN_CLI" "$RUN_ID" "$RUN_ROOT" "snapshot" "$SNAP_STATUS" "$SNAPSHOT_OUT"
    echo "== run manifest (final, including the snapshot stage) =="
    uv run --no-sync python -m "$RUN_CLI" manifest --run-id "$RUN_ID"         --run-root "$RUN_ROOT" --started-at "$RUN_STARTED" || true
    if [ "$SNAP_RC" -ne 0 ]; then
        echo "snapshot_readiness is below BACKTEST_ELIGIBLE (expected while any"
        echo "required capability is blocked). Readiness is NEVER forced here."
    fi
    echo

    if [ "$STATUS" -eq 0 ]; then
        echo "RESULT: full_dataset_contract_ready = TRUE"
        echo "A real DatasetSnapshot may now be built; readiness is still computed by"
        echo "the builder gate (never overridden here)."
    else
        echo "RESULT: full_dataset_contract_ready = FALSE (expected while cells are blocked)"
        echo "No snapshot can be BACKTEST_ELIGIBLE until every required capability has"
        echo "real, authenticated evidence."
    fi
    exit "$STATUS"
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
