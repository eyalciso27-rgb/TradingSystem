"""Backup creation, manifest, integrity verification — fail closed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from trading_persistence.audit import AuditCheckpoint
from trading_persistence.backup import (
    MANIFEST_FILENAME,
    MANIFEST_HASH_FILENAME,
    BackupError,
    BackupIntegrityError,
    BackupPolicy,
    DirectoryArchiveSource,
    EnvironmentMismatchError,
    create_backup,
    sha256_file,
    verify_backup,
    verify_object_store,
)

from tests.backup.conftest import FakeEncryptor, FileSource, content_addressed_store, make_sources


def make_backup(root: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint) -> Path:
    return create_backup(
        root=root,
        environment="paper",
        sources=make_sources(),
        encryptor=FakeEncryptor(),
        alembic_revision="0005",
        audit_checkpoint=checkpoint,
        policy=policy,
    )


def test_manifest_created_with_expected_fields(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_backup(tmp_path, policy, checkpoint)
    manifest = json.loads((backup_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == "1"
    assert manifest["environment"] == "paper"
    assert manifest["alembic_revision"] == "0005"
    assert manifest["audit_checkpoint"] == {"last_sequence_no": 7, "last_hash": "a" * 64}
    assert manifest["policy"]["rpo_target_minutes"] == 15
    assert manifest["policy"]["rto_target_minutes"] == 240
    components = {a["component"] for a in manifest["artifacts"]}
    assert components == {"postgresql", "configuration"}
    for artifact in manifest["artifacts"]:
        assert set(artifact) == {"component", "object_key", "sha256", "size_bytes", "encryption"}
        assert artifact["encryption"] == "age"
        stored = backup_dir / artifact["object_key"]
        assert stored.exists()
        assert sha256_file(stored) == artifact["sha256"]
    # nothing unencrypted left behind
    assert not list(backup_dir.glob("*.dump"))


def test_verify_backup_passes_on_intact_backup(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_backup(tmp_path, policy, checkpoint)
    manifest = verify_backup(backup_dir)
    assert manifest.backup_id == backup_dir.name
    # verification is idempotent and read-only
    assert verify_backup(backup_dir).backup_id == manifest.backup_id


def test_corrupt_artifact_fails_closed(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_backup(tmp_path, policy, checkpoint)
    artifact = next(backup_dir.glob("postgresql.dump.age"))
    artifact.write_bytes(artifact.read_bytes() + b"tamper")
    with pytest.raises(BackupIntegrityError, match="hash mismatch|size mismatch"):
        verify_backup(backup_dir)


def test_missing_artifact_fails_closed(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_backup(tmp_path, policy, checkpoint)
    next(backup_dir.glob("configuration.tar.gz.age")).unlink()
    with pytest.raises(BackupIntegrityError, match="missing"):
        verify_backup(backup_dir)


def test_tampered_manifest_fails_closed(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_backup(tmp_path, policy, checkpoint)
    manifest_path = backup_dir / MANIFEST_FILENAME
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["environment"] = "dev"  # forge the environment
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(BackupIntegrityError, match="manifest hash mismatch"):
        verify_backup(backup_dir)
    (backup_dir / MANIFEST_HASH_FILENAME).unlink()
    with pytest.raises(BackupIntegrityError, match="missing"):
        verify_backup(backup_dir)


def test_environment_namespaces_are_isolated(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    for environment in ("dev", "paper"):
        create_backup(
            root=tmp_path,
            environment=environment,
            sources=make_sources(),
            encryptor=FakeEncryptor(),
            alembic_revision="0005",
            audit_checkpoint=checkpoint,
            policy=policy,
        )
    dev_dirs = list((tmp_path / "dev").iterdir())
    paper_dirs = list((tmp_path / "paper").iterdir())
    assert len(dev_dirs) == 1 and len(paper_dirs) == 1
    assert not set(dev_dirs) & set(paper_dirs)


def test_live_environment_rejected(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    with pytest.raises(EnvironmentMismatchError, match="LIVE stays disabled"):
        create_backup(
            root=tmp_path,
            environment="live",
            sources=make_sources(),
            encryptor=FakeEncryptor(),
            alembic_revision="0005",
            audit_checkpoint=checkpoint,
            policy=policy,
        )


def test_backup_never_touches_existing_backups(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    first = make_backup(tmp_path, policy, checkpoint)
    before = {p: sha256_file(p) for p in first.rglob("*") if p.is_file()}
    second = make_backup(tmp_path, policy, checkpoint)
    assert first != second
    assert {p: sha256_file(p) for p in first.rglob("*") if p.is_file()} == before


def test_no_secret_material_in_manifest(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_backup(tmp_path, policy, checkpoint)
    text = (backup_dir / MANIFEST_FILENAME).read_text(encoding="utf-8").lower()
    for marker in ("password", "pgpassword", "token", "credential", "database_url", "secret"):
        assert marker not in text


def test_failed_source_leaves_no_partial_backup(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    class ExplodingSource:
        component = "postgresql"

        def produce(self, workdir: Path) -> Path:
            raise RuntimeError("pg_dump exploded")

    with pytest.raises(RuntimeError):
        create_backup(
            root=tmp_path,
            environment="paper",
            sources=[ExplodingSource()],
            encryptor=FakeEncryptor(),
            alembic_revision="0005",
            audit_checkpoint=checkpoint,
            policy=policy,
        )
    assert list((tmp_path / "paper").iterdir()) == []


def test_directory_source_archives_tree(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.bin").write_bytes(b"payload")
    artifact = DirectoryArchiveSource("object_store", root).produce(tmp_path)
    assert artifact.name == "object_store.tar.gz"
    with pytest.raises(BackupError, match="does not exist"):
        DirectoryArchiveSource("sessions", tmp_path / "nope").produce(tmp_path)


def test_object_store_reference_verification(tmp_path: Path) -> None:
    store = content_addressed_store(tmp_path, [b"one", b"two"])
    assert verify_object_store(store) == []
    bad = next(p for p in store.rglob("*") if p.is_file())
    bad.write_bytes(b"corrupted content")
    problems = verify_object_store(store)
    assert len(problems) == 1 and "not reflected in object key" in problems[0]


def test_file_source_helper_is_consistent(tmp_path: Path) -> None:
    source = FileSource("postgresql", "postgresql.dump", b"x")
    assert source.produce(tmp_path).read_bytes() == b"x"
