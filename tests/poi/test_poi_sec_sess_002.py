"""POI-SEC-SESS-002 drill — Encrypted Runtime Session Store (real run).

Executes the canonical PoI steps against the REAL IMP-0303 store (this is the
component under test — nothing external is mocked):

1. Encrypt session at rest   -> on-disk bytes carry no plaintext material.
2. Restart adapter           -> a fresh store instance recovers the session.
3. Rotate wrapping key       -> re-encrypt under a new key; old key fails closed.
4. Revoke one session        -> the other connector's session is untouched.
5. Verify logs redaction     -> session material never reaches log output.

Pass criteria (POI_BACKLOG): session survives restart; secrets absent from
logs/DB/domain tables; single-session revoke works.
"""

from __future__ import annotations

import io
import json
import logging
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
)
from trading_contracts.enums import Environment
from trading_observability import configure_logging

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
# Synthetic session material for the drill — NOT a real credential.
FAKE_TDLIB_BLOB = b"drill-session-material-\x00\x01\x02 not-a-real-token"


def _key() -> SecretValue:
    return SecretValue(Fernet.generate_key().decode("ascii"))


def test_step1_session_encrypted_at_rest(tmp_path: Path) -> None:
    store = RuntimeSessionStore(tmp_path, Environment.PAPER, _key())
    store.save("telegram-user", FAKE_TDLIB_BLOB, now=NOW)
    on_disk = (tmp_path / "PAPER" / "telegram-user.session").read_bytes()
    assert FAKE_TDLIB_BLOB not in on_disk
    assert FAKE_TDLIB_BLOB.hex().encode("ascii") not in on_disk
    assert b"drill-session-material" not in on_disk


def test_step2_session_survives_restart(tmp_path: Path) -> None:
    key = _key()
    RuntimeSessionStore(tmp_path, Environment.PAPER, key).save(
        "telegram-user", FAKE_TDLIB_BLOB, now=NOW
    )
    # "restart": a brand-new store instance over the same root + key
    restarted = RuntimeSessionStore(tmp_path, Environment.PAPER, key)
    metadata, blob = restarted.load("telegram-user", now=NOW)
    assert blob == FAKE_TDLIB_BLOB
    assert metadata.environment is Environment.PAPER


def test_step3_wrapping_key_rotation(tmp_path: Path) -> None:
    old_key, new_key = _key(), _key()
    old_store = RuntimeSessionStore(tmp_path, Environment.PAPER, old_key)
    old_store.save("telegram-user", FAKE_TDLIB_BLOB, now=NOW)

    # Rotation procedure: decrypt under the old wrapping key, re-encrypt
    # under the new one (metadata rotations counter advances via rotate()).
    _, blob = old_store.load("telegram-user", now=NOW)
    new_store = RuntimeSessionStore(tmp_path, Environment.PAPER, new_key)
    metadata = new_store.rotate("telegram-user", blob, now=NOW)
    assert metadata.rotations >= 1

    # New key decrypts; the OLD key now fails closed (no silent fallback).
    _, recovered = new_store.load("telegram-user", now=NOW)
    assert recovered == FAKE_TDLIB_BLOB
    with pytest.raises(CorruptSessionError):
        old_store.load("telegram-user", now=NOW)


def test_step4_revoke_one_session_only(tmp_path: Path) -> None:
    key = _key()
    store = RuntimeSessionStore(tmp_path, Environment.PAPER, key)
    store.save("telegram-user", FAKE_TDLIB_BLOB, now=NOW)
    store.save("whatsapp-user", b"other-connector-material", now=NOW)

    assert store.revoke("telegram-user") is True
    with pytest.raises(MissingSessionError):
        store.load("telegram-user", now=NOW)
    _, other = store.load("whatsapp-user", now=NOW)
    assert other == b"other-connector-material"  # untouched


def test_step5_logs_never_contain_session_material(tmp_path: Path) -> None:
    key = _key()
    store = RuntimeSessionStore(tmp_path, Environment.PAPER, key)
    metadata = store.save("telegram-user", FAKE_TDLIB_BLOB, now=NOW)

    stream = io.StringIO()
    configure_logging(service="connectors", environment="paper", stream=stream)
    logger = logging.getLogger("poi.drill")
    # Log safe metadata AND (deliberately) try to log material under a
    # sensitive key — central redaction must scrub it.
    logger.info(
        "session stored",
        extra={
            "connector": metadata.connector_id,
            "session_blob": FAKE_TDLIB_BLOB.hex(),
            "rotations": metadata.rotations,
        },
    )
    output = stream.getvalue()
    document = json.loads(output.strip())
    assert document["session_blob"] == "[REDACTED]"
    assert FAKE_TDLIB_BLOB.hex() not in output
    assert "drill-session-material" not in output


def test_fail_closed_paths(tmp_path: Path) -> None:
    key = _key()
    store = RuntimeSessionStore(tmp_path, Environment.PAPER, key)
    # missing -> AUTH_REQUIRED path
    with pytest.raises(MissingSessionError):
        store.load("never-created", now=NOW)
    # tampered -> corrupt, and the error message leaks nothing
    store.save("telegram-user", FAKE_TDLIB_BLOB, now=NOW)
    path = tmp_path / "PAPER" / "telegram-user.session"
    path.write_bytes(path.read_bytes()[:-4] + b"XXXX")
    with pytest.raises(CorruptSessionError) as corrupt:
        store.load("telegram-user", now=NOW)
    assert "drill-session-material" not in str(corrupt.value)
    # expired -> re-auth required
    store.save(
        "expiring", FAKE_TDLIB_BLOB, now=NOW, expires_at=NOW + timedelta(minutes=5)
    )
    with pytest.raises(ExpiredSessionError):
        store.load("expiring", now=NOW + timedelta(minutes=6))
    # PAPER/LIVE separation: a PAPER blob is invisible to another environment
    research = RuntimeSessionStore(tmp_path, Environment.RESEARCH, key)
    with pytest.raises(MissingSessionError):
        research.load("telegram-user", now=NOW)
