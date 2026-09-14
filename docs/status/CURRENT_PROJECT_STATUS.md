# Current Project Status — Trading System

> Canonical status snapshot. The **repository is authoritative**; where this
> document and any chat summary disagree, the repo wins (§10). Verified against
> `main` @ `b4d65de` (PR #55 merged). Tests passing is **never** integration
> evidence; nothing is marked verified without real masked evidence.
>
> **Authoritative project map:**
> [`docs/roadmaps/TRADING_SYSTEM_MASTER_PLAN.md`](../roadmaps/TRADING_SYSTEM_MASTER_PLAN.md)
> with the machine-readable
> [`docs/status/COMPONENT_EVIDENCE_MATRIX.yaml`](COMPONENT_EVIDENCE_MATRIX.yaml)
> and the four-track
> [`docs/roadmaps/ACCELERATED_EXECUTION_PLAN.md`](../roadmaps/ACCELERATED_EXECUTION_PLAN.md).
> This file remains the dated snapshot; the master plan is the map.
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
| Date | 2026-08-13 |
| `main` commit | `b4d65de` (Merge PR #55) — PHASE-6 adapters on branch `feat/phase6-source-adapters` (unmerged) |
| Baseline | `docs/baseline-v0.5/` |
| Canonical tasks (counted from YAML) | **82** |
| `IMPLEMENTED` canonical tasks | **69** (IMP-0001…IMP-1206 + IMP-1301 + the 4 buildable PHASE-6 adapters IMP-0603/0604/0607/0608 — code, fakes-tested, real PoIs BLOCKED — on top of IMP-0605 normalization) |
| Remaining canonical tasks | **13** (IMP-0601/0602/0606 ToS-blocked adapters; IMP-1302–1304; IMP-1401–1404; IMP-1501–1503) |
| PoIs PASS | **2 of 15** (POI-PLAT-ARM-001, POI-SEC-SESS-002) |
| Deferred owner-runs | **DEF-19 PASS**; DEF-14 DEFERRED; the other **18 NOT_RUN** |
| Current stage | **Evidence Delivery** (feature delivery paused after PHASE-13/IMP-1301) |
| Paper Auto / Live | **DISABLED / DISABLED** |
| Next broker-changing action | **DEF-18** post-fix cancellation, any permitted non-closed session (§9) |
| Next non-broker action | **build the point-in-time research dataset** for the now-FROZEN `baseline-trend-breakout-v1` — needs no broker (§9) |

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

- M7 read-only runtime orchestration/audit machinery is **IMPLEMENTED and
  TESTED_WITH_FAKES**: a supplied frozen snapshot runs only through
  `MvpTradingPolicy.evaluate_read_only`, and immutable audit/replay evidence
  uses the existing tamper-evident chain plus content-addressed object store.
  This is evaluate-only engineering evidence: it mints no submission
  authorization, creates no executable plan, calls no broker, and establishes
  no `SHADOW_PASS`, `PAPER_PASS`, real integration, or profitability evidence.
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
| POI-MD-001 | **BLOCKED** (aggregate) — **realtime sub-gate PASS** | `evidence/POI-MD-001-REALTIME.md` (owner run 2026-07-21) **plus a second read-only observation for `F`**, `evidence/POI-MD-001-REALTIME-F-20260731.md` (2026-07-31): `marketDataType=1` REALTIME, no entitlement error, valid bid/ask/last + sizes, canonical freshness REALTIME, clean disconnect, read-only. **The observed quote is historical and marked `quote_reuse: FORBIDDEN_STALE` — it may never be reused as a run input.** Blocker: *"External provider not selected; benchmark/fallback/provider ADR deferred until owner-approved provider availability."* | deferred to a future provider-selection task (DEF-14); unfreeze only on an owner-approved provider |
| POI-IBKR-001 | **BLOCKED** (order lifecycle incomplete) | A real owner-run DID reach the broker order path: **submit, acknowledgement and idempotent resubmit passed**, the order never filled — but the **cancellation FAILED** and the owner cancelled by hand. Root causes fixed (`D-CANCEL-01…08`, `D-CLOCK-01…04`). Post-fix, **DEF-19 `verify-clean` PASSES** (2026-07-30T17:49:25Z, `evidence/POI-IBKR-001-VERIFY-CLEAN.md`): account verified clean — zero working lifecycle orders, zero non-zero positions, zero unresolved broker state, reconciliation `findings=0`, clean disconnect. **Still missing: any post-fix real cancellation, any real fill, any protection, any restart recovery.** | **DEF-18** post-fix cancellation (live session), then fill/protection/ reconciliation, then **DEF-20** restart recovery |
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
  fakes only (`tests/poi/test_poi_ibkr_lifecycle_cli.py`, 66 drills incl. a
  pre-merge safety review of 12 failure modes) —
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
3. POI-IBKR-001 order lifecycle incomplete. Submit and acknowledgement are
   proven on a real Paper run; **cancellation is not** (it failed, was fixed,
   and has not been re-run). **DEF-19 verify-clean PASSES** post-fix, so the
   account is verified clean, but **DEF-18 (post-fix cancellation) and DEF-20
   (restart-recovery cancellation) are both `NOT_RUN`**, and there is **no real
   fill, protection or restart-recovery evidence of any kind**.
4. ~~Owner-run lifecycle CLI not built~~ — **built 2026-07-21 (WP-A3)** and
   since hardened; partially exercised for real (submit/ack/verify-clean).
   Evidence produced **before** the D-CLOCK-01 fix carries fabricated
   timestamps and **must be regenerated, not trusted**.
5. IMP-1302 gated on POI-IBKR-001 PASS.
6. Strategy profitability: **PROFITABILITY_NOT_PROVEN** for every strategy.
   `baseline-trend-breakout-v1` is now **FROZEN** and owner-approved (v1,
   `OWNER-EYAL-2026-08-13-BASELINE-TREND-BREAKOUT-V1`,
   `docs/strategies/baseline-trend-breakout-v1.yaml`, immutable — a material
   change requires a NEW `version_id`, never an edit to v1), but there is
   still no point-in-time dataset, no out-of-sample, and no shadow evidence.
   **No strategy is eligible for any automated mode**, and eligibility
   requires BOTH `OUT_OF_SAMPLE_PASS` AND `SHADOW_PASS` plus a formal owner Go.
7. Social, AI, news, InvestingPro and TradingView PoIs all remain
   **BLOCKED** on owner-supplied accounts/hosts (ADR-0014, ADR-0013).
8. The **AI Review Committee is designed but not implemented** as an active
   runtime: no I/O contract module, no replayable orchestration. It is a
   decision **input** and never an execution authority.
9. **PAPER_AUTO and LIVE remain frozen/DISABLED**; no plan in this repository
   enables Live (IMP-1503 produces a recommendation only).
10. Host operations: **B-02** chaos drills not run, **B-03** IBC/watchdog host
    patches not upstreamed, **B-04** watchdog deep probe not enabled.
    Operational hardening (account-safety gate, read-only status/deep-probe
    CLI, systemd/tmpfiles/logrotate assets, read-only host audit) is
    **IMPLEMENTED AND TESTED WITH FAKES** and **READY FOR OWNER-HOST
    ACTIVATION**, but **NOT EXECUTED** on the host — so B-02/B-03/B-04 stay
    OPEN and DEF-12/DEF-13 stay `NOT_RUN`. See
    [`OPERATIONAL_HARDENING.md`](../runbooks/OPERATIONAL_HARDENING.md).
    Note the deliberate behaviour change: the **unattended watchdog no longer
    restarts the Gateway** (D-WATCHDOG-01) — recovery is owner-authorized.

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

> **There are now TWO next actions, and they run in parallel.** Treating them
> as sequential is the single largest avoidable delay in the project.
>
> **1 — Broker (needs a permitted non-closed session).** **DEF-18**, the
> post-fix cancellation verification: fresh realtime quote → cancel-path dry
> run → new same-day token → **exactly one** `--execute` with
> `--journal-dir` → `verify-clean`. **Regular, pre-market or after-hours all
> qualify** — only a *closed* session is refused, because it has no live
> two-sided quote. See
> [`POI_IBKR_001_OWNER_RUN.md`](../runbooks/POI_IBKR_001_OWNER_RUN.md).
> Never reuse a historical quote or an old token; never retry after `UNKNOWN`
> or an unconfirmed cancellation. **DEF-20 remains NOT_RUN afterwards**, and
> runs in its own window — broker-mutating owner-runs are never concurrent.
>
> **2 — Strategy (needs no broker at all).** **`baseline-trend-breakout-v1`
> is FROZEN** (owner-approved 2026-08-13, immutable v1 —
> [`docs/strategies/baseline-trend-breakout-v1.yaml`](../strategies/baseline-trend-breakout-v1.yaml),
> pinned by `tests/strategy/test_frozen_baseline_manifest.py`). It requires no
> social and no AI input, so it is the baseline every later claim must beat.
> The next step is building the point-in-time research dataset, which must
> happen **before** any evaluation result is inspected against the frozen
> manifest.
>
> Only the owner may run either; only the owner may mark a PoI. Everything
> downstream (IMP-1302 → 1303 → 1304, then PHASE-14) stays gated, and
> PAPER_AUTO and LIVE stay prohibited.

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
  **82**; a 2026-08 code re-audit corrected the implemented count from an
  over-stated **72** to **65** (the 7 PHASE-6 source adapters had never been
  built — only IMP-0605 normalization existed); the 2026-08-13 PHASE-6 package
  then built the 4 ToS-permitted adapters (IMP-0603/0604/0607/0608), lifting the
  count to **69** — their code is IMPLEMENTED but their real PoIs stay BLOCKED,
  and IMP-0601/0602/0606 remain NOT_IMPLEMENTED (no ToS-permitted path). "ADR-0015"
  was proposed in chat but **does not exist**; IBC host install is reported
  working but has **no host evidence in the repo**.
- 2026-08-13 readiness packages (no count change): **Track A** (strategy freeze,
  point-in-time dataset foundation, backtest readiness, ingestion readiness) —
  `DatasetStatus` stays `REAL_DATASET_NOT_BUILT`, runs stay
  `PROFITABILITY_NOT_PROVEN`, no trading parameter invented. **Track C** — the 4
  built adapters are `ACTIVATION_READY` (secret-store integration, read-only
  preflight, sanitized PoI evidence, owner runbooks in
  `docs/status/PHASE6_SOURCE_ACTIVATION.md`); real PoIs stay BLOCKED and a FAKE
  probe is always `NOT_EVIDENCE`, never PASS. PHASE-13 is
  `OFFLINE_ENGINEERING_COMPLETE` (IMP-1301 implemented; IMP-1302/1303
  EVIDENCE_PENDING on POI-IBKR-001). Canonical IMPLEMENTED count unchanged at
  **69**.
