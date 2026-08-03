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
| **B** | IBKR and operations | permitted non-closed session + owner host | **now** |
| **C** | External sources | owner accounts | when accounts exist |
| **D** | AI and committee | provider authorization (ablation only: Track A baseline) | **now**, on provider authorization |

**A and B run fully in parallel.** C is owner-gated and can start the moment an
account appears. D's provider work can start as soon as the owner authorizes a
provider; only the committee **ablation** waits for Track A, because its value
is a comparative claim that needs a baseline to be measured against.

> ### ⛔ Parallelism is ACROSS tracks, never between broker-mutating runs
> **Parallelism applies to Tracks A/B/C/D — not to state-changing broker
> experiments on one account.** On the same Paper account and Gateway, the
> following must **never overlap**: DEF-18 cancel-path execution ·
> fill/protection/reconciliation · DEF-20 restart-recovery cancellation ·
> Gateway/process restarts · destructive host chaos drills.
>
> Two concurrent owner-runs on one account cannot be reconciled: an order or
> position observed by one run may have been created, cancelled or orphaned by
> the other, a restart can sever a run mid-flight, and the resulting evidence
> proves nothing about either. Serialize them; see the global hard stops.

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
- **Dependencies:** merged fixes (done); a **permitted non-closed session
  (regular, pre-market or after-hours)** with a fresh positive two-sided quote
  and every existing freshness, spread and non-marketable guard satisfied. A
  **closed** session is refused — it has no live two-sided quote. Regular hours
  are **not** required for a cancel-path run.
- **Serialization:** exclusive. No other broker-mutating run and no
  Gateway/process restart may be in progress on this account.
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
- **Dependencies:** B-WP1 PASS; **market-open conditions appropriate for a real
  fill** (a marketable entry must actually be fillable — unlike the cancel path,
  this genuinely needs an open, liquid session).
- **Serialization:** exclusive. Never concurrent with DEF-18, DEF-20, a
  Gateway restart or a destructive drill.
- **Evidence:** a real fill; exactly one resting **GTC** protective STOP sized
  never above the position; reconciliation with zero unexplained findings.
- **Exit:** protection and reconciliation leave `TESTED_WITH_FAKES`.
- **Hard stop:** any unprotected fill ⇒ owner action, by hand, immediately.

### B-WP3 — DEF-20 restart-recovery cancellation *(own window, after DEF-18)*
- **Work:** cancel a lifecycle order recovered after a process/Gateway restart,
  using the **same** `--client-id` that submitted it.
- **Dependencies:** B-WP1 (DEF-18) complete. It runs in its **own controlled
  clean-account window** — `verify-clean` must PASS immediately before it
  starts, so the only working order on the account is the one this drill
  deliberately created.
- **Serialization:** ⛔ **exclusive — NOT concurrent with B-WP2.** DEF-20
  deliberately restarts the Gateway/process while an order is working. If a
  fill-path run were live at that moment the restart could sever it mid-flight,
  and neither run's evidence would be interpretable. Run B-WP2 and B-WP3 in
  separate windows, in either order, with a passing `verify-clean` between them.
- **Evidence:** recovered order cancelled by canonical `client_order_id`;
  post-cancel snapshot clean; no foreign/manual order ever touched.
- **Exit:** DEF-20 PASS; restart recovery proven.
- **Hard stop:** ambiguous recovered identity ⇒ nothing is cancelled (by
  design) ⇒ owner resolves by hand. Never begin while any other broker-mutating
  run is in progress.

### B-WP4a — Non-mutating operational hardening *(safe to parallelize)*
Touches no broker state and restarts nothing, so it may run alongside any
other track and alongside B-WP1–3.
- **Work:** repository / systemd / IBC configuration and **upstreaming the host
  patches with a recorded transcript (B-03, DEF-12)**; watchdog code and
  **read-only** deep-probe preparation (B-04, DEF-13 groundwork); port-exposure
  audit; journal directory permissions; log rotation; NTP/chrony verification;
  reproducibility documentation for the host build.
- **Dependencies:** owner host access. **Runs in parallel — it changes no
  broker state and triggers no restart.**
- **Evidence:** committed configuration, audit output and host build docs.
- **Exit:** the host is reproducible from the repo; the deep probe is ready to
  arm.
- **Hard stop:** no host secret, credential or account id enters the repo. If a
  step would restart the Gateway or touch an order, it belongs in B-WP4b.

> NTP/chrony matters more than it looks: evidence timestamps are UTC
> wall-clock (D-CLOCK-01), so a drifting host clock silently corrupts every
> artifact it stamps.

### B-WP4b — Destructive / open-lifecycle host drills *(scheduled window only)*
- **Work:** chaos and recovery drills (B-02, DEF-11); Gateway/process restart
  drills; arming the deep probe against a live Gateway; anything exercised with
  an order open.
- **Dependencies:** owner host access **and an explicitly scheduled owner-run
  window**.
- **Serialization:** ⛔ **exclusive.** Must **never** run while DEF-18, the
  fill-path or DEF-20 is in progress. Start from a passing `verify-clean`.
- **Evidence:** host transcripts committed and reproducible from the repo.
- **Exit:** IMP-1303 satisfiable.
- **Hard stop:** no host secret, credential or account id enters the repo. Any
  position or order left behind is resolved by the owner, by hand — the harness
  never flattens.

