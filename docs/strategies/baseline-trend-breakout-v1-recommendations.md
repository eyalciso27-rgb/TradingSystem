# baseline-trend-breakout-v1 — Future Research Recommendations

**Recorded:** 2026-09-07  
**Status:** RECOMMENDATIONS ONLY — NOT part of frozen V1 semantics  
**Baseline:** `baseline-trend-breakout-v1` v1  
**Frozen fingerprint:** `sha256:75f299b89e408ef332cd39a46f92d417ef637b3c5a5c1712801774c9e84dfed0`

## Decision

Keep the current frozen V1 unchanged through the first valid PIT backtest/OOS evaluation.
The items below are candidate challengers for later empirical testing. They MUST NOT
silently alter V1. Any material strategy change requires a new version/fingerprint and
the applicable replay/OOS/shadow qualification path.

## Recommended research candidates

### 1. Market-regime filter — HIGH priority

Test whether new breakout entries should be blocked during an adverse broad-market
regime. Candidate definitions (for example SPY relative to a long moving average) must
be specified and frozen before inspecting comparative results. Do not assume a regime
filter improves performance; prove it against the unchanged V1 control.

### 2. Earnings-risk filter — HIGH priority

Test a rule that blocks new entries when a scheduled earnings release is too close.
Separately test policy for an already-open position approaching earnings. These are two
different risk decisions. Candidate entry exclusion windows such as 1/3/5 trading days
may be evaluated only under a pre-registered experiment design rather than selected
post-hoc.
Earnings-calendar evidence used historically must itself satisfy point-in-time semantics:
the event date/knowledge used by a decision must have been known at that decision time.
A later corrected calendar must not be projected backward. Overnight gap risk remains
important because a broker-side stop-market cannot guarantee the planned 0.25% loss
budget across a gap.

### 3. Volume-contraction confirmation — MEDIUM/HIGH priority

V1 intentionally retains volume as analytical data but does not require it. Test whether
volume dry-up / supply-contraction confirmation improves expectancy, DSR, drawdown or
tail behavior enough to justify the reduced opportunity count. Treat this as an ablation,
not as an assumed VCP requirement.

### 4. Additional base-depth constraint — MEDIUM priority

V1 already rejects a trade when `EntryPrice - StructuralStop > 2.5 * ATR20`. Evaluate
whether an additional base-width/depth rule (percentage-based or another pre-registered
volatility-normalized measure) materially improves outcomes. Do not choose a threshold
by optimizing on final OOS.

## Suggested experiment structure

Use unchanged V1 as the control and introduce one change at a time:

- `V1-R`: V1 + market-regime filter.
- `V1-E`: V1 + earnings-risk filter.
- `V1-V`: V1 + volume-contraction confirmation.
- `V1-B`: V1 + additional base-depth constraint.
- Combined V2: only after isolated candidates demonstrate robust incremental value.
## Evaluation guardrails

- Preserve survivorship-bias-free, strict point-in-time inputs for every challenger.
- Do not turn an expected win rate (for example 35-40%) into a target or acceptance rule.
- Compare expectancy, DSR, drawdown/tail risk, turnover, costs and sample size.
- Preserve the frozen minimum-evidence requirements and locked final OOS discipline.
- A plausible economic story is not evidence; reject additions that do not survive the
  pre-registered evaluation and robustness tests.
- Do not enable PAPER_AUTO or LIVE because of these recommendations.

## Current owner decision

**No strategy change now.** Continue the evidence program using the frozen
`baseline-trend-breakout-v1` as the clean baseline. Revisit these recommendations only
after the baseline has a valid PIT dataset and evaluation evidence.