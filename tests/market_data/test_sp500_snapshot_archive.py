"""A-WP2 — current S&P 500 / GICS monthly snapshot archive (offline closure).

This proves the archive contract documented in
``config/data-sources/sp500-snapshots/README.md`` for the CURRENT S&P 500
constituent list, including its optional CURRENT-only GICS context
(``ADR-0017``, ``FREE_RECONSTRUCTED``), on top of the ``UniverseStore`` /
``ConstituentSnapshot`` machinery in
``trading_market_data_core.sp500_current_universe``:

* the archive's dated layout is IMMUTABLE — a conflicting candidate for a date
  already on record is refused, even when it comes from a differently-encoded
  but equally "current" representation of the same source page;
* every archived entry binds TWO independent SHA-256 checksums, and the
  binding is ENFORCED by the archive itself, not merely by the caller:
  ``UniverseStore.accept_with_raw_artifact`` refuses to archive a snapshot
  that carries no raw-checksum binding, or one whose binding does not match
  the raw bytes actually supplied, and on success it durably retains the raw
  artifact alongside the normalized entry so the binding stays verifiable
  later via ``verify_raw_artifact`` — independent of whatever ephemeral
  fixture or download buffer produced those bytes in the first place;
* every archived entry carries the required provenance metadata, and its GICS
  fields are always labelled CURRENT / ``FREE_RECONSTRUCTED`` — never
  point-in-time history;
* a refresh that fails, parses to nothing, or fails validation NEVER costs the
  last-known-good archived snapshot (fail-closed retention), on both the
  normalized-only path (``accept``/``refresh_universe``) and the raw-bound
  path (``accept_with_raw_artifact``).

Entirely offline: this reads only the recorded fixtures under
``tests/fixtures/sp500/`` (the same recorded API captures used by
``test_sp500_wikitext_regression.py``). No network call is made, no historical
GICS or point-in-time membership is constructed, and no strategy parameter is
touched.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from trading_market_data_core.sp500_current_universe import (
    RAW_CHECKSUM_NOTE_PREFIX,
    ConstituentSnapshot,
    UniverseStore,
    bind_raw_source,
    load_snapshot,
    parse_html_constituents,
    parse_wikitext_constituents,
    raw_artifact_checksum,
    recorded_raw_checksum,
    refresh_universe,
    validate_snapshot,
)
from trading_market_data_core.trading_readiness import (
    DEFAULT_FRESHNESS,
    GicsMode,
    TradingMode,
    UniverseListStatus,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sp500"
WIKITEXT_FIXTURE = FIXTURES / "wikipedia_constituents.wikitext.json"
HTML_FIXTURE = FIXTURES / "wikipedia_constituents.html.json"

AS_OF = date(2026, 8, 1)
FETCHED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)


def _raw_bytes() -> bytes:
    return WIKITEXT_FIXTURE.read_bytes()


def _wikitext_snapshot(as_of: date = AS_OF) -> ConstituentSnapshot:
    return parse_wikitext_constituents(_raw_bytes(), as_of=as_of, fetched_at=FETCHED_AT)


def _raw_bound_snapshot(as_of: date = AS_OF) -> tuple[ConstituentSnapshot, bytes]:
    """A snapshot genuinely bound to the checksum of the real raw fixture
    bytes it was parsed from — the precondition ``accept_with_raw_artifact``
    enforces."""
    raw = _raw_bytes()
    snapshot = bind_raw_source(_wikitext_snapshot(as_of=as_of), raw_artifact_checksum(raw))
    return snapshot, raw


# == raw + normalized checksum bindings, ENFORCED at archive time =============


def test_accept_with_raw_artifact_archives_and_durably_retains_the_binding(
    tmp_path: Path,
) -> None:
    snapshot, raw = _raw_bound_snapshot()
    raw_checksum = recorded_raw_checksum(snapshot)
    assert raw_checksum is not None and raw_checksum.startswith("sha256:")
    assert raw_checksum != snapshot.checksum, (
        "the raw and normalized checksums authenticate different content and"
        " must never collapse into the same value"
    )

    store = UniverseStore(tmp_path / "archive")
    path, findings = store.accept_with_raw_artifact(snapshot, raw)

    assert path is not None and findings == ()
    reloaded = load_snapshot(path)
    assert reloaded.checksum == snapshot.checksum
    assert recorded_raw_checksum(reloaded) == raw_checksum
    # Durable, independent re-verification against the RETAINED copy — not
    # against the ``raw`` bytes object still sitting in this test's memory.
    assert store.verify_raw_artifact(reloaded) == ()


def test_a_snapshot_with_no_raw_checksum_binding_is_refused(tmp_path: Path) -> None:
    """A snapshot that never called ``bind_raw_source`` carries no raw
    provenance claim at all, and the raw-bound archive path must refuse it
    rather than silently archiving it as though the binding were optional."""
    store = UniverseStore(tmp_path)
    unbound = _wikitext_snapshot()

    path, findings = store.accept_with_raw_artifact(unbound, _raw_bytes())

    assert path is None
    assert findings and "no raw source checksum binding" in findings[0]
    assert store.snapshots() == ()


def test_a_raw_checksum_binding_that_does_not_match_the_supplied_bytes_is_refused(
    tmp_path: Path,
) -> None:
    """The archive recomputes the raw checksum itself; a snapshot's CLAIMED
    binding is never trusted over the bytes actually handed to the archive."""
    store = UniverseStore(tmp_path)
    snapshot = bind_raw_source(_wikitext_snapshot(), "sha256:" + "a" * 64)

    path, findings = store.accept_with_raw_artifact(snapshot, _raw_bytes())

    assert path is None
    assert findings and "checksum mismatch" in findings[0]
    assert store.snapshots() == (), "nothing may be archived on a raw-binding mismatch"
    assert not (tmp_path / f"SP500-{AS_OF.isoformat()}.raw").exists()


def test_the_retained_raw_artifact_is_reverifiable_after_the_original_bytes_are_gone(
    tmp_path: Path,
) -> None:
    """The whole point of durable retention: verification later must not
    depend on the caller still holding the original raw bytes/fixture."""
    snapshot, raw = _raw_bound_snapshot()
    store = UniverseStore(tmp_path)
    path, findings = store.accept_with_raw_artifact(snapshot, raw)
    assert path is not None and findings == ()
    reloaded = load_snapshot(path)

    del raw  # the caller's copy is gone; only the store's retained copy remains

    assert store.verify_raw_artifact(reloaded) == ()


def test_tampering_the_retained_raw_artifact_after_archival_is_detected(
    tmp_path: Path,
) -> None:
    snapshot, raw = _raw_bound_snapshot()
    store = UniverseStore(tmp_path)
    path, findings = store.accept_with_raw_artifact(snapshot, raw)
    assert path is not None and findings == ()
    reloaded = load_snapshot(path)

    raw_path = tmp_path / f"SP500-{AS_OF.isoformat()}.raw"
    assert raw_path.is_file()
    raw_path.write_bytes(raw_path.read_bytes() + b"tampered")

    findings = store.verify_raw_artifact(reloaded)

    assert findings and "checksum mismatch" in findings[0]


def test_the_raw_artifact_is_write_once_and_a_byte_identical_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    snapshot, raw = _raw_bound_snapshot()
    store = UniverseStore(tmp_path)
    first_path, first_findings = store.accept_with_raw_artifact(snapshot, raw)
    second_path, second_findings = store.accept_with_raw_artifact(snapshot, raw)

    assert first_findings == () and second_findings == ()
    assert first_path == second_path
    assert second_path is not None
    assert store.verify_raw_artifact(load_snapshot(second_path)) == ()


def test_a_conflicting_date_reuse_never_silently_swaps_the_retained_raw_artifact(
    tmp_path: Path,
) -> None:
    """A second, differently-bound candidate for an already-archived date is
    refused by the normalized (dated-layout) immutability check, and the
    ORIGINAL raw artifact already on disk must survive that attempt
    untouched — the raw side can never be swapped out from under a refused
    normalized rewrite."""
    store = UniverseStore(tmp_path)
    original, original_raw = _raw_bound_snapshot()
    path, findings = store.accept_with_raw_artifact(original, original_raw)
    assert path is not None and findings == ()
    raw_path = tmp_path / f"SP500-{AS_OF.isoformat()}.raw"
    retained_before = raw_path.read_bytes()

    other_raw = HTML_FIXTURE.read_bytes()
    conflicting = bind_raw_source(
        parse_html_constituents(other_raw, as_of=AS_OF, fetched_at=FETCHED_AT),
        raw_artifact_checksum(other_raw),
    )
    assert conflicting.checksum != original.checksum

    second_path, refusal = store.accept_with_raw_artifact(conflicting, other_raw)

    assert second_path is None
    assert refusal and "already archived immutably" in refusal[0]
    assert raw_path.read_bytes() == retained_before, "the retained raw artifact must be untouched"


# == required provenance metadata + CURRENT-only GICS =========================


def test_an_archived_fixture_derived_snapshot_carries_required_provenance(
    tmp_path: Path,
) -> None:
    snapshot, raw = _raw_bound_snapshot()
    store = UniverseStore(tmp_path / "archive")
    path, findings = store.accept_with_raw_artifact(snapshot, raw)
    assert path is not None and findings == ()

    body = json.loads(path.read_text(encoding="utf-8"))
    for field in (
        "schema_version", "index_id", "as_of", "fetched_at", "source_id",
        "source_authority", "constituent_count", "is_point_in_time_history",
        "gics_mode", "checksum", "notes",
    ):
        assert field in body, f"missing required provenance field: {field}"

    assert body["is_point_in_time_history"] is False
    assert body["gics_mode"] == GicsMode.FREE_RECONSTRUCTED.value
    assert any(
        note.startswith(RAW_CHECKSUM_NOTE_PREFIX) for note in body["notes"]
    ), "the raw checksum binding must be visible in the archived provenance record"

    apple = next(c for c in body["constituents"] if c["ticker"] == "AAPL")
    assert apple["gics_sector"] == "Information Technology"
    # CURRENT classification only: nothing here is, or claims to be, PIT GICS.
    assert "point_in_time" not in json.dumps(apple).lower()


# == immutable dated layout ====================================================


def test_a_conflicting_same_date_candidate_from_the_alternate_representation_is_refused(
    tmp_path: Path,
) -> None:
    """Two representations of the SAME page (wikitext vs. rendered HTML) are not
    guaranteed to normalize to byte-identical archive bodies (their diagnostic
    notes differ) — so one must never silently overwrite the other's archived
    entry for the same date. The archive stays immutable; it does not attempt
    to reconcile "equivalent-looking" content."""
    store = UniverseStore(tmp_path)
    wikitext_snapshot = _wikitext_snapshot()
    first_path, findings = store.accept(wikitext_snapshot)
    assert first_path is not None and findings == ()
    original_bytes = first_path.read_bytes()

    html_snapshot = parse_html_constituents(
        HTML_FIXTURE.read_bytes(), as_of=AS_OF, fetched_at=FETCHED_AT
    )
    assert html_snapshot.checksum != wikitext_snapshot.checksum, (
        "the two representations must differ in their normalized body for this"
        " test to actually exercise a conflict"
    )

    second_path, refusal = store.accept(html_snapshot)

    assert second_path is None
    assert refusal and "already archived immutably" in refusal[0]
    assert first_path.read_bytes() == original_bytes
    latest = store.latest()
    assert latest is not None
    assert latest.checksum == wikitext_snapshot.checksum


def test_an_identical_resubmission_of_a_fixture_derived_snapshot_is_idempotent(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path)
    snapshot = _wikitext_snapshot()
    first_path, first_findings = store.accept(snapshot)
    second_path, second_findings = store.accept(snapshot)

    assert first_findings == () and second_findings == ()
    assert first_path == second_path
    assert len(store.snapshots()) == 1


# == fail-closed last-known-good retention on invalid refresh =================


def test_a_refresh_that_produces_no_candidate_retains_the_last_known_good(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path)
    good = _wikitext_snapshot()
    store.accept(good)

    current, status, findings = refresh_universe(
        store, None, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )

    assert current is not None and current.checksum == good.checksum
    assert status is UniverseListStatus.CURRENT_LIST_VALID
    assert any("keeping the last-known-good list" in f for f in findings)


def test_a_refresh_with_an_invalid_candidate_never_costs_the_archived_snapshot(
    tmp_path: Path,
) -> None:
    """A candidate that fails validation (here: an emptied-out constituent
    list, as a genuinely bad parse would produce) must be rejected, and the
    previously archived fixture-derived, GICS-bearing snapshot must survive
    untouched."""
    store = UniverseStore(tmp_path)
    good = _wikitext_snapshot()
    store.accept(good)

    broken = replace(
        _wikitext_snapshot(as_of=date(2026, 9, 1)), constituents=(), notes=()
    )
    broken = replace(broken, checksum=broken.compute_checksum())
    assert validate_snapshot(broken) != ()

    current, status, findings = refresh_universe(
        store, broken, now=NOW, policy=DEFAULT_FRESHNESS[TradingMode.PAPER]
    )

    assert current is not None and current.checksum == good.checksum
    assert current.count == good.count
    assert status is UniverseListStatus.CURRENT_LIST_VALID
    assert any("last-known-good retained" in f for f in findings)
    assert len(store.snapshots()) == 1, "the broken candidate must never reach the archive"


def test_a_raw_bound_refresh_that_fails_the_raw_check_never_costs_the_archive(
    tmp_path: Path,
) -> None:
    """The raw-bound path has its own fail-closed retention story: a refresh
    candidate whose raw binding cannot be authenticated must be refused
    without disturbing the last-known-good, exactly like a normalized-only
    validation failure does."""
    store = UniverseStore(tmp_path)
    good, good_raw = _raw_bound_snapshot()
    store.accept_with_raw_artifact(good, good_raw)

    tampered_bytes = HTML_FIXTURE.read_bytes()  # does NOT hash to good's binding
    path, findings = store.accept_with_raw_artifact(good, tampered_bytes)

    assert path is None
    assert findings and "checksum mismatch" in findings[0]
    latest = store.latest()
    assert latest is not None and latest.checksum == good.checksum
    assert store.verify_raw_artifact(latest) == ()


def test_a_tampered_archived_entry_is_skipped_by_latest_not_served(
    tmp_path: Path,
) -> None:
    """Defense in depth for the archive itself: even a real, GICS-bearing
    fixture-derived entry must be refused once its on-disk bytes no longer
    authenticate, rather than served as though nothing happened."""
    store = UniverseStore(tmp_path)
    older_path, _ = store.accept(_wikitext_snapshot(as_of=date(2026, 7, 1)))
    newer_path, _ = store.accept(_wikitext_snapshot(as_of=date(2026, 8, 1)))
    assert older_path is not None and newer_path is not None

    payload = json.loads(newer_path.read_text(encoding="utf-8"))
    payload["constituents"][0]["gics_sector"] = "TAMPERED"
    newer_path.write_text(json.dumps(payload), encoding="utf-8")

    latest = store.latest()

    assert latest is not None
    assert latest.as_of == date(2026, 7, 1), "the tampered later entry must be skipped"
