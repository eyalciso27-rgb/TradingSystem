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
| Single-use authorization | Cross-cutting test: a consumed `SubmitAuthorization` cannot be spent twice |
| No IBKR leakage in domain | Architecture guardrails (ibapi only inside `broker-ibkr`) — green in CI |

### NOT covered — requires the real owner-run (or later work)

| Gap | Why it needs the owner-run / follow-up |
|---|---|
| Real IBKR Paper submit/ack/fill/cancel | Only the live Gateway proves IBKR's actual behavior (order ids, statuses, timing) |
| Real partial fill | Cannot be forced deterministically on Paper; observed opportunistically or documented honestly as not reproduced |
| Real disconnect/reconnect mid-order | Requires the live Gateway session |
| Commission/fill reports normalization | Real `commissionReport` / execution details arrive only from IBKR |
| **Realtime market data** | Marketable-limit fill scenario needs live quotes — **blocked ~3 days on the realtime subscription window**; the cancel-path scenario (far-from-market DAY limit) can run on delayed data with owner care, but the recorded evidence should note the data mode |
| Owner-run CLI (`scripts/poi_ibkr_lifecycle.py`) | Not yet written — deliberately gated: it is the first code path that submits a real order; build it only under an explicit owner approval, wired to `IbkrPaperAdapter` with the same allowlist/DU/4002 gates as the read-only harnesses |
| Watchdog deep-probe dependency + `gatewaystart.sh` patches back into repo | Operational fixes made on the host (version/config/java paths, systemd drop-ins) must be folded into the repo scripts (separate PR) |
| Paper-account evidence | Masked account snapshot, order ids, state transitions, reconciliation transcript — only from the real run |

## 2. Owner-run steps (when approved — not now)

1. Owner approves opening POI-IBKR-001 (explicit message) and decides on the
   POI-MD-001 dependency (realtime evidence vs. documented deviation).
2. Build the owner-run CLI (separate PR): `scripts/poi_ibkr_lifecycle.py`
   with `--scenario snapshot|reject|cancel|fill|reconnect|reconcile`,
   localhost+4002+DU gates, masked output, one order at a time, no retry.
3. Owner runs scenario-by-scenario over PuTTY, pasting masked output here.
4. Evidence recorded to `docs/poi/evidence/POI-IBKR-001.md`; owner marks PASS.

### Exact commands (future, after the CLI PR is approved & merged)

```bash
# on the ARM64 host, Gateway up via IBC/systemd, VNC visible:
uv run python scripts/poi_ibkr_lifecycle.py --scenario snapshot
uv run python scripts/poi_ibkr_lifecycle.py --scenario reject     # no fill risk
uv run python scripts/poi_ibkr_lifecycle.py --scenario cancel     # far-limit DAY
uv run python scripts/poi_ibkr_lifecycle.py --scenario fill       # 1 share, marketable limit
uv run python scripts/poi_ibkr_lifecycle.py --scenario reconnect
uv run python scripts/poi_ibkr_lifecycle.py --scenario reconcile
```

## 3. Required evidence

**Realtime evidence (blocked ~3 days):** marketable-limit fill with
`marketDataType=1`, valid positive bid/ask/last at submit time, fill price
sanity vs. quote.

**Paper-account evidence (owner-run):** masked account id (`DU*****xx`),
account snapshot before/after, order id + full normalized state transitions
(SUBMITTING→ACKNOWLEDGED→FILLED/CANCELLED), protection order visible at
broker after the fill, reconciliation transcript (clean + one induced
mismatch), gateway/IBC/adapter versions.

## 4. Rollback / abort procedure

- **Abort at any step:** cancel the open order in the CLI; if the CLI is
  dead, cancel manually in the Gateway UI (VNC). Verify with `reconcile`.
- **Position left over:** close manually in the Gateway UI (never from
  code; flatten stays hard-disabled). Re-run `reconcile` until clean.
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
| **Realtime window (~3 days)** | fill-scenario evidence; POI-MD-001 realtime PASS |
| **Owner approval (explicit)** | building the owner-run CLI; any real submit; opening IMP-1302 |
| **Neither (ready now)** | everything in the fakes-only matrix (16 scenarios, green); runbook + checklists (`docs/runbooks/PAPER_MANUAL_ONE_TRADE.md`) |
