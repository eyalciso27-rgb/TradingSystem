"""A-WP2 — current S&P 500 / GICS monthly snapshot archive (offline closure).

This proves the archive contract documented in
``config/data-sources/sp500-snapshots/README.md`` for the CURRENT S&P 500
constituent list, including its optional CURRENT-only GICS context
(``ADR-0017``, ``FREE_RECONSTRUCTED``), on top of the ``UniverseStore`` /
``ConstituentSnapshot`` machinery already implemented in
``trading_market_data_core.sp500_current_universe``:

* the archive's dated layout is IMMUTABLE — a conflicting candidate for a date
  already on record is refused, even when it comes from a differently-encoded
  but equally "current" representation of the same source page;
* every archived entry binds TWO independent SHA-256 checksums: a
  **raw** checksum over the exact bytes of the fetched source artifact, and a
  **normalized** checksum that self-authenticates the parsed, archived body —
  each fails closed on its own axis, and neither can stand in for the other;
* every archived entry carries the required provenance metadata, and its GICS
  fields are always labelled CURRENT / ``FREE_RECONSTRUCTED`` — never
  point-in-time history;
* a refresh that fails, parses to nothing, or fails validation NEVER costs the
  last-known-good archived snapshot (fail-closed retention).

Entirely offline: this reads only the recorded fixtures under
``tests/fixtures/sp500/`` (the same recorded API captures used by
``test_sp500_wikitext_regression.py``). No network call is made, no historical
GICS or point-in-time membership is constructed, and no strategy parameter is
touched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from trading_market_data_core.dataset_v1_builder import BuilderError, EvidenceBinding
from trading_market_data_core.sp500_current_universe import (
    ConstituentSnapshot,
    UniverseStore,
    load_snapshot,
    parse_html_constituents,
    parse_wikitext_constituents,
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

#: The convention this archive uses to bind an entry back to the exact raw
#: bytes it was derived from (see the README). ``notes`` is already a
#: free-form provenance field on ``ConstituentSnapshot``; this is simply a
#: recognised, greppable prefix within it, exactly like the existing
#: ``DIAGNOSTIC_NOTE_PREFIX`` convention in the same module.
RAW_CHECKSUM_NOTE_PREFIX = "raw source checksum: "


def _wikitext_snapshot(as_of: date = AS_OF) -> ConstituentSnapshot:
    return parse_wikitext_constituents(
        WIKITEXT_FIXTURE.read_bytes(), as_of=as_of, fetched_at=FETCHED_AT
    )


def _raw_artifact(tmp_path: Path, fixture: Path = WIKITEXT_FIXTURE) -> tuple[Path, str]:
    """Copy a fixture into ``tmp_path`` (never mutate the checked-in fixture)
    and return its path with the sha256 digest of its actual bytes."""
    raw_path = tmp_path / fixture.name
    raw_path.write_bytes(fixture.read_bytes())
    checksum = "sha256:" + hashlib.sha256(raw_path.read_bytes()).hexdigest()
    return raw_path, checksum


def _bind_raw_checksum(
    snapshot: ConstituentSnapshot, raw_checksum: str
) -> ConstituentSnapshot:
    """Record the raw-artifact checksum on the snapshot and reseal it.

    Sealing (recomputing ``checksum`` over the new body, including the new
    note) is what makes the normalized checksum bind the raw checksum too:
    tampering the recorded raw checksum after the fact is just as much a
    corruption of the archived body as tampering a ticker.
    """
    bound = replace(
        snapshot, notes=snapshot.notes + (f"{RAW_CHECKSUM_NOTE_PREFIX}{raw_checksum}",)
    )
    return replace(bound, checksum=bound.compute_checksum())


def _recorded_raw_checksum(snapshot: ConstituentSnapshot) -> str | None:
    for note in snapshot.notes:
        if note.startswith(RAW_CHECKSUM_NOTE_PREFIX):
            return note[len(RAW_CHECKSUM_NOTE_PREFIX) :]
    return None


# == raw + normalized checksum bindings =======================================


def test_an_archived_entry_binds_an_independent_raw_and_normalized_checksum(
    tmp_path: Path,
) -> None:
    raw_path, raw_checksum = _raw_artifact(tmp_path)
    snapshot = _bind_raw_checksum(_wikitext_snapshot(), raw_checksum)

    # Both checksums are real sha256 digests, and they authenticate DIFFERENT
    # content: the raw fetched payload vs. the normalized, parsed archive body.
    assert raw_checksum.startswith("sha256:")
    assert snapshot.checksum.startswith("sha256:")
    assert raw_checksum != snapshot.checksum
    EvidenceBinding(name="wikitext-raw", path=raw_path, checksum=raw_checksum).verify()
    assert snapshot.checksum == snapshot.compute_checksum()

    store = UniverseStore(tmp_path / "archive")
    archived_path, findings = store.accept(snapshot)
    assert archived_path is not None and findings == ()

    reloaded = load_snapshot(archived_path)
    assert reloaded.checksum == snapshot.checksum
    assert _recorded_raw_checksum(reloaded) == raw_checksum, (
        "the archived, reloaded entry must still carry the exact raw checksum"
        " it was bound to at archive time"
    )


def test_tampering_the_raw_artifact_is_detected_independently_of_the_archive(
    tmp_path: Path,
) -> None:
    """The raw checksum fails closed on its own axis, even though the archived
    normalized snapshot (already sealed in memory) is untouched by the tamper."""
    raw_path, raw_checksum = _raw_artifact(tmp_path)
    snapshot = _bind_raw_checksum(_wikitext_snapshot(), raw_checksum)
    assert snapshot.checksum == snapshot.compute_checksum()  # unaffected so far

    raw_path.write_bytes(raw_path.read_bytes() + b"\ntampered")

    with pytest.raises(BuilderError, match="checksum mismatch"):
        EvidenceBinding(name="wikitext-raw", path=raw_path, checksum=raw_checksum).verify()
    # The normalized side is a separate, already-sealed value: tampering the
    # raw file on disk cannot retroactively change it.
    assert snapshot.checksum == snapshot.compute_checksum()


def test_a_fabricated_raw_checksum_never_verifies(tmp_path: Path) -> None:
    """A decorative digest that was never computed from the real bytes must be
    rejected outright, exactly like the normalized-checksum case elsewhere."""
    raw_path, _ = _raw_artifact(tmp_path)
    fabricated = "sha256:" + "f" * 64
    with pytest.raises(BuilderError, match="checksum mismatch"):
        EvidenceBinding(name="wikitext-raw", path=raw_path, checksum=fabricated).verify()


# == required provenance metadata + CURRENT-only GICS =========================


def test_an_archived_fixture_derived_snapshot_carries_required_provenance(
    tmp_path: Path,
) -> None:
    _, raw_checksum = _raw_artifact(tmp_path)
    snapshot = _bind_raw_checksum(_wikitext_snapshot(), raw_checksum)
    store = UniverseStore(tmp_path / "archive")
    path, findings = store.accept(snapshot)
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
