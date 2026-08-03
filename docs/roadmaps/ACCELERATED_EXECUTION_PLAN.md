# Accelerated Execution Plan — four parallel tracks

> Companion to [`TRADING_SYSTEM_MASTER_PLAN.md`](TRADING_SYSTEM_MASTER_PLAN.md).
> The master plan says *where the project is*; this says *what to do next and
> in what order*.
>
> **The acceleration is parallelism.** Strategy evidence and broker evidence
> are independent critical paths that have been treated as sequential. They are
> not. Track A needs no broker; Track B needs no strategy. Running them at the
> same time removes the single largest source of avoidable delay.
>
> Nothing here enables PAPER_AUTO or LIVE. Both stay **DISABLED**.

---

## Track overview

| Track | Name | Blocked by | Can start |
|---|---|---|---|
| **A** | Strategy and evidence | nothing | **now** |
| **B** | IBKR and operations | live market session + owner host | **now** |
| **C** | External sources | owner accounts | when accounts exist |
| **D** | AI and committee | AI provider + Track A baseline | after A-WP1 |

**A and B run fully in parallel.** C is owner-gated and can start the moment an
account appears. D is deliberately last: the committee's value can only be
*measured* against a baseline that Track A produces.

---

## Track A — Strategy and evidence

**Why first.** It is the only track with zero external blockers, and it
addresses the largest unknown in the project: whether there is any edge at all.
Every other track is plumbing by comparison.

### A-WP1 — Freeze the baseline hypothesis
- **Work:** owner completes [`STRATEGY_FREEZE_TEMPLATE.md`](../strategies/STRATEGY_FREEZE_TEMPLATE.md)
  for `baseline-trend-breakout-v1` (no social, no AI).
- **Dependencies:** none.
- **Evidence:** a committed freeze record with an owner approval reference and
  the freezing commit.
- **Exit:** status `FROZEN`, registered **before** any evaluation result is
  inspected.
- **Hard stop:** if any parameter is invented by tooling rather than decided by
  the owner, stop — the freeze is void.

### A-WP2 — Point-in-time research dataset
- **Work:** build the dataset with `as_of` semantics and full lineage; run the
  no-look-ahead audit.
- **Dependencies:** A-WP1 (so the required fields are known).
- **Parallel:** can overlap A-WP1 for the *ingestion* half.
- **Evidence:** dataset manifest + lineage + passing no-look-ahead audit.
- **Exit:** `persistence` and `replay` move off fakes.
- **Hard stop:** any look-ahead leak, or any field without a point-in-time
  timestamp.

### A-WP3 — Replay and in-sample backtest
- **Work:** deterministic replay of the frozen manifest; in-sample backtest with
  the **owner-approved cost, slippage and spread assumptions**.
- **Dependencies:** A-WP1, A-WP2.
- **Evidence:** reproducible run + metrics with confidence intervals.
- **Exit:** `HISTORICALLY_TESTED`.
- **Hard stop:** results inspected before the freeze; costs assumed rather than
  approved.

### A-WP4 — Walk-forward and out-of-sample
- **Work:** walk-forward on windows never touched in fitting; stress suite
  (gaps, halts, illiquidity, regime shift, outage); doubled-cost sensitivity.
- **Dependencies:** A-WP3.
- **Evidence:** OOS results + stress report + uncertainty analysis.
- **Exit:** `OUT_OF_SAMPLE_PASS` — or an honest **no-edge** conclusion.
- **Hard stop:** **any** parameter change after seeing OOS results. That
  requires a new `strategy_id` and a fresh untouched window.

### A-WP5 — Live shadow
- **Work:** a defined live-shadow window, evaluate-only, no orders.
- **Dependencies:** A-WP4 pass.
- **Evidence:** shadow report + divergence analysis vs backtest.
- **Exit:** `SHADOW_PASS`.
- **Hard stop:** any `SubmitAuthorization` minted on the shadow path.

### A-WP6 — Formal Go / No-Go
- **Work:** owner records GO / NO-GO / MORE-RESEARCH against the criteria
  frozen in A-WP1.
- **Dependencies:** A-WP4 + A-WP5.
- **Evidence:** a decision record.
- **Exit:** unblocks IMP-1401 **only** on GO.
- **Hard stop:** a GO without both OOS and Shadow evidence is invalid.

---

## Track B — IBKR and operations

**Why parallel.** None of this needs a strategy. It proves broker *mechanics*.

