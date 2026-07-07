"""Shared fakes for backup/restore tests (no PostgreSQL/age needed)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from trading_persistence.audit import AuditCheckpoint
from trading_persistence.backup import BackupPolicy, BackupSource


class FakeEncryptor:
    """Reversible stand-in for age: prefixes a marker byte string."""

    MARKER = b"FAKE-AGE\n"

    @property
    def scheme(self) -> str:
        return "age"

    def encrypt(self, source: Path, destination: Path) -> None:
        destination.write_bytes(self.MARKER + source.read_bytes())

    def decrypt(self, source: Path, destination: Path) -> None:
        data = source.read_bytes()
        assert data.startswith(self.MARKER), "artifact was not encrypted by FakeEncryptor"
        destination.write_bytes(data[len(self.MARKER) :])


class FileSource:
    """BackupSource producing a fixed plaintext file."""

    def __init__(self, component: str, filename: str, payload: bytes) -> None:
        self._component = component
        self._filename = filename
        self._payload = payload

    @property
    def component(self) -> str:
        return self._component

    def produce(self, workdir: Path) -> Path:
        target = workdir / self._filename
        target.write_bytes(self._payload)
        return target


def content_addressed_store(root: Path, payloads: list[bytes]) -> Path:
    """Build a valid content-addressed object store directory."""
    store = root / "objects"
    store.mkdir(parents=True)
    for payload in payloads:
        digest = hashlib.sha256(payload).hexdigest()
        (store / digest[:2]).mkdir(exist_ok=True)
        (store / digest[:2] / digest).write_bytes(payload)
    return store


@pytest.fixture
def policy() -> BackupPolicy:
    # Security spec section 13.1/13.2 values, provided as input.
    return BackupPolicy(
        database_full_frequency="daily",
        retention_days=30,
        offsite_frequency="daily",
        rpo_target_minutes=15,
        rto_target_minutes=240,
    )


@pytest.fixture
def checkpoint() -> AuditCheckpoint:
    return AuditCheckpoint(last_sequence_no=7, last_hash="a" * 64)


def make_sources() -> list[BackupSource]:
    return [
        FileSource("postgresql", "postgresql.dump", b"PGDMP fake dump content"),
        FileSource("configuration", "configuration.tar.gz", b"config archive"),
    ]
