# POI-IBKR-001 — Readiness Report (prepared to the owner-run point)

Status: **PREPARED — NOT RUN.** POI-IBKR-001 stays **BLOCKED** in
`docs/poi/STATUS.yaml` until the owner's manual run passes. Nothing in this
report changes any PoI status.

Canonical record (frozen `POI_BACKLOG.yaml`): *"IBKR Paper execution — prove
account, orders, protection, fills and reconciliation in Paper."*
**PASS:** no duplicate orders; protection follows fills; normalized events;
recovery deterministic; host decision documented.
**FAIL:** blind retry, unprotected fill, unstable gateway, or domain leakage
of IBKR fields.

## 1. Gap analysis — covered vs. uncovered

### Covered already (code + fakes-only proof)

| Requirement | Where proven |
|---|---|
| Connect TWS/IB Gateway | Host: Gateway 10.48 + IBC auto-login + systemd (operational, done 2026-07-15); read-only PoI PASS 2026-07-10 |
| Account snapshot | `POI-IBKR-READONLY` PASS (masked DU account); `FakeBroker.get_account_snapshot` conformance |
| Broker port + conformance | `trading_broker_core` port; conformance suite passes for FakeBroker **and** `IbkrPaperAdapter` |
| Submit → ack | `tests/poi/test_paper_manual_lifecycle.py` scenarios 3–4 (real `ExecutionAgent` over `FakeBroker`) |
| No duplicate orders (DoD) | Scenario 12: duplicate queue delivery → same outcome, one broker order; broker-side idempotency by `client_order_id` |
| Protection follows fills (DoD) | Scenarios 5–8: partial→full fill, stop+target (bracket), `unprotected_positions() == ()` |
| Cancel / replace | Scenario 9: cancel confirmed before replacement; unknown cancel leg → replacement refused (fail closed) |
| Disconnect / reconnect | Scenario 10: terminal failure while down (no blind retry), clean reconciliation after reconnect |
| Unknown-state reconciliation | Scenario 13 + agent's `NEEDS_REVIEW` on `UNKNOWN_BROKER_RESPONSE`; engine recommends, never acts |
| Broker rejection | Scenario 14: terminal, never retried |
| Protection capability failure | Scenario 15: loud `NEEDS_REVIEW` + `POSITION_WOULD_BE_UNPROTECTED` + entry block via reconciliation |
| Restart during lifecycle | Scenario 16: watchdog restart gate honors `order-lifecycle.active`; state rebuild via startup reconciliation |
| No new entry after restart until reconciliation (entry latch) | Scenario 17: the ExecutionAgent's required `ReconciliationLatch` refuses a new entry after a broker-session rotation until a CURRENT-session account-safety CLEAN clears it; a stale pre-restart CLEAN cannot clear it; zero new/duplicate orders across restart; cancel/protective paths are never gated |
| Single-use authorization | Cross-cutting test: a consumed `SubmitAuthorization` cannot be spent twice |
| No IBKR leakage in domain | Architecture guardrails (ibapi only inside `broker-ibkr`) — green in CI |

### NOT covered — requires the real owner-run (or later work)

| Gap | Why it needs the owner-run / follow-up |
|---|---|
| Real IBKR Paper submit/ack/fill/cancel | Only the live Gateway proves IBKR's actual behavior (order ids, statuses, timing) |
| Real partial fill | Cannot be forced deterministically on Paper; observed opportunistically or documented honestly as not reproduced |
| Real disconnect/reconnect mid-order | Requires the live Gateway session |
| Commission/fill reports normalization | Real `commissionReport` / execution details arrive only from IBKR |
| ~~**Realtime market data**~~ | **CLEARED 2026-07-21** — realtime sub-gate PASS (`evidence/POI-MD-001-REALTIME.md`); per [ADR-0016](../adr/ADR-0016-poi-ibkr-001-market-data-dependency.md) that satisfies the market-data dependency for this PoI. POI-MD-001 aggregate stays BLOCKED |
| ~~Owner-run CLI~~ | **BUILT (WP-A3, not run):** `scripts/poi_ibkr_lifecycle.py` reuses ExecutionAgent + ProtectionManager + ReconciliationEngine over `BrokerPort`; localhost/4002-only with an explicit LIVE-port refusal, fixed instrument allowlist, ≤1 share and ≤$50, BUY+LIMIT entries only, dry-run-first with a parameter-bound confirmation token, fail-closed with cleanup, masked output. Proven with fakes only (`tests/poi/test_poi_ibkr_lifecycle_cli.py`), incl. the pre-merge safety review: rejected protection, partial-fill remainder, surprise cancel-path fill, unknown broker state, cross-day token/order-id scoping, protective-leg envelope |
| Watchdog deep-probe dependency + `gatewaystart.sh` patches back into repo | Operational fixes made on the host (version/config/java paths, systemd drop-ins) must be folded into the repo scripts (separate PR) |
| Paper-account evidence | Masked account snapshot, order ids, state transitions, reconciliation transcript — only from the real run |

