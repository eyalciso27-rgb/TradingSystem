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
| Single next task | **Owner executes the POI-IBKR-001 Paper lifecycle run** (§9) — `docs/runbooks/POI_IBKR_001_OWNER_RUN.md`; no code task remains open |

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
| POI-MD-001 | **BLOCKED** (aggregate) — **realtime sub-gate PASS** | `evidence/POI-MD-001-REALTIME.md` (owner run 2026-07-21): `marketDataType=1` REALTIME, no entitlement error, valid bid/ask/last + sizes, timestamp, clean disconnect, read-only. Blocker: *"External provider not selected; benchmark/fallback/provider ADR deferred until owner-approved provider availability."* | deferred to a future provider-selection task; unfreeze only on an owner-approved provider (Track A / A2) |
| POI-IBKR-001 | **NOT RUN / BLOCKED** | read-only pre-check only; missing: real submit/ack/fill/cancel/protection/reconciliation evidence. Market-data dependency narrowed by [ADR-0016](../adr/ADR-0016-poi-ibkr-001-market-data-dependency.md); owner-run CLI **built but never executed** (WP-A3) | owner runs `docs/runbooks/POI_IBKR_001_OWNER_RUN.md` (7 steps) and marks the PoI |
| Other P0/P1 PoIs | per `docs/poi/STATUS.yaml` | social/news/AI/TV remain BLOCKED on external accounts/hosts (ADR-0014) | Track A/C |

No other integration is verified. A real submit path now **exists in code**
(`scripts/poi_ibkr_lifecycle.py`, WP-A3) but has **never been executed**: it
refuses to send anything without `--execute` plus a confirmation token derived
from a dry run of identical parameters, and it is `TESTED_WITH_FAKES` only.

## 6. Prepared but not run

- POI-IBKR-001 readiness pack: gap analysis, owner-run steps, rollback/abort,
  PASS/FAIL criteria (`docs/poi/POI-IBKR-001-READINESS.md`).
- IMP-1302 one-trade Paper Manual runbook: operator checklist,
  smallest-safe-order policy, allowlist (F/T; AAPL cancel-path only), abort
  conditions, reconciliation checklist, post-run template
  (`docs/runbooks/PAPER_MANUAL_ONE_TRADE.md`).
- POI-IBKR-001 owner-run harness (WP-A3): `scripts/poi_ibkr_lifecycle.py`
  (`preflight | dry-run | cancel-path | fill-path | reconnect | cleanup`)
  reusing ExecutionAgent + ProtectionManager + ReconciliationEngine, plus the
  7-step sequence in `docs/runbooks/POI_IBKR_001_OWNER_RUN.md`. Proven with
  fakes only (`tests/poi/test_poi_ibkr_lifecycle_cli.py`, 46 drills) —
  **never run against IBKR**.
- IBC/watchdog host deployment: code merged (PR #31); the owner reports the
  Gateway auto-starts, but host patches (gateway version/config/java paths,
  systemd drop-ins) are **not yet upstreamed** and no host transcript is
  recorded in the repo — treat as prepared, not verified.

## 7. Current blockers (`BLOCKED`)

1. ~~Realtime market-data entitlement~~ — **CLEARED 2026-07-21**: owner run
   observed `marketDataType=1` (REALTIME) with no entitlement error
   (`evidence/POI-MD-001-REALTIME.md`). IBKR realtime path proven; nothing
   else is implied.
2. POI-MD-001 **aggregate** BLOCKED — *"External provider not selected;
   benchmark/fallback/provider ADR deferred until owner-approved provider
   availability."* (owner decision 2026-07-21). The four sub-items are
   **deferred to a future provider-selection task**; no provider is selected,
   assumed, purchased or integrated. **Unfreeze condition:** an external
   provider is selected and owner-approved, with access, commercial terms,
   data scope, freshness class and test credentials available.
   *(`DEFERRED` is not used as a status: it is absent from the ADR-0015
   vocabulary and is not a canonical registry state.)*
3. Real Paper submit/ack/fill/cancel never run (POI-IBKR-001) — **the only
   remaining blocker is the owner executing the run**; ADR-0016 cleared the
   market-data dependency and the CLI is built.
4. ~~Owner-run lifecycle CLI not built~~ — **built 2026-07-21 (WP-A3)**,
   `TESTED_WITH_FAKES`, never executed.
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

> **Next: the owner executes the POI-IBKR-001 Paper lifecycle run** — the
> seven steps in
> [`docs/runbooks/POI_IBKR_001_OWNER_RUN.md`](../runbooks/POI_IBKR_001_OWNER_RUN.md)
> (preflight → dry-run → cancel-path → verify flat → fill-path → reconnect →
> cleanup), then records `docs/poi/evidence/POI-IBKR-001.md` and marks the
> PoI. Only the owner may run it; only the owner may mark it.
>
> No coding task is open. WP-A3 delivered ADR-0016, the owner-run CLI and its
> fakes-only proof; everything downstream (IMP-1302 → 1303 → 1304) stays gated
> on the owner-run evidence, and PAPER_AUTO and LIVE stay prohibited.

### Background (how we got here)

The freeze/approval task is **complete**: ADR-0015 formally approves the
Evidence Delivery pivot, this status model, the three tracks, the authority
boundaries, Paper Manual as an integration-validation stage only, the
Shadow → Restricted Paper Auto → validation → future Live progression, the
prohibition on automatic strategy promotion, and the process for making EVD
tasks canonical.

**Track A / A1 is complete:** the realtime market-data sub-gate PASSED on the
owner host (2026-07-21) — `marketDataType=1`, entitlement confirmed, valid
quote fields, clean disconnect (`docs/poi/evidence/POI-MD-001-REALTIME.md`).
This proves the **IBKR realtime path only**; POI-MD-001 aggregate stays
BLOCKED and execution stays gated by POI-IBKR-001.

**Track A / A2 is deferred** (no approved external provider). Because the
frozen backlog declares `POI-IBKR-001 depends: [POI-PLAT-ARM-001, POI-MD-001]`,
that deferral also stalled A3–A6 — until
[ADR-0016](../adr/ADR-0016-poi-ibkr-001-market-data-dependency.md) (2026-07-21)
narrowed the dependency: the proven realtime sub-gate satisfies the market-data
dependency **for POI-IBKR-001 only**, while POI-MD-001 stays BLOCKED. The ADR
is reversible if the lifecycle run reveals a market-data need it does not
cover.

**Track A / A3 is complete in code:** the owner-run lifecycle CLI is built and
proven with fakes; it has never contacted IBKR.

If the owner prefers Track B instead, the equivalent non-broker task is the
ADR-0015 §9 amendment ADR that would make `EVD-0001…EVD-0013` canonical.
No parallel implementation task is opened. `EVD-0001…EVD-0013` remain
**non-canonical** until that amendment process completes.

## 10. Update policy & repo-vs-summary discrepancies

- Any PR changing a milestone, PoI, blocker or next task updates this file.
- Tests are never a PoI PASS; no integration/strategy status without real
  evidence; never rely on chat memory; counts come only from the YAML.
- Known discrepancies (repo wins): chat said 76 total tasks → YAML says
  **82**; chat's running count ~59 → verified **72** implemented; "ADR-0015"
  was proposed in chat but **does not exist**; IBC host install is reported
  working but has **no host evidence in the repo**.
