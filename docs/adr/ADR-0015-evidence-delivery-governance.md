# ADR-0015: Evidence Delivery pivot, status model and authority boundaries

## Status

ACCEPTED (owner decision, 2026-07-17)

Supersedes nothing. Complements ADR-0014 (external PoIs deferred; core
development on mocks/fixtures allowed). This ADR changes **how work is
selected and how claims are made** — it does not change the architecture, does
not mark any PoI PASS, and does not authorize any real activation.

## Context

72 of the 82 canonical tasks in the frozen `docs/baseline-v0.5/CLAUDE_CODE_TASKS.yaml`
are `IMPLEMENTED` (verified against `main`), yet almost the entire system is
proven only with fakes:

- The full order lifecycle exists and is exercised by 16 fakes-only scenarios
  (`tests/poi/test_paper_manual_lifecycle.py`); **no real order has ever been
  sent**.
- Every PHASE-9…PHASE-12 engine (fusion, decision, risk, allocator,
  pre-submit, replay, backtest, funnel, counterfactual, champion/challenger,
  policy workflow) runs on deterministic fixtures only. **No real historical
  dataset has been processed** — nothing is `HISTORICALLY_TESTED`.
- The only integration verified against the real world is
  **POI-IBKR-READONLY** (read-only scope). POI-MD-001 is partial/BLOCKED,
  POI-IBKR-001 has **not run**.
- No strategy has any profitability evidence. There is no frozen hypothesis,
  no point-in-time research dataset, no out-of-sample result and no shadow
  measurement.

Continuing to consume the backlog would add more `IMPLEMENTED`-but-unproven
surface. The scarce resource is **evidence**, not features.

## Decision

The owner approves the following, formally and bindingly.

### 1. Evidence Delivery pivot

Automatic progression through the old backlog **stops**. Work is selected from
`docs/roadmaps/EVIDENCE_ROADMAP.md`. No canonical task is deleted; each is
dispositioned Continue / Evidence / Frozen with explicit unfreeze conditions.
Delivering a feature is no longer "progress" unless it produces evidence.

### 2. Binding evidence status model

The vocabulary in `docs/status/CURRENT_PROJECT_STATUS.md` §1 is **binding** and
must be used verbatim in all status claims:

`IMPLEMENTED` · `TESTED_WITH_FAKES` · `INTEGRATION_VERIFIED` ·
`HISTORICALLY_TESTED` · `OUT_OF_SAMPLE_PASS` · `SHADOW_PASS` · `PAPER_PASS` ·
`PROFITABILITY_NOT_PROVEN` · `BLOCKED`

Rules: passing tests is **never** integration evidence; no integration is
`INTEGRATION_VERIFIED` without real masked evidence under
`docs/poi/evidence/`; every strategy is `PROFITABILITY_NOT_PROVEN` by default
until `OUT_OF_SAMPLE_PASS` **and** `SHADOW_PASS` exist; a status is never
inferred from chat memory — only from the repository.

### 3. The three tracks

- **Track A — Critical Integration Evidence.** Realtime market-data evidence →
  POI-MD-001 close-out → owner-approved Paper lifecycle CLI → POI-IBKR-001 →
  short Paper Manual → IMP-1302…1304.
- **Track B — Strategy Evidence.** Research contracts → hypothesis freeze →
  point-in-time datasets → replay/backtest → train/validation/test →
  walk-forward → out-of-sample → regime analysis → stress tests → shadow →
  formal Go/No-Go.
- **Track C — Frozen Expansion.** IMP-1401…IMP-1503, Paper Auto, Live, new
  sources, new AI models, non-research Dashboard expansion, multi-Gateway,
  unrestricted parameter search, automatic strategy promotion. **Frozen, not
  cancelled**; each item has a written unfreeze condition.

### 4. Authority boundaries (binding)

| Actor | May | May NOT |
|---|---|---|
| **Owner** | approve strategies & versions, Strategy Manifests, parameters, risk limits, universe/allowlist, promotion & rollback, and transitions Shadow → Restricted Paper Auto → Live | approve routine individual trades (see §5) |
| **AI Committee** | produce assessments, objections, missing-evidence lists, risk flags; participate as `REQUIRED`/`VETO`/`SOFT_SCORE`/`BONUS`/`CONTEXT` per the Strategy Manifest | create `OrderIntent`; create `SubmitAuthorization`; send any order; change a strategy; promote a version; bypass Risk or Pre-Submit |
| **Decision Engine** | derive ENTER / WATCH / REJECT automatically from the Manifest + committee output | override Risk; submit anything |
| **Risk Engine** | veto absolutely, including when the committee supports | size beyond policy; submit |
| **Allocator** | size within the approved envelope | increase beyond the Risk-approved quantity |
| **Pre-Submit** | act as the final deterministic fail-closed gate | pass anything without a valid authorization path |
| **Execution Agent** | send orders to the broker — the **only** component that may | invent or edit any sealed pipeline output |

