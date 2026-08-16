# Dataset V1 contract

Dataset V1 is the minimum research/backtest dataset contract required before a full historical strategy run may claim survivorship-safe, point-in-time inputs.

## Non-negotiable rules

1. **Stable identity, not ticker-only identity.** Every row references `security_id`. A ticker/symbol is an attribute and may change. IBKR `conId` is retained where available as broker identity evidence, but the dataset contract remains provider-neutral.
2. **Point-in-time membership.** Index membership is represented by effective intervals. A backtest at date `T` may use only members whose interval contains `T`; today's constituent list must never be projected backwards.
3. **As-of classification.** GICS data carries effective intervals. The sector/industry observed for a historical date is the classification effective on that date, not the latest classification.
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

Effective intervals are inclusive (`valid_from <= as_of <= valid_to`); an omitted `valid_to` means the fact remains effective until superseded. Overlapping membership or GICS intervals for the same logical key are invalid.

## Validation gates

`DatasetV1.validate()` rejects duplicate identities, unknown security references, overlapping PIT intervals, duplicate daily prices, non-positive close prices, invalid split ratios, empty provenance sources and naive provenance timestamps. It also warns when membership rows exist without corresponding delisting coverage, because absence of a delisting event must not be interpreted as proof of survival.

`DatasetCapabilityMatrix.assert_full_dataset_contract_ready()` is the execution gate for the full contract. It refuses readiness while any required capability lacks `EMPIRICALLY_VERIFIED` evidence.

## Empirical state on 2026-08-16

The owner-run `POI-IBKR-HIST.json` proves the six active-name IBKR historical capabilities: paper/DU connectivity gate, daily TRADES/RTH schema, contract identity/conId, historical head timestamp, BID/ASK/MIDPOINT, and symbol-match discovery. Those cells are therefore `EMPIRICALLY_VERIFIED` in `DATASET-V1-CAPABILITY-MATRIX.yaml`.

It does **not** prove historical S&P 500 membership, as-of GICS, delisted-name coverage, or event-level corporate actions. Those four cells remain `BLOCKED_EXTERNAL`. Consequently `full_dataset_contract_ready` remains false.

## External source acceptance criteria

A future provider/source may fill a blocked cell only if the evidence demonstrates all fields needed by that cell, historical effective dates, stable identity mapping, reproducible provenance, and a sample containing at least one historical change/event where applicable. Current-state-only snapshots are insufficient for PIT membership or GICS. A source that silently drops delisted names is insufficient for delisted coverage.

No provider is selected or implied by this contract. Provider selection, commercial terms and entitlements remain separate owner decisions.

## Scope boundary

This contract concerns research/historical data only. It does not mark `POI-IBKR-001` (the Paper order lifecycle) PASS and does not authorize live trading, dataset backtests, or OOS claims by itself.
