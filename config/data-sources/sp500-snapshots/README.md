# Current S&P 500 / GICS monthly snapshot archive (contract)

This documents the archive **contract** for the CURRENT S&P 500 constituent
list and its optional CURRENT-only GICS context. It mirrors
`config/data-sources/sp500-documents/README.md` in spirit but describes a
different artifact: a **derived, monthly forward snapshot**, not an owner-
supplied source document.

The implementing code is `trading_market_data_core.sp500_current_universe`
(`ConstituentSnapshot`, `UniverseStore`, `refresh_universe`). This directory
holds only this README — it is **not** where live snapshot files are written.
The runtime archive root is `docs/poi/evidence/sp500-current/`
(`DEFAULT_UNIVERSE_ROOT` in `apps/scheduler`), populated by the scheduler's
monthly maintenance job or the `sp500_universe_cli` diagnostic tool. Nothing in
this repository fetches Wikipedia during tests or CI; every claim below is
verified offline against recorded fixtures in
`tests/market_data/test_sp500_snapshot_archive.py`.

## Scope (ADR-0017)

* This is the **CURRENT** index membership only — "the index as of one date".
  It is never point-in-time (PIT) history and is never substituted for it.
* GICS context here (`gics_sector` / `gics_sub_industry` per constituent,
  `gics_mode` on the snapshot) is **current classification only**,
  `FREE_RECONSTRUCTED` from a `PUBLIC_SECONDARY` source. It is
  non-authoritative, carries no effective/knowledge dates, and can never be
  used as historical (as-of) GICS. `is_point_in_time_history` is always
  `false` on every entry produced here.
* Collecting one snapshot per month does build a genuine forward history from
  the day collection starts — that is a by-product, not a claim of
  reconstructed past history.

## Immutable dated layout

One entry per `(index_id, as_of)`, named `{index_id}-{as_of}.json` (e.g.
`SP500-2026-08-01.json`). An archived entry is **write-once**:

* a later `accept()` call for a date already on record succeeds silently only
  when the candidate is **byte-identical** to what is archived (an idempotent
  retry after e.g. a crashed refresh process);
* any other candidate for a date already on record is **refused**, even one
  that looks "equivalent" (for example, a different-but-current-page
  representation whose diagnostic notes differ). A new observation always
  gets a new `as_of`; a past date is never corrected in place.

## Required provenance metadata

Every archived entry carries, at minimum:

`schema_version`, `index_id`, `as_of`, `fetched_at`, `source_id`,
`source_authority`, `constituent_count`, `is_point_in_time_history`,
`gics_mode`, `notes`, `checksum`.

## Raw and normalized checksum bindings

Every entry binds **two independent SHA-256 checksums**:

* **Normalized checksum** (`checksum`): computed over the canonical,
  sorted-keys JSON body of the archived, parsed snapshot (the checksum field
  itself excluded). It self-authenticates on every load — `load_snapshot`
  recomputes it and refuses a mismatch (tampered or stale file), and
  `UniverseStore.accept` refuses to archive a candidate whose declared
  checksum does not authenticate its own body.
* **Raw checksum**: the SHA-256 of the exact bytes of the fetched source
  artifact (the raw API payload) the entry was parsed from. It is recorded
  inside `notes` using the greppable prefix `raw source checksum: `, the same
  convention already used for parse diagnostics (`DIAGNOSTIC_NOTE_PREFIX`) in
  `sp500_current_universe`. Because it lives inside `notes`, which is itself
  part of the checksummed body, the raw checksum is bound into the normalized
  checksum too: altering the recorded raw checksum after archiving is exactly
  as detectable as altering a ticker.

Each checksum fails closed on its own axis and neither can stand in for the
other: a tampered raw artifact is caught by recomputing its digest against the
retained raw bytes; a tampered archived body is caught by recomputing the
normalized checksum on load. See
`test_an_archived_entry_binds_an_independent_raw_and_normalized_checksum` and
the tamper/fabrication tests beside it.

## Fail-closed last-known-good retention

`refresh_universe` / `UniverseStore.accept` never let a refresh destroy a good
list:

* a refresh that produces no candidate (fetch failure) keeps the last-known-
  good snapshot and reports why;
* a candidate that fails validation (empty, implausible count, duplicate
  tickers, missing identity, a checksum that does not authenticate its own
  body, or a candidate that incorrectly claims to be point-in-time history) is
  **refused**, and the previously archived snapshot is retained unchanged;
* a candidate that conflicts with an already-archived date for a *different*
  reason (immutability) is likewise refused without disturbing any other
  archived date.

Freshness (`CURRENT_LIST_VALID` / `CURRENT_LIST_STALE` /
`CURRENT_LIST_UNAVAILABLE`) is evaluated against the last-known-good entry
that actually survived — never against a rejected candidate.
