# Phase-13 Deferred Owner-Run Ledger

> Canonical ledger of every Phase-13 run that **could not be performed** in a
> no-broker, market-closed, non-host environment. It mirrors the
> machine-readable manifest `PHASE_13_DEFERRED_OWNER_RUNS.yaml` (schema-tested
> by `tests/operations/test_deferred_manifest.py`).
>
> **No broker connection was made and no order was sent, cancelled or modified
> while producing this ledger.** Every command below is a *template* for the
> owner to run later on the host; nothing here was executed.
>
> **The harness never flattens.** Any open position must be closed by the owner
> by hand in the IB Gateway / TWS UI. Nothing here changes
> `docs/poi/STATUS.yaml`; **POI-IBKR-001 remains BLOCKED / NOT RUN** until the
> owner records real evidence.
>
> **Prices are placeholders.** `<CURRENT_PRICE>`, `<FAR_BELOW_MARKET>` and
> `<MARKETABLE_LIMIT>` must each be taken from a **fresh positive quote** at run
> time — never an invented or stale number.

## Failed owner-run attempts (recorded evidence — both are FAILURES)

Two real owner-run cancel-path attempts were made on the Paper Gateway and
**both failed**. Neither is a PASS; POI-IBKR-001 stays BLOCKED / NOT RUN.

| Attempt | Result | Classification |
|---|---|---|
| Read-Only API enabled during a submit scenario | The Gateway's Read-Only API blocked the order path | **FAILED owner-run evidence** — Read-Only API must be **disabled** for submitting scenarios (it is only enabled for the read-only market-data harness) |
| Read-Only API disabled, cancel-path execute | `ERROR 1 … 10052 Invalid time in force:Empty` — the IBKR `Order` reached the API with an **empty TIF**. The harness then correctly failed closed: `UNKNOWN_BROKER_STATE`, `RECONCILE_BEFORE_ANY_RETRY` | **FAILED owner-run evidence** — root cause was the adapter never setting `order.tif`; fixed in code (D-TIF-01). **No retry/resend was performed and none is permitted after UNKNOWN state.** |

**MANDATORY before any next execution:** run the read-only reconciliation
**DEF-16** below (operation reference `DCCA3A59ED4FBE87`) and confirm the
account is clean (no open POI order, no F position, no unexpected working F
order, clean disconnect). Do **not** re-run any `--execute` scenario until that
reconciliation passes.

## Legend

| Placeholder | Meaning |
|---|---|
| `<ALIAS>` | the configured account alias (never a `DU` id) |
| `<BROKER_MODULE>` | `trading_broker_ibkr.paper_adapter` |
| `<CURRENT_PRICE>` | a fresh positive quote for the symbol at run time |
| `<FAR_BELOW_MARKET>` | a limit ≥ 20 % below `<CURRENT_PRICE>` (cannot fill) |
| `<MARKETABLE_LIMIT>` | at/just above the ask, ≤ $50 notional (fills) |
| `<TOKEN>` | the confirmation token printed by the matching dry-run |
| `<RUN_REF>` | the operation reference printed in the fill-path evidence (NOT the token) |
| `<YYYYMMDD>` | the UTC run date |

Blocker vocabulary: `MARKET_CLOSED`, `OWNER_HOST_REQUIRED`,
`IBKR_PAPER_EXECUTION_REQUIRED`, `IB_GATEWAY_RESTART_REQUIRED`,
`MARKET_DATA_WINDOW_REQUIRED`, `EXTERNAL_PROVIDER_DEFERRED`,
`EXTERNAL_INFRASTRUCTURE_NOT_IMPLEMENTED`.

---

## DEF-01-cancel-path-dry-run — IMP-1302 / POI-IBKR-001
- **Blocker:** `OWNER_HOST_REQUIRED`
- **Sends / cancels / modifies an order:** no (what-if preview only)
- **Owner confirmation required:** no
- **Prerequisites:** IB Gateway Paper up on the host (Read-Only API **disabled**);
  `SECRETS_ROOT` exported with `$SECRETS_ROOT/PAPER/ibkr/paper-account-id`; a
  fresh quote to choose `<FAR_BELOW_MARKET>`.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario cancel-path --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --quantity 1 --limit-price <FAR_BELOW_MARKET> \
    --reference-price <CURRENT_PRICE> \
    --evidence-out /tmp/poi-ibkr-001-cancel-dryrun-<YYYYMMDD>.md
  ```
- **Expected safe result:** what-if preview succeeds; `NOTHING WAS SENT`; an
  operation reference is printed.
- **Abort criteria:** preflight fails; any submit attempted; harness prints `REFUSED`.
- **Evidence:** `/tmp/poi-ibkr-001-cancel-dryrun-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `DRY_RUN_RECORDED`
- **Cleanup:** none (nothing is sent). **The harness never flattens.**

