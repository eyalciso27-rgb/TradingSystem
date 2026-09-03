# TradingSystem — MVP Delivery Plan

> **Status:** OWNER-DIRECTED DELIVERY OVERLAY  
> **Date:** 2026-09-01  
> **Purpose:** define the shortest safe path to the current TradingSystem MVP without replacing the full-product architecture or weakening any existing safety/evidence requirement.
>
> This document controls **current MVP delivery priority and sequencing**. It does not replace safety ADRs, the Master Architecture Baseline, frozen strategy records, or canonical evidence. If this document conflicts with a binding safety invariant, ADR, frozen owner decision, test-enforced contract, or observed repository fact, the stricter/canonical repository fact wins.

---

## 1. MVP outcome

The current MVP is complete only when the system can autonomously execute **one real owner-frozen strategy** end-to-end in **IBKR Paper**, while obeying the mandatory risk/safety envelope and proving recovery, reconciliation, protection, auditability, and fail-closed behavior.

The target path is:

```text
MARKET DATA / PIT INPUT
        -> FROZEN STRATEGY
        -> SIGNAL / NO TRADE
        -> DECISION
        -> RISK / SAFETY
        -> PORTFOLIO / SIZE
        -> FINAL PRE-SUBMIT
        -> SINGLE-USE PAPER AUTHORIZATION
        -> EXECUTION AGENT
        -> IBKR PAPER
        -> BROKER-RESIDENT PROTECTION
        -> FILLS / POSITION
        -> EXIT / RISK REDUCTION
        -> RECONCILIATION / AUDIT
```

The MVP is **not** defined by feature count, UI breadth, number of integrations, or profitability claims. It is defined by one correct, reproducible, protected Paper-trading lifecycle under the frozen strategy and mandatory safety rules.

---

## 2. Non-negotiable invariants

These rules are not delivery trade-offs and may not be weakened to make the MVP finish faster:

- The Risk Engine and safety gates retain absolute veto authority.
- AI/LLM components never hold broker submit authority.
- The canonical ExecutionAgent/ExecutionService remains the only broker submission path.
- Paper and Live remain isolated. **Live stays disabled and is outside this MVP.**
- Strategy decides whether an opportunity exists; Risk/Portfolio decide whether it is allowed and at what size.
- Risk may reduce quantity; it may not invent a trade or increase strategy/risk authority.
- `NO TRADE` must never become an execution intent.
- Exact broker instrument identity is required; ticker-only ambiguity may not reach submission.
- Unknown/missing/stale account, P&L, position, order, market-data, protection, reconciliation, ownership, halt-store, or audit state blocks **new entries**.
- Safe risk-reducing actions such as cancel/protect/reduce may remain available when entries are blocked.
- Missing P&L is never converted to zero.
- Unknown exposure is never converted to zero.
- No blind retry is permitted for unknown broker/order state.
- Every automatic order must be attributable through strategy -> decision -> risk -> authorization -> execution.
- Required protective STOP must be broker-resident and proven, not inferred from a capability flag.
- Durable idempotency, durable audit, restart/recovery, and reconciliation are mandatory.
- Autonomous Paper authorization is disabled by default and is an owner-gated final switch.

---

## 3. Canonical source hierarchy

For implementation and AutoDev planning, use the following hierarchy:

1. Binding safety ADRs, architecture guardrails, and test-enforced invariants.
2. `Master Architecture Baseline` and canonical Decision Contract.
3. `docs/roadmaps/TRADING_SYSTEM_MASTER_PLAN.md` for the full-product project map and evidence model.
4. **This file — `docs/roadmaps/MVP_DELIVERY_PLAN.md` — for current MVP delivery priority and critical-path sequencing.**
5. Frozen owner strategy records and owner decision artifacts.
6. Functional/Data/Integration/Security/Technology/Policy specifications.
7. `docs/status/COMPONENT_EVIDENCE_MATRIX.yaml` and other machine-readable evidence/status records.
8. Actual code, tests, CI results, broker evidence, and merged repository facts.

When prose and repository facts disagree, do not guess. Preserve the stricter safety interpretation and update the stale planning/status artifact.

`AutoDev`'s external `master_plan.json` is a **machine-readable projection** of canonical repository decisions. It is not an independent authority allowed to redefine product scope, strategy semantics, safety policy, or completion criteria.

---

## 4. Current repository facts at creation

This plan was created from `main` at:

```text
e05d74a2e703435fc5c3dde2fde2703b601dfe7d
```

The following milestones are already merged and must be treated as facts, not rebuilt:

| Milestone | Repository evidence | Delivery state |
|---|---|---|
| Protected Paper execution lifecycle | PR #94 | MERGED |
| One frozen MVP strategy + trading policy -> canonical execution intent | PR #95 | MERGED |
| Read-only MVP operations-console shell | PR #96 | MERGED; real backend truth integration may still be needed |
| Autonomous Paper runtime foundation, Phase A / DenyAll | PR #97 | MERGED |
| AutoDev full-GitHub autonomy canary | PR #98 | MERGED |
| Canonical frozen-baseline evidence reconciliation | PR #99 | MERGED |
| A-WP2 PIT owner decision | PR #100 | MERGED |
| S&P 500 forward immutable snapshot hardening | PR #101 | MERGED |
| Dataset manifest lineage + no-look-ahead binding | PR #102 | MERGED |
| Canonical MVP delivery plan (this file) | PR #103 | MERGED |
| A-WP2-SP500-HISTORICAL-SECONDARY-PIT-EVAL — offline provenance registry of candidate secondary S&P membership-history sources (nothing fetched, nothing integrated, nothing promoted to authoritative) | PR #104 | MERGED |
| A-WP2-SP500-CURRENT-GICS-ARCHIVE — current/forward S&P 500 + current-GICS immutable monthly snapshot archive hardening (write-once dated layout, raw+normalized checksum binding, crash-safe two-write commit, fail-closed last-known-good retention) | PR #105 | MERGED |
| A-WP2-CORPORATE-ACTION-PIT-VALIDATION — fail-closed, offline, fixture-only structural validator for corporate-action point-in-time records | PR #106 | MERGED |

Do not create replacement implementations for these milestones unless a concrete defect or missing acceptance criterion is proven. **In particular, PR #104, #105 and #106 are complete; do not re-open, re-plan or re-implement A-WP2-SP500-HISTORICAL-SECONDARY-PIT-EVAL, A-WP2-SP500-CURRENT-GICS-ARCHIVE or A-WP2-CORPORATE-ACTION-PIT-VALIDATION.**

---

## 5. Current MVP critical path

The MVP critical path is deliberately narrower than the full product roadmap.

### M1 — Protected execution

**State:** DONE in code / merged via PR #94.  
**Purpose:** exact instrument identity, sealed executable plan, durable pre-submit/audit, single-share protected Paper bracket, broker-resident STOP, idempotency, recovery, cleanup, reconciliation, and fail-closed ambiguous states.

Remaining evidence, if not already recorded as canonical PASS, must be completed through the designated owner/broker qualification path; do not reimplement the execution lifecycle.

### M2 — Frozen strategy and trading policy

**State:** DONE in code / merged via PR #95.  
**Selected strategy:** `baseline-trend-breakout-v1`.

The strategy must remain owner-frozen and deterministic. The MVP uses one strategy only. Strategy semantics are not to be silently refit after seeing evaluation results.

Frozen behavior includes the owner-approved base-window rule:

```text
Evaluate candidate base lengths from 20 trading days down to 10.
Select the first/largest valid window satisfying every frozen predicate.
No valid window => NO TRADE.
```

Any future strategy-semantic change requires a new version/fingerprint and the applicable replay/OOS/shadow approval path.

### M3 — Point-in-time strategy dataset and evidence

**State (reconciled 2026-09-03): WAITING_EXTERNAL / BLOCKED_FOR_OWNER.**

Already merged (repository fact; do not rebuild):

- A-WP2 owner decision — PR #100.
- Forward S&P 500 immutable snapshot behavior — PR #101.
- Dataset manifest lineage / no-look-ahead binding — PR #102.
- S&P historical-secondary source registry (offline provenance record of
  candidate secondary sources; nothing fetched or integrated) — PR #104.
- Current/forward S&P 500 + current-GICS immutable snapshot archive hardening
  — PR #105.
- Fail-closed corporate-action point-in-time qualification (offline,
  fixture-only structural validator) — PR #106.

#### Validation machinery vs. empirical integration evidence

M3 now has substantial **validation and ingestion machinery** merged and
tested. None of it is, by itself, **empirical PIT integration evidence** for
the frozen strategy, and passing its tests is never treated as such:

