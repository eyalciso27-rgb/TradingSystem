#!/usr/bin/env bash
# Dataset V1 — real historical source qualification (owner-run, fail-closed).
#
# ONE command for the owner host. It:
#   1. preflights the toolchain and repository layout;
#   2. DISCOVERS whether a real historical source is configured;
#   3. verifies every existing evidence artifact (recomputing its checksum);
#   4. prints PASS/PARTIAL/FAIL and the capability state for each of the four
#      external Dataset V1 capabilities;
#   5. prints the resulting readiness and the evidence paths/checksums.
#
# It never prints credentials, never fabricates evidence and never promotes a
# capability by itself: promotion happens only through the authenticated
# evidence produced by a configured REAL source.
#
# SCOPE NOTE (read this before believing the SEC line): this runner reports
# whether SEC access is CONFIGURED. It performs no SEC request, no SEC
# ingestion and no SEC qualification. See "SEC EDGAR" below.
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
    # CONFIGURATION CHECK ONLY. This runner issues no SEC request.
    local SEC_STATUS="NOT_CONFIGURED"
    if [ -n "${SEC_EDGAR_USER_AGENT:-}" ]; then
        SEC_STATUS="USER_AGENT_CONFIGURED"
    else
        echo "SEC EDGAR: SEC_EDGAR_USER_AGENT is not set."
        echo "  SEC automated access requires a real contact, e.g.:"
        echo "  export SEC_EDGAR_USER_AGENT='Your Name your.email@example.com'"
    fi
    echo "SEC EDGAR: $SEC_STATUS (configuration only; this runner performs NO SEC"
    echo "  request, NO ingestion and NO qualification)"
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

    # -- 4. verify each evidence artifact individually ------------------------
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

    # -- 5. consolidated report + readiness -----------------------------------
    echo "== capability report =="
    local STATUS
    set +e
    uv run --no-sync python -m "$CLI" report \
        --config "$SOURCE_CONFIG" --evidence-dir "$EVIDENCE_DIR"
    STATUS=$?
    set -e

    echo
    echo "== SOURCE QUALIFICATION =="
    echo "(these are ACCESS/CONFIGURATION states, not capability qualification;"
    echo " capability states come from the authenticated evidence above)"
    printf '%-28s %s\n' "IBKR (OWNER_PRIMARY)" "$IBKR_STATUS"
    printf '%-28s %s\n' "SEC EDGAR (OFFICIAL)" "$SEC_STATUS (not exercised)"
    printf '%-28s %s\n' "S&P PUBLIC ANNOUNCEMENTS" "NOT_CONFIGURED"
    printf '%-28s %s\n' "FREE SECONDARY" "NOT_USED"
    echo
    echo "evidence_dir  $EVIDENCE_DIR"
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
