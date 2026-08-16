# Dataset V1 contract

Dataset V1 is the minimum research/backtest dataset contract required before a full historical strategy run may claim survivorship-safe, point-in-time inputs.

## Non-negotiable rules

1. **Stable identity, not ticker-only identity.** Every row references `security_id`. A ticker/symbol is an attribute and may change. IBKR `conId` is retained where available as broker identity evidence, but the dataset contract remains provider-neutral.
2. **Point-in-time membership.** Index membership is represented by effective intervals. A backtest at date `T` may use only members whose interval contains `T`; today's constituent list must never be projected backwards.
3. **As-of classification.** GICS data carries effective intervals. The sector/industry observed for a historical date is the classification effective on that date, not the latest classification.
3a. **Bitemporal — knowledge time as well as event time.** Membership and GICS assertions carry BOTH effective validity (`valid_from`/`valid_to`) AND knowledge time (`known_at`). Every as-of API enforces both axes: a fact is usable at decision date `T` only if its interval contains `T` **and** it was known by `T` (default knowledge cutoff = end of day `T`, UTC; an explicit `known_as_of` is accepted). A later restatement (a correction with a later `known_at`, linked by `supersedes`) is invisible to any earlier decision — **restatements can never leak backwards**. Re-additions are disjoint intervals; a correction supersedes the record it restates rather than overlapping it.
4. **Delisted names remain addressable.** Historical securities are not removed from identity history because they later delisted, merged or changed symbol. A delisting is a bitemporal `DelistingRecord` carrying (where the source supplies them) `listing_date`, `delisted_on`, `last_trading_date`, a typed `DelistingReason`, venue and successor. `lifecycle_state_as_of` distinguishes **NOT_YET_EXISTED / ACTIVE / DELISTED / UNKNOWN** — and **missing price data is never interpreted as a delisting**: with no lifecycle facts the answer is `UNKNOWN`.
4a. **Symbols are attributes, never identity.** `SymbolHistoryRecord` records the ticker an identity traded under during an interval; a rename adds a new symbol interval under the SAME `security_id`. `resolve_security_by_symbol_as_of` resolves a HISTORICAL ticker — today's symbol map is never used as history.
5. **Corporate actions are event-level data.** Splits, reverse splits, cash/special/stock dividends, mergers, acquisitions, spinoffs, symbol changes, exchange changes and delistings are bitemporal `CorporateActionRecord` events with effective/ex/record/payment dates, `Decimal` ratio and cash amounts, currency, predecessor/successor and provenance. An adjusted-price series is useful but is not a substitute for the event ledger.
5a. **Identity transitions are exposed, never auto-applied.** `successors_of` reports the successor identities asserted by visible merger/acquisition/spin-off events; the Dataset Builder decides whether to stitch, so price histories are never blindly concatenated across different securities. A terminal transition (merger/acquisition) must name a successor, may not point at itself, may not contradict another terminal successor for the same security, and may not form a cycle. A spin-off legitimately produces several successors.
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

## Retraction / tombstone semantics

A **retraction** states that an earlier assertion was determined **never to have
been valid** — distinct from a correction (which restates it). It is modelled by
an explicit `RetractionRecord` (`target_record_id`, `known_at`, provenance,
reason), **never** by a zero-length interval. The retracted record keeps its
immutable identity and stays in the dataset.

Resolution is deterministic (`resolve_visible`), in this order:

1. only assertions with `known_at <= known_as_of` are visible;
2. assertions targeted by a **retraction visible at the cutoff** are dropped;
3. assertions **superseded by a surviving correction** are dropped.

Because retraction is applied *before* supersession, **retracting a correction
restores the record that correction had superseded**. Earlier backtests still see
exactly what was known then; later backtests no longer treat the retracted
assertion as valid. The mechanism is shared by membership, GICS, symbol history,
delistings and corporate actions. Retracting a retraction is unsupported and
fails closed (it would make resolution order ambiguous), as do self-targeting
retractions, duplicate retractions of one record, and retractions of unknown
records.

