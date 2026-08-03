# Strategy Freeze Template — owner decision record

> **Every field below is an OWNER DECISION.** This repository contains no
> trading parameter and must never invent one: not a threshold, not a stop
> distance, not a position size, not a universe. A blank field is a blank
> field — it is never filled in with a plausible-looking default.
>
> **The freeze happens BEFORE any evaluation result is inspected.** That
> ordering is the whole point: parameters chosen after seeing results are
> fitted to the sample, and any performance number that follows is
> meaningless. This document is the artifact that proves the ordering.
>
> Copy this file to `docs/strategies/<strategy-id>.md`, complete it, and have
> the owner approve it. Until then the strategy is `NOT_FROZEN` and
> `PROFITABILITY_NOT_PROVEN`, and no automated mode may consider it.

---

## Recommended first hypothesis: `baseline-trend-breakout-v1`

Freeze this one **first**, before any social or AI-dependent hypothesis.

**Why.** It requires **no social input and no AI input**. That makes it a clean
baseline: it isolates the ordinary market-structure edge (or the absence of
one) from every other moving part. Once it exists, any later claim that a
WhatsApp signal, a news feed or the AI Review Committee adds value becomes a
**measurable** claim — the challenger must beat this baseline. Without it,
every such claim is unfalsifiable.

It is also the fastest hypothesis to evaluate, because it depends on **zero
blocked PoIs**: market data and price history are enough. Every social, news,
InvestingPro, TradingView and AI PoI is currently BLOCKED on owner accounts, so
a hypothesis that needs them cannot be evaluated at all right now.

**Recommended, not decided.** The owner may freeze a different hypothesis
first; that is an owner decision, and this file records whichever one is
chosen.

---

## 0. Identity

| Field | Value |
|---|---|
| `strategy_id` | *(e.g. `baseline-trend-breakout-v1` — version it; never mutate a frozen id)* |
| Version | |
| Author | |
| Owner approval date (UTC) | |
| Status | `NOT_FROZEN` → `FROZEN` on owner approval |
| Depends on social sources? | |
| Depends on AI / committee? | |

---

## 1. Hypothesis

> State the edge as a falsifiable claim, including **why** it should exist
> (a market-structure or behavioural reason). "It backtests well" is not a
> hypothesis.

| Field | Decision |
|---|---|
| Hypothesis statement | |
| Why this edge should exist | |
| What observation would **falsify** it | |

---

## 2. Universe and liquidity

| Field | Decision |
|---|---|
| Universe definition | |
| Explicit instrument allowlist (if any) | |
| Minimum liquidity filter | |
| Minimum price filter | |
| Maximum spread filter | |
| Exclusions (e.g. earnings windows, halts, illiquid sessions) | |
| Survivorship-bias handling | |

---

## 3. Timeframe and setup

| Field | Decision |
|---|---|
| Primary timeframe | |
| Confirming timeframe(s) | |
| Setup definition (state, not a picture) | |
| Trigger definition (the precise event) | |
| Maximum time from setup to trigger | |

---

## 4. Entry, exit and invalidation

| Field | Decision |
|---|---|
| Entry rule (order type + price basis) | |
| Stop rule (initial protective stop) | |
| Target rule | |
| Time exit | |
| Invalidation (setup void before entry) | |
| Partial exits / scaling | |
| Re-entry policy | |

> The protective leg must be a broker-side **STOP**, `GTC`, sized never to
> exceed the position held. The harness never flattens.

---

## 5. Sizing and risk limits

| Field | Decision |
|---|---|
| Position sizing method | |
| Risk per trade | |
| Maximum concurrent positions | |
| Maximum portfolio exposure | |
| Maximum single-instrument exposure | |
| Maximum sector/correlation exposure | |
| Daily loss limit | |
| Weekly / monthly loss limit | |
| Action on limit breach | |

> The Risk Engine holds **absolute veto** over every one of these. The
> kill-switch is deterministic and never waits for an LLM.

---

