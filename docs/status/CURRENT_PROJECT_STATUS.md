# Current Project Status — Trading System

> Canonical status snapshot. The **repository is authoritative**; where this
> document and any chat summary disagree, the repo wins (see §17). Every claim
> here is verified against `main` at the commit below. Tests passing is **not**
> a PoI PASS; integration is never marked PASS without real masked evidence.

## 1. Snapshot

| Field | Value |
|---|---|
| Date | 2026-07-16 |
| `main` commit | `624343b` (Merge PR #34) |
| Baseline | `docs/baseline-v0.5/` |
| Canonical tasks (from `CLAUDE_CODE_TASKS.yaml`) | **82** |
| Completed canonical tasks | **72** (PHASE-0 … PHASE-12 = IMP-0001…IMP-1206, plus IMP-1301) |
| Completion | **72 / 82 = 87.8 %** |
| Current phase | **PHASE-13 — End-to-End Shadow and Paper Manual** |
| Next canonical task | **IMP-1302 — Run Paper Manual** (BLOCKED; see §12/§14) |

> **Count discrepancy (repo wins):** earlier chat figures used a total of
> **76** and a running completed count around **59**. The frozen YAML actually
> declares **82** task ids, and all PHASE-0…PHASE-12 deliverables plus IMP-1301
> are present and merged in `main` (verified by module presence + 34 merged
> PRs). The authoritative figure is therefore **72 / 82**.

## 2. Completed canonical work (verified by deliverable presence in `main`)

- **PHASE-0** (IMP-0001…0004) — monorepo skeleton, guardrails, baseline docs, CI.
- **PHASE-1** (IMP-0101…0105) — canonical enums, event envelope, source/social
  and decision/risk/order contracts, versioned config loader.
- **PHASE-2** (IMP-0201…0205) — PostgreSQL core schema, point-in-time storage,
  transactional outbox, consumer inbox, object-storage port
  (`persistence/alembic/versions/0001…0009`, `outbox.py`, `inbox.py`).
- **PHASE-3** (IMP-0301…0305) — authentication/RBAC, static secret loading,
  runtime session store, tamper-evident audit, trust/quarantine (`auth.py`,
  `trust.py`).
- **PHASE-4** (IMP-0401…0405) — Docker Compose stacks, observability, backup/
  restore, emergency control service, notification service (`emergency.py`,
  `notifications.py`).
- **PHASE-5** (IMP-0501…0503) — P0/P1 PoI execution + provider/runtime freeze
  (`docs/poi/STATUS.yaml`).
- **PHASE-6** (IMP-0601…0608) — source adapters (WhatsApp/Discord/Telegram
  user + bot, cross-adapter normalization, InvestingPro, news/filings,
  TradingView) (`apps/connectors`, `contracts/social.py`).
- **PHASE-7** (IMP-0701…0705) — instrument master, market-data provider port,
  feature snapshots, trading calendar, corporate-action workflow
  (`market-data-core/*`, `domain/instruments.py`, `domain/calendar.py`,
  `corporate_actions.py`).
- **PHASE-8** (IMP-0801…0806) — technical / fundamental / news-NLP / AI-extraction
  / market-regime engines + quant/ML registry scaffolding (`technical_engine.py`,
  `fundamental_engine.py`, `news_engine.py`, `ai_tasks.py`, `regime_engine.py`,
  `model_registry.py`).
- **PHASE-9** (IMP-0901…0906) — strategy-manifest evaluator, signal fusion,
  deterministic decision engine, risk engine, portfolio allocator, final
  pre-submit check (`strategy_manifest.py`, `signal_fusion.py`,
  `decision_engine.py`, `risk_engine.py`, `portfolio_allocator.py`,
  `pre_submit_check.py`).
- **PHASE-10** (IMP-1001…1006) — broker core port + conformance, IBKR Paper
  adapter, execution agent, position coordinator, broker-side protection,
  reconciliation (`broker-core/{port,fake,conformance}.py`,
  `broker-ibkr/paper_adapter.py`, `execution-agent/{agent,protection,
  reconciliation}.py`, `position_coordinator.py`).
- **PHASE-11** (IMP-1101…1107) — Operations Console: shell, source connections,
  candidate workspace, approval queue, positions/orders/reconciliation,
  emergency controls, research/calibration (`apps/dashboard/src/components/
  {app-nav,sources-screen,candidate-workspace,approval-queue,positions-orders,
  emergency-controls,research-screen,dashboard}.tsx`).
- **PHASE-12** (IMP-1201…1206) — replay runner, backtest engine, opportunity
  funnel, counterfactual shadow, champion/challenger, policy-change workflow
  (`replay_runner.py`, `backtest_engine.py`, `opportunity_funnel.py`,
  `counterfactual_shadow.py`, `champion_challenger.py`,
  `policy_change_workflow.py`).
- **PHASE-13 / IMP-1301** — E2E shadow scenarios, evaluate-only (`e2e_shadow.py`).

**IMP-1302 is NOT complete** (see §7/§14).

## 3. Completed safety & platform capabilities

| Capability | Module / evidence |
|---|---|
| Canonical contracts & configuration | `contracts/{enums,social,decision}.py`, `config/loader.py` |
| Point-in-time storage | `persistence/alembic/versions/0002_point_in_time_storage.py` |
| Outbox / inbox / idempotency | `persistence/outbox.py`, `persistence/inbox.py` |
| Authentication / RBAC / tamper-evident audit | `auth.py`, `alembic/0005_tamper_evident_audit.py` |
| Emergency halt (flatten hard-disabled) | `emergency.py`, `alembic/0006_emergency_commands.py` |
| Source adapters (user + bot; read-only for trading input) | `apps/connectors`, `contracts/social.py` |
| Strategy manifest & signal fusion | `strategy_manifest.py`, `signal_fusion.py` |
| Decision / risk / allocator / pre-submit | `decision_engine.py`, `risk_engine.py`, `portfolio_allocator.py`, `pre_submit_check.py` |
| Broker core (port + conformance + fake) | `broker-core/{port,conformance,fake}.py` |
| Execution agent (sole submitter; single-use auth) | `execution-agent/agent.py` |
| Position coordination (single exit writer) | `position_coordinator.py` |
| Broker-side protection (every fill protected) | `execution-agent/protection.py` |
| Reconciliation (recommends, never acts) | `execution-agent/reconciliation.py` |
| Operations Console | `apps/dashboard/src/components/*.tsx` |
| Replay / backtest / funnel / counterfactual / champion-challenger / policy workflow | PHASE-12 modules above |
| E2E shadow evaluate-only (no SubmitAuthorization on shadow path) | `e2e_shadow.py` + `pre_submit_check.evaluate_only` |

## 4. IBKR integration status

- IB Gateway **Paper** reachable on `127.0.0.1:4002` (owner host, read-only PoI).
- DU account **masked** everywhere (`DU*****xx`); full id never printed.
- `reqCurrentTime` works (read-only session).
- Contract resolution works (read-only `resolve_contract`).
- Read-only **market-data allowlist** enforced (no order/cancel surface).
- **POI-IBKR-READONLY = PASS** (2026-07-10; `docs/poi/evidence/POI-IBKR-READONLY.md`;
  recorded as `readonly_precheck` under POI-IBKR-001).
- **POI-IBKR-001 = NOT RUN / BLOCKED** (order-lifecycle paper experiment has not run).
- **No real submit path exists yet** — no order-placing code path is wired to
  a live broker; the owner-run CLI is deliberately unbuilt (§8/§12).

## 5. Market-data status

- Explicit `--market-data-mode delayed` works (dev/research).
- Realtime and delayed ticks are **separated** (never silently mixed).
- Delayed ticks **66–71 and 88** mapped (bid/ask/last, sizes, exchange timestamp).
- Invalid prices (`-1`/`0`/NaN/None) **fail** (finite-and-positive required).
- Code **10167** handled as a non-blocking delayed **notice** (documented).
- **PASS_DEVELOPMENT_DELAYED** achieved on the real host (delayed data, dev only).
- Delayed mode is always **`execution_eligible=false`**.
- **Realtime evidence still pending** (entitlement window).

## 6. Current PoI table (verified against `docs/poi/STATUS.yaml`)

| PoI | Status | Evidence | Missing evidence | Next action |
|---|---|---|---|---|
| POI-IBKR-READONLY | **PASS** | `evidence/POI-IBKR-READONLY.md` (2026-07-10, DU*****32) | — | none (kept as read-only pre-check under POI-IBKR-001) |
| POI-MD-001 | **BLOCKED** (partial) | delayed PASS_DEVELOPMENT_DELAYED on host; harness merged | realtime `marketDataType=1` quotes; external-provider benchmark/fallback | run realtime evidence when entitlement active (§14) |
| POI-IBKR-001 | **NOT RUN / BLOCKED** | read-only pre-check PASS only | real submit/ack/fill/cancel/partial, protection at broker, reconciliation transcript, masked paper evidence | after POI-MD-001 + owner-approved CLI (§12) |

## 7. PR #34 readiness work (prepared only — NOT a PASS)

- **Fakes-only lifecycle drill** (`tests/poi/test_paper_manual_lifecycle.py`).
- **16 scenarios**: owner-approval gate, pre-submit, submit, ack, partial fill,
  full fill, protection creation, stop/target ack, cancel/replace, disconnect/
  reconnect, reconciliation, duplicate delivery, stale event, broker rejection,
  protection failure, restart during lifecycle.
- Drives the **real** `ExecutionAgent` + `ProtectionManager` +
  `ReconciliationEngine` over the canonical **FakeBroker** (same broker the
  conformance suite runs against the IBKR adapter).
- Proves **idempotency** (agent + broker; zero duplicate orders), **protection**
  (every fill protected; zero unprotected), **reconciliation** (detects
  mismatch, fails closed, recommends-never-acts), and the **restart gate**
  (honors `order-lifecycle.active`).
- **Readiness report** (`docs/poi/POI-IBKR-001-READINESS.md`) and **one-trade
  Paper Manual runbook** (`docs/runbooks/PAPER_MANUAL_ONE_TRADE.md`).
- **Prepared only.** This does **not** make POI-IBKR-001 or IMP-1302 PASS.

## 8. Operational status (IBC / watchdog)

- IBC auto-login + watchdog **code merged in PR #31**
  (`scripts/install_ibc_gateway.sh`, `scripts/start_ibc_gateway.sh`,
  `scripts/ib_gateway_watchdog.py`, `systemd/*`, runbook).
- The owner **reported** the Gateway starts automatically via systemd on the
  host after operational patches (Gateway version 1048, config/java paths,
  systemd sandboxing drop-ins). **These host patches are not yet folded back
  into the repo scripts** — a follow-up PR is required so a clean install works
  without manual edits.
- **Do not claim the host installation is complete without host evidence**
  (no masked host transcript is recorded in the repo).
- **Do not enable a second restart manager**: the watchdog is the single
  restart manager (IBC owns only 2FA relogin); its deep `reqCurrentTime` probe
  needs the broker Python package on the host and is **still deferred** to
  avoid a restart loop.

## 9. Current blockers

1. Realtime market-data **entitlement pending** (activation window).
2. **POI-MD-001 realtime evidence** pending (and external-provider benchmark).
3. Real Paper **submit / ack / fill / cancel not run**.
4. **Owner-run CLI not built** (`scripts/poi_ibkr_lifecycle.py`) — the first
   real-submit code path; requires explicit owner approval.
5. **IMP-1302 cannot start** until POI-IBKR-001 is PASS.

## 10. Work completed before realtime (no external dependency)

- Fakes-only lifecycle readiness (16 scenarios, green).
- Operator checklist; smallest-safe-order policy (LIMIT DAY, 1 share, ≤ USD 30).
- Symbol allowlist (F primary, T alternate, AAPL cancel-path only).
- Abort conditions; reconciliation checklist; evidence & post-run templates.

## 11. Remaining canonical tasks (from the YAML)

`IMP-1302`, `IMP-1303`, `IMP-1304` (PHASE-13); `IMP-1401`, `IMP-1402`,
`IMP-1403`, `IMP-1404` (PHASE-14); `IMP-1501`, `IMP-1502`, `IMP-1503`
(PHASE-15). **10 tasks remaining** (72 + 10 = 82).

## 12. Exact execution order

1. Wait for realtime entitlement.
2. Run POI-MD-001 realtime evidence.
3. Close POI-MD-001 **only if all requirements pass**.
4. Owner approves creation of the real Paper lifecycle CLI.
5. Build and review the owner-run CLI in a **separate PR**.
6. Run POI-IBKR-001 in Paper.
7. Start IMP-1302.
8. Continue IMP-1303 and IMP-1304.
9. Only then restricted Paper Auto (PHASE-14).
10. **Live stays disabled.**

## 13. Explicitly deferred (forbidden until earlier gates pass)

Live Gateway · Live credentials · port 4001 · multi-Gateway architecture ·
Dashboard runtime mutation (owner-gated settings; ADR pending) · PAPER_AUTO ·
unrestricted submit · Live activation.

## 14. Next task (single)

> **Run POI-MD-001 realtime evidence when the market-data entitlement becomes
> active.** No parallel implementation task is opened.

## 15. Status vocabulary (binding definitions)

| Term | Meaning |
|---|---|
| **completed** | Canonical deliverable (code + tests + docs) merged to `main`; DoD met **in the repo**. Not a statement about real integration. |
| **prepared** | Built to the owner-run point using fakes/fixtures/synthetic events; the real integration has **not** been exercised. |
| **tested** | Passes automated tests (unit/integration with fakes). **Not** evidence of real external behavior. |
| **owner-run** | A manual run performed by the owner against a real external system (host/broker), producing masked evidence. |
| **PASS** | A PoI marked PASS by the **owner** with real masked evidence recorded under `docs/poi/evidence/`. |
| **BLOCKED** | Cannot proceed pending an external dependency (entitlement, host, or explicit owner approval). |

## 16. Update policy

- Any PR that changes a **milestone, PoI status, blocker, or next task** must
  update this document in the same PR.
- **Tests alone are not a PoI PASS.**
- **No integration is marked PASS without real masked evidence.**
- **Do not rely on chat memory** — verify against the repo.
- `CLAUDE_CODE_TASKS.yaml` is **not** edited unless a proven error is found;
  counts and percentages are computed **only** from the actual YAML.

## 17. Repo-vs-summary discrepancies (repo authoritative)

| Claim | Chat said | Repo (authoritative) |
|---|---|---|
| Total canonical tasks | 76 | **82** |
| Completed count | ~59 | **72** (PHASE-0…12 + IMP-1301, verified by deliverable presence) |
| ADR for runtime market-data-mode setting | proposed "ADR-0015" | **not created** (deferred pending owner approval) |
| IBC host install | "working" per chat | code merged (PR #31); **no host evidence in repo**; host patches not yet upstreamed |
