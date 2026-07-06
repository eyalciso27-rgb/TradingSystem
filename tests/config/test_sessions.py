"""Runtime session store tests (IMP-0303).

Done criterion: one connector session can be revoked without affecting
others. All session material in fixtures is fake.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from trading_config.secrets import SecretValue
from trading_config.sessions import (
    CorruptSessionError,
    ExpiredSessionError,
    MissingSessionError,
    RuntimeSessionStore,
    SessionPathError,
)
from trading_contracts.enums import Environment

NOW = datetime(2026, 7, 5, 10, 0, tzinfo=UTC)
BLOB = b"fake-tdlib-session-bytes-not-real"


@pytest.fixture()
def key() -> SecretValue:
    return SecretValue(Fernet.generate_key().decode("ascii"))


@pytest.fixture()
def store(tmp_path: Path, key: SecretValue) -> RuntimeSessionStore:
    return RuntimeSessionStore(tmp_path, Environment.PAPER, key)


def test_save_load_round_trip(store: RuntimeSessionStore) -> None:
    store.save("telegram-user", BLOB, now=NOW)
    metadata, blob = store.load("telegram-user", now=NOW)
    assert blob == BLOB
    assert metadata.environment is Environment.PAPER
    assert metadata.rotations == 0


def test_plaintext_never_persisted(store: RuntimeSessionStore, tmp_path: Path) -> None:
    store.save("telegram-user", BLOB, now=NOW)
    on_disk = (tmp_path / "PAPER" / "telegram-user.session").read_bytes()
    assert BLOB not in on_disk
    assert BLOB.hex().encode() not in on_disk  # not even hex-encoded plaintext


def test_revoke_one_connector_leaves_others_untouched(store: RuntimeSessionStore) -> None:
    store.save("telegram-user", b"fake-a", now=NOW)
    store.save("whatsapp-user", b"fake-b", now=NOW)
    store.save("discord-user", b"fake-c", now=NOW)

    assert store.revoke("whatsapp-user") is True

    with pytest.raises(MissingSessionError):
        store.load("whatsapp-user", now=NOW)
    # The DoD: every other connector session still loads.
    assert store.load("telegram-user", now=NOW)[1] == b"fake-a"
    assert store.load("discord-user", now=NOW)[1] == b"fake-c"
    assert store.list_connectors() == ["discord-user", "telegram-user"]


def test_missing_session_fails_closed(store: RuntimeSessionStore) -> None:
    with pytest.raises(MissingSessionError) as excinfo:
        store.load("never-created", now=NOW)
    assert "re-authentication" in str(excinfo.value)


def test_expired_session_fails_closed(store: RuntimeSessionStore) -> None:
    store.save("telegram-user", BLOB, now=NOW, expires_at=NOW + timedelta(hours=1))
    later = NOW + timedelta(hours=2)
    with pytest.raises(ExpiredSessionError):
        store.load("telegram-user", now=later)


def test_corrupt_blob_fails_closed(store: RuntimeSessionStore, tmp_path: Path) -> None:
    store.save("telegram-user", BLOB, now=NOW)
    path = tmp_path / "PAPER" / "telegram-user.session"
    path.write_bytes(b"tampered" + path.read_bytes()[8:])
    with pytest.raises(CorruptSessionError):
        store.load("telegram-user", now=NOW)


def test_wrong_key_fails_closed(tmp_path: Path, key: SecretValue) -> None:
    RuntimeSessionStore(tmp_path, Environment.PAPER, key).save("c1", BLOB, now=NOW)
    other_key = SecretValue(Fernet.generate_key().decode("ascii"))
    other_store = RuntimeSessionStore(tmp_path, Environment.PAPER, other_key)
    with pytest.raises(CorruptSessionError):
        other_store.load("c1", now=NOW)


def test_invalid_key_rejected_at_construction(tmp_path: Path) -> None:
    with pytest.raises(CorruptSessionError):
        RuntimeSessionStore(tmp_path, Environment.PAPER, SecretValue("not-a-key"))


def test_environment_separation_no_fallback(tmp_path: Path, key: SecretValue) -> None:
    paper = RuntimeSessionStore(tmp_path, Environment.PAPER, key)
    live = RuntimeSessionStore(tmp_path, Environment.LIVE, key)
    paper.save("telegram-user", BLOB, now=NOW)
    # LIVE never falls back to a PAPER session.
    with pytest.raises(MissingSessionError):
        live.load("telegram-user", now=NOW)


def test_rotation_replaces_blob_and_counts(store: RuntimeSessionStore) -> None:
    store.save("telegram-user", b"fake-old", now=NOW)
    metadata = store.rotate("telegram-user", b"fake-new", now=NOW + timedelta(minutes=1))
    assert metadata.rotations == 1
    _, blob = store.load("telegram-user", now=NOW + timedelta(minutes=2))
    assert blob == b"fake-new"


def test_rotate_after_expiry_is_reauth(store: RuntimeSessionStore) -> None:
    store.save("telegram-user", b"fake-old", now=NOW, expires_at=NOW + timedelta(minutes=1))
    metadata = store.rotate("telegram-user", b"fake-new", now=NOW + timedelta(hours=1))
    assert metadata.rotations == 1
    assert store.load("telegram-user", now=NOW + timedelta(hours=1))[1] == b"fake-new"


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "c:d", "..", "~h"])
def test_unsafe_connector_ids_rejected(store: RuntimeSessionStore, bad: str) -> None:
    with pytest.raises(SessionPathError):
        store.save(bad, BLOB, now=NOW)


def test_metadata_repr_contains_no_session_material(store: RuntimeSessionStore) -> None:
    metadata = store.save("telegram-user", BLOB, now=NOW)
    assert BLOB.hex() not in repr(metadata)
    assert "blob" not in repr(metadata)
