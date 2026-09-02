# Current S&P 500 / GICS monthly snapshot archive (contract)

This documents the archive **contract** for the CURRENT S&P 500 constituent
list and its optional CURRENT-only GICS context. It mirrors
`config/data-sources/sp500-documents/README.md` in spirit but describes a
different artifact: a **derived, monthly forward snapshot**, not an owner-
supplied source document.

The implementing code is `trading_market_data_core.sp500_current_universe`
(`ConstituentSnapshot`, `UniverseStore`, `refresh_universe`, `bind_raw_source`,
`UniverseStore.accept_with_raw_artifact`, `UniverseStore.verify_raw_artifact`,
`UniverseStore.latest`). This directory holds only this README — it is **not**
where live snapshot files are written.
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

Write-once holds under **real concurrency**, not only a single caller: the
whole read-check-write sequence for one `(index_id, as_of)` — both `accept`
and `accept_with_raw_artifact` — is serialized behind a per-date interprocess
lock (`fcntl.flock` on a `.{index_id}-{as_of}.lock` file next to the archive;
released automatically if the holder dies, so no stale-lock cleanup is
needed). Without it, two writers (two refresh processes, or a scheduled
refresh racing a manual CLI run) could each observe an absent entry, both pass
validation, and both `os.replace` — the second one silently winning and
either discarding the first writer's accepted candidate (both calls report
success, only one survives) or pairing a normalized entry from one candidate
with the raw sidecar from another. With the lock, exactly one concurrent
writer for a given date wins; the other blocks until the first has fully
committed (or fully failed), then re-observes reality and is refused (or,
byte-identical, treated as idempotent) under the ordinary write-once rule.
See `test_concurrent_conflicting_writers_for_the_same_date_never_both_win`.

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
* on a clean refusal — missing binding, mismatched binding, or a
  normalized-side validation/immutability failure detected before any write
  is attempted — **nothing is written**, neither the raw sidecar nor the
  normalized entry.

### Selection re-verifies the raw sidecar — this is not just a manual check

`UniverseStore.latest()` (and therefore `due_for_refresh`, freshness
evaluation, and every consumer of the last-known-good list) re-verifies a
raw-bound entry's retained sidecar on every lookup, not only when a caller
happens to invoke `verify_raw_artifact` explicitly. An entry whose normalized
body still authenticates but whose retained raw artifact has since been lost
or tampered with is demoted exactly as a tampered normalized body would be,
and selection falls back to the newest entry that is fully valid on every
axis it declares (which may itself carry no raw binding, or one that still
verifies). A raw-bound archive can never silently regress to serving
unverifiable raw provenance just because nobody asked.

### Crash-safety: two artifacts, not one atomic operation

The raw sidecar and the normalized entry are two separate files, so committing
both can never be a single atomic filesystem operation. Each write is
individually atomic (temp file in the same directory, `fsync`, then
`os.replace` — a reader only ever sees the complete old content or the
complete new content, never a partial write), and the two are committed in a
specific order: **raw first, normalized second**. That ordering is what
prevents the dangerous failure mode: a crash or storage failure between the
two writes can leave at most an **unreferenced, harmless raw sidecar** with no
normalized entry claiming it yet — never a normalized entry that claims a raw
binding which is not durably backed by real bytes on disk. Retrying
`accept_with_raw_artifact` with the exact same `(snapshot, raw_bytes)`
afterwards recovers idempotently: the already-durable raw side is left
untouched (write-once) and only the missing normalized side is written.

`UniverseStore.accept` (without a raw artifact) remains available for callers
with no raw artifact to retain (e.g. hand-authored fixtures, or the plain
CLI/manual override path); it still enforces normalized self-authentication
and dated-layout immutability, it just does not carry a raw-provenance
guarantee. See `tests/market_data/test_sp500_snapshot_archive.py` for the
enforcement, durability, tamper-detection, selection-fallback and
crash-recovery proofs on both axes, including failure-injection tests for the
two-write commit sequence.

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
  refused without disturbing any other archived date;
* an entry that was once good but whose retained raw sidecar has since failed
  (lost or tampered) is demoted at selection time (see above) rather than
  served — this is "last-known-good", not "last-archived".

Freshness (`CURRENT_LIST_VALID` / `CURRENT_LIST_STALE` /
`CURRENT_LIST_UNAVAILABLE`) is evaluated against the last-known-good entry
that actually survived — never against a rejected or since-invalidated
candidate.