## Price adjustment boundary (no double adjustment)

`DailyPriceRecord.adjustment_status` is explicit: `RAW_UNADJUSTED`,
`PROVIDER_ADJUSTED`, `INTERNALLY_ADJUSTED` or `UNKNOWN` (the **fail-closed
default** — an unlabelled series may not be adjusted, because double adjustment
cannot be ruled out). Validation enforces the boundary:

- a `RAW_UNADJUSTED` row may not carry an `adjusted_close`;
- an `INTERNALLY_ADJUSTED` row **must** name the `adjusted_with_ledger`
  fingerprint of the corporate-action ledger already applied, so the same actions
  are never applied twice;
- a row that names `adjusted_with_ledger` while not internally adjusted is
  contradictory.

The raw price observations, the adjusted observations and the corporate-action
ledger are three separate things; the Builder decides which to use.

## PIT reference-layer ingestion

`packages/py/market-data-core/src/trading_market_data_core/dataset_v1_ingestion.py`
is the provider-neutral, offline pipeline for the blocked reference/event cells
(membership, as-of GICS, delisted history, corporate actions). It provides:

- injected `MembershipSource` / `GicsSource` / `DelistingSource` /
  `CorporateActionSource` ports plus `ingest_symbol_history` and
  `build_retraction` (no commercial provider is selected or hard-coded; in-memory
  FIXTURE sources drive the tests);
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
only, as `CONTRACT_READY`). Synthetic or manually fabricated events are never
evidence. No real reference source or evidence is wired in this repository, so
`pit_sp500_membership`, `asof_gics`, `delisted_security_history` and
`corporate_actions` **all remain `BLOCKED_EXTERNAL`** and
`full_dataset_contract_ready` stays false.

## Builder / materialization / reproducible snapshot

`packages/py/market-data-core/src/trading_market_data_core/dataset_v1_builder.py`
composes the model into an immutable, versioned, point-in-time **snapshot**.

**Explicit build inputs — no hidden "today".** `BuildConfig` requires `as_of`,
`known_as_of` (tz-aware), `index_id`, an `AdjustmentPolicy` and a `LineagePolicy`.
Nothing reads a wall clock to decide visibility; `built_at` is manifest metadata
and is **excluded from the fingerprint**, so rebuilding later yields the same
`snapshot_id`.

**Materialization.** The universe is the membership effective at `as_of` and known
by `known_as_of` (later-delisted names retained, future entrants excluded, the
**historical** ticker resolved via symbol history). GICS is resolved as-of and
reports `KNOWN` / `UNKNOWN` / `NO_SOURCE_COVERAGE` — never back-projected. Prices
are bounded by `as_of` **and** by the knowledge cutoff on their observation time;
invalid prices are rejected; forward-fill is **off** unless explicitly enabled
with a written justification.

**Adjust exactly once.** `RAW` keeps raw prices; `PROVIDER_ADJUSTED` never
re-adjusts a provider series; `INTERNAL_LEDGER_ADJUSTED` adjusts **raw** prices
only, requires visible corporate-action coverage, applies splits/reverse splits
deterministically (`ratio` = new shares per old share; only prices strictly
before the effective date are restated) and stamps `adjusted_with_ledger`.
`UNKNOWN` or **mixed** adjustment status fails closed. Dividend adjustment is
deliberately not implemented and fails closed rather than emitting unverified
adjusted returns.

**Lineage is recorded, never applied.** `LineagePolicy.NO_STITCH` is the default
and the only implemented policy; `STITCH_ECONOMIC_LINEAGE` fails closed so
stitching can never happen implicitly. Links are typed
(`SAME_SECURITY_SYMBOL_CHANGE`, `TERMINATED_PREDECESSOR`, `SUCCESSOR_SECURITY`,
`SPINOFF_CHILD`, `UNKNOWN_AMBIGUOUS`) and the policy is recorded in the manifest.

