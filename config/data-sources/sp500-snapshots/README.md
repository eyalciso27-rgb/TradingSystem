# Current S&P 500 / GICS monthly snapshot archive (contract)

This documents the archive **contract** for the CURRENT S&P 500 constituent
list and its optional CURRENT-only GICS context. It mirrors
`config/data-sources/sp500-documents/README.md` in spirit but describes a
different artifact: a **derived, monthly forward snapshot**, not an owner-
supplied source document.

The implementing code is `trading_market_data_core.sp500_current_universe`
(`ConstituentSnapshot`, `UniverseStore`, `refresh_universe`, `bind_raw_source`,
`UniverseStore.accept_with_raw_artifact`, `UniverseStore.verify_raw_artifact`).
This directory holds only this README — it is **not** where live snapshot
files are written.
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

## Raw and normalized checksum bindings (ENFORCED, not advisory)

Every entry binds **two independent SHA-256 checksums**, and the binding is
enforced by the archive itself at write time — it is not a convention callers
may or may not follow:

* **Normalized checksum** (`checksum`): computed over the canonical,
  sorted-keys JSON body of the archived, parsed snapshot (the checksum field
  itself excluded). It self-authenticates on every load — `load_snapshot`
  recomputes it and refuses a mismatch (tampered or stale file), and
  `UniverseStore.accept` refuses to archive a candidate whose declared
  checksum does not authenticate its own body.
* **Raw checksum**: the SHA-256 of the exact bytes of the fetched source
  artifact (the raw API payload) the entry was parsed from. `bind_raw_source`
  records it inside `notes` using the greppable prefix
  `raw source checksum: `, the same convention already used for parse
  diagnostics (`DIAGNOSTIC_NOTE_PREFIX`) in `sp500_current_universe`, then
  reseals the snapshot. Because `notes` is part of the checksummed body, the
  raw checksum is bound into the normalized checksum too: altering the
  recorded raw checksum after binding is exactly as detectable as altering a
  ticker.

Archiving through `UniverseStore.accept_with_raw_artifact(snapshot, raw_bytes)`
is what turns this from a claim into an enforced invariant:

* it refuses to archive a snapshot that carries **no** raw-checksum binding at
  all (`bind_raw_source` was never called);
* it recomputes the raw checksum from the `raw_bytes` actually supplied and
  refuses to archive if that does not match what the snapshot declares — a
  snapshot's own claim is never trusted over the real bytes;
* on success it **durably retains** the raw bytes in a write-once sidecar file
  (`{index_id}-{as_of}.raw`, deliberately outside the `*.json` glob
  `UniverseStore.snapshots()`/`latest()` use) next to the normalized entry, so
  the binding stays independently re-verifiable later via
  `UniverseStore.verify_raw_artifact` — without depending on whatever
  ephemeral fixture, HTTP response buffer, or download tempfile produced those
  bytes originally, and that sidecar is immutable under the same write-once
  rule as the normalized entry: a differently-bound candidate for a date
  already on record can never silently replace the retained raw artifact;
* on any failure — missing binding, mismatched binding, or a normalized-side
  validation/immutability failure — **nothing is written**, neither the raw
  sidecar nor the normalized entry, so a partially-bound archive entry can
  never exist on disk.

`UniverseStore.accept` (without a raw artifact) remains available for callers
with no raw artifact to retain (e.g. hand-authored fixtures, or the plain
CLI/manual override path); it still enforces normalized self-authentication
and dated-layout immutability, it just does not carry a raw-provenance
guarantee. See `tests/market_data/test_sp500_snapshot_archive.py` for the
enforcement, durability, tamper-detection and write-once-immutability proofs
on both axes.

## Fail-closed last-known-good retention

Neither the normalized-only path nor the raw-bound path ever lets a refresh
destroy a good list:

* a refresh that produces no candidate (fetch failure) keeps the last-known-
  good snapshot and reports why;
* a candidate that fails validation (empty, implausible count, duplicate
  tickers, missing identity, a checksum that does not authenticate its own
  body, or a candidate that incorrectly claims to be point-in-time history) is
  **refused**, and the previously archived snapshot is retained unchanged;
* a raw-bound candidate whose raw-checksum binding cannot be authenticated
  against the supplied bytes is likewise refused, and the previously archived
  (normalized + retained-raw) snapshot is retained unchanged;
* a candidate that conflicts with an already-archived date for a *different*
  reason (immutability, on either the normalized or the raw axis) is likewise
  refused without disturbing any other archived date.

Freshness (`CURRENT_LIST_VALID` / `CURRENT_LIST_STALE` /
`CURRENT_LIST_UNAVAILABLE`) is evaluated against the last-known-good entry
that actually survived — never against a rejected candidate.
