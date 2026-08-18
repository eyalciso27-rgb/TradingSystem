# ADR-0017: Historical GICS and PIT S&P membership are optional, not trading gates

## Status

ACCEPTED (owner decision, 2026-08-18)

## Context

Dataset V1 defined ten REQUIRED capabilities, and any one of them being
unproven held the whole contract below readiness. Two of those ten —
`asof_gics` and `pit_sp500_membership` — turned out to be unobtainable from
free official sources:

* **as-of GICS**: S&P DJI publishes the GICS *structure* publicly but licenses
  company-level assignments; SEC assigns SIC, a different taxonomy from a
  different body; free datasets carry only current classifications with no
  effective or knowledge dates. Recorded in detail as `ASOF_GICS_BLOCKER`
  (`blocker_class = LICENSE_REQUIRED`).
* **PIT S&P membership**: automated retrieval from the publisher is blocked by
  bot protection (HTTP 403, robots.txt included), which this project does not
  circumvent. Owner-supplied official documents work, but reconstructing a full
  multi-decade history is a data-acquisition project.

The forcing question: *is either of these actually required to place a safe
trade now?* Neither is. Deciding to buy or sell a currently tradable security
depends on broker connectivity, contract identity, tradability, usable market
data, sizing, risk limits and protective orders — not on what sector a company
was assigned in 2018, nor on which index it belonged to then.

Treating them as mandatory conflated two different questions: **can we trade
safely today** and **how faithful is a historical backtest**.

## Decision

Split the contract in two.

1. **TRADING_READINESS** (`PAPER_READY` / `LIVE_READY` / `BLOCKED`) depends only
   on gates genuinely required to place a safe trade. `asof_gics` and
   `pit_sp500_membership` are NOT among them and can never block it.
2. **BACKTEST_DATA_QUALITY** (`APPROXIMATE` / `PIT_STRICT` / `INCOMPLETE`) is
   where historical fidelity lives. `PIT_STRICT` keeps every existing strict
   evidence rule.

Requirements become **strategy-derived** rather than global: a capability is
mandatory only when the selected strategy and mode actually depend on it. A
strategy that ignores sector never needs GICS; a strategy trading all eligible
US equities never needs index membership; an `SP500_CURRENT` strategy needs a
sufficiently fresh CURRENT list — which is not, and may never be substituted
by, historical PIT membership.

GICS becomes optional context with three modes, defaulting to `DISABLED`:
`DISABLED` / `FREE_RECONSTRUCTED` / `LICENSED`. `FREE_RECONSTRUCTED` is marked
non-authoritative and may inform analytics; it can never become a hard veto.

No paid data source is required for Paper or Live.

## Consequences

**Positive.** Paper and Live stop being blocked by data that does not affect
trade safety. Backtest fidelity is stated honestly rather than implied.
Requirements become explainable per strategy.

**Negative / risks, and how they are contained.**

* *An approximate backtest could be mistaken for a strict one.* Contained by
  making the quality level explicit and by refusing to label a run `PIT_STRICT`
  when its inputs are approximate.
* *A current constituent list could be mistaken for history.* Contained by
  keeping the two on separate types and forbidding substitution, with a test.
* *Free GICS could drift into authority.* Contained by the mode enum, explicit
  provenance, and the rule that optional context never vetoes.
* *A stale universe list could be used silently.* Contained by explicit
  `CURRENT_LIST_VALID` / `STALE` / `UNAVAILABLE` states and a configured
  freshness policy, stricter for Live than Paper.

**Explicitly unchanged.** Nothing about broker safety is relaxed: IBKR
connectivity and account-mode gates, contract validation, market-data
validation, risk limits, position sizing and protective-order requirements all
remain mandatory for both Paper and Live. Paper Auto and Live trading remain
prohibited by CLAUDE.md; this ADR changes readiness *semantics*, not
authorisation to trade.

**Preserved, not deleted.** The PIT membership engine, the GICS bitemporal
model, historical source ingestion, S&P document ingestion, the GICS license
blocker evidence and the strict snapshot/evidence machinery all remain. They
are reclassified as optional enrichment and strict-backtest inputs.

## Alternatives considered

* *Buy a licensed GICS history.* Rejected for now: it does not improve trade
  safety, and the owner requires a free-only solution. Remains a future upgrade.
* *Keep the gates and never reach readiness.* Rejected: it blocks work that has
  no genuine dependency on the missing data.
* *Silently drop the two capabilities from the required set.* Rejected: that
  hides the distinction. Gating is made mode- and strategy-derived instead, so
  strict backtests still demand the evidence.