### B-WP1 — DEF-18 post-fix cancellation ◀ **the next broker-changing action**
- **Work:** fresh realtime quote → cancel-path dry run → new same-day token →
  **exactly one** `--execute` with `--journal-dir` → `verify-clean`.
- **Dependencies:** merged fixes (done); a **permitted live market session**.
- **Evidence:** artifact showing submit → ack → cancel accepted →
  **cancel confirmed** → never-filled (observed) → zero remaining lifecycle
  orders → clean reconciliation.
- **Exit:** DEF-18 PASS.
- **Hard stops:** never reuse the historical `F` prices; never reuse a token;
  **never retry after `UNKNOWN` or an unconfirmed cancellation** — inspect and
  cancel by hand instead. The harness never flattens.

### B-WP2 — Fill, protection, reconciliation
- **Work:** DEF-03/04 fill-path, DEF-05 protection verification, DEF-08
  reconciliation after a real fill.
- **Dependencies:** B-WP1 PASS; regular market hours.
- **Evidence:** a real fill; exactly one resting **GTC** protective STOP sized
  never above the position; reconciliation with zero unexplained findings.
- **Exit:** protection and reconciliation leave `TESTED_WITH_FAKES`.
- **Hard stop:** any unprotected fill ⇒ owner action, by hand, immediately.

### B-WP3 — DEF-20 restart-recovery cancellation
- **Work:** cancel a lifecycle order recovered after a process/Gateway restart,
  using the **same** `--client-id` that submitted it.
- **Dependencies:** B-WP1; can run **in parallel with B-WP2**.
- **Evidence:** recovered order cancelled by canonical `client_order_id`;
  post-cancel snapshot clean; no foreign/manual order ever touched.
- **Exit:** DEF-20 PASS; restart recovery proven.
- **Hard stop:** ambiguous recovered identity ⇒ nothing is cancelled (by
  design) ⇒ owner resolves by hand.

### B-WP4 — Host operations: B-02 / B-03 / B-04
- **Work:** chaos and recovery drills (B-02, DEF-11); upstream IBC/watchdog host
  patches with a recorded transcript (B-03, DEF-12); enable the watchdog deep
  probe (B-04, DEF-13).
- **Dependencies:** owner host access. **Fully parallel with B-WP1–3.**
- **Evidence:** host transcripts committed and reproducible from the repo.
- **Exit:** IMP-1303 satisfiable.
- **Hard stop:** no host secret, credential or account id enters the repo.

### B-WP5 — IMP-1302 / 1303 / 1304 closure
- **Work:** Paper Manual one-trade record; chaos-drill record; defect-register
  closure.
- **Dependencies:** B-WP1–4.
- **Evidence:** owner marks POI-IBKR-001 from real evidence.
- **Exit:** POI-IBKR-001 PASS; the Paper-Auto **precondition** (not permission)
  is met.

---

## Track C — External sources

**Why separate.** Every work package is blocked on something only the owner can
supply. Each is independent — one account unblocks exactly one package.

### C-WP1 — Social user sessions
- WhatsApp (POI-SOC-WA-USER), Discord (POI-SOC-DC-USER), Telegram user via
  TDLib (POI-SOC-TG-USER), then coexistence (POI-SOC-TG-COEX) and the
  supplemental bot (POI-SOC-TG-BOT).
- **Dependencies:** owner accounts + a controlled host; TDLib build.
- **Evidence:** masked per-source evidence proving the **user-session** path.
- **Exit:** each PoI PASS.
- **Hard stops:** the bot path may **never** substitute for the user path; no
  silent fallback; social input stays **read-only** for trading and may never
  invoke BUY/SELL/HALT/FLATTEN.

### C-WP2 — News, InvestingPro, TradingView
- **Dependencies:** owner-authorized access per source.
- **Evidence:** POI-NEWS-001, POI-INVPRO-001, POI-TV-001.
- **Exit:** each PoI PASS.
- **Hard stop:** no credential in the repo, ever.

### C-WP3 — External market-data provider (DEF-14)
- **Work:** provider selection ADR, benchmark, outage/fallback test,
  field-ownership matrix.
- **Dependencies:** an owner-approved provider with commercial terms.
- **Evidence:** provider ADR + benchmark + fallback drill.
- **Exit:** POI-MD-001 **aggregate** PASS.
- **Hard stop:** no provider is selected, assumed, purchased or integrated
  without owner approval.

### C-WP4 — Remaining P0/P1 PoIs
- POI-EMERG-001, POI-OBS-001.
- **Exit:** PHASE-5 exit gate becomes satisfiable.

