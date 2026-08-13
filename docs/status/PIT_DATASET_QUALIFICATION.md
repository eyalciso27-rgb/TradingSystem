# PIT Dataset Qualification — `baseline-trend-breakout-v1` v1

> Data-qualification plan around the owner's **existing** paid access (IBKR,
> TradingView, InvestingPro). No new provider selected. No purchase, no strategy
> change, no backtest, no OOS. Frozen strategy fingerprint
> `sha256:75f299b8…dfed0`.

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