## DEF-02-cancel-path-execute — IMP-1302 / POI-IBKR-001
- **Blocker:** `IBKR_PAPER_EXECUTION_REQUIRED`
- **Sends / cancels / modifies an order:** **yes** (submits then cancels)
- **Owner confirmation required:** **yes** (`--confirm <TOKEN>`)
- **Prerequisites:** DEF-01 completed; `<TOKEN>` is that dry-run's token; the
  resting limit is ≥ 20 % below the market so it **cannot fill**.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario cancel-path --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --quantity 1 --limit-price <FAR_BELOW_MARKET> \
    --reference-price <CURRENT_PRICE> --execute --confirm <TOKEN> \
    --evidence-out /tmp/poi-ibkr-001-cancel-execute-<YYYYMMDD>.md
  ```
- **Expected safe result:** submit → `ACKNOWLEDGED` → cancel → `CANCELLED` →
  `never_filled` → clean reconciliation.
- **Abort criteria:** any fill event; cancel not confirmed; `UNKNOWN` broker
  state (**do not resend** — inspect the UI).
- **Evidence:** `/tmp/poi-ibkr-001-cancel-execute-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `CANCEL_PATH_PASS`
- **Cleanup:** if the order did not cancel, cancel it by hand in the Gateway UI,
  then re-run preflight. **The harness never flattens.**

## DEF-03-fill-path-dry-run — IMP-1302 / POI-IBKR-001
- **Blocker:** `OWNER_HOST_REQUIRED`
- **Sends / cancels / modifies an order:** no (what-if preview only)
- **Owner confirmation required:** no
- **Prerequisites:** account flat (DEF-02 cleaned up); a fresh quote to choose
  `<MARKETABLE_LIMIT>` (≤ $50 notional).
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario fill-path --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --quantity 1 --limit-price <MARKETABLE_LIMIT> \
    --evidence-out /tmp/poi-ibkr-001-fill-dryrun-<YYYYMMDD>.md
  ```
- **Expected safe result:** what-if preview succeeds; `NOTHING WAS SENT`;
  operation reference printed.
- **Abort criteria:** preflight fails; any submit attempted.
- **Evidence:** `/tmp/poi-ibkr-001-fill-dryrun-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `DRY_RUN_RECORDED`
- **Cleanup:** none. **The harness never flattens.**

## DEF-04-fill-path-execute — IMP-1302 / POI-IBKR-001
- **Blocker:** `IBKR_PAPER_EXECUTION_REQUIRED`
- **Requires an open market:** **yes** (a marketable limit needs an open market)
- **Sends / cancels / modifies an order:** **yes** (submits, fills, protects)
- **Owner confirmation required:** **yes**
- **Prerequisites:** US market **open**; DEF-03 completed; `<TOKEN>` is that
  dry-run's token; **record the printed operation reference** for DEF-07.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario fill-path --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --quantity 1 --limit-price <MARKETABLE_LIMIT> --execute --confirm <TOKEN> \
    --evidence-out /tmp/poi-ibkr-001-fill-execute-<YYYYMMDD>.md
  ```
- **Expected safe result:** submit → `FILLED` → position reported → protective
  `STOP` submitted → no unprotected position.
- **Abort criteria:** a fill without accepted protection; `UNKNOWN` state; any
  short or cross-symbol position (**fail closed**).
- **Evidence:** `/tmp/poi-ibkr-001-fill-execute-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `FILL_PATH_PASS`
- **Cleanup:** leaves an **open position + stop**; close them by hand in the
  Gateway UI after DEF-07/08. **The harness never flattens.**

## DEF-05-protection-verification — IMP-1302 / POI-IBKR-001
- **Blocker:** `IBKR_PAPER_EXECUTION_REQUIRED`
- **Requires an open market:** yes · **mutates broker state:** yes ·
  **owner confirmation:** yes
