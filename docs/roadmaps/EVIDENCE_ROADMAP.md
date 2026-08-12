# Evidence Roadmap — from Feature Delivery to Evidence Delivery

> Status: **APPROVED / FROZEN by
> [ADR-0015](../adr/ADR-0015-evidence-delivery-governance.md)** (owner
> decision, 2026-07-17). Documentation only: no application code, no IBKR
> connection, no orders, no API keys, no PoI status change. No canonical task
> is deleted; tasks are Continue / Evidence / Frozen with explicit unfreeze
> conditions. Status vocabulary: `docs/status/CURRENT_PROJECT_STATUS.md` §1.
> Authority boundaries, the no-auto-promotion rule and the process for making
> EVD tasks canonical are binding per ADR-0015.

**The pivot:** 65/82 canonical tasks are `IMPLEMENTED` (corrected 2026-08 from
an over-stated 72 — the 7 PHASE-6 source adapters were never built), but almost
everything else is only `TESTED_WITH_FAKES`. The scarce resource is now
**evidence** — real integration evidence and real strategy evidence — not more
features. Work is
split into three tracks; Track C is frozen until evidence gates pass.

---

## Track A — CRITICAL INTEGRATION EVIDENCE

Goal: prove the built execution machine against the real world, minimally.

| # | Item | Status today | Exit criterion |
|---|---|---|---|
| A1 | Realtime market-data evidence (entitlement window) | ✅ **DONE 2026-07-21** — owner run: `marketDataType=1` REALTIME, no entitlement error, valid bid/ask/last + sizes, clean disconnect (`docs/poi/evidence/POI-MD-001-REALTIME.md`) | met |
| A2 | **POI-MD-001** close-out: external-provider benchmark, outage/fallback test, field-ownership matrix, provider ADR | **BLOCKED — deferred**: *"External provider not selected; benchmark/fallback/provider ADR deferred until owner-approved provider availability."* (owner decision 2026-07-21). IBKR realtime path proven in A1 | owner marks PASS with field-ownership matrix + provider ADR — only after the unfreeze condition below |
| A3 | Owner-approved **Paper lifecycle CLI** (first real-submit code path; localhost/4002/DU gates; one order at a time; no retry) | ✅ **BUILT 2026-07-21 (WP-A3), never run** — `scripts/poi_ibkr_lifecycle.py`; reuses ExecutionAgent + ProtectionManager + ReconciliationEngine; dry-run-first + confirmation token; `TESTED_WITH_FAKES` only | met on merge; execution is A4 |
| A4 | **POI-IBKR-001** owner-run: submit / ack / partial fill / fill / protection / cancel / reconnect / reconciliation | **NOT RUN / BLOCKED** — prepared: `docs/runbooks/POI_IBKR_001_OWNER_RUN.md` (7 steps). Only blocker left is the owner executing it | owner marks PASS per frozen backlog criteria |
| A5 | **Short Paper Manual** — integration proof ONLY (one-trade runbook; not an operating model) | prepared | IMP-1302 DoD: zero duplicate orders, zero required unprotected positions |
| A6 | **IMP-1302 → IMP-1303 → IMP-1304** (Paper Manual, chaos/recovery drills, close operational defects) | gated on A4 | phase regression passes |

**A2 deferral (owner decision, 2026-07-21).** There is currently **no approved
external market-data provider**. No provider is selected, assumed, purchased,
recommended or integrated; no API key is added and no adapter is built. The
four sub-items — external-provider benchmark, outage/fallback test,
field-ownership matrix, provider ADR — are deferred to a future
**provider-selection task**.

> **Unfreeze / activation condition for A2:** an external provider is
> **selected and owner-approved**, with access, commercial terms, data scope,
> freshness class and test credentials available.

POI-IBKR-001 declares `depends: [POI-PLAT-ARM-001, POI-MD-001]` in the frozen
backlog. **[ADR-0016](../adr/ADR-0016-poi-ibkr-001-market-data-dependency.md)
(2026-07-21) resolves that**: the proven realtime sub-gate satisfies the
market-data dependency **for POI-IBKR-001 only**, so A3–A6 may proceed while
A2 stays deferred. The ADR is reversible — if the lifecycle run reveals a
market-data need the sub-gate does not cover, the runs stop and the full
dependency is restored.