### B-WP5 — IMP-1302 / 1303 / 1304 closure
- **Work:** Paper Manual one-trade record; chaos-drill record; defect-register
  closure.
- **Dependencies:** B-WP1, B-WP2, B-WP3, B-WP4a, B-WP4b.
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

**Only the ablation waits.** The committee's *worth* is a comparative claim, so
**D-WP3 ablation** needs the Track A baseline. Everything before it does not:
provider governance, secret storage, budgets, timeouts, rate limits and
malformed-output rejection are safe, useful and independent, and they may run
**in parallel with Track A**. Deferring the whole track would waste that
window; running the ablation early would produce a number with nothing to
compare it to.

### D-WP1 — AI provider PoI *(may start immediately)*
- **Work:** POI-AI-001 against an owner-authorized provider.
- **Dependencies:** the owner supplies an **authorized provider, credentials
  and an evaluation budget**. Nothing else. **This may start immediately once
  those exist — it does not wait for Track A.**
- **Parallel:** runs alongside Track A.
- **Evidence:** schema-validated structured output **plus** a malformed-output
  rejection drill.
- **Exit:** POI-AI-001 PASS.
- **Hard stop:** LLM output is untrusted structured input; unvalidated output
  never reaches a decision. No credential in the repo.

### D-WP1b — Provider governance *(parallel with Track A)*
- **Work:** provider governance and selection record; secret storage; spend
  budgets and caps; request timeouts; rate limiting and backoff;
  malformed-output rejection paths.
- **Dependencies:** none beyond D-WP1's provider authorization. **Runs in
  parallel with Track A.**
- **Evidence:** governance record + budget/timeout/rate-limit configuration +
  a rejection drill.
- **Exit:** the provider can be used within owner-approved limits.
- **Hard stop:** no uncapped spend; no unbounded retry; the kill-switch never
  waits for an LLM.

### D-WP2 — Committee I/O contract and replayable orchestration
- **Work:** define the 7-role contract; deterministic replay of reviews.
- **Dependencies:** D-WP1 **and explicit owner approval to begin**. Contract
  groundwork may then proceed in parallel with Track A — it is design and
  plumbing, not evaluation.
- **Evidence:** contract module + a review that replays identically.
- **Exit:** the committee becomes runnable — as a **decision input only**.
- **Hard stop:** the committee creates no `OrderIntent`, no
  `SubmitAuthorization`, sends no order, changes no strategy, promotes no
  version, and never bypasses Risk or Pre-Submit. **It is never an execution
  authority.**

### D-WP3 — Committee ablation
- **Work:** run the experiment matrix with the committee **on** and **off**
  against the Track A baseline.
- **Dependencies:** ⛔ **gated** — the **frozen Track A baseline** (A-WP1) plus
  the required historical/OOS evidence (A-WP3, A-WP4), and D-WP2. This gate
  does not move.
- **Evidence:** ablation report with confidence intervals.
- **Exit:** an evidence-based decision to keep or drop the committee.
- **Hard stop:** **no committee output enters the decision path before the
  ablation demonstrates marginal value.** If it shows none, the committee does
  **not** enter the decision path.

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

1. ⛔ **No two broker-mutating or Gateway-restarting owner-run procedures may
   run concurrently on the same Paper account/Gateway.** DEF-18, fill-path,
   DEF-20, Gateway/process restarts and destructive chaos drills are each
   **exclusive**: one at a time, each starting from a passing `verify-clean`.
   Non-mutating work (B-WP4a, and Tracks A/C/D) is unaffected and stays
   parallel.
2. An `UNKNOWN` broker state or an unconfirmed cancellation ⇒ **no retry**,
   reconcile and inspect by hand.
3. An unprotected real position ⇒ owner acts by hand; the harness never
   flattens.
4. A parameter changed after inspecting evaluation results ⇒ the OOS window is
   burned; new `strategy_id` required.
5. A GO recorded without both OOS and Shadow evidence ⇒ invalid.
6. Social input reaching an execution verb ⇒ architectural violation.
7. The AI committee described or wired as an execution authority ⇒
   architectural violation.
8. A credential, token or unmasked account id entering the repo.
9. PAPER_AUTO or LIVE enabled before every listed gate ⇒ prohibited.

---

## Sequencing summary

```
now ──┬── Track A: freeze ─▶ dataset ─▶ replay/backtest ─▶ OOS ─▶ shadow ─▶ Go/No-Go
      │
      ├── Track B: broker-mutating runs are SERIALIZED, one window each:
      │     DEF-18 ─▶ verify-clean ─▶ fill/protection/reconciliation
      │            ─▶ verify-clean ─▶ DEF-20 ─▶ verify-clean ─▶ 1302/1303/1304
      │     (B-WP2 and B-WP3 may be swapped, never overlapped)
      │     └── B-WP4a non-mutating hardening ....... parallel, always safe
      │     └── B-WP4b destructive drills ........... own scheduled window only
      │
      ├── Track C: (owner accounts) social ─ news ─ InvestingPro ─ TV ─ provider
      │
      └── Track D: AI PoI + governance (parallel with A) ─▶ committee contract
                     ─▶ ablation (GATED on the frozen A baseline + OOS)
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