- **Prerequisites:** verified from the DEF-04 run; **confirm in the Gateway UI
  that the stop quantity covers the FULL position** (the receipt cannot prove
  quantity).
- **Command:** (same executed fill-path run as DEF-04)
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario fill-path --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --quantity 1 --limit-price <MARKETABLE_LIMIT> --execute --confirm <TOKEN> \
    --evidence-out /tmp/poi-ibkr-001-protection-<YYYYMMDD>.md
  ```
- **Expected safe result:** exactly one protective `STOP` resting; `SELL`;
  quantity = position; stop below entry.
- **Abort criteria:** stop rejected/absent; quantity mismatch.
- **Evidence:** `/tmp/poi-ibkr-001-protection-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `PROTECTION_VERIFIED`
- **Cleanup:** cancel the stop and close the position by hand. **Never flattens.**

## DEF-06-gateway-restart — IMP-1303 / POI-IBKR-001
- **Blocker:** `IB_GATEWAY_RESTART_REQUIRED`
- **Sends / cancels / modifies an order:** no · **owner confirmation:** yes
- **Prerequisites:** DEF-04 position + stop still open; restart the Gateway on
  the host (`systemctl restart ibgateway`), wait for login.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario preflight --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --evidence-out /tmp/poi-ibkr-001-restart-preflight-<YYYYMMDD>.md
  ```
- **Expected safe result:** after restart, preflight reconnects; the position
  and stop are still reported.
- **Abort criteria:** Gateway does not return; API unavailable; login loop.
- **Evidence:** `/tmp/poi-ibkr-001-restart-preflight-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `RESTART_OBSERVED`
- **Cleanup:** proceed to DEF-07/08/09; position stays open until manual close.
  **The harness never flattens.**

## DEF-07-reconnect-operation-reference — IMP-1303 / POI-IBKR-001
- **Blocker:** `IB_GATEWAY_RESTART_REQUIRED`
- **Sends / cancels / modifies an order:** no · **owner confirmation:** yes
- **Prerequisites:** `<RUN_REF>` is the operation reference printed in the
  DEF-04 evidence. The adapter **orderRef persistence + open-order recovery fix
  (D-ADAPTER-01) is implemented and contract-tested**; its empirical round-trip
  against a real Gateway is **DEF-15** below.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario reconnect --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --run-ref <RUN_REF> \
    --evidence-out /tmp/poi-ibkr-001-reconnect-<YYYYMMDD>.md
  ```
- **Expected safe result:** exactly one matching working stop for the
  operation; no naked position; clean reconciliation.
- **Abort criteria:** missing/wrong `--run-ref`; naked position; ambiguous or
  foreign stop; a short position.
- **Evidence:** `/tmp/poi-ibkr-001-reconnect-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `RECONNECT_VERIFIED`
- **Cleanup:** proceed to DEF-09; close the position by hand. **Never flattens.**

## DEF-08-reconciliation — IMP-1303 / POI-IBKR-001
- **Blocker:** `OWNER_HOST_REQUIRED`
- **Sends / cancels / modifies an order:** no · **owner confirmation:** yes
- **Prerequisites:** reconciliation is recorded as part of DEF-07; this captures
  its result.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario reconnect --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --run-ref <RUN_REF> \
    --evidence-out /tmp/poi-ibkr-001-reconciliation-<YYYYMMDD>.md
  ```
- **Expected safe result:** `reconciled=true`; no blocking findings; protected
  position matches.
- **Abort criteria:** `POSITION_MISMATCH` / `PROTECTION_MISMATCH` /
  `UNEXPECTED_BROKER_ORDER`.
- **Evidence:** `/tmp/poi-ibkr-001-reconciliation-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `RECONCILIATION_VERIFIED`
- **Cleanup:** proceed to DEF-09. **The harness never flattens.**

## DEF-09-cleanup — IMP-1302 / POI-IBKR-001
- **Blocker:** `IBKR_PAPER_EXECUTION_REQUIRED`
- **Sends / cancels / modifies an order:** **yes** (cancels PoI orders only) ·
  **owner confirmation:** yes