Target flow: Sources → Analysis Engines → AI Committee Assessments → Decision
Engine → Risk Engine → Allocator → Pre-Submit → Execution Agent → Protection →
Reconciliation.

### 5. The Owner does not approve routine individual trades

Owner authority is exercised over **strategies, versions, envelopes and mode
transitions** — not per-trade. Manual per-trade approval exists **only** inside
the Paper Manual integration-validation stage (§6) and is explicitly a
temporary scaffold, never the operating model.

### 6. Paper Manual is a short integration-validation stage only

Paper Manual exists to prove submit / ack / partial fill / fill / protection /
cancel / reconnect / reconciliation against a real broker. It is **not** an
operating model and must not become the steady state. Its DoD remains the
canonical IMP-1302 criterion: zero duplicate orders and zero required
unprotected positions.

### 7. Mode progression

**Shadow → Restricted Paper Auto → validation → future Live readiness.**
Each transition requires explicit owner approval and the evidence gates of the
preceding stage. Restricted Paper Auto is the intended operating model after
Track B evidence. **Live remains disabled**; IMP-1503 produces a
recommendation only, never an activation.

### 8. Automatic strategy promotion is prohibited

No component — including the AI Committee, the champion/challenger arena and
any metric, timer or model result — may promote a strategy or version, or move
an active pointer. Promotion and rollback are documented **human** actions
(consistent with the existing policy-change workflow: append-only pointer
records, mandatory human actor, no auto-promotion path).

### 9. Proposed EVD tasks remain non-canonical

`EVD-0001` … `EVD-0013` (Evidence Roadmap, Track B) are **proposals only**.
They are **not** canonical tasks and confer no authority to implement. The
frozen baseline `docs/baseline-v0.5/CLAUDE_CODE_TASKS.yaml` is **not modified
by this ADR**, because no existing governance rule authorizes amending the
frozen baseline; CLAUDE.md requires an explicit ADR + owner approval for any
such change.

**Governance process to make an EVD task canonical** (all steps required, in
order):

1. A dedicated amendment ADR (e.g. `ADR-0016`) proposing the exact task ids,
   titles, scope, deliverables, `done_criteria`, `forbidden` list and
   dependencies, plus the phase they attach to.
2. Explicit owner approval recorded in that ADR (`ACCEPTED` + date).
3. A single follow-up task — **"Amend CLAUDE_CODE_TASKS.yaml with
   governance-approved EVD tasks"** — that applies the amendment in one
   reviewable PR, changing nothing else.
4. Traceability updated (each new task mapped to a requirement/test id) and
   `docs/status/CURRENT_PROJECT_STATUS.md` counts recomputed **from the YAML**.
5. Until steps 1–4 complete, EVD work may only produce **documents and
   proposals** — no application code under an EVD id.

### 10. Future AI Dashboard governance requirements

Binding requirements for the future Operations-Console AI administration
(design constraints recorded now; **nothing is implemented in this ADR** and
**no API key is added or connected**):

- **Configurable through the Dashboard:** AI provider and model selection;
  role→model assignment for every committee role; prompt **versions**;
  budgets; timeouts; retries; rate limits; and the fallback policy.
- **Secrets storage:** API keys are stored **only** in a server-side encrypted
  secret store. Never in the repository, never in client-side state, never in
  a browser-reachable response, never in logs or telemetry.
- **Write-only secrets:** a secret is **never returned or displayed after
  save** — no read-back endpoint, no "reveal" control, no prefill.
- **Permitted UI surface only:** configured / not-configured, a non-reversible
  fingerprint, connection status, last validation time and result, and
  rotation metadata (created/rotated/expires). Nothing else.
- Any provider/model/prompt/budget change is an audited, attributed action;
  it never changes trading mode, risk limits, promotion state or PoI status.
- These requirements do **not** authorize building the screens: that needs its
  own governance-approved task (§9) and, for runtime-mutable settings, the
  separately-deferred owner-gated settings decision.

## Consequences

- Status claims become falsifiable and comparable; "done" no longer implies
  "works" or "profitable".
- Track C work is paused, which slows visible feature output on purpose.
- Nothing here unblocks Track A: realtime entitlement, POI-MD-001 and
  POI-IBKR-001 remain the gating evidence, and the Paper lifecycle CLI still
  requires separate explicit owner approval before it is built.
- The frozen baseline stays untouched until the §9 process runs.

## Compliance / non-goals of this ADR

This ADR writes **no application code**, connects to **no** broker, sends
**no** order, adds **no** API key, implements **neither** the AI Committee
**nor** any Dashboard screen, does not start IMP-1302, and does not modify any
PoI status or `CLAUDE_CODE_TASKS.yaml`.

## Next task after approval

**Track A / A1 — run POI-MD-001 realtime evidence when the market-data
entitlement becomes active.** No parallel implementation task is opened.