| Merged machinery | What it proves | What it does NOT prove |
|---|---|---|
| Dataset V1 ingestion/qualification pipeline (`trading_market_data_core.dataset_v1*`, `scripts/run_dataset_v1_real_qualification.sh`) | the pipeline can discover, checksum, parse and qualify real IBKR/SEC/S&P artifacts when it is run against them | nothing — it has not been run against real sources in this reconciliation; it is an **owner-run** tool |
| S&P forward snapshot archive (PR #101, hardened by PR #105) | immutable, checksum-bound, crash-safe monthly CURRENT S&P 500 / current-GICS snapshots, building genuine forward history from collection start | historical (pre-collection) S&P membership or historical GICS — explicitly forbidden from being treated as such |
| S&P historical-secondary source registry (PR #104) | which candidate secondary sources exist and exactly what would be required to validate one (checksum, timestamps, 2-source cross-check) | nothing about historical S&P membership itself — every candidate is unfetched (`checksum_status: NOT_YET_COMPUTED`) and, even fully validated, stays `SECONDARY_RESEARCH_PIT` — it can never become authoritative (ADR-0017) |
| Corporate-action PIT validator (PR #106) | that a *given* corporate-action fixture is internally PIT-consistent (four timestamps ordered, stable UUID identity, non-cyclic correction lineage) | that any real corporate-action record exists for the frozen strategy's universe — it is explicitly OFFLINE and FIXTURE-ONLY and never performs a network call |
| Dataset V1 real-source qualifiers (`qualify_corporate_actions`, `qualify_pit_membership`, `qualify_delisted_security_history`) | the pipeline can qualify real SEC/IBKR evidence once it is fetched | nothing until the owner-run qualification script is actually executed against live SEC EDGAR / IBKR sources |

Required A-WP2 principles:

- point-in-time semantics;
- immutable raw/snapshot evidence;
- source provenance and SHA-256 binding;
- `observed_at` / `available_at` / `as_of` semantics where relevant;
- no current-state projection into the past;
- no survivorship-biased substitution presented as PIT truth;
- missing required historical evidence remains an explicit blocker/gap, not fabricated data.

#### S&P 500 membership

Two mechanisms are intentionally separate:

1. **Historical reconstruction:** use legitimate S&P membership-change evidence with addition/removal effective dates and publication/knowledge timing where available.
2. **Forward archive:** refresh the current S&P 500 universe approximately monthly and write immutable dated snapshots instead of overwriting prior state.

For official S&P announcement ingestion, the existing repository path is:

```text
config/data-sources/sp500-documents/
```

Accepted owner-supplied official artifacts may include HTML, text, CSV, or text-layer PDF. Preserve the original artifact, provenance, and SHA-256; parse/build the canonical announcement manifest through the repository ingestion path.

Do **not** bypass S&P bot protection, robots restrictions, authentication, or access controls. Secondary websites may be used only under an explicitly approved source role; they must not silently become authoritative historical S&P truth.

If the monthly refresh fails, retain the last-known-good snapshot and record the refresh failure. Never convert failed/empty/UNKNOWN universe state into a healthy empty universe.

#### Historical GICS

The requirement is true company-level as-of classification history:

```text
security -> GICS code/classification -> effective date -> knowledge date
```

Do not fabricate historical GICS from current GICS, current sector, SEC SIC, company descriptions, or present-day classifications.

Known source boundary:

- IBKR does not prove company-level historical GICS assignment history.
- SEC SIC is not GICS and is not a substitute.
- A free, legitimate company-level historical GICS assignment series has not been established as canonical evidence.

If a permitted source supplies **current** GICS, immutable monthly snapshots may build trustworthy forward history from collection start. They do not constitute historical backfill for earlier periods.

#### Remaining M3 gaps (reconciled 2026-09-03) — all owner/external

After PR #104, #105 and #106, every remaining M3 gap for the frozen
`baseline-trend-breakout-v1` manifest requires an owner-supplied artifact, a
licensed data source, credentials, or an owner-run. None is closable by
further autonomous implementation:

1. **Historical S&P 500 membership evidence not yet admitted as PIT truth.**
   The ingestion path (`config/data-sources/sp500-documents/` +
   `bash scripts/run_dataset_v1_real_qualification.sh`) is fully built and
   waiting empty. Automated retrieval from `spglobal.com` returns HTTP 403 /
   robots protection and this project does not circumvent that (ADR-0017).
   **Owner action required:** download official S&P DJI historical
   index-change announcement documents (HTML/text/CSV/text-layer PDF) and
   place them under `config/data-sources/sp500-documents/`, then run the
   qualification script. The historical-secondary registry (PR #104) may at
   most cross-check this evidence — it can never substitute for it
   (`eligible_for_promotion_without_validation: false`,
   `authoritative_official_evidence: false` on every candidate).
2. **True historical company-level GICS effective/knowledge-dated history.**
   Recorded `BLOCKED_EXTERNAL` / `LICENSE_REQUIRED` in
   `docs/poi/evidence/dataset-v1/asof-gics-blocker.json`: S&P DJI licenses
   company-level GICS assignments; SEC SIC is a different taxonomy and is not
   an acceptable substitute; no free, legitimate, company-level historical
   GICS series has been established. **Owner action required:** an explicit
   owner decision to license a qualifying provider (with an ADR, per
   CLAUDE.md's "no paid provider without owner approval"), or an owner
   decision to accept this as a permanent V1 gap and scope the strategy's
   GICS sector-concentration rule accordingly. No autonomous or free path
   exists.
3. **Survivorship-free delisted-name daily OHLCV.** IBKR does not price
   delisted names historically; no free source has been established (see
   `docs/status/PIT_DATASET_QUALIFICATION.md` §2/§4, blocker N-02/N-03 in
   `docs/status/COMPONENT_EVIDENCE_MATRIX.yaml`). **Owner action required:**
   select and approve an external market-data provider with delisted-name
   coverage, commercial terms and test credentials.
4. **Effective-dated corporate actions for the frozen universe.** The
   qualification pipeline (`qualify_corporate_actions`, SEC EDGAR / Form 25
   ingestion) and the new PIT structural validator (PR #106) both exist, but
   neither has been run against real data — that requires the owner-run
   `scripts/run_dataset_v1_real_qualification.sh` on a host with network
   access to SEC EDGAR (and, for cross-checking, an IBKR Paper session).
   **Owner action required:** execute that owner-run and record the resulting
   evidence under `docs/poi/evidence/`.

**No dependency-safe autonomous M3 implementation task remains.** Every path
forward requires an owner-supplied artifact, a licensing decision, or an
owner-run with network/broker access that this worker may not perform.
AutoDev must not select, plan, or fabricate work to close gaps 1–4 above; it
must wait for the named owner action and re-inspect `main` afterward.

### M4 — Historical replay / backtest qualification

**State:** PENDING after required A-WP2 dataset evidence is sufficient for the frozen strategy.

Requirements:

- deterministic replay;
- point-in-time inputs only;
- owner-approved costs/spread/slippage assumptions;
- no look-ahead;
- reproducible manifest/fingerprint;
- results tied to the exact frozen strategy version;
- explicit failure when required data is unavailable.

In-sample success alone does not make the strategy eligible.

### M5 — Final OOS / stress qualification

**State:** PENDING.

Requirements:

- untouched final out-of-sample window;
- no parameter refit based on OOS result;
- strategy/version fingerprint unchanged;
- costs and execution assumptions recorded;
- stress/regime coverage required by the canonical strategy-evidence policy;
- result recorded as evidence, not merely a passing test suite.

A failing OOS/stress result is not repaired by silently changing the same frozen version.

### M6 — Shadow qualification

**State:** PENDING.

Run the frozen strategy in evaluate-only/shadow mode with no submit authority. Required outputs include deterministic decision lineage, `NO TRADE` reasons, risk results, data freshness, and strategy-to-execution-intent traceability.

Shadow cannot mutate authorization or broker state.

### M7 — Autonomous runtime integration — Phase B

**State:** PENDING. PR #97 Phase A foundation is already merged.

Integrate, without creating an alternate submit path:

- owned IBKR Paper session;
- real market-data path;
- frozen strategy schedule/input;
- strategy -> decision -> risk -> sealed intent;
- canonical ExecutionAgent handoff;
- broker order/fill/position observations;
- startup + periodic reconciliation;
- reconnect/recovery;
- durable halt/kill-switch state;
- single-runtime ownership;
- real backend truth projection for operations/health surfaces where required.

Unknown/ambiguous state continues to block new entries.

### M8 — Owner-gated autonomous Paper authorization — Phase C

**State:** PENDING / DISABLED DEFAULT.

Only after the preceding gates are proven may the owner approve a narrowly scoped autonomous Paper authorization implementation/activation.

Requirements:

- Paper only;
- disabled by default;
- Live impossible through this path;
- exact environment/account safety proof;
- current-session reconciliation clean;
- durable halt state healthy;
- strategy/risk/intent identities match;
- final pre-submit evidence fresh;
- authorization cannot approve more quantity/risk than RiskEngine allowed;
- loss of required evidence blocks the next entry.

### M9 — End-to-end Paper qualification

**State:** PENDING.

Prove the real operational chain:

```text
startup
-> ownership
-> broker/market-data connectivity
-> reconciliation CLEAN
-> halt state OK
-> strategy evaluates
-> SIGNAL or NO TRADE
-> account/P&L/position/exposure/liquidity/freshness checks
-> risk approval/reduction/denial
-> Paper authorization
-> protected submit
-> parent + STOP broker acknowledgment
-> fill/order/position durable observation
-> strategy/risk exit lifecycle
-> reconciliation
-> clean restart/recovery behavior
```

Qualification must include failure-path evidence for unknown state, stale data, missing P&L, missing protection, ownership loss, reconnect/recovery mismatch, halt-store failure, duplicate callbacks, and idempotent restart behavior as applicable.

### M10 — MVP COMPLETE

`MVP COMPLETE` may be emitted only by deterministic completion logic when every required milestone/gate is proven.

Minimum completion artifact:

```json
{
  "project": "TradingSystem-MVP",
  "status": "COMPLETE",
  "final_main_sha": "<sha>",
  "frozen_strategy": "baseline-trend-breakout-v1",
  "required_workstreams_complete": true,
  "open_required_blockers": 0,
  "final_ci": "PASS",
  "final_paper_qualification": "PASS",
  "live_enabled": false,
  "completed_at": "<timestamp>"
}
```

An LLM saying "done", a service being `active`, a green unit test suite, or a merged PR by itself is not sufficient completion evidence.

---

## 6. Explicitly deferred / not on the current MVP critical path

The following may exist in the full-product architecture but must not delay the one-strategy autonomous Paper MVP unless the frozen strategy or a mandatory safety invariant actually depends on them:

- multiple simultaneous strategies;
- advanced multi-model Fusion;
- ML-controlled trade authority;
- social-source dependency for the selected baseline strategy;
- WhatsApp/Discord completion;
- advanced external-news automatic strategy;
- advanced fundamental portfolio optimization;
- production multi-broker support;
- quantity > 1 protected-entry lifecycle / advanced partial-fill protection resizing;
- advanced take-profit structures unless required by the frozen strategy;
- HFT / institutional execution algorithms;
- Live trading;
- automatic Live promotion;
- self-modifying active strategy;
- automatic parameter optimization after OOS observation;
- nonessential dashboard expansion.

Deferred does not mean deleted. It means outside the current MVP critical path.

---

## 7. Dashboard scope for the MVP

PR #96 provides the read-only operations-console shell. For MVP qualification, the dashboard is useful only to the extent it reports canonical backend truth.

Critical operator-visible states should ultimately include:

- environment and authorization mode;
- runtime ownership/state;
- IBKR and market-data health;
- reconciliation state;
- halt/kill-switch state;
- account/P&L state without UNKNOWN->0 coercion;
- open positions/orders;
- frozen strategy/decision/risk/execution lineage;
- exact blocker / next safe action;
- broker protection state;
- UNKNOWN states prominently.

Dashboard polish is not allowed to delay the core safe Paper loop.

---

## 8. AutoDev operating contract for this plan

AutoDev must use this plan as the current delivery-priority overlay while preserving the higher-priority safety/canonical sources listed above.

### 8.1 Work selection

For each autonomous work item, the Architect must identify:

- delivery milestone (`M3` ... `M9`);
- exact unmet acceptance criterion;
- repository evidence proving it is unmet;
- files/ownership boundaries;
- required tests/evidence;
- explicit safety non-goals.

Do not create work because a generic roadmap feature exists if it is deferred by this MVP plan.

### 8.2 Existing work ownership

AutoDev must not retroactively claim ownership of historical/manual PRs. Previously merged work is repository fact. New AutoDev PRs remain subject to the deterministic ownership and merge policy.

### 8.3 Merge gate

Autonomous merge remains permitted only when all configured deterministic gates pass, including at minimum:

- AutoDev ownership for AutoDev-created PRs;
- non-draft state;
- tested SHA == current PR HEAD;
- reviewed SHA == current PR HEAD;
- required local verification;
- required GitHub CI success;
- independent review success;
- structured Architect `MERGE` decision;
- no unresolved BLOCKER/HIGH finding;
- no unauthorized Live/PAPER_AUTO/broker/safety/secret weakening.

If required evidence cannot be read, merge fails closed.

### 8.4 Progress state

A running process is not progress. AutoDev/dashboard should distinguish:

```text
RUNNING
WAITING_EXTERNAL
IDLE
BLOCKED
STALLED
FAILED
COMPLETE
```

Durable progress includes a real state transition, commit, push, CI result, review result, Architect decision, merge, evidence publication, or completion of a work item.

Recommended policy:

- `STALLED`: no durable progress for >60 minutes and no explicitly known external wait.
- retry limit: after 3 unsuccessful autonomous repair attempts for the same root problem, stop and mark `BLOCKED_FOR_OWNER`.
- `COMPLETE`: only after the deterministic MVP completion gate in §5 M10.

### 8.5 `master_plan.json`

The external AutoDev state file should mirror this delivery plan and canonical repository facts. At minimum it should carry:

- current milestone/workstream;
- task;
- acceptance criterion;
- dependency state;
- current branch/PR/SHA;
- CI/review/Architect states;
- blocker;
- retry count;
- last durable progress timestamp;
- next action;
- remaining MVP milestones;
- final completion-gate state.

It must not mark a milestone complete solely from an LLM assertion.

---

## 9. Evidence vs implementation vocabulary

Do not collapse these into one percentage or status:

```text
IMPLEMENTED
TESTED_WITH_FAKES
INTEGRATION_VERIFIED / HISTORICALLY_TESTED
OUT_OF_SAMPLE_PASS
SHADOW_PASS
PAPER_PASS
```

Passing CI proves software quality for the tested contract. It does not by itself prove broker behavior, data completeness, strategy profitability, OOS validity, or Paper readiness.

When reporting progress, report at least:

- implementation state;
- evidence/qualification state;
- current critical-path milestone;
- explicit blocker count.

---

## 10. Change-control rules

Changes to any of the following require explicit owner decision and the applicable canonical versioning/governance process:

- frozen strategy semantics or numeric parameters;
- risk envelope limits;
- Paper/Live authority;
- broker submit authority;
- strategy eligibility criteria;
- evidence requirements;
- PIT/no-look-ahead requirements;
- critical-path definition or MVP completion gate.

When such a decision is made, update:

1. the canonical owner decision/frozen record;
2. this delivery plan if sequencing/scope changes;
3. relevant specification/ADR;
4. traceability/evidence status artifacts;
5. AutoDev `master_plan.json` projection.

Do not use chat history as the only durable record of a product/safety decision.

---

## 11. Immediate next-work rule

**Reconciled 2026-09-03.** AutoDev has already merged the A-WP2 owner-decision
(PR #100), forward-S&P-snapshot (PR #101), DatasetManifest/no-look-ahead (PR
#102), the S&P historical-secondary source registry (PR #104), the
current/forward S&P 500 + current-GICS archive hardening (PR #105), and the
corporate-action PIT validator (PR #106). **None of these may be recreated.**
A prior Architect pass duplicated PR #104's task; this reconciliation exists
to prevent that recurring.

Having re-inspected `main` against §5 M3 (including the "Validation machinery
vs. empirical integration evidence" table and the "Remaining M3 gaps"
subsection), **M3 is WAITING_EXTERNAL / BLOCKED_FOR_OWNER.** Every remaining
M3 acceptance criterion for the frozen strategy depends on one of: an
owner-supplied official S&P document, an owner licensing decision for
historical GICS, an owner decision on a delisted-name data provider, or an
owner-run of the real qualification pipeline against SEC EDGAR / IBKR. None of
these is a dependency-safe autonomous implementation task. The Architect must
**not** invent M3 implementation work to keep the loop occupied; it must
record `WAITING_EXTERNAL` and surface the four owner actions listed in §5.

M4 → M5 → M6 remain gated on M3 dataset evidence and stay `PENDING`. A later
milestone (e.g. parts of M7) may be worked in parallel **only** when doing so
does not consume or assume an unresolved upstream owner/evidence decision and
does not weaken fail-closed behavior — selecting such work is a separate
Architect decision, out of scope for this reconciliation.

---

## 12. Definition of success

The intended MVP is simple to state and strict to prove:

> **One frozen strategy trades autonomously in IBKR Paper, exactly according to its strategy rules, only when the mandatory risk/safety envelope allows it, with broker-resident protection, durable audit/idempotency, kill-switch, recovery/reconciliation, and fail-closed handling of every critical unknown.**

Everything else is either supporting evidence, an operator surface, or post-MVP product expansion.
