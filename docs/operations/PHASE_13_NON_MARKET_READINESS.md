# Phase-13 Non-Market Readiness Report

> Consolidated status of the Phase-13 items (IMP-1302 / IMP-1303 / IMP-1304)
> after the non-market completion bundle. It separates what is genuinely done
> without a market or host, what still requires the market/host, and what is
> deferred by owner decision.
>
> **No broker connection was made and no order was sent, cancelled or modified.**
> Nothing here marks any PoI PASS. **POI-IBKR-001 remains BLOCKED / NOT RUN.**

## 1. COMPLETED WITHOUT MARKET

### Code
- Owner-run lifecycle harness `scripts/poi_ibkr_lifecycle.py`:
  `preflight | dry-run | cancel-path | fill-path | reconnect | cleanup`, reusing
  `ExecutionAgent` + `ProtectionManager` + `ReconciliationEngine` over
  `BrokerPort`. Localhost/4002-only, LIVE-port refused, fixed instrument
  allowlist, ≤1 share / ≤$50, BUY+LIMIT entries, dry-run-first with a
  parameter-bound **confirmation token**, fail-closed with cleanup, masked
  output, and a separate **non-authorizing operation reference** embedded in
  order ids (the token is never embedded).
- Evidence artifact enriched (this bundle): records **intended parameters**,
  **entry broker order identity**, **fill identity**, and protection identity,
  so an owner can correlate the artifact with the real broker order.

### Local / CI tests (fakes only, no IBKR)
- `tests/poi/test_poi_ibkr_lifecycle_cli.py` — endpoint/instrument/size envelope,
  dry-run-first + token binding, per-operation reconnect stop matching
  (stale/foreign/cross-symbol/ambiguous/short all fail closed), duplicate
  suppression, partial/surprise fill protection, cleanup-no-flatten, operation
  reference vs confirmation token separation, and evidence-completeness.
- `tests/poi/test_paper_manual_lifecycle.py` — the 16-scenario end-to-end drill
  over the canonical `FakeBroker`.
- `tests/chaos/test_recovery_drills.py` — disconnect before/after accept, lost /
  delayed / duplicate callback, service restart, stale market/account data,
  UI-outage halt, reconciliation after restart, missing/wrong operation
  reference, ambiguous stop, short/cross-symbol position.
- `tests/db/test_chaos_recovery_db.py` — outbox atomicity, at-least-once
  redelivery with stable identity, DB-row idempotency, consumer-inbox
  idempotency, content-addressed dedup (PostgreSQL-gated; runs in CI).

### Static safety proofs
- `ruff`, full-workspace `mypy` (strict), architecture guardrails
  (`ibapi` confined to the adapter), baseline verification, gitleaks.
- Schema test `tests/operations/test_deferred_manifest.py` rejects unsafe
  deferred-run records (Live port/env, missing `uv run --no-sync`, execute
  without a confirmation placeholder, invented prices, Phase 14/15).

### Fake / contract drills
- All broker mechanics proven against `FakeBroker` and the persistence layer;
  no drill touches a real broker execution surface.

### Documentation
- `docs/operations/PHASE_13_DEFERRED_OWNER_RUNS.md` + `.yaml` (this bundle) —
  canonical deferred owner-run ledger and machine-readable manifest.
- `docs/runbooks/POI_IBKR_001_OWNER_RUN.md`, `PAPER_MANUAL_ONE_TRADE.md`,
  `CHAOS_RECOVERY_DRILLS.md` — host commands all use `uv run --no-sync`.
- `docs/operations/IMP_1304_DEFECT_REGISTER.md` — updated with the integrated
  audit findings.

### Defect closure (IMP-1304)
- The reconnect protection identity chain (D-01 → D-01d) is closed in code:
  per-operation matching bound to the non-authorizing operation reference, with
  the confirmation token kept authorization-only. No **open code P0/P1**
  remains after this bundle (see the defect register). One P1 finding
  (**D-ADAPTER-01**) is BLOCKED on broker connectivity — see §2.

## 2. NOT COMPLETED — REQUIRES MARKET OR OWNER HOST

Each maps to a record in `PHASE_13_DEFERRED_OWNER_RUNS.{md,yaml}`.

- **Actual cancel-path execution** — DEF-02 (`IBKR_PAPER_EXECUTION_REQUIRED`).
- **Actual fill-path execution** — DEF-04 (`IBKR_PAPER_EXECUTION_REQUIRED`,
  market open).
- **Actual protection placement** — DEF-05.
- **Actual Gateway restart** — DEF-06 (`IB_GATEWAY_RESTART_REQUIRED`).
- **Actual reconnect / reconciliation** — DEF-07 / DEF-08. **Blocked twice:**
  by the host requirement **and** by **D-ADAPTER-01** (the paper adapter does
  not set `order.orderRef` and `get_open_orders` returns only in-memory orders,
  so after a real Gateway restart a fresh reconnect invocation cannot recover
  the harness order id / operation reference from the live Gateway; that fix
  needs a broker-connected environment to implement and verify).
- **Actual cleanup** — DEF-09.
- **Market-hours quote evidence** — DEF-10 (`MARKET_DATA_WINDOW_REQUIRED`).
- **Host chaos drills** — DEF-11.
- **Watchdog / IBC host evidence** — DEF-12, DEF-13.

## 3. DEFERRED BY OWNER DECISION

- **External provider selection** — no provider approved (Track A / A2 owner
  decision); DEF-14.
- **Provider benchmark / fallback / ADR** — deferred with the provider decision.
- **NATS adapter** — `trading_event_bus` is a skeleton; the consumer-inbox
  idempotency contract it will use is proven at the persistence layer, but no
  NATS adapter is implemented and **no end-to-end NATS outage coverage is
  claimed** (`EXTERNAL_INFRASTRUCTURE_NOT_IMPLEMENTED`).
- **Phase 14** — frozen.
- **Phase 15** — frozen.

## 4. Explicit status statements

- **IMP-1302 is NOT complete.** All non-market Paper Manual work (workflow,
  tests, evidence fields, runbooks) is done; actual completion depends on the
  owner-run Paper lifecycle (DEF-01…DEF-09).
- **IMP-1303 owner-run is NOT complete.** The automated non-destructive drills
  are done; the destructive host drills (DEF-06, DEF-11) require the owner host
  and an open lifecycle.
- **IMP-1304 has no open code P0/P1 defect**, but one P1 (D-ADAPTER-01) is
  BLOCKED on broker connectivity, and **Phase 13 is still NOT complete** — its
  market/host-dependent items remain outstanding.
- **POI-IBKR-001 remains BLOCKED / NOT RUN** until the owner records real
  evidence; `docs/poi/STATUS.yaml` is unchanged.
- **Paper Auto and Live remain prohibited.**