- **Prerequisites:** runs after DEF-07/08; cancels only orders whose id starts
  with `poi-ibkr-001`.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario cleanup --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --evidence-out /tmp/poi-ibkr-001-cleanup-<YYYYMMDD>.md
  ```
- **Expected safe result:** every PoI order cancelled; reconciliation clean.
- **Abort criteria:** a cancel is refused; an unexpected order remains.
- **Evidence:** `/tmp/poi-ibkr-001-cleanup-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `CLEANUP_VERIFIED`
- **Cleanup:** cleanup cancels **orders only** — it **NEVER flattens**. Close
  the 1-share position by hand in the Gateway/TWS UI, then re-run preflight
  until flat and clean.

## DEF-10-market-data-open-session — IMP-1303 / POI-MD-001
- **Blocker:** `MARKET_DATA_WINDOW_REQUIRED`
- **Requires an open market:** yes · **sends an order:** no
- **Prerequisites:** US market open; realtime entitlement window; Read-Only API
  **enabled** for the read-only harness.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_market_data_readonly.py \
    --host 127.0.0.1 --port 4002 --symbol F --market-data-mode realtime \
    --session-module trading_broker_ibkr.poi_market_data_session
  ```
- **Expected safe result:** `marketDataType=1` REALTIME; valid positive
  bid/ask/last; clean disconnect. Prices are captured live — **never invented**.
- **Abort criteria:** entitlement error; delayed downgrade; invalid/negative prices.
- **Evidence:** `/tmp/poi-md-001-open-session-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `MD_OPEN_SESSION_RECORDED`
- **Cleanup:** none (read-only; no order surface). **Never flattens.**

## DEF-11-owner-run-host-chaos-drills — IMP-1303 / POI-IBKR-001
- **Blocker:** `IB_GATEWAY_RESTART_REQUIRED`
- **Sends an order:** no (destructive host actions) · **owner confirmation:** yes
- **Prerequisites:** the destructive host drills in
  `docs/runbooks/CHAOS_RECOVERY_DRILLS.md` §4; requires an open lifecycle
  (DEF-04) to exercise the watchdog reconciliation gate.
- **Command:** (post-restart preflight capture)
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario preflight --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --evidence-out /tmp/poi-ibkr-001-hostchaos-<YYYYMMDD>.md
  ```
- **Expected safe result:** a Gateway kill inhibits restart while
  `order-lifecycle.active`; recovery is a reconciliation, never a blind restart.
- **Abort criteria:** any blind restart during an active lifecycle; a naked position.
- **Evidence:** `/tmp/poi-ibkr-001-hostchaos-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `HOST_CHAOS_RECORDED`
- **Cleanup:** DEF-09 cleanup then manual position close. **Never flattens.**

## DEF-12-ibc-watchdog-host-evidence — IMP-1304 / POI-PLAT-ARM-001
- **Blocker:** `OWNER_HOST_REQUIRED`
- **Sends an order:** no
- **Prerequisites:** IBC auto-login installed under systemd; host patches
  upstreamed to the repo.
- **Command:**
  ```bash
  uv run --no-sync python scripts/ib_gateway_watchdog.py --status
  ```
- **Expected safe result:** watchdog reports IBC/systemd health; a host
  transcript is captured in the repo.
- **Abort criteria:** watchdog cannot read gateway state; systemd unit missing.
- **Evidence:** `/tmp/ib-gateway-watchdog-status-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `HOST_EVIDENCE_RECORDED`
- **Cleanup:** none (read-only status). **Never flattens.**

## DEF-13-watchdog-deep-probe-activation — IMP-1304 / POI-PLAT-ARM-001
- **Blocker:** `OWNER_HOST_REQUIRED`
- **Sends an order:** no
- **Prerequisites:** the broker python package present on the host so the deep
  probe can run.
- **Command:**
  ```bash
  uv run --no-sync python scripts/ib_gateway_watchdog.py --deep-probe --status
  ```
- **Expected safe result:** deep probe classifies API state correctly; no false
  restart.
- **Abort criteria:** deep probe crashes or triggers a spurious restart.
- **Evidence:** `/tmp/ib-gateway-watchdog-deepprobe-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `DEEP_PROBE_ACTIVATED`
- **Cleanup:** none (read-only status). **Never flattens.**

