# Dataset V1 contract

Dataset V1 is the minimum research/backtest dataset contract required before a full historical strategy run may claim survivorship-safe, point-in-time inputs.

## Non-negotiable rules

1. **Stable identity, not ticker-only identity.** Every row references `security_id`. A ticker/symbol is an attribute and may change. IBKR `conId` is retained where available as broker identity evidence, but the dataset contract remains provider-neutral.
2. **Point-in-time membership.** Index membership is represented by effective intervals. A backtest at date `T` may use only members whose interval contains `T`; today's constituent list must never be projected backwards.
3. **As-of classification.** GICS data carries effective intervals. The sector/industry observed for a historical date is the classification effective on that date, not the latest classification.
3a. **Bitemporal — knowledge time as well as event time.** Membership and GICS assertions carry BOTH effective validity (`valid_from`/`valid_to`) AND knowledge time (`known_at`). Every as-of API enforces both axes: a fact is usable at decision date `T` only if its interval contains `T` **and** it was known by `T` (default knowledge cutoff = end of day `T`, UTC; an explicit `known_as_of` is accepted). A later restatement (a correction with a later `known_at`, linked by `supersedes`) is invisible to any earlier decision — **restatements can never leak backwards**. Re-additions are disjoint intervals; a correction supersedes the record it restates rather than overlapping it.
4. **Delisted names remain addressable.** Historical securities are not removed from identity history because they later delisted, merged or changed symbol.
5. **Corporate actions are event-level data.** Splits, dividends, mergers, spinoffs, symbol changes and delistings carry effective dates and provenance. An adjusted-price series is useful but is not a substitute for the event ledger.
6. **Every externally sourced historical fact has provenance.** `source` and timezone-aware `observed_at` are mandatory. Source record identifiers may be attached where available.
7. **Fail closed.** Dataset V1 is not full-contract ready until every required capability has real empirical evidence. Code/schema availability alone cannot promote a capability to `EMPIRICALLY_VERIFIED`.

## Canonical records

The executable contract lives in `packages/py/market-data-core/src/trading_market_data_core/dataset_v1.py` and defines:

- `SecurityIdentity`
- `MembershipRecord`
- `GicsRecord`
- `DelistingRecord`
- `CorporateActionRecord`
- `DailyPriceRecord`
- `Provenance`
- `DatasetV1`
- `DatasetCapabilityMatrix`

Effective intervals are inclusive (`valid_from <= as_of <= valid_to`); an omitted `valid_to` means the fact remains effective until superseded. `MembershipRecord` and `GicsRecord` additionally carry a `known_at` knowledge timestamp and an optional `supersedes` link (bitemporal). Overlaps are evaluated over the current, non-superseded survivors: a correction supersedes the record it restates (not a conflict), and a genuine re-addition's disjoint intervals are permitted, but two overlapping non-superseding intervals for the same key are invalid. The ingestion pipeline lives in `dataset_v1_ingestion.py`.

## Validation gates

`DatasetV1.validate()` rejects duplicate identities, unknown security references, overlapping PIT intervals, duplicate daily prices, non-positive close prices, invalid split ratios, empty provenance sources and naive provenance timestamps. It also warns when membership rows exist without corresponding delisting coverage, because absence of a delisting event must not be interpreted as proof of survival.

`DatasetCapabilityMatrix.assert_full_dataset_contract_ready()` is the execution gate for the full contract. It refuses readiness while any required capability lacks `EMPIRICALLY_VERIFIED` evidence.

## PIT reference-layer ingestion

`packages/py/market-data-core/src/trading_market_data_core/dataset_v1_ingestion.py`
is the provider-neutral, offline pipeline for the two highest-value blocked
cells (membership, as-of GICS). It provides:

- injected `MembershipSource` / `GicsSource` ports (no commercial provider is
  selected or hard-coded; an in-memory FIXTURE source drives the tests);
- normalization that resolves a provider `symbol` to a stable `security_id` via
  an injected `IdentityResolver` and **fails closed on an unknown security**
  (historical truth is never keyed by ticker);
- ADD/REMOVE → interval and GICS-assignment → interval converters with an
  explicit **entry/exit boundary convention** (a security removed effective `D`
  is not a member on `D`; its last membership day is `D − 1`);
- corrections/restatements as explicit `corrects`-linked interval assertions
  that become `supersedes` edges;
- **deterministic, idempotent** imports (`record_id` is a content hash; exact
  duplicates collapse; re-ingesting identical raw rows is a no-op) and an
  order-independent `dataset_fingerprint`;
- a fail-closed promotion decision (`assess_reference_capability`) and a
  `ReferenceReadinessReport` that shows exactly which cell can or cannot be
  promoted and why.

**Promotion is fail-closed.** A cell reaches `EMPIRICALLY_VERIFIED` only when a
**REAL** source that provides historical effective dates AND knowledge time
supplies at least one historical change, validation passes, AND a verifiable
real-evidence checksum is present. A **FIXTURE** source, a **current-snapshot-only**
source, a source with no historical change, missing checksum, or any validation
error keeps the cell `BLOCKED_EXTERNAL` (a clean fixture proves the machinery
only, as `CONTRACT_READY`). No real reference source or evidence is wired in this
repository, so both cells remain `BLOCKED_EXTERNAL` and
`full_dataset_contract_ready` stays false.

## Empirical state on 2026-08-16

The owner-run `POI-IBKR-HIST.json` proves the six active-name IBKR historical capabilities: paper/DU connectivity gate, daily TRADES/RTH schema, contract identity/conId, historical head timestamp, BID/ASK/MIDPOINT, and symbol-match discovery. Those cells are therefore `EMPIRICALLY_VERIFIED` in `DATASET-V1-CAPABILITY-MATRIX.yaml`.

It does **not** prove historical S&P 500 membership, as-of GICS, delisted-name coverage, or event-level corporate actions. Those four cells remain `BLOCKED_EXTERNAL`. Consequently `full_dataset_contract_ready` remains false.

## External source acceptance criteria

A future provider/source may fill a blocked cell only if the evidence demonstrates all fields needed by that cell, historical effective dates, stable identity mapping, reproducible provenance, and a sample containing at least one historical change/event where applicable. Current-state-only snapshots are insufficient for PIT membership or GICS. A source that silently drops delisted names is insufficient for delisted coverage.

No provider is selected or implied by this contract. Provider selection, commercial terms and entitlements remain separate owner decisions.

## Scope boundary

This contract concerns research/historical data only. It does not mark `POI-IBKR-001` (the Paper order lifecycle) PASS and does not authorize live trading, dataset backtests, or OOS claims by itself.