## 2. Owner-run steps (prepared — **not run**)

1. ~~Owner approves opening POI-IBKR-001 and decides the POI-MD-001
   dependency~~ — done: [ADR-0016](../adr/ADR-0016-poi-ibkr-001-market-data-dependency.md).
2. ~~Build the owner-run CLI~~ — done (WP-A3): `scripts/poi_ibkr_lifecycle.py`,
   `--scenario preflight|dry-run|cancel-path|fill-path|reconnect|cleanup`.
3. **Next (owner):** run the seven-step sequence in
   [`docs/runbooks/POI_IBKR_001_OWNER_RUN.md`](../runbooks/POI_IBKR_001_OWNER_RUN.md)
   — exact commands, preconditions, abort paths and PASS/FAIL criteria.
4. Evidence recorded to `docs/poi/evidence/POI-IBKR-001.md`; owner marks PASS.

Until step 3 produces real evidence, POI-IBKR-001 is **BLOCKED / NOT RUN**.

> **Entry-reconciliation latch (post the bounded-recovery merge).** On the
> owner host the running Gateway rotates a broker-session token on each start,
> and `poi_ibkr_lifecycle.py` wires the ExecutionAgent's required
> `ReconciliationLatch`. A fill/cancel **execute** run is therefore refused
> (`RECONCILIATION_REQUIRED`) until the owner obtains a CURRENT-session
> account-safety **CLEAN** — the read-only
> `scripts/ib_gateway_watchdog.py --status --deep-probe` from the same
> deferred-run manifest — which clears the latch for that session. This is the
> intended "no new orders after restart until reconciliation" behavior; it
> sends and changes nothing.

## 3. Required evidence

**Realtime evidence — obtained 2026-07-21** (`evidence/POI-MD-001-REALTIME.md`):
`marketDataType=1`, valid positive bid/ask/last with sizes, clean disconnect.
At fill time the owner still records the quote context (limit vs. ask, fill
price sanity) in the run evidence.

**Paper-account evidence (owner-run):** masked account id (`DU*****xx`),
account snapshot before/after, order id + full normalized state transitions
(SUBMITTING→ACKNOWLEDGED→FILLED/CANCELLED), protection order visible at
broker after the fill, reconciliation transcript (clean + one induced
mismatch), gateway/IBC/adapter versions.

## 4. Rollback / abort procedure

- **Abort at any step:** `--scenario cleanup`; if the harness is dead, cancel
  manually in the Gateway UI (VNC). Verify with `--scenario reconnect`.
- **Position left over:** close manually in the Gateway UI (never from
  code; flatten stays hard-disabled). Re-run `reconnect` until clean.
- **Gateway unstable:** stop the run (that is a FAIL criterion), keep logs.
- **Anything UNKNOWN:** stop; reconciliation before any further action —
  never a blind resend.

## 5. PASS / FAIL criteria (owner marks; verbatim from the frozen backlog)

PASS requires **all**: zero duplicate orders across every scenario incl.
forced re-delivery; every fill protected at the broker within the run;
events normalized to canonical states (no raw IBKR statuses in evidence);
deterministic recovery after disconnect/restart via reconciliation; host
decision documented. Any blind retry, unprotected fill, unstable gateway or
IBKR-field leakage ⇒ FAIL.

## 6. What remains blocked on what

| Blocker | Items |
|---|---|
| ~~Realtime window~~ | **cleared** 2026-07-21 (realtime sub-gate PASS; ADR-0016) |
| ~~Owner-run CLI~~ | **built** (WP-A3), fakes-only proof; never executed |
| **Owner execution (only remaining blocker)** | the seven-step run in `docs/runbooks/POI_IBKR_001_OWNER_RUN.md`; the evidence file; the PASS mark; and therefore IMP-1302 |
| **Not blocked** | the fakes-only matrix (16 scenarios + 66 harness drills, green); runbooks (`PAPER_MANUAL_ONE_TRADE.md`, `POI_IBKR_001_OWNER_RUN.md`) |