## 6. Sessions and data

| Field | Decision |
|---|---|
| Allowed sessions (regular / pre-market / after-hours) | |
| Explicitly forbidden sessions | |
| Data requirements (fields, history depth) | |
| Freshness class required per field | |
| Behaviour when data is stale or missing | |
| Point-in-time / as-of requirement | |

---

## 7. Source roles and vetoes

Use the existing `ManifestRole` grammar: `REQUIRED` · `VETO` · `SOFT_SCORE` ·
`BONUS` · `CONTEXT`.

| Source | Role | Justification |
|---|---|---|
| Market data | | |
| Technical engine | | |
| Fundamental engine | | |
| News / NLP | | |
| Regime engine | | |
| WhatsApp / Discord / Telegram | | |
| InvestingPro | | |
| TradingView | | |
| AI Review Committee | | |

> **Binding.** Social messages may never directly invoke BUY, SELL, HALT or
> FLATTEN. Social adapters are read-only for trading input. The AI Review
> Committee is a **decision input, never an execution authority** — it creates
> no `OrderIntent`, no `SubmitAuthorization`, and can never bypass Risk or
> Pre-Submit.

---

## 8. Cost assumptions

> Optimistic cost assumptions are the most common way a backtest lies.

| Field | Decision |
|---|---|
| Commission model | |
| Slippage model | |
| Spread assumption | |
| Partial-fill assumption | |
| Borrow / financing costs | |
| Sensitivity: results if costs are **doubled** | |

---

## 9. Evaluation metrics and uncertainty

| Field | Decision |
|---|---|
| Primary metric | |
| Secondary metrics | |
| Minimum sample size (trades) | |
| Confidence-interval method | |
| Multiple-comparisons control | |
| Minimum effect size worth acting on | |
| How "no edge" would be concluded | |

---

## 10. Data splits and validation

| Field | Decision |
|---|---|
| Train window | |
| Validation window | |
| **Test / OOS window (never touched during fitting)** | |
| Walk-forward scheme (windows, step, re-fit policy) | |
| Embargo / purge between splits | |
| Stress scenarios (gaps, halts, illiquidity, regime shift, outage) | |

---

## 11. Shadow

| Field | Decision |
|---|---|
| Shadow window length | |
| Shadow start condition | |
| Metrics measured in shadow | |
| Acceptable divergence from backtest | |
| Abort condition | |

> The shadow path is **evaluate-only** and mints no `SubmitAuthorization`.

---

## 12. Go / No-Go criteria

Define these **now**, before any result exists.

| Field | Decision |
|---|---|
| GO requires (all of) | |
| NO-GO if (any of) | |
| MORE-RESEARCH if | |
| Decision owner | |
| Decision date | |
| Recorded outcome | |

> Eligibility requires **both** `OUT_OF_SAMPLE_PASS` **and** `SHADOW_PASS`,
> plus a formal owner Go. Promotion is always a documented human action —
> automatic promotion is never permitted.

---

## 13. The no-refitting commitment

> **Signed commitment.** Once this document is frozen, **no parameter in it may
> be changed after inspecting evaluation results.** Any change creates a **new
> `strategy_id`** with its own freeze record and its own untouched OOS window.
>
> Changing a threshold because the result disappointed, re-running until a
> window looks good, or quietly widening the universe are all the same error:
> they convert an out-of-sample test into an in-sample fit and destroy the only
> evidence that mattered.

| Field | Value |
|---|---|
| Owner signature / approval reference | |
| Date frozen (UTC) | |
| Commit that froze it | |
| Superseded by (if re-frozen) | |

---

## 14. Status after freeze

Every strategy starts here and moves only on evidence:

`PROFITABILITY_NOT_PROVEN` → `HISTORICALLY_TESTED` → `OUT_OF_SAMPLE_PASS` →
`SHADOW_PASS` → (owner Go) → eligible for Restricted Paper Auto → `PAPER_PASS`.

**No strategy in this repository has passed any of these stages.**