---

## Track D — AI and committee

**Why last.** The committee's worth is a *comparative* claim. Without the Track
A baseline there is nothing to compare against, and an unmeasured committee is
just cost and latency on the critical path.

### D-WP1 — AI provider PoI
- **Work:** POI-AI-001 against an owner-authorized provider.
- **Dependencies:** owner credentials.
- **Evidence:** schema-validated structured output **plus** a malformed-output
  rejection drill.
- **Exit:** POI-AI-001 PASS.
- **Hard stop:** LLM output is untrusted structured input; unvalidated output
  never reaches a decision.

### D-WP2 — Committee I/O contract and replayable orchestration
- **Work:** define the 7-role contract; deterministic replay of reviews.
- **Dependencies:** D-WP1.
- **Evidence:** contract module + a review that replays identically.
- **Exit:** the committee becomes runnable — as a **decision input only**.
- **Hard stop:** the committee creates no `OrderIntent`, no
  `SubmitAuthorization`, sends no order, changes no strategy, promotes no
  version, and never bypasses Risk or Pre-Submit. **It is never an execution
  authority.**

### D-WP3 — Committee ablation
- **Work:** run the experiment matrix with the committee **on** and **off**
  against the Track A baseline.
- **Dependencies:** A-WP4, D-WP2.
- **Evidence:** ablation report with confidence intervals.
- **Exit:** an evidence-based decision to keep or drop the committee.
- **Hard stop:** if ablation shows no marginal value, the committee does **not**
  enter the decision path.

---

## Gated end-state (all tracks converge)

These stay **FROZEN** and are listed for completeness only.

| Stage | Gate |
|---|---|
| **IMP-1401** select one eligible strategy | A-WP6 GO **and** OOS + Shadow evidence |
| **IMP-1402 Restricted Paper Auto** | IMP-1302–1304 complete **and** IMP-1401 approved. Restricted scope: one strategy, minimal size, explicit allowlist, monitoring + auto-demote armed |
| **IMP-1403 / IMP-1404** | with IMP-1402 — auto-demote proven, probation reviewed |
| **IMP-1501** six-month Paper validation | PHASE-14 complete; a genuine six-month window, not a shortened one |
| **IMP-1502** evaluate | with IMP-1501 |
| **IMP-1503** future Live-readiness **recommendation** | with IMP-1501. **This produces a recommendation only — Live stays DISABLED.** |

---

## Global hard stops

Any one of these stops the affected track immediately:

1. An `UNKNOWN` broker state or an unconfirmed cancellation ⇒ **no retry**,
   reconcile and inspect by hand.
2. An unprotected real position ⇒ owner acts by hand; the harness never
   flattens.
3. A parameter changed after inspecting evaluation results ⇒ the OOS window is
   burned; new `strategy_id` required.
4. A GO recorded without both OOS and Shadow evidence ⇒ invalid.
5. Social input reaching an execution verb ⇒ architectural violation.
6. The AI committee described or wired as an execution authority ⇒
   architectural violation.
7. A credential, token or unmasked account id entering the repo.
8. PAPER_AUTO or LIVE enabled before every listed gate ⇒ prohibited.

---

## Sequencing summary

```
now ──┬── Track A: freeze ─▶ dataset ─▶ replay/backtest ─▶ OOS ─▶ shadow ─▶ Go/No-Go
      │
      ├── Track B: DEF-18 ─▶ fill/protection/reconciliation ─▶ DEF-20 ─▶ 1302/1303/1304
      │           └── B-WP4 host drills (fully parallel)
      │
      ├── Track C: (owner accounts) social ─ news ─ InvestingPro ─ TV ─ provider
      │
      └── Track D: (after A baseline) AI PoI ─▶ committee contract ─▶ ablation
                                   │
                                   ▼
                     IMP-1401 ─▶ IMP-1402 Paper Auto ─▶ IMP-1403/1404
                                   ▼
                     IMP-1501 six months ─▶ IMP-1502 ─▶ IMP-1503 recommendation
                                   ▼
                            LIVE remains DISABLED
```

---

## NOT EXECUTED

- ❌ no post-fix real cancel-path execute (DEF-18);
- ❌ no real cancellation after the latest changes;
- ❌ no fill-path owner-run;
- ❌ no Gateway restart-recovery cancellation (DEF-20);
- ❌ no Live connection and no Live order;
- ❌ no PAPER_AUTO activation.