Paper Manual is **only** integration evidence. The target operating model is
**Restricted Paper Auto** (IMP-1402) — reached only after Track B evidence.

## Track B — STRATEGY EVIDENCE

Goal: replace `PROFITABILITY_NOT_PROVEN` with real, replayable evidence for a
small set of named hypotheses. Everything point-in-time; no look-ahead; every
run pinned to exact versions and seeds (the PHASE-12 machinery exists —
`IMPLEMENTED`+`TESTED_WITH_FAKES` — and now gets real data).

Pipeline (in order):
canonical research contracts → **hypothesis freeze** → point-in-time datasets
→ replay → backtest → train/validation/test split → walk-forward →
out-of-sample → regime analysis → stress tests → shadow → **formal Go/No-Go**.

Named hypotheses (frozen before any data is inspected):
- `baseline-trend-breakout-v1` — no-social baseline.
- `whatsapp-raw-watchlist-v1` — raw WhatsApp watchlist signal, measurement only.
- `whatsapp-technical-confirmed-v1` — WhatsApp signal gated by technical
  confirmation.

An **experiment matrix** crosses hypotheses × datasets × regimes × committee
ablations (below), with evaluation metrics + statistical uncertainty
(expectancy, drawdown, hit rate, confidence intervals; decision quality
separated from luck/data/execution per IMP-1502's standard).

### Proposed Evidence tasks — **non-canonical**

Per [ADR-0015 §9](../adr/ADR-0015-evidence-delivery-governance.md) these IDs
are **proposals only**: they are not canonical tasks, confer no authority to
write application code, and become canonical only through an amendment ADR +
owner approval + a dedicated "Amend CLAUDE_CODE_TASKS.yaml" task.

| Proposed ID | Title | Exit criterion |
|---|---|---|
| EVD-0001 | Evidence governance and status model | owner freezes this roadmap + status vocabulary |
| EVD-0002 | `DailyPremarketWatchlistObservation` contract | canonical, versioned, point-in-time contract merged |
| EVD-0003 | `StrategyHypothesis` + `ExperimentDefinition` contracts | frozen hypothesis/experiment schema; hash-pinned |
| EVD-0004 | Point-in-time research dataset builder | real datasets with as_of/lineage; no look-ahead audit passes |
| EVD-0005 | Baseline hypothesis freeze (`baseline-trend-breakout-v1`) | hypothesis frozen + registered before data inspection |
| EVD-0006 | WhatsApp raw measurement (`whatsapp-raw-watchlist-v1`) | measurement-only dataset + report; no trading input |
| EVD-0007 | WhatsApp technical-confirmed experiment (`whatsapp-technical-confirmed-v1`) | experiment defined + run per matrix |
| EVD-0008 | Experiment matrix | matrix registered; every cell replayable |
| EVD-0009 | Evaluation metrics + statistical uncertainty | metric definitions + CI methodology approved |
| EVD-0010 | AI Review Committee contract + replayable orchestration | committee I/O contract; deterministic replay of reviews |
| EVD-0011 | Out-of-sample / walk-forward / stress evaluation | OOS windows never touched in fitting; stress suite run |
| EVD-0012 | Shadow evidence | defined live-shadow window measured, no orders |
| EVD-0013 | Formal Go/No-Go decision | owner decision recorded (GO / NO-GO / MORE-RESEARCH) |

## Track C — FROZEN EXPANSION (paused, not cancelled)

Frozen until the listed gates pass. **Nothing here is deleted.**

| Item | Unfreeze condition |
|---|---|
| IMP-1401 Select one eligible strategy | Track B `OUT_OF_SAMPLE_PASS` + `SHADOW_PASS` + EVD-0013 GO |
| IMP-1402 Enable restricted auto scope (**Paper Auto**) | IMP-1302–1304 done **and** IMP-1401 approved |
| IMP-1403 Monitor and auto-demote | with IMP-1402 |
| IMP-1404 Complete probation review | with IMP-1402 |
| IMP-1501 Operate for minimum six months | PHASE-14 complete |
| IMP-1502 Evaluate strategy and operations | with IMP-1501 |
| IMP-1503 Live readiness recommendation | with IMP-1501; **Live stays disabled regardless** |
| New sources / new AI models | Track B shows marginal value of existing ones first |
| Dashboard expansion not needed for research | explicit owner request + ADR |
| Multi-Gateway architecture | a concrete scaling need + ADR |
| Unrestricted parameter search | never without EVD-0009 uncertainty controls + owner approval |
| Automatic strategy promotion | **never** (promotion is a documented human action) |

---

## AI Review Committee

A structured multi-role review whose output is a **formal input to the
Decision Engine — never an execution authority.**

### Roles
1. Technical Analyst · 2. Fundamental/News Analyst · 3. Risk Critic ·
4. Data Quality Critic · 5. Strategy Critic · 6. Devil's Advocate ·
7. Chair/Synthesizer (summarizes only; adds no new position).

### Authorities (binding separation)

**Owner** — approves strategies & versions, Strategy Manifests, parameters,
risk limits, universe/allowlist, promotion/rollback, and transitions between
Shadow / Restricted Paper Auto / Live. Does **not** approve routine trades.

**AI Committee** — produces assessments, objections, missing-evidence lists
and risk flags; may participate as `REQUIRED` / `VETO` / `SOFT_SCORE` /
`BONUS` / `CONTEXT` per the Strategy Manifest (the existing `ManifestRole`
grammar). It does **not** create OrderIntent, does **not** create
SubmitAuthorization, sends **no** order, changes **no** strategy, promotes
**no** version, and can **never** bypass Risk or Pre-Submit.

**Decision Engine** — automatically derives ENTER / WATCH / REJECT from the
Strategy Manifest + committee output. **Risk Engine** — absolute veto even
when the committee supports. **Allocator** — sizes within the approved
envelope only. **Pre-Submit** — final deterministic fail-closed gate.
**Execution Agent** — the only component that ever sends an order.

### Target flow
Sources → Analysis Engines → AI Committee Assessments → Decision Engine →
Risk Engine → Allocator → Pre-Submit → Execution Agent → Protection →
Reconciliation.

### Committee principles
- Each member files an **independent initial position** before seeing others.
- All members receive the **same versioned Evidence Bundle**.
- Structured output: `SUPPORT / OPPOSE / ABSTAIN / INSUFFICIENT_EVIDENCE`
  with confidence, evidence refs, contradictions, missing evidence,
  assumptions and risk flags.
- **Disagreement is preserved** — never crushed into a single score.
- The Chair synthesizes only. **Consensus is not proof of profitability.**
- Model failure or missing evidence ⇒ `INSUFFICIENT_EVIDENCE` /
  `NEEDS_REVIEW` — never a silent default.
- Every review records model version, prompt version, evidence fingerprint,
  timestamp and the full output (replayable — EVD-0010).

### Committee ablation (part of the experiment matrix)
Runs compared: **no committee** · **analyst only** · **analyst + critic** ·
**full committee**; plus per-critic marginal contribution and a dedicated
analysis of **disagreement cases** (where members split — did dissent carry
information?). The committee earns manifest weight only if ablation shows
positive marginal value.

---

## Next task (single)

A1 is **done**, A2 is **deferred**, ADR-0016 unblocked the dependency, and A3
(the owner-run lifecycle CLI) is **built but never executed**. The single next
task is therefore not a coding task at all:

> **A4 — the owner executes the POI-IBKR-001 Paper lifecycle run**, following
> the seven steps in
> [`docs/runbooks/POI_IBKR_001_OWNER_RUN.md`](../runbooks/POI_IBKR_001_OWNER_RUN.md),
> then records `docs/poi/evidence/POI-IBKR-001.md` and marks the PoI.
>
> Only the owner may run it and only the owner may mark it. Until then
> POI-IBKR-001 stays **BLOCKED / NOT RUN**, IMP-1302 stays gated, and
> PAPER_AUTO and LIVE stay prohibited.

If the owner prefers to work Track B instead, the equivalent non-broker task
is the **ADR-0015 §9 amendment ADR** that would make `EVD-0001…0013` canonical.
No parallel implementation task is opened either way.
