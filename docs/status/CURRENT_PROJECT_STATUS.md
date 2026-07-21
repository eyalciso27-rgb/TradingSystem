# Current Project Status — Trading System

> Canonical status snapshot. The **repository is authoritative**; where this
> document and any chat summary disagree, the repo wins (§10). Verified against
> `main` @ `624343b` (PR #34 merged). Tests passing is **never** integration
> evidence; nothing is marked verified without real masked evidence.
>
> **Mode: EVIDENCE DELIVERY**, approved and frozen by
> [ADR-0015](../adr/ADR-0015-evidence-delivery-governance.md). Automatic
> feature progression through the old backlog is paused. The plan forward is
> `docs/roadmaps/EVIDENCE_ROADMAP.md`; the status vocabulary below (§1),
> the authority boundaries, the no-auto-promotion rule and the EVD→canonical
> process are binding per that ADR.

## 1. Status vocabulary (binding)

| Status | Meaning |
|---|---|
| `IMPLEMENTED` | Deliverable merged to `main` (code + tests + docs). Says nothing about real integration or profitability. |
| `TESTED_WITH_FAKES` | Behavior proven only with fakes/fixtures/synthetic events. |
| `INTEGRATION_VERIFIED` | Proven against a real external system by an owner-run with masked evidence in `docs/poi/evidence/`. |
| `HISTORICALLY_TESTED` | Evaluated on real point-in-time historical datasets (replay/backtest). |
| `OUT_OF_SAMPLE_PASS` | Passed on held-out/walk-forward data that was never used for fitting. |
| `SHADOW_PASS` | Passed live-shadow measurement (real events, no orders) over a defined window. |
| `PAPER_PASS` | Passed a defined Paper window with real broker mechanics. |
| `PROFITABILITY_NOT_PROVEN` | Explicit default for every strategy until OUT_OF_SAMPLE + SHADOW evidence exists. |
| `BLOCKED` | Cannot proceed pending an external dependency or explicit owner approval. |

## 2. Snapshot

| Field | Value |
|---|---|
| Date | 2026-07-16 |
| `main` commit | `624343b` (Merge PR #34) |
| Baseline | `docs/baseline-v0.5/` |
| Canonical tasks (counted from YAML) | **82** |
| `IMPLEMENTED` canonical tasks | **72** (IMP-0001…IMP-1206 + IMP-1301) |
| Current stage | **Evidence Delivery** (feature delivery paused after PHASE-13/IMP-1301) |
| Single next task | **Freeze and approve the Evidence Roadmap and status model** (§9) |

## 3. What is completed in code (`IMPLEMENTED`)

All PHASE-0…PHASE-12 deliverables plus IMP-1301, verified by module presence
in `main` (34 merged PRs): canonical contracts/enums/envelope
(`trading_contracts`), versioned config loader, PostgreSQL schema +
point-in-time storage + outbox/inbox (`persistence/alembic/0001…0009`),
auth/RBAC/audit/trust, emergency halt + notifications, source adapters
(`apps/connectors`), instrument master/calendar/market-data port/feature
snapshots/corporate actions, analysis engines
(technical/fundamental/news/AI/regime + model registry), strategy manifest +
signal fusion + decision + risk + allocator + final pre-submit, broker core
(port/fake/conformance) + IBKR Paper adapter + execution agent + position
coordinator + protection + reconciliation, Operations Console (7 screens),
replay/backtest/funnel/counterfactual/champion-challenger/policy-change
workflow, E2E shadow (evaluate-only; no SubmitAuthorization on the shadow
path), IBC auto-login + watchdog scripts, market-data read-only harness +
session/client (realtime/delayed separated; delayed ticks 66–71/88 mapped;
invalid prices fail; 10167 = non-blocking delayed notice).

## 4. What is proven only with fakes (`TESTED_WITH_FAKES`)

- The **entire order lifecycle**: `tests/poi/test_paper_manual_lifecycle.py`
  (16 scenarios) drives the real ExecutionAgent + ProtectionManager +
  ReconciliationEngine over the canonical FakeBroker — idempotency (zero
  duplicate orders), protection-follows-fills, fail-closed reconciliation,
  restart gate. **No real order has ever been sent.**
- All PHASE-9…PHASE-12 engines (fusion/decision/risk/allocator/pre-submit,
  replay/backtest/funnel/counterfactual/champion/policy workflow):
  deterministic fixtures only. **No real historical dataset has been run** —
  nothing is `HISTORICALLY_TESTED` yet.
- E2E shadow scenarios: frozen fixtures for every canonical source type.
- IBC watchdog: process/socket/session fakes.

## 5. PoIs actually passed (`INTEGRATION_VERIFIED`)

| PoI | Status | Evidence / gap | Next action |
|---|---|---|---|
| POI-IBKR-READONLY | **PASS** (read-only scope only) | `evidence/POI-IBKR-READONLY.md`, 2026-07-10, DU*****32 | none |
| POI-MD-001 | **BLOCKED** (partial) | delayed-mode `PASS_DEVELOPMENT_DELAYED` achieved on the real host (`execution_eligible=false`); missing: realtime `marketDataType=1` evidence + external-provider benchmark | realtime run when entitlement active (Track A) |
| POI-IBKR-001 | **NOT RUN / BLOCKED** | read-only pre-check only; missing: real submit/ack/fill/cancel/protection/reconciliation evidence | after POI-MD-001 + owner-approved CLI (Track A) |
| Other P0/P1 PoIs | per `docs/poi/STATUS.yaml` | social/news/AI/TV remain BLOCKED on external accounts/hosts (ADR-0014) | Track A/C |

No other integration is verified. **No real submit path exists** — the
owner-run CLI is deliberately unbuilt.

## 6. Prepared but not run

- POI-IBKR-001 readiness pack: gap analysis, owner-run steps, rollback/abort,
  PASS/FAIL criteria (`docs/poi/POI-IBKR-001-READINESS.md`).
- IMP-1302 one-trade Paper Manual runbook: operator checklist,
  smallest-safe-order policy, allowlist (F/T; AAPL cancel-path only), abort
  conditions, reconciliation checklist, post-run template
  (`docs/runbooks/PAPER_MANUAL_ONE_TRADE.md`).
- IBC/watchdog host deployment: code merged (PR #31); the owner reports the
  Gateway auto-starts, but host patches (gateway version/config/java paths,
  systemd drop-ins) are **not yet upstreamed** and no host transcript is
  recorded in the repo — treat as prepared, not verified.

## 7. Current blockers (`BLOCKED`)

1. Realtime market-data entitlement (activation window).
2. POI-MD-001 realtime evidence + external-provider benchmark.
3. Real Paper submit/ack/fill/cancel never run (POI-IBKR-001).
4. Owner-run lifecycle CLI not built (requires explicit owner approval).
5. IMP-1302 gated on POI-IBKR-001 PASS.
6. Strategy profitability: **PROFITABILITY_NOT_PROVEN** for every strategy —
   no hypothesis freeze, no point-in-time dataset, no out-of-sample, no
   shadow evidence exists yet (Track B).

## 8. Remaining canonical tasks (all kept; none deleted)

Dispositions are defined in `docs/roadmaps/EVIDENCE_ROADMAP.md` (§Track
mapping); FROZEN means paused with explicit unfreeze conditions, not
cancelled.

| Task | Title | Disposition |
|---|---|---|
| IMP-1302 | Run Paper Manual | **Evidence** (Track A — integration proof only) |
| IMP-1303 | Run chaos and recovery drills | **Evidence** (Track A) |
| IMP-1304 | Close operational defects | **Continue** (Track A, as defects surface) |
| IMP-1401 | Select one eligible strategy | **Frozen** until Track B Go |
| IMP-1402 | Enable restricted auto scope | **Frozen** until IMP-1302–1304 + Track B Go |
| IMP-1403 | Monitor and auto-demote | **Frozen** with IMP-1402 |
| IMP-1404 | Complete probation review | **Frozen** with IMP-1402 |
| IMP-1501 | Operate for minimum six months | **Frozen** until PHASE-14 complete |
| IMP-1502 | Evaluate strategy and operations | **Frozen** with IMP-1501 |
| IMP-1503 | Produce future Live readiness recommendation | **Frozen**; Live remains disabled |

## 9. Single next task

The freeze/approval task is **complete**: ADR-0015 formally approves the
Evidence Delivery pivot, this status model, the three tracks, the authority
boundaries, Paper Manual as an integration-validation stage only, the
Shadow → Restricted Paper Auto → validation → future Live progression, the
prohibition on automatic strategy promotion, and the process for making EVD
tasks canonical.

> **Next task: Track A / A1 — run POI-MD-001 realtime evidence when the
> market-data entitlement becomes active.**
> No parallel implementation task is opened. `EVD-0001…EVD-0013` remain
> **non-canonical** until the ADR-0015 §9 amendment process completes.

## 10. Update policy & repo-vs-summary discrepancies

- Any PR changing a milestone, PoI, blocker or next task updates this file.
- Tests are never a PoI PASS; no integration/strategy status without real
  evidence; never rely on chat memory; counts come only from the YAML.
- Known discrepancies (repo wins): chat said 76 total tasks → YAML says
  **82**; chat's running count ~59 → verified **72** implemented; "ADR-0015"
  was proposed in chat but **does not exist**; IBC host install is reported
  working but has **no host evidence in the repo**.
