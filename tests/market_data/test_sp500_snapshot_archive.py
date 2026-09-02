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
* that raw-sidecar verification is wired into SELECTION, not just available
  for a caller to invoke manually: ``UniverseStore.latest()`` demotes a
  raw-bound entry whose retained sidecar has been lost or tampered with
  exactly as it would a normalized body that fails to authenticate, and falls
  back to the newest entry that is fully valid on every axis it declares;
* the raw artifact and the normalized entry are each written atomically
  (temp file + fsync + rename) and committed in an order — raw first,
  normalized second — chosen so that a crash or storage failure between the
  two writes can never leave a normalized entry claiming a raw binding that
  is not durably backed by real bytes on disk; at worst it leaves a harmless,
  unreferenced raw sidecar that a retry with the same inputs heals;
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

import pytest
from trading_market_data_core import sp500_current_universe as _module
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


# == raw-sidecar verification is wired into SELECTION, not just available ====


def test_latest_skips_a_raw_bound_entry_whose_retained_sidecar_is_tampered(
    tmp_path: Path,
) -> None:
    """``latest()`` must not serve a newer entry whose independent raw
    provenance has failed, even though its normalized body is still intact —
    it demotes it exactly as it would a tampered normalized body, and falls
    back to the newest entry that is fully valid."""
    store = UniverseStore(tmp_path)
    older, older_raw = _raw_bound_snapshot(as_of=date(2026, 7, 1))
    newer, newer_raw = _raw_bound_snapshot(as_of=date(2026, 8, 1))
    assert store.accept_with_raw_artifact(older, older_raw)[0] is not None
    assert store.accept_with_raw_artifact(newer, newer_raw)[0] is not None

    newer_raw_path = tmp_path / f"SP500-{date(2026, 8, 1).isoformat()}.raw"
    newer_raw_path.write_bytes(newer_raw_path.read_bytes() + b"tampered")

    latest = store.latest()

    assert latest is not None
    assert latest.as_of == date(2026, 7, 1)


def test_latest_skips_a_raw_bound_entry_whose_retained_sidecar_is_missing(
    tmp_path: Path,
) -> None:
    store = UniverseStore(tmp_path)
    older, older_raw = _raw_bound_snapshot(as_of=date(2026, 7, 1))
    newer, newer_raw = _raw_bound_snapshot(as_of=date(2026, 8, 1))
    store.accept_with_raw_artifact(older, older_raw)
    store.accept_with_raw_artifact(newer, newer_raw)

    (tmp_path / f"SP500-{date(2026, 8, 1).isoformat()}.raw").unlink()

    latest = store.latest()

    assert latest is not None and latest.as_of == date(2026, 7, 1)


def test_latest_returns_none_when_the_only_entry_is_raw_bound_and_invalidated(
    tmp_path: Path,
) -> None:
    """No fallback candidate exists at all: latest() must report unavailable
    rather than serving the one entry it can no longer authenticate."""
    store = UniverseStore(tmp_path)
    snapshot, raw = _raw_bound_snapshot()
    store.accept_with_raw_artifact(snapshot, raw)
    (tmp_path / f"SP500-{AS_OF.isoformat()}.raw").unlink()

    assert store.latest() is None


def test_latest_is_unaffected_by_the_raw_check_for_a_normalized_only_entry(
    tmp_path: Path,
) -> None:
    """An entry archived through the plain ``accept()`` path never declared a
    raw binding, so it is not subject to raw-sidecar verification at all."""
    store = UniverseStore(tmp_path)
    path, findings = store.accept(_wikitext_snapshot())
    assert path is not None and findings == ()

    latest = store.latest()

    assert latest is not None and latest.as_of == AS_OF


# == atomic, crash-safe archival (raw + normalized are not a single write) ===


def test_a_write_failure_persisting_the_raw_artifact_leaves_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A storage failure during the FIRST (raw) atomic write must leave
    nothing on disk on either axis, and a clean retry afterwards must still
    succeed — the failure must not corrupt or half-populate the archive."""
    store = UniverseStore(tmp_path)
    snapshot, raw = _raw_bound_snapshot()

    def _boom(_path: Path, _data: bytes) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(_module, "_atomic_write_bytes", _boom)
    with pytest.raises(OSError, match="simulated storage failure"):
        store.accept_with_raw_artifact(snapshot, raw)
    monkeypatch.undo()

    assert store.snapshots() == ()
    raw_path = tmp_path / f"SP500-{AS_OF.isoformat()}.raw"
    assert not raw_path.exists()
    assert list(tmp_path.glob("*.tmp")) == [], "no temp file may survive a failed write"

    # Storage works again: retrying with the SAME inputs succeeds cleanly.
    path, findings = store.accept_with_raw_artifact(snapshot, raw)
    assert path is not None and findings == ()
    assert store.verify_raw_artifact(load_snapshot(path)) == ()


def test_a_crash_between_the_raw_and_normalized_writes_recovers_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash AFTER the raw artifact is durably committed but BEFORE the
    normalized entry is written must leave at most a harmless, unreferenced
    raw sidecar — never a normalized entry claiming a raw binding that isn't
    durably backed — and a retry with the same inputs must recover fully."""
    store = UniverseStore(tmp_path)
    snapshot, raw = _raw_bound_snapshot()
    real_atomic_write = _module._atomic_write_bytes
    calls = {"n": 0}

    def _fail_on_second_write(path: Path, data: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated crash before the normalized write commits")
        real_atomic_write(path, data)

    monkeypatch.setattr(_module, "_atomic_write_bytes", _fail_on_second_write)
    with pytest.raises(OSError, match="simulated crash"):
        store.accept_with_raw_artifact(snapshot, raw)
    monkeypatch.undo()

    # The normalized side never became visible...
    assert store.snapshots() == ()
    assert store.latest() is None
    # ...but the raw side is ALREADY durable: it was fsynced and renamed into
    # place before the normalized write was even attempted.
    raw_path = tmp_path / f"SP500-{AS_OF.isoformat()}.raw"
    assert raw_path.is_file()
    assert raw_artifact_checksum(raw_path.read_bytes()) == recorded_raw_checksum(snapshot)
    assert list(tmp_path.glob("*.tmp")) == []

    # A retry with the SAME inputs recovers: the already-durable raw side is
    # left untouched (write-once), and only the missing normalized side is
    # written.
    path, findings = store.accept_with_raw_artifact(snapshot, raw)
    assert path is not None and findings == ()
    reloaded = load_snapshot(path)
    assert store.verify_raw_artifact(reloaded) == ()
    latest = store.latest()
    assert latest is not None and latest.checksum == snapshot.checksum


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
