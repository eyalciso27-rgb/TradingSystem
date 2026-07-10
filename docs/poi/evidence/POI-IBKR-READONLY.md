# POI-IBKR-READONLY — Evidence

**Status: NOT RUN** (template only — nothing below is filled in, nothing is
claimed as PASS).

Scope: read-only infrastructure pre-check for POI-IBKR-001 per the PoI plan
v2 — first attempt on Oracle Linux ARM64 with the **official IBKR installer
only**. This PoI proves the gateway runs, paper login works, and account /
positions can be read. It never places, modifies or cancels an order and it
does not mark POI-IBKR-001 (full paper execution) as PASS.

Safety preconditions (all mandatory before running the harness):

- Gateway logged in to **PAPER** only; LIVE is never used.
- Gateway **Read-Only API = ON**.
- API bound to **127.0.0.1:4002** only; no firewall/security-list ports
  opened.
- Credentials typed only in the Gateway UI by the owner; never in code,
  ENV, logs or this file. Account ids appear **masked only** (`DU*****xx`).

## 1. Installer provenance (owner fills in)

| Field | Value |
|---|---|
| Official IBKR download URL used | _(must be the official IBKR site)_ |
| Installer filename | `ibgateway-latest-standalone-linux-arm.sh` _(or exact stable name)_ |
| Installer checksum (sha256) | |
| Download date | |

## 2. Host + runtime architecture

| Field | Value |
|---|---|
| Host | Oracle Linux ARM64 _(fallback used? which, why)_ |
| Host architecture (`uname -m`) | _(expected: aarch64)_ |
| Bundled JVM architecture (`jvm/bin/java -version` os.arch) | _(expected: aarch64 native, NOT emulated x86_64)_ |
| IB Gateway version | |

## 3. Gateway configuration proof

| Check | Evidence | Result |
|---|---|---|
| Paper mode login (manual, 2FA) | _(screenshot ref / note; no credentials)_ | |
| Read-Only API = ON | _(settings screenshot ref)_ | |
| Port binding 127.0.0.1:4002 only | _(`ss -ltn` / `netstat` output showing 127.0.0.1:4002, nothing on 0.0.0.0)_ | |
| Live API port not in use | _(same output: no listener on the live port)_ | |

## 4. Harness run (scripts/poi_ibkr_readonly.py)

Run command (manual, on the Oracle ARM64 host via PuTTY, after the owner has
logged in to PAPER in the Gateway VNC session and enabled Read-Only API):

```bash
uv run python scripts/poi_ibkr_readonly.py \
  --host 127.0.0.1 --port 4002 \
  --session-module trading_broker_ibkr.poi_readonly_session
```

Paste the harness checklist output here (it is already sanitized/masked):

```text
(not run)
```

| Check | Result |
|---|---|
| API health (req_current_time) | |
| Paper account id prefix `DU` (masked, e.g. `DU*****67`) | |
| Account snapshot read-only (fields present; values withheld) | |
| Positions read-only (count only) | |
| Clean disconnect | |

## 5. No-order proof

| Check | Evidence | Result |
|---|---|---|
| Harness allowlist enforced (connect/current time/account summary/positions/disconnect only) | harness source + tests | |
| Gateway API log contains no order/cancel/modify requests | _(log excerpt ref)_ | |
| No secrets in this file or in harness output | reviewed | |

## 6. Verdict

| Field | Value |
|---|---|
| PASS / FAIL | **NOT RUN** |
| Run date | |
| Operator | owner (manual) |
| Notes / follow-ups (host ADR input) | |
