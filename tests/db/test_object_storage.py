"""Object storage port tests (IMP-0205) — local, no database needed.

Done criteria: hash verification and retention tests pass.
"""

import os
from pathlib import Path

import pytest
from trading_persistence.object_storage import (
    FilesystemObjectStorage,
    InvalidReferenceError,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStoragePort,
    content_ref,
)

DATA = b"raw evidence bytes"


@pytest.fixture()
def store(tmp_path: Path) -> FilesystemObjectStorage:
    return FilesystemObjectStorage(tmp_path / "objects")


def test_put_get_round_trip_with_content_ref(store: FilesystemObjectStorage) -> None:
    stored = store.put(DATA)
    assert stored.ref == content_ref(DATA)
    assert stored.ref.startswith("sha256:")
    assert stored.size_bytes == len(DATA)
    assert store.get(stored.ref) == DATA
    assert store.exists(stored.ref)


def test_put_is_idempotent(store: FilesystemObjectStorage, tmp_path: Path) -> None:
    first = store.put(DATA)
    second = store.put(DATA)
    assert first == second
    files = [p for p in (tmp_path / "objects").rglob("*") if p.is_file()]
    assert len(files) == 1  # same content stored once


def test_hash_verification_fails_closed_on_corruption(
    store: FilesystemObjectStorage, tmp_path: Path
) -> None:
    stored = store.put(DATA)
    digest = stored.ref.removeprefix("sha256:")
    (tmp_path / "objects" / digest[:2] / digest).write_bytes(b"tampered!")
    with pytest.raises(ObjectIntegrityError):
        store.get(stored.ref)


def test_missing_and_invalid_references_fail(store: FilesystemObjectStorage) -> None:
    with pytest.raises(ObjectNotFoundError):
        store.get(content_ref(b"never stored"))
    with pytest.raises(InvalidReferenceError):
        store.get("not-a-ref")
    with pytest.raises(InvalidReferenceError):
        store.get("sha256:short")


def test_retention_purge_keeps_pinned_and_recent(
    store: FilesystemObjectStorage, tmp_path: Path
) -> None:
    pinned = store.put(b"pinned evidence")
    old_unpinned = store.put(b"old attachment")
    recent = store.put(b"recent attachment")

    # Age the first two objects far past the cut-off.
    for stored in (pinned, old_unpinned):
        digest = stored.ref.removeprefix("sha256:")
        os.utime(tmp_path / "objects" / digest[:2] / digest, (0, 0))

    purged = store.purge_unpinned(older_than_seconds=3600, pinned=frozenset({pinned.ref}))

    assert purged == [old_unpinned.ref]  # only the old, unpinned object went
    assert store.exists(pinned.ref)  # pinned survives retention (R1/R4)
    assert store.exists(recent.ref)  # recent objects are kept
    assert not store.exists(old_unpinned.ref)


def test_filesystem_adapter_satisfies_the_port(store: FilesystemObjectStorage) -> None:
    port: ObjectStoragePort = store  # structural typing check
    assert port.exists(content_ref(b"x")) is False
