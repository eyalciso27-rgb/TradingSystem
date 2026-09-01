# Trading System — Master Plan (canonical source of truth)

> **This is the authoritative project map.** It consolidates the baseline task
> list, the PoI registry, the Phase-13 deferred ledger, the defect register and
> the evidence roadmap into one place.
>
> Verified against `main` @ `b4d65de` (PR #55 merged, 2026-08-02).
> The **repository is authoritative**: where this document and any chat summary
> disagree, the repo wins. **Tests passing is never integration evidence.**
>
> Machine-readable companion: [`docs/status/COMPONENT_EVIDENCE_MATRIX.yaml`](../status/COMPONENT_EVIDENCE_MATRIX.yaml).
> The two are cross-checked by `tests/status/test_project_consistency.py`; if
> they disagree, CI fails.
>
> **After this lands, do not open planning-only PRs** unless a real milestone
> or an owner decision changes.

---

## 1. Headline verdict

| Question | Answer |
|---|---|
| Is the system built? | **Mostly in code** — **69 of 82** canonical tasks are `IMPLEMENTED`. Four PHASE-6 source adapters with an official/ToS-permitted path (Telegram-user/TDLib, Telegram-bot, News/filings, TradingView) are now built (code, fakes-tested; real PoIs BLOCKED). Three (WhatsApp-user, Discord-user, InvestingPro) remain **NOT built** — no ToS-permitted access path. |
| Is it integrated with the real world? | **Almost nowhere.** 2 of 15 PoIs PASS. |
| Can it trade? | **No.** Paper Manual is blocked; Paper Auto and Live are **DISABLED**. |
| Is any strategy profitable? | **Unknown — `PROFITABILITY_NOT_PROVEN`.** The baseline hypothesis `baseline-trend-breakout-v1` is now FROZEN and owner-approved; no point-in-time dataset, OOS or shadow evidence exists yet. |
| What is the single biggest gap? | **Evidence, not code.** |

The project's centre of gravity has moved from *building* to *proving*. Nearly
all remaining risk is in things no amount of further coding can resolve: real
broker mechanics, real external sources, real historical data, and an owner
decision about what the system is actually supposed to trade.

---

## 2. Architecture and components

```
        SOURCES (read-only for trading input, always)
  WhatsApp·Discord·Telegram(user+bot)·InvestingPro·News·TradingView
                              │
                       normalization
                              ▼
   MARKET DATA ─────▶  ANALYSIS ENGINES  ◀───── AI PROVIDER (untrusted,
   instrument master   technical·fundamental·           schema-validated)
   calendar·features   news/NLP·regime·registry
                              │
                              ▼
                    AI REVIEW COMMITTEE  ── input only, never authority
                              │
                              ▼
   STRATEGY MANIFEST ─▶ FUSION ─▶ DECISION ─▶ RISK (absolute veto)
                                                 │
                                                 ▼
                                            ALLOCATOR
                                                 │
                                                 ▼
                                    FINAL PRE-SUBMIT CHECK
                                    (single-use SubmitAuthorization)
                                                 │
                                                 ▼
                                       EXECUTION AGENT  ← the ONLY submit path
                                                 │
                                                 ▼
                              BROKER PORT ─▶ IBKR PAPER ADAPTER
                                                 │
                              PROTECTION · RECONCILIATION (recommends only)
```

Cross-cutting: persistence (point-in-time, outbox/inbox), security
(auth/RBAC/audit/trust/sessions), operations console, emergency halt,
observability, replay/backtest/shadow, IBC + watchdog on the ARM64 host.

**Binding architectural rules** (from `CLAUDE.md` and the baseline):

- Social messages may **never** directly invoke BUY, SELL, HALT or FLATTEN.
- Social adapters are **read-only** for trading input in the MVP.
- The user-session path is primary; a bot path is supplemental and may never
  silently substitute for it.
- IBKR is the first broker adapter, **not** a domain dependency — no
  IBKR identifier or SDK import exists outside `trading_broker_ibkr`
  (enforced by `scripts/check_architecture.py`).
- Paper and Live are isolated in code, config, credentials, queues, accounts,
  UI and deployment.
- The Risk Engine has **absolute veto**. The kill-switch is deterministic and
  never waits for an LLM.
- LLM output is **untrusted structured input** and must pass schema validation.
- Point-in-time semantics; no look-ahead in replay/backtest.

---

## 3. The 82 canonical tasks

Counted from [`docs/baseline-v0.5/CLAUDE_CODE_TASKS.yaml`](../baseline-v0.5/CLAUDE_CODE_TASKS.yaml)
— **82 tasks across 16 phases**. This count is asserted in CI.

| Phase | Name | Tasks | State |
|---|---|---|---|
| PHASE-0 | Repository and Governance | 4 | ✅ IMPLEMENTED |
| PHASE-1 | Canonical Contracts and Configuration | 5 | ✅ IMPLEMENTED |
| PHASE-2 | Platform Data and Messaging | 5 | ✅ IMPLEMENTED |
| PHASE-3 | Security, Identity, Audit and Sessions | 5 | ✅ IMPLEMENTED |
| PHASE-4 | Infrastructure, Docker and Operations Core | 5 | ✅ IMPLEMENTED |
| PHASE-5 | Proof-of-Integration Execution | 3 | ⚠️ code complete, **exit gate NOT satisfied** |
| PHASE-6 | Production Source Adapters | 8 | 🟡 **4 of 7 adapters BUILT** (code, fakes-tested; real PoIs BLOCKED): IMP-0603 Telegram-user/TDLib, IMP-0604 Telegram-bot, IMP-0607 news/filings, IMP-0608 TradingView, on top of IMP-0605 normalization. IMP-0601 WhatsApp-user, IMP-0602 Discord-user, IMP-0606 InvestingPro remain **NOT_IMPLEMENTED** (no ToS-permitted access path) |
| PHASE-7 | Instrument, Market Data and Trading Calendar | 5 | ✅ IMPLEMENTED |
| PHASE-8 | Research and Signal Engines | 6 | ✅ IMPLEMENTED (fakes only) |
| PHASE-9 | Fusion, Decision, Risk and Portfolio | 6 | ✅ IMPLEMENTED (fakes only) |
| PHASE-10 | Broker Execution and Position Management | 6 | ✅ IMPLEMENTED (fakes + 1 partial real run) |
| PHASE-11 | Operations Console | 7 | ✅ IMPLEMENTED |
| PHASE-12 | Replay, Backtesting and Calibration | 6 | ✅ IMPLEMENTED (fakes only) |
| PHASE-13 | End-to-End Shadow and Paper Manual | 4 | 🟡 **OFFLINE_ENGINEERING_COMPLETE**: IMP-1301 shadow harness IMPLEMENTED; IMP-1302 Paper-Manual + IMP-1303 chaos/recovery are **EVIDENCE_PENDING** (owner-run, gated on POI-IBKR-001 **BLOCKED**); IMP-1304 defects ongoing |
| PHASE-14 | Restricted Paper Auto | 4 | 🧊 FROZEN |
| PHASE-15 | Long-run Operation and Live Readiness | 3 | 🧊 FROZEN |

### 3.1 Completed — 69 tasks

`IMP-0001 … IMP-1206` plus `IMP-1301`, plus the **four PHASE-6 adapters with an
official / ToS-permitted access path** (IMP-0603, IMP-0604, IMP-0607, IMP-0608)
built by the PHASE-6 package, **minus the three ToS-blocked adapters**
(IMP-0601, IMP-0602, IMP-0606). `IMPLEMENTED` means *merged to `main` with code,
tests and docs*. It says **nothing** about real integration or profitability —
the four new adapters are tested with deterministic fakes only and their
real-account PoIs remain **BLOCKED**.

> **PHASE-6 correction (2026-08 code re-audit).** A re-audit of `main` found
> `apps/connectors` is an "IMP-0001 skeleton placeholder, no business logic"
> and that no adapter module or adapter test existed; the over-stated "72
> IMPLEMENTED" was corrected to 65 (only IMP-0605 normalization existed).
>
> **PHASE-6 implementation package (2026-08-13).** The four adapters that have
> an official/ToS-permitted path were then built as real production modules
> under `packages/py/application/src/trading_application/source_adapters/`
> (`telegram_user.py`, `telegram_bot.py`, `filings.py`, `tradingview.py`) on a
> shared `framework.py` + `social_base.py`, each with deterministic-fake
> contract/reconnect/failure tests under `tests/adapters/` — lifting the count
> to **69**. Their code is IMPLEMENTED; their PoIs stay BLOCKED. The three
> ToS-blocked adapters were deliberately **not** built (see §3.2 and
> `docs/status/PHASE6_SOURCE_ADAPTERS.md`); no unofficial client was fabricated.

> **Strategy Evidence Foundation + Source Activation (2026-08-13).** Two
> readiness packages were added. **Track A** (strategy freeze, point-in-time
> dataset foundation, backtest readiness, real-data ingestion readiness) makes
> the research pipeline production-ready **without inventing any trading
> parameter**: `DatasetStatus` stays `REAL_DATASET_NOT_BUILT` and every run
> remains `PROFITABILITY_NOT_PROVEN` — freezing a strategy alone is not
> evidence; a point-in-time dataset, a walk-forward/Final-OOS pass and a
> shadow-evidence pass are all still required (see the A-WP1 entry below: the
> baseline was frozen the same day). **Track C** takes the four merged
> adapters to
> `ACTIVATION_READY` — production secret-store integration, read-only preflight,
> sanitized PoI evidence artifacts, and per-source owner runbooks
> (`docs/status/PHASE6_SOURCE_ACTIVATION.md`) — while their real-account PoIs
> stay **BLOCKED** and a FAKE probe can only ever be `NOT_EVIDENCE`, never PASS.
> **These are readiness machinery for existing tasks; the canonical IMPLEMENTED
> count is unchanged at 69.**

> **A-WP1 — baseline hypothesis FROZEN (2026-08-13).** The owner completed
> `STRATEGY_FREEZE_TEMPLATE.md` for `baseline-trend-breakout-v1` and it
> transitioned DRAFT → FROZEN: 34/34 mandatory owner parameters recorded with
> provenance, the structural fusion manifest (`technical.breakout` /
> `REQUIRED` / `TRIGGERED`, no invented thresholds), owner_approval_ref
> `OWNER-EYAL-2026-08-13-BASELINE-TREND-BREAKOUT-V1`, and an immutable
> fingerprint pinned by `tests/strategy/test_frozen_baseline_manifest.py`
> (`docs/strategies/baseline-trend-breakout-v1.yaml`). No evaluation result was
> inspected before the freeze; no parameter is invented. This resolves the
> freeze step only — `DatasetStatus` stays `REAL_DATASET_NOT_BUILT`, no
> backtest has run, and the strategy remains `PROFITABILITY_NOT_PROVEN` with
> no `OUT_OF_SAMPLE_PASS` or `SHADOW_PASS` evidence.

> **PHASE-5 caveat.** `IMP-0501…0503` are implementation-complete (registry,
> evidence structure, drills, provider catalog) but the **exit gate is not
> satisfied**: 2 of 12 P0 PoIs PASS, 10 are BLOCKED, and 0 alternate ADRs
> exist. An ADR may not be created merely to paper over a missing account.

### 3.2 Remaining — 13 tasks

| Task | Title | Disposition |
|---|---|---|
| IMP-0601 | Build WhatsApp user adapter | **NOT_IMPLEMENTED** — no ToS-permitted personal-user path; blocked on owner ADR decision |
| IMP-0602 | Build Discord user adapter | **NOT_IMPLEMENTED** — user self-bot violates Discord ToS; blocked on owner ADR decision |
| IMP-0606 | Build InvestingPro adapter | **NOT_IMPLEMENTED** — no official API; scraping violates ToS; blocked on owner ADR decision |
| IMP-1302 | Run Paper Manual | Evidence (Track B) — gated on POI-IBKR-001 |
| IMP-1303 | Run chaos and recovery drills | Evidence (Track B) — B-02/B-03/B-04 |
| IMP-1304 | Close operational defects | Continue as defects surface |
| IMP-1401 | Select one eligible strategy | 🧊 Frozen until Track A Go |
| IMP-1402 | Enable restricted auto scope (**Paper Auto**) | 🧊 Frozen |
| IMP-1403 | Monitor and auto-demote | 🧊 Frozen with IMP-1402 |
| IMP-1404 | Complete probation review | 🧊 Frozen with IMP-1402 |
| IMP-1501 | Operate for minimum six months | 🧊 Frozen until PHASE-14 complete |
| IMP-1502 | Evaluate strategy and operations | 🧊 Frozen with IMP-1501 |
| IMP-1503 | Produce future Live readiness recommendation | 🧊 Frozen; **Live stays disabled** |

Nothing is deleted. FROZEN means paused with explicit unfreeze conditions.

---

## 4. Evidence status

Vocabulary is binding (ADR-0015): `IMPLEMENTED` → `TESTED_WITH_FAKES` →
`INTEGRATION_VERIFIED` / `HISTORICALLY_TESTED` → `OUT_OF_SAMPLE_PASS` →
`SHADOW_PASS` → `PAPER_PASS`. Every strategy defaults to
`PROFITABILITY_NOT_PROVEN`.

### 4.1 PoI registry — 2 PASS of 15

| PoI | Priority | Status |
|---|---|---|
| POI-PLAT-ARM-001 | P0 | ✅ **PASS** |
| POI-SEC-SESS-002 | P0 | ✅ **PASS** |
| POI-MD-001 | P0 | ❌ BLOCKED (realtime **sub-gate** PASS) |
| POI-IBKR-001 | P0 | ❌ BLOCKED (read-only pre-check PASS; DEF-19 PASS) |
| POI-SOC-WA-USER | P0 | ❌ BLOCKED — owner account |
| POI-SOC-DC-USER | P0 | ❌ BLOCKED — owner account |
| POI-SOC-TG-USER | P0 | ❌ BLOCKED — TDLib + owner login |
| POI-SOC-TG-COEX | P0 | ❌ BLOCKED — depends on the two above |
| POI-INVPRO-001 | P0 | ❌ BLOCKED — authorized login + browser host |
| POI-NEWS-001 | P0 | ❌ BLOCKED — provider access |
| POI-AI-001 | P0 | ❌ BLOCKED — AI provider credentials |
| POI-EMERG-001 | P0 | ❌ BLOCKED |
| POI-SOC-TG-BOT | P1 | ❌ BLOCKED |
| POI-TV-001 | P1 | ❌ BLOCKED |
| POI-OBS-001 | P1 | ❌ BLOCKED |

### 4.2 Real integration — what has actually touched the outside world

**Proven:**

- ARM64 host + container toolchain (POI-PLAT-ARM-001).
- Session security (POI-SEC-SESS-002).
- IBKR **read-only** market data: `marketDataType=1 REALTIME`, no entitlement
  error, valid two-sided quote, clean disconnect — sub-gate 2026-07-21 and an
  `F` observation 2026-07-31.
- IBKR **read-only** account verification: DEF-19 `verify-clean` **PASS**
  (2026-07-30T17:49:25Z) — zero working lifecycle orders, zero non-zero
  positions, zero unresolved broker state, reconciliation `findings=0`, clean
  disconnect.

**Attempted and failed (then fixed, not re-proven):** one real Paper cancel-path
owner-run reached the broker. Submit, acknowledgement and idempotent resubmit
passed; **the cancellation failed** and the owner cancelled by hand. Root causes
are fixed (`D-CANCEL-01…08`, `D-CLOCK-01…04`) but **no post-fix real order or
cancellation exists**.

**Never done:** any real fill, any real protective stop, any restart-recovery
cancellation, any social/news/AI/InvestingPro/TradingView integration, any real
historical dataset, any live shadow window.

> **Evidence produced before the D-CLOCK-01 fix carries fabricated timestamps
> and must be regenerated, not trusted.**

### 4.3 Deferred owner-run ledger

20 records. **DEF-19 PASS**, **DEF-14 DEFERRED**, all 18 others **NOT_RUN** —
including **DEF-18** (post-fix cancellation) and **DEF-20** (restart-recovery
cancellation).

---

## 5. Component state

Full detail, field by field, is in
[`COMPONENT_EVIDENCE_MATRIX.yaml`](../status/COMPONENT_EVIDENCE_MATRIX.yaml).
Summary:

| Group | Components | State |
|---|---|---|
| Platform / security | platform, security | ✅ INTEGRATION_VERIFIED |
| Persistence | persistence | Implemented; no real dataset |
| Social sources | telegram-user, telegram-bot, telegram-coexistence | **Adapter code IMPLEMENTED** (TDLib user, supplemental bot, coexistence), fakes-tested; **PoIs BLOCKED** on owner accounts. whatsapp-user, discord-user **NOT_IMPLEMENTED** (no ToS-permitted path) |
| Other externals | news, tradingview | **Adapter code IMPLEMENTED** (news/filings point-in-time, TradingView webhook), fakes-tested; **PoIs BLOCKED**. investingpro **NOT_IMPLEMENTED** (no ToS-permitted path) |
| Market data | market-data | Read-only verified (IBKR); aggregate BLOCKED |
| External provider | external-market-data-provider | **NOT_IMPLEMENTED**; no provider selected |
| AI | ai-provider, ai-analysis-engines | Implemented; provider PoI BLOCKED; engines fakes-only |
| AI Review Committee | ai-review-committee | **NOT_IMPLEMENTED — designed only** |
| Decision pipeline | strategy-manifests, fusion, decision, risk, allocation, pre-submit | Implemented; fakes only; **`baseline-trend-breakout-v1` manifest is FROZEN (A-WP1)** but not yet evaluated on real data |
| Broker stack | ibkr-adapter, execution, protection, reconciliation | Implemented; partial real evidence; DEF-18/20 outstanding |
| Operations | dashboard, ibc, watchdog | Implemented/partial; host evidence missing (B-03/B-04) |
| Event bus | event-bus-nats | **SKELETON** — N-01, needs an ADR |
| Research | replay, backtest, shadow | Implemented; fakes only; no dataset, no OOS, no shadow |

### 5.1 AI Review Committee — designed, not running

Seven roles (Technical, Fundamental/News, Risk Critic, Data Quality Critic,
Strategy Critic, Devil's Advocate, Chair who synthesizes only). It exists
**only as a design** in the evidence roadmap: there is no runtime module, no
I/O contract and no replayable orchestration.

> **Binding separation.** The committee is a formal **input to the Decision
> Engine and never an execution authority.** It does not create `OrderIntent`,
> does not create `SubmitAuthorization`, sends no order, changes no strategy,
> promotes no version, and can never bypass Risk or Pre-Submit. Its value must
> be demonstrated by **ablation** against the no-committee baseline before it
> is relied on at all.

---

## 6. Strategy state

**There is no frozen, owner-approved, evidence-qualified or
production-eligible strategy** cleared for automated trading. That is a
precise claim, and the distinction matters — what is missing now is
*evidence*, not machinery, and not a frozen hypothesis: the baseline
hypothesis itself is already frozen and owner-approved; it is the evidence
(dataset, OOS, shadow) and the formal owner Go that remain outstanding:

| | State |
|---|---|
| Strategy **infrastructure** | ✅ **exists** — manifest evaluator, fusion, decision, risk, allocator, pre-submit, replay, backtest, funnel, champion/challenger (all `TESTED_WITH_FAKES`) |
| Proposed **hypotheses** | ✅ **exist** — e.g. `baseline-trend-breakout-v1`, and the WhatsApp measurement/experiment ideas in the evidence roadmap |
| A **frozen** hypothesis | ✅ **yes** — `baseline-trend-breakout-v1` v1, status `FROZEN`, owner_approval_ref `OWNER-EYAL-2026-08-13-BASELINE-TREND-BREAKOUT-V1`, frozen 2026-08-13, immutable fingerprint `sha256:75f299b8…`, pinned by `tests/strategy/test_frozen_baseline_manifest.py` (see `docs/strategies/baseline-trend-breakout-v1.yaml`) |
| Point-in-time **dataset** | ❌ none |
| `HISTORICALLY_TESTED` | ❌ none |
| `OUT_OF_SAMPLE_PASS` | ❌ none |
| `SHADOW_PASS` | ❌ none |
| Eligibility | ❌ **no strategy is eligible** for any automated mode |

Every strategy is `PROFITABILITY_NOT_PROVEN`.

The recommended first hypothesis is **`baseline-trend-breakout-v1`**, because
it requires **no social and no AI input**. That makes it a clean baseline: any
later claim that a social source or the AI committee adds value must beat it.

The owner has completed
[`docs/strategies/STRATEGY_FREEZE_TEMPLATE.md`](../strategies/STRATEGY_FREEZE_TEMPLATE.md)
for `baseline-trend-breakout-v1` and the manifest is now **FROZEN and
immutable** — any material change requires a NEW `version_id` (supersede),
never an edit to v1. The freeze happened **before** any evaluation result was
inspected, per the required ordering. No trading parameter is invented
anywhere in this repository, and none may be. The next step is building the
point-in-time research dataset (Track A) so the frozen manifest can be
evaluated.

---

## 7. IBKR and operations

**Working:** localhost-only + port-4002-only + `DU`-account gates; instrument
resolution; submit and acknowledgement; idempotent resubmit; orderRef identity;
read-only reconciliation; a lossless callback router; a durable single-use
execute-attempt journal; real UTC evidence timestamps.

**Fixed but unproven on real infrastructure:** cancellation
(`cancelOrder(id, OrderCancel())`), stale-ack handling, order-error correlation,
restart recovery of cancellable orders, cleanup semantics.

**Never exercised for real:** fill, protection, restart recovery, chaos drills.

Operational gaps: **B-02** (host chaos drills), **B-03** (IBC/watchdog host
patches not upstreamed, no transcript), **B-04** (watchdog deep probe off).

---

## 8. Explicit blockers

| ID | Blocker | Needs |
|---|---|---|
| B-01 | POI-IBKR-001 lifecycle incomplete — no real fill/protection/restart | Owner run |
| B-02 | Chaos/recovery host drills not run | Owner host |
| B-03 | IBC/watchdog host patches not upstreamed; no transcript | Owner host |
| B-04 | Watchdog deep probe not enabled | Owner host |
| N-01 | NATS JetStream adapter absent | Owner decision + ADR |
| N-02 | External market-data provider not selected | Owner decision |
| N-03 | POI-MD-001 aggregate BLOCKED | Owner decision (provider) |
| STRAT-01 | `baseline-trend-breakout-v1` is FROZEN; still no dataset, OOS or shadow evidence | Owner decision |
| SOC-01 | All social/news/InvestingPro/TradingView/AI PoIs BLOCKED | Owner accounts |

**Owner-dependency summary:** almost every remaining blocker needs an owner
account, an owner host action, an owner run or an owner decision. Very little
is waiting on code.

---

## 9. Dependency graph

```
 POI-IBKR-001 (BLOCKED)
   ├── DEF-19 verify-clean ......................... ✅ PASS
   ├── DEF-18 post-fix cancellation ................ ❌ NOT_RUN  ◀── NEXT
   ├── DEF-04/05 fill + protection ................. ❌ NOT_RUN
   ├── DEF-08 reconciliation after a real fill ..... ❌ NOT_RUN
   └── DEF-20 restart-recovery cancellation ........ ❌ NOT_RUN
          │
          ▼
      IMP-1302 Paper Manual ──▶ IMP-1303 chaos drills ──▶ IMP-1304 defects
                                        │
 Strategy track (independent, can run in parallel):
   freeze hypothesis ✅ FROZEN ─▶ point-in-time dataset ❌ NOT_RUN ─▶ replay/backtest
        └─▶ walk-forward / OOS ─▶ live shadow ─▶ formal Go/No-Go
                                        │
          ┌─────────────────────────────┴─────────────────────────────┐
          ▼                                                           ▼
   IMP-1401 select eligible strategy                        (No-Go ⇒ stop)
          ▼
   IMP-1402 Restricted Paper Auto  (requires IMP-1302–1304 AND Go)
          ▼
   IMP-1403 monitor/demote ─▶ IMP-1404 probation ─▶ IMP-1501 six months
          ▼
   IMP-1502 evaluate ─▶ IMP-1503 Live-readiness RECOMMENDATION (Live stays off)
```

**Two independent critical paths.** Broker evidence and strategy evidence do
not block each other and should run in parallel — that is the single largest
schedule saving available.

**But parallelism is ACROSS tracks only.** Within Track B, every
broker-mutating owner-run is **exclusive**: DEF-18, the fill path, DEF-20,
Gateway/process restarts and destructive chaos drills must never overlap on
the same Paper account and Gateway, and each starts from a passing
`verify-clean`. Non-mutating operational hardening stays parallel.

---

## 10. Frozen expansion

Paused, not cancelled. Unfreeze conditions are explicit:

| Item | Unfreeze condition |
|---|---|
| IMP-1401 | OOS + Shadow pass **and** a formal Go |
| IMP-1402 Paper Auto | IMP-1302–1304 complete **and** IMP-1401 approved |
| IMP-1403 / IMP-1404 | with IMP-1402 |
| IMP-1501–1503 | PHASE-14 complete; **Live stays disabled regardless** |
| New sources / new AI models | existing ones must first show marginal value |
| Dashboard expansion | explicit owner request + ADR |
| Multi-Gateway architecture | a concrete scaling need + ADR |
| Unrestricted parameter search | never without uncertainty controls + owner approval |
| Automatic strategy promotion | **never** — promotion is a documented human action |

**PAPER_AUTO is DISABLED. LIVE is DISABLED.** No plan in this repository
enables Live; the furthest any task goes is IMP-1503, which produces a
*recommendation*.

---

## 11. What happens next

The four-track plan is
[`ACCELERATED_EXECUTION_PLAN.md`](ACCELERATED_EXECUTION_PLAN.md). In one line:

> **Track A (strategy) and Track B (IBKR) start immediately and in parallel;
> Track C (sources) and Track D (AI) unblock only as the owner supplies
> accounts and decisions.**

The single next broker-changing action is **DEF-18**, in a **permitted
non-closed session (regular, pre-market or after-hours)** with a fresh
positive two-sided quote — regular hours are not required for a cancel-path
run, only a **closed** session is refused — per
[`POI_IBKR_001_OWNER_RUN.md`](../runbooks/POI_IBKR_001_OWNER_RUN.md).
Broker-mutating owner-runs are **serialized**: DEF-18, the fill path, DEF-20,
Gateway restarts and destructive drills never overlap on one account.
The single next strategy action is **building the point-in-time research
dataset** for the now-FROZEN `baseline-trend-breakout-v1`, which needs no
broker at all.

---

## 12. NOT EXECUTED

- ❌ no post-fix real cancel-path execute (DEF-18);
- ❌ no real cancellation after the latest changes;
- ❌ no fill-path owner-run;
- ❌ no Gateway restart-recovery cancellation (DEF-20);
- ❌ no Live connection and no Live order;
- ❌ no PAPER_AUTO activation.

**POI-IBKR-001 BLOCKED. POI-MD-001 BLOCKED. PROFITABILITY_NOT_PROVEN.**
