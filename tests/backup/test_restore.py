"""Restore: validation order, fail-closed behavior, verification, report."""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from trading_persistence.audit import AuditCheckpoint
from trading_persistence.backup import (
    BackupPolicy,
    DirectoryArchiveSource,
    RestoreFailedError,
    RestoreReport,
    create_backup,
    restore_backup,
)

from tests.backup.conftest import FakeEncryptor, content_addressed_store, make_sources


class RestoreHarness:
    """Injectable collaborators simulating the database and filesystem."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.empty = True
        self.restored_dumps: list[bytes] = []
        self.revision = "0005"
        self.audit_problems: list[str] = []
        self.checkpoints_seen: list[AuditCheckpoint] = []

    def database_is_empty(self) -> bool:
        return self.empty

    def restore_database(self, dump: Path) -> None:
        self.restored_dumps.append(dump.read_bytes())

    def current_alembic_revision(self) -> str:
        return self.revision

    def verify_audit(self, checkpoint: AuditCheckpoint) -> list[str]:
        self.checkpoints_seen.append(checkpoint)
        return self.audit_problems

    def restore_directory(self, component: str, artifact: Path) -> Path:
        target = self.tmp_path / "restored"
        target.mkdir(exist_ok=True)
        with tarfile.open(artifact, "r:gz") as archive:
            archive.extractall(target, filter="data")
        return target / component


def make_paper_backup(
    tmp_path: Path,
    policy: BackupPolicy,
    checkpoint: AuditCheckpoint,
    *,
    with_object_store: bool = False,
) -> Path:
    sources = make_sources()
    if with_object_store:
        store = content_addressed_store(tmp_path / "src", [b"alpha", b"beta"])
        renamed = store.parent / "object_store"
        shutil.move(str(store), renamed)
        sources.append(DirectoryArchiveSource("object_store", renamed))
    return create_backup(
        root=tmp_path / "backups",
        environment="paper",
        sources=sources,
        encryptor=FakeEncryptor(),
        alembic_revision="0005",
        audit_checkpoint=checkpoint,
        policy=policy,
    )


def run_restore(
    backup_dir: Path, harness: RestoreHarness, **overrides: object
) -> RestoreReport:
    kwargs: dict[str, object] = dict(
        backup_dir=backup_dir,
        target_environment="paper",
        encryptor=FakeEncryptor(),
        confirm=True,
        database_is_empty=harness.database_is_empty,
        restore_database=harness.restore_database,
        current_alembic_revision=harness.current_alembic_revision,
        verify_audit=harness.verify_audit,
        restore_directory=harness.restore_directory,
    )
    kwargs.update(overrides)
    return restore_backup(**kwargs)  # type: ignore[arg-type]


def test_full_restore_succeeds_with_report(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint, with_object_store=True)
    harness = RestoreHarness(tmp_path)
    report = run_restore(backup_dir, harness)
    assert report.succeeded
    names = [s.name for s in report.steps]
    assert names == [
        "validate_artifacts_and_manifest",
        "validate_environment",
        "require_confirmation",
        "validate_restore_target",
        "decrypt_artifacts",
        "restore_database",
        "verify_migration_state",
        "verify_audit_checkpoint",
        "verify_object_references",
    ]
    assert harness.restored_dumps == [b"PGDMP fake dump content"]
    assert harness.checkpoints_seen == [checkpoint]
    assert report.rpo_target_minutes == 15 and report.rto_target_minutes == 240
    assert report.duration_seconds >= 0 and report.finished_at
    assert "PGPASSWORD" not in report.to_json()


def test_dry_run_validates_without_writing(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    report = run_restore(backup_dir, harness, dry_run=True, confirm=False)
    assert report.succeeded and report.dry_run
    assert report.steps[-1].status == "skipped"
    assert harness.restored_dumps == []


def test_environment_mismatch_rejected_before_any_write(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    with pytest.raises(RestoreFailedError, match="backup is for 'paper'"):
        run_restore(backup_dir, harness, target_environment="dev")
    assert harness.restored_dumps == []


def test_live_target_rejected(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    with pytest.raises(RestoreFailedError, match="LIVE stays disabled"):
        run_restore(backup_dir, harness, target_environment="live")


def test_restore_requires_explicit_confirmation(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    with pytest.raises(RestoreFailedError, match="explicit confirmation"):
        run_restore(backup_dir, harness, confirm=False)
    assert harness.restored_dumps == []


def test_nonempty_database_rejected_without_controlled_flag(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    harness.empty = False
    with pytest.raises(RestoreFailedError, match="silent overwrite is forbidden"):
        run_restore(backup_dir, harness)
    report = run_restore(backup_dir, harness, controlled_nonempty=True)
    assert report.succeeded


def test_corrupt_backup_rejected_before_restore(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    artifact = next(backup_dir.glob("postgresql.dump.age"))
    artifact.write_bytes(b"garbage")
    harness = RestoreHarness(tmp_path)
    with pytest.raises(RestoreFailedError) as excinfo:
        run_restore(backup_dir, harness)
    assert excinfo.value.report.steps[0].status == "failed"
    assert harness.restored_dumps == []  # nothing written after failed validation


def test_migration_state_mismatch_fails(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    harness.revision = "9999"
    with pytest.raises(RestoreFailedError, match="does not match"):
        run_restore(backup_dir, harness)


def test_audit_checkpoint_failure_fails_restore(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    harness.audit_problems = ["hash chain broken at sequence 5"]
    with pytest.raises(RestoreFailedError, match="audit chain failed"):
        run_restore(backup_dir, harness)


def test_object_reference_failure_fails_restore(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    # store containing a file whose content hash is NOT in its key
    bad_store = tmp_path / "src" / "object_store"
    bad_store.mkdir(parents=True)
    (bad_store / "deadbeef").write_bytes(b"does not match its key")
    backup_dir = create_backup(
        root=tmp_path / "backups",
        environment="paper",
        sources=[*make_sources(), DirectoryArchiveSource("object_store", bad_store)],
        encryptor=FakeEncryptor(),
        alembic_revision="0005",
        audit_checkpoint=checkpoint,
        policy=policy,
    )
    harness = RestoreHarness(tmp_path)
    with pytest.raises(RestoreFailedError, match="object reference verification failed"):
        run_restore(backup_dir, harness)


def test_failed_restore_report_is_carried_on_exception(
    tmp_path: Path, policy: BackupPolicy, checkpoint: AuditCheckpoint
) -> None:
    backup_dir = make_paper_backup(tmp_path, policy, checkpoint)
    harness = RestoreHarness(tmp_path)
    harness.audit_problems = ["broken"]
    with pytest.raises(RestoreFailedError) as excinfo:
        run_restore(backup_dir, harness)
    report = excinfo.value.report
    assert not report.succeeded
    assert any(s.name == "verify_audit_checkpoint" and s.status == "failed" for s in report.steps)
    assert report.finished_at != ""