**Manifest + integrity.** The manifest carries contract/builder versions,
snapshot id, fingerprint, as_of/known_as_of, index, counts, source names/versions/
checksums, verified evidence references, adjustment and lineage policy, readiness,
blocked capabilities, validation errors, warnings and the coverage report; it has
its own `checksum()`. `serialize_snapshot` emits canonical deterministic JSON
(Decimals as strings, ISO dates/timestamps, sorted keys), and
`snapshot_fingerprint_from_serialized` re-verifies both the manifest checksum and
the content fingerprint, so manifest **or** data tampering is detected.

**Reproducibility guarantees (tested).** Same inputs/config → same snapshot id;
input and ingestion ordering are irrelevant; wall clock does not affect the
fingerprint; a later restatement or retraction does **not** alter an older
snapshot rebuilt with the same `known_as_of`, while a later `known_as_of`
legitimately differs; adjustment policy, lineage policy, `as_of` and source
checksums all change the fingerprint.

## Backtest contract boundary

The future backtest engine consumes a **materialized, validated `DatasetSnapshot`**
— never raw ingestion records.

It **may assume**: the universe is point-in-time and survivorship-safe; symbols
are historical; GICS coverage state is explicit; prices are bounded by `as_of`,
knowledge-gated, `Decimal`, and adjusted **exactly once** per the recorded policy;
lineage links are informational; the manifest fingerprint identifies the exact
inputs.

It **must never infer**: that missing price data means delisting; that an
`UNKNOWN` GICS is the current sector; that lineage may be stitched; that it may
re-adjust prices; or that a successful build implies data quality.

**Required readiness:** a backtest may only run on a snapshot whose
`ReadinessLevel` is `BACKTEST_ELIGIBLE`. The levels are `CONTRACT_ONLY` →
`PARTIAL_DATA` → `EMPIRICAL_REFERENCE_READY` → `BACKTEST_ELIGIBLE`, computed from
**real capability evidence** (plus clean validation and full price coverage).
Fixtures reach `CONTRACT_ONLY` at most; while the four external cells are
`BLOCKED_EXTERNAL`, no snapshot can be backtest-eligible.

## Survivorship-bias protection

Proven by acceptance tests: a security present in historical membership stays in
the historical universe after it later delists; a delisted security remains
resolvable by stable `security_id`; a missing current broker contract
(`ibkr_con_id is None`) does not erase historical existence; historical symbol
resolution never uses today's symbol map; and today's surviving universe is never
used as the historical membership answer.

## Empirical state on 2026-08-16

The owner-run `POI-IBKR-HIST.json` proves the six active-name IBKR historical capabilities: paper/DU connectivity gate, daily TRADES/RTH schema, contract identity/conId, historical head timestamp, BID/ASK/MIDPOINT, and symbol-match discovery. Those cells are therefore `EMPIRICALLY_VERIFIED` in `DATASET-V1-CAPABILITY-MATRIX.yaml`.

It does **not** prove historical S&P 500 membership, as-of GICS, delisted-name coverage, or event-level corporate actions. Those four cells remain `BLOCKED_EXTERNAL`. Consequently `full_dataset_contract_ready` remains false.

## External source acceptance criteria

A future provider/source may fill a blocked cell only if the evidence demonstrates all fields needed by that cell, historical effective dates, stable identity mapping, reproducible provenance, and a sample containing at least one historical change/event where applicable. Current-state-only snapshots are insufficient for PIT membership or GICS. A source that silently drops delisted names is insufficient for delisted coverage.

No provider is selected or implied by this contract. Provider selection, commercial terms and entitlements remain separate owner decisions.

## Scope boundary

This contract concerns research/historical data only. It does not mark `POI-IBKR-001` (the Paper order lifecycle) PASS and does not authorize live trading, dataset backtests, or OOS claims by itself.
