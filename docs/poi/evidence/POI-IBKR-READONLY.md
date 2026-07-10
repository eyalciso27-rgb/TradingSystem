# POI-IBKR-READONLY — Evidence

**Status: PASS** (recorded 2026-07-10; manual owner run).

Scope: read-only infrastructure pre-check for POI-IBKR-001 per the PoI plan
v2 — executed on Oracle Linux ARM64 with the official IBKR installer path.
This PoI proves the gateway runs, paper login works, and account /
positions can be read. It never places, modifies or cancels an order and it
does **not** mark POI-IBKR-001 (full paper execution) as PASS — that PoI
stays BLOCKED until its own order-lifecycle tests run.

Safety preconditions (all were mandatory and honored on the run):

- Gateway logged in to **PAPER** only; LIVE was never used.
- Gateway **Read-Only API = ON**.
- API bound to **127.0.0.1:4002** only; no firewall/security-list ports
  opened.
- Credentials typed only in the Gateway UI by the owner; never in code,
  ENV, logs or this file. Account ids appear **masked only** (`DU*****xx`).

## 1. Installer provenance (owner)

| Field | Value |
|---|---|
| Official IBKR download URL used | official IBKR downloads site (Linux ARM64 category) |
| Installer filename | `ibgateway-latest-standalone-linux-arm.sh` |
| Installer checksum (sha256) | _(owner may append)_ |
| Download date | 2026-07 (owner install) |

## 2. Host + runtime architecture

| Field | Value |
|---|---|
| Host | Oracle Linux ARM64 (no fallback needed) |
| Host architecture (`uname -m`) | aarch64 |
| Bundled JVM architecture | ARM64 build ran natively (gateway up and connected) |
| IB Gateway version | _(owner may append)_ |

## 3. Gateway configuration proof

| Check | Evidence | Result |
|---|---|---|
| Paper mode login (manual, 2FA) | Owner logged in via the Gateway VNC session; "API Server = connected", "Market Data Farm = ON" | PASS |
| Read-Only API = ON | Precondition confirmed by owner before the run | PASS |
| Port binding 127.0.0.1:4002 only | Harness endpoint gate connected on 127.0.0.1:4002; live port never used | PASS |
| Live API port not in use | Harness refuses any port other than 4002 by design | PASS |

## 4. Harness run (scripts/poi_ibkr_readonly.py)

Run command (manual, on the Oracle ARM64 host via PuTTY, after the owner
logged in to PAPER in the Gateway VNC session and enabled Read-Only API):

```bash
uv run python scripts/poi_ibkr_readonly.py \
  --host 127.0.0.1 --port 4002 \
  --session-module trading_broker_ibkr.poi_readonly_session
```

Harness checklist output (already sanitized/masked at the source):

```text
POI-IBKR-READONLY harness poi-ibkr-readonly-1.0.0
  [PASS] endpoint: 127.0.0.1:4002 (localhost + paper API port only)
  [PASS] connect: API socket connected
  [PASS] api_health: req_current_time answered
  [PASS] paper_account: account DU*****32
  [PASS] account_snapshot: net_liquidation/total_cash/buying_power present (values withheld from output)
  [PASS] positions_readonly: 1 position(s), count only
  [PASS] disconnect: clean disconnect
RESULT: PASS
(read-only allowlist enforced; no order API was ever callable)
```

| Check | Result |
|---|---|
| API health (req_current_time) | PASS |
| Paper account id prefix `DU` (masked: `DU*****32`) | PASS |
| Account snapshot read-only (fields present; values withheld) | PASS |
| Positions read-only (count only: 1) | PASS |
| Clean disconnect | PASS |

## 5. No-order proof

| Check | Evidence | Result |
|---|---|---|
| Harness allowlist enforced (connect / current time / account summary / positions / disconnect only) | harness + session + client source and tests; `ReadOnlyGuard` refuses everything else | PASS |
| No placeOrder / cancelOrder / reqGlobalCancel / submit invoked | no such surface exists in the harness, session or client (static-scan tests); harness printed "no order API was ever callable" | PASS |
| No LIVE, no PAPER_AUTO | PAPER login only; port 4002 only; harness is read-only and creates nothing | PASS |
| No secrets in this file or in harness output | account id masked (`DU*****32`); snapshot values withheld; no credentials anywhere | PASS |

## 6. Verdict

| Field | Value |
|---|---|
| PASS / FAIL | **PASS** |
| Run date | 2026-07-10 |
| Operator | owner (manual, via PuTTY; Gateway in VNC session) |
| Notes / follow-ups (host ADR input) | Official IBKR Linux ARM64 gateway runs natively on the Oracle ARM64 host — supports keeping this host in the future host ADR. POI-IBKR-001 (full paper execution incl. orders) remains BLOCKED and is a separate, explicitly-approved experiment. IMP-1002 may now be scheduled. |
