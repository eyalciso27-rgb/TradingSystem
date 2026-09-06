# PIT Dataset Qualification — `baseline-trend-breakout-v1` v1

> Data-qualification plan around the owner's **existing** paid access (IBKR,
> TradingView, InvestingPro). No new provider selected. No purchase, no strategy
> change, no backtest, no OOS. Frozen strategy fingerprint
> `sha256:75f299b8…dfed0`.

> **2026-09-03 reconciliation.** Since this matrix was written, PR #104 added
> an offline S&P historical-secondary source registry
> (`config/data-sources/sp500-historical-source-registry.json` — nothing
> fetched, nothing integrated, nothing promoted to authoritative), PR #105
> hardened the current/forward S&P 500 + current-GICS immutable snapshot
> archive, and PR #106 added a fail-closed, offline, fixture-only
> corporate-action PIT structural validator. None of the three closes a
> **GAP** row below — every remaining GAP requiring an **official** source is
> still WAITING_EXTERNAL (owner-supplied official documents, a
> historical-GICS licensing decision, or a delisted-name/corporate-action
> provider decision). See
> [`../roadmaps/MVP_DELIVERY_PLAN.md`](../roadmaps/MVP_DELIVERY_PLAN.md) §5
> M3 for the authoritative, itemized owner-action list.
>
> **2026-09-06 follow-up (ChatGPT review #1).** The above had not evaluated
> one legitimate free candidate for the PIT S&P 500 membership and as-of GICS
> GAP rows: MediaWiki's own contemporaneous revision history for the
> Wikipedia S&P 500 article. That evaluation is built and tested
> (`trading_market_data_core.dataset_v1_wikipedia_pit`) and referenced from
> `ASOF_GICS_BLOCKER["free_secondary_research_alternative"]`.
>
> **2026-09-06 follow-up (ChatGPT review #2, same day).** Review #1 was
> fixture-only (no real revision fetched) and claimed both membership AND
> GICS without a cross-check for GICS. Both closed: ONE real experiment is
> now recorded (`scripts/run_wikipedia_revision_history_experiment.py
> --as-of 2023-12-31`; 503/503 tickers corroborated; see
> `docs/poi/evidence/dataset-v1/wikipedia-revision-history/`), and the claim
> is narrowed to **MEMBERSHIP only** — it is a free, cross-checked
> `SECONDARY_RESEARCH_PIT` path usable for `BacktestQuality.APPROXIMATE`
> membership research (ADR-0017). GICS/sector from this source is captured
> but `CAPTURED_UNEVALUATED_NOT_ADMITTED` (no independent dated GICS source
> to cross-check against). Neither the membership evidence nor the captured
> GICS values close either GAP row below, which remain WAITING_EXTERNAL for
> **official** evidence.
>
> **2026-09-06 follow-up (A-WP2-WIKIPEDIA-REVISION-HISTORY-DATASET-BUILD,
> same day).** Re-verified independently: no owner-approved ABSOLUTE
> start/end date for the frozen strategy's full backtest window exists
> anywhere on main (only the RELATIVE 60/20/20 chronological split and
> 60-month-context/12-month-evaluation walk-forward proportions are frozen,
> `docs/strategies/baseline-trend-breakout-v1.yaml`). Rather than invent one,
> this task built the reusable, deterministic, resumable and idempotent BATCH
> machinery on top of the single-date experiment above
> (`WikipediaDatasetStore` / `run_wikipedia_revision_history_batch` /
> `build_dataset_manifest` in `dataset_v1_wikipedia_pit.py`,
> `scripts/run_wikipedia_revision_history_batch.py`), covered by offline,
> fixture-based tests proving it is resumable, idempotent, and fail-closed
> against a later revision entering the dataset through a retry, a resumed
> run, or a hand-imported entry file. The single already-recorded real
> experiment (2023-12-31) is unchanged. The gap is recorded precisely as
> `WINDOW_DEFINITION_REQUIRED`
> (`dataset_v1_wikipedia_pit.WINDOW_DEFINITION_REQUIRED`); it blocks only the
> FULL-WINDOW empirical MEMBERSHIP research dataset, not this machinery, and
> it does not touch either GAP row below.

## 1. Sourcing principle (owner correction #1)
The remaining reference-data gaps do **not** have to be bought from one bundled
provider. Historical S&P 500 membership, historical GICS, delisted price history
and corporate actions **may be sourced independently** if that gives a cheaper,
cleaner, better-proven PIT dataset. The objective is the **minimum legitimate
additional data**, not a single vendor.

## 2. Capability matrix (status: DOCUMENTED → EMPIRICALLY_VERIFIED)
`EMPIRICALLY_VERIFIED` flips **only** after the owner runs the read-only IBKR
qualification (`docs/poi/IBKR-HISTORICAL-QUALIFICATION-RUNBOOK.md`) and records
`docs/poi/evidence/POI-IBKR-HIST.json`. Until then IBKR cells read
`DOCUMENTED (PENDING_EMPIRICAL)`.

| REQUIREMENT | IBKR | TRADINGVIEW | INVESTINGPRO | REMAINING GAP |
|---|---|---|---|---|
| Daily OHLCV (active names) | DOCUMENTED yes (pending empirical) | limited CSV | no API | none (IBKR) once verified |
| Daily OHLCV (delisted names) | documented **no** | no | unlikely | **GAP** — survivorship |
| Head-timestamp / history depth | pending empirical | n/a | n/a | verify per run |
| BID/ASK/MIDPOINT bars | pending empirical (entitlement-dependent) | no | no | soft (fallback allowed) |
| Corporate actions (effective-dated) | partial (adjusted bars) | adjusted only | no clean export | **GAP** |
| GICS sector, as-of history | no | no | almost certainly no | **GAP** |
| PIT S&P 500 membership | no | no | no | **GAP** |
| Stable identifiers | conId (pending empirical) | no | no | partial |
| Commission schedule | owner Account Management (not API) | n/a | n/a | owner reads report |
| Trading calendar | yes (+ domain IMP-0704) | n/a | n/a | none |

## 3. Revised R8 — PIT availability requirement BY RECORD TYPE (owner correction #2)
Do **not** require a commercial historical revision tape for every OHLCV bar.
The timestamps are defined per the four-timestamp model already in
`dataset_manifest` (`event_at`, `observed_at`, `available_at`, `ingested_at`;
`superseded_at` bounds a correction's historical validity). Revision/correction
**lineage** is required **only** where the frozen strategy's PIT semantics
actually depend on it:

| Record type | effective_at | observed_at | available_at | ingested_at | Revision/correction lineage required? |
|---|---|---|---|---|---|
| **Daily OHLCV** | bar close (RTH) | vendor publish of the close | = bar close (usable next session) | ingest time | **No** for V1. Daily bars are effectively final; rare late adjustments are handled as a **corporate-action** re-statement (below), not a per-bar revision tape. |
| **Index membership** (S&P 500) | add/drop **effective date** | announcement/observation date | when the change was usable in a decision | ingest time | **Yes** — membership changes over time; a historical decision must see only the members effective then. Effective-dated add/drop history is mandatory (drives the universe). |
| **GICS classification** | classification **effective date** | reclassification observation | usable-from | ingest time | **Yes** — sector can change; the sector-concentration rule must use the classification effective at the decision, so as-of history with effective dates is required. |
| **Corporate actions** (split/div) | ex/effective date | announcement date | usable-from (≥ announcement) | ingest time | **Yes** — split re-statement changes historical price levels (Resistance/BaseLow/stops); PIT-correct adjustment needs effective-dated CA records. |
| **Identifier / symbol changes** | change **effective date** | observation | usable-from | ingest time | **Yes (lightweight)** — a stable id + effective-dated symbol map is needed to follow a security across renames/delistings; a full revision tape is not. |

**Net:** only **membership, GICS, corporate actions, and identifier changes**
need effective-dated history/lineage for V1. **Daily OHLCV does not** need a
commercial revision tape — that removes a large, expensive requirement.

## 4. Smallest remaining external gap (unbundled)
After IBKR (read-only) supplies active-name OHLCV + volume + conId + calendar,
the minimum legitimate additional data — **independently sourceable** — is:
1. **PIT S&P 500 membership** with add/drop effective dates (incl. delisted tickers);
2. **As-of GICS sector** history with effective dates;
3. **Effective-dated corporate actions** (splits/divs) for PIT-correct price levels;
4. **Delisted-name daily OHLCV** (survivorship-free), for names in (1) that IBKR cannot price historically.

Soft/optional: **historical NBBO** (the frozen strategy permits the 10 bps / 5
bps-adverse fallback). **Fundamentals: not needed by V1.**

No provider is selected here (EODHD/FMP/Norgate/S&P Global explicitly deferred).

## 5. Next real-evidence step
Owner runs the read-only IBKR qualification (§ runbook) → records
`POI-IBKR-HIST.json` → this matrix's IBKR cells move to `EMPIRICALLY_VERIFIED`,
and the true remaining gap (items 1–4 above) is confirmed against real coverage.