## DEF-15-orderref-roundtrip-verification — IMP-1304 / POI-IBKR-001
- **Blocker:** `IB_GATEWAY_RESTART_REQUIRED`
- **Sends / cancels / modifies an order:** no · **owner confirmation:** yes
- **Prerequisites:** the D-ADAPTER-01 code fix (orderRef persistence +
  `reqAllOpenOrders` recovery, implemented + contract-tested in this bundle); a
  resting stop from DEF-04 and a real Gateway restart (DEF-06).
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario reconnect --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --run-ref <RUN_REF> \
    --evidence-out /tmp/poi-ibkr-001-orderref-roundtrip-<YYYYMMDD>.md
  ```
- **Expected safe result:** the **real Gateway preserves `orderRef`**; a fresh
  process recovers the stop from `reqAllOpenOrders`; the operation reference
  round-trips; the broker-specific `orderRef` size is confirmed within
  `ORDER_REF_MAX_LEN`.
- **Abort criteria:** `orderRef` not preserved/truncated by the Gateway;
  recovery misses the stop.
- **Evidence:** `/tmp/poi-ibkr-001-orderref-roundtrip-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `ORDERREF_ROUNDTRIP_VERIFIED`
- **Cleanup:** DEF-09 cleanup then manual position close. **Never flattens.**

## DEF-16-readonly-reconciliation-after-failed-run — IMP-1304 / POI-IBKR-001
- **Blocker:** `OWNER_HOST_REQUIRED`
- **Sends / cancels / modifies an order:** no (read-only reconnect) ·
  **owner confirmation:** no
- **Prerequisites:** **MANDATORY** after the two failed cancel-path attempts
  (Read-Only API reject + empty-TIF reject) and **before ANY next execution**.
- **Command:**
  ```bash
  uv run --no-sync python scripts/poi_ibkr_lifecycle.py \
    --scenario reconnect --account-alias <ALIAS> --broker-module <BROKER_MODULE> \
    --symbol F --run-ref DCCA3A59ED4FBE87 \
    --evidence-out /tmp/poi-ibkr-001-reconcile-after-failure-<YYYYMMDD>.md
  ```
- **Expected safe result:** **no open POI order for operation reference
  `DCCA3A59ED4FBE87`**; **no F position**; **no unexpected working F order**;
  clean reconciliation; **clean disconnect**.
- **Abort criteria:** any open POI/F order, any F position, or an unclean
  reconciliation → resolve before proceeding.
- **Evidence:** `/tmp/poi-ibkr-001-reconcile-after-failure-<YYYYMMDD>.md`
- **Status before → after PASS:** `NOT_RUN` → `ACCOUNT_CONFIRMED_CLEAN`
- **Cleanup:** if any POI order remains → DEF-09 cleanup; if any F position
  exists → close it by hand in the Gateway UI. **The harness never flattens.**

## DEF-14-external-provider-benchmark — IMP-1303 / POI-MD-001
- **Blocker:** `EXTERNAL_PROVIDER_DEFERRED`
- **Sends an order:** no · **owner confirmation:** yes
- **Prerequisites:** an owner-approved external market-data provider (**none
  selected** — deferred by Track A / A2); a provider ADR, benchmark plan,
  outage/fallback plan, field-ownership matrix.
- **Command:** (IBKR realtime capture is the interim reference only; the
  external benchmark is not runnable until a provider exists)
  ```bash
  uv run --no-sync python scripts/poi_ibkr_market_data_readonly.py \
    --host 127.0.0.1 --port 4002 --symbol F --market-data-mode realtime \
    --session-module trading_broker_ibkr.poi_market_data_session
  ```
- **Expected safe result:** **not runnable** until a provider is selected;
  POI-MD-001 aggregate stays **BLOCKED**.
- **Abort criteria:** any attempt to select/integrate a provider without owner
  approval.
- **Evidence:** `/tmp/poi-md-001-provider-benchmark-<YYYYMMDD>.md`
- **Status:** `DEFERRED` (unchanged by this bundle).
- **Cleanup:** none. **Never flattens.**

---

## Ordering for a full owner-run session

`DEF-01 → DEF-02 → (clean up) → DEF-03 → DEF-04 (market open) → DEF-05 →
DEF-06 → DEF-07 → DEF-08 → DEF-09 → close position by hand`. DEF-10 runs during
any open-market window. DEF-11–13 are host-only. DEF-14 stays deferred.

**Every one of these is BLOCKED today** on the market being open, the owner
host, IB Gateway, real Paper execution, or the deferred provider decision. None
were performed. **POI-IBKR-001 remains BLOCKED / NOT RUN.**
