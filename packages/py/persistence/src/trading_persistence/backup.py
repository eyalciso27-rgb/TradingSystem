"""Deterministic, fail-closed backup and restore (IMP-0403).

Authority: Security and Operations spec section 13 (components, initial
policy, RPO/RTO targets, restore rules), Technology spec section 7
(pg_dump/base backup), section 8 (SHA-256 integrity on write/read/backup,
artifact metadata: hash, size, encryption, object key), section 14 (SOPS +
age as the canonical at-rest encryption mechanism) and section 30.7 (audit
checkpoints stored outside the database failure domain).

Design rules enforced here:

- Backups are per-environment namespaces (``<root>/<environment>/<id>``);
  dev, test and paper artifacts never share a namespace and LIVE is
  rejected outright.
- Every artifact is encrypted (age) and recorded in a manifest with its
  SHA-256 and size; the manifest itself is hashed. Verification recomputes
  everything and fails closed — a corrupt, missing or mismatched artifact
  aborts, and there is no silent fallback to an older backup.
- Restore only ever writes after every validation passed, only into an
  empty database (or under an explicit controlled-restore flag), and only
  into the environment the backup was taken from.
- Policy values (frequencies, retention, RPO/RTO targets, section
  13.1/13.2) are inputs, never constants in code. Credentials are never
  accepted as arguments, stored in manifests, or logged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from trading_persistence.audit import AuditCheckpoint

MANIFEST_VERSION = "1"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_HASH_FILENAME = "manifest.json.sha256"

# LIVE is deliberately absent: it stays disabled and must never share
# backup tooling state with the environments below.
ALLOWED_ENVIRONMENTS: tuple[str, ...] = ("dev", "test", "paper")


class BackupError(Exception):
    """Base error for backup/restore failures (always fail closed)."""


class BackupIntegrityError(BackupError):
    """An artifact or manifest failed hash/size/shape verification."""


class EnvironmentMismatchError(BackupError):
    """Backup environment does not match the restore target environment."""


class RestoreTargetError(BackupError):
    """The restore target is unsafe (non-empty database, no confirmation)."""


class RestoreFailedError(BackupError):
    """Restore aborted fail-closed; carries the step-by-step report."""

    def __init__(self, message: str, report: RestoreReport) -> None:
        super().__init__(message)
        self.report = report


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class BackupPolicy:
    """Section 13.1/13.2 policy values — provided as configuration input."""

    database_full_frequency: str
    retention_days: int
    offsite_frequency: str
    rpo_target_minutes: int
    rto_target_minutes: int


@dataclass(frozen=True, slots=True)
class ManifestArtifact:
    """Per-artifact metadata (Technology spec section 8)."""

    component: str
    object_key: str
    sha256: str
    size_bytes: int
    encryption: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    manifest_version: str
    backup_id: str
    environment: str
    created_at: str
    alembic_revision: str
    policy: BackupPolicy
    audit_checkpoint: AuditCheckpoint
    artifacts: tuple[ManifestArtifact, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @staticmethod
    def from_json(payload: str) -> BackupManifest:
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise BackupIntegrityError(f"manifest is not valid JSON: {exc}") from exc
        try:
            if raw["manifest_version"] != MANIFEST_VERSION:
                raise BackupIntegrityError(
                    f"unsupported manifest_version {raw['manifest_version']!r}"
                )
            return BackupManifest(
                manifest_version=raw["manifest_version"],
                backup_id=raw["backup_id"],
                environment=raw["environment"],
                created_at=raw["created_at"],
                alembic_revision=raw["alembic_revision"],
                policy=BackupPolicy(**raw["policy"]),
                audit_checkpoint=AuditCheckpoint(**raw["audit_checkpoint"]),
                artifacts=tuple(ManifestArtifact(**a) for a in raw["artifacts"]),
            )
        except (KeyError, TypeError) as exc:
            raise BackupIntegrityError(f"manifest has invalid shape: {exc}") from exc


class Encryptor(Protocol):
    """Port for the canonical at-rest encryption mechanism (age)."""

    @property
    def scheme(self) -> str: ...

    def encrypt(self, source: Path, destination: Path) -> None: ...

    def decrypt(self, source: Path, destination: Path) -> None: ...


CommandRunner = Callable[[Sequence[str], Mapping[str, str] | None], None]


def _default_runner(command: Sequence[str], env: Mapping[str, str] | None) -> None:
    subprocess.run(list(command), env=dict(env) if env is not None else None, check=True)


class AgeEncryptor:
    """SOPS/age-compatible artifact encryption via the ``age`` binary.

    The recipient is a public key (not a secret). The identity file used
    for decryption is a runtime-mounted secret path per IMP-0302 — only its
    path is handled here and it is never read, stored or logged.
    """

    def __init__(
        self,
        *,
        recipient: str,
        identity_file: Path | None = None,
        age_binary: str = "age",
        runner: CommandRunner = _default_runner,
    ) -> None:
        self._recipient = recipient
        self._identity_file = identity_file
        self._age = age_binary
        self._run = runner

    @property
    def scheme(self) -> str:
        return "age"

    def encrypt(self, source: Path, destination: Path) -> None:
        self._run(
            [self._age, "--encrypt", "-r", self._recipient, "-o", str(destination), str(source)],
            None,
        )

    def decrypt(self, source: Path, destination: Path) -> None:
        if self._identity_file is None:
            raise BackupError("decryption requires an age identity file (IMP-0302 secret path)")
        self._run(
            [
                self._age,
                "--decrypt",
                "-i",
                str(self._identity_file),
                "-o",
                str(destination),
                str(source),
            ],
            None,
        )


class BackupSource(Protocol):
    """Produces one plaintext artifact file inside ``workdir``."""

    @property
    def component(self) -> str: ...

    def produce(self, workdir: Path) -> Path: ...


class PostgresDumpSource:
    """Consistent PostgreSQL snapshot via ``pg_dump`` custom format.

    Never copies live database files. Connection settings arrive as
    non-secret parts; the password travels only through the subprocess
    environment (``PGPASSWORD``) supplied by the caller from the IMP-0302
    secret store — it is never placed on the command line or logged.
    """

    component = "postgresql"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        subprocess_env: Mapping[str, str],
        pg_dump_binary: str = "pg_dump",
        runner: CommandRunner = _default_runner,
    ) -> None:
        self._args = [
            pg_dump_binary,
            "--format=custom",
            "--no-password",
            f"--host={host}",
            f"--port={port}",
            f"--username={user}",
            f"--dbname={database}",
        ]
        self._env = dict(subprocess_env)
        self._run = runner

    def produce(self, workdir: Path) -> Path:
        target = workdir / "postgresql.dump"
        self._run([*self._args, f"--file={target}"], self._env)
        if not target.exists() or target.stat().st_size == 0:
            raise BackupError("pg_dump produced no output; failing closed")
        return target


class DirectoryArchiveSource:
    """Archive a directory tree (object store, sessions, config references,
    NATS JetStream file store) into a tar.gz artifact.

    For NATS the caller must point at a quiesced/consistent copy of the
    JetStream store; live directories of a running server are not a
    consistent snapshot.
    """

    def __init__(self, component: str, root: Path) -> None:
        self._component = component
        self._root = root

    @property
    def component(self) -> str:
        return self._component

    def produce(self, workdir: Path) -> Path:
        if not self._root.is_dir():
            raise BackupError(f"{self._component}: directory {self._root} does not exist")
        target = workdir / f"{self._component}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            archive.add(self._root, arcname=self._component)
        return target


def _require_environment(environment: str) -> None:
    if environment not in ALLOWED_ENVIRONMENTS:
        raise EnvironmentMismatchError(
            f"environment {environment!r} is not backupable/restorable "
            f"(allowed: {ALLOWED_ENVIRONMENTS}; LIVE stays disabled)"
        )


def create_backup(
    *,
    root: Path,
    environment: str,
    sources: Sequence[BackupSource],
    encryptor: Encryptor,
    alembic_revision: str,
    audit_checkpoint: AuditCheckpoint,
    policy: BackupPolicy,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Create one encrypted, hashed backup namespace and return its path.

    Never touches existing backups: the namespace must not already exist,
    and nothing is deleted.
    """
    _require_environment(environment)
    if not sources:
        raise BackupError("no backup sources given; refusing to write an empty backup")
    created = now()
    backup_id = f"{created.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    namespace = root / environment / backup_id
    if namespace.exists():
        raise BackupError(f"backup namespace {namespace} already exists; refusing overwrite")

    staging = root / environment / f".staging-{backup_id}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        artifacts: list[ManifestArtifact] = []
        for source in sources:
            plaintext = source.produce(staging)
            object_key = f"{plaintext.name}.{encryptor.scheme}"
            encrypted = staging / object_key
            encryptor.encrypt(plaintext, encrypted)
            if not encrypted.exists() or encrypted.stat().st_size == 0:
                raise BackupError(f"{source.component}: encryption produced no output")
            plaintext.unlink()
            artifacts.append(
                ManifestArtifact(
                    component=source.component,
                    object_key=object_key,
                    sha256=sha256_file(encrypted),
                    size_bytes=encrypted.stat().st_size,
                    encryption=encryptor.scheme,
                )
            )
        manifest = BackupManifest(
            manifest_version=MANIFEST_VERSION,
            backup_id=backup_id,
            environment=environment,
            created_at=created.isoformat(),
            alembic_revision=alembic_revision,
            policy=policy,
            audit_checkpoint=audit_checkpoint,
            artifacts=tuple(artifacts),
        )
        manifest_path = staging / MANIFEST_FILENAME
        manifest_path.write_text(manifest.to_json(), encoding="utf-8")
        (staging / MANIFEST_HASH_FILENAME).write_text(
            sha256_file(manifest_path), encoding="utf-8"
        )
        staging.rename(namespace)  # atomic publish: partial backups never look complete
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return namespace


def verify_backup(backup_dir: Path) -> BackupManifest:
    """Verify manifest hash, shape, and every artifact hash/size. Fail closed."""
    manifest_path = backup_dir / MANIFEST_FILENAME
    hash_path = backup_dir / MANIFEST_HASH_FILENAME
    if not manifest_path.is_file() or not hash_path.is_file():
        raise BackupIntegrityError(f"{backup_dir}: manifest or manifest hash missing")
    expected = hash_path.read_text(encoding="utf-8").strip()
    actual = sha256_file(manifest_path)
    if actual != expected:
        raise BackupIntegrityError(
            f"{backup_dir}: manifest hash mismatch (expected {expected}, got {actual})"
        )
    manifest = BackupManifest.from_json(manifest_path.read_text(encoding="utf-8"))
    _require_environment(manifest.environment)
    if not manifest.artifacts:
        raise BackupIntegrityError(f"{backup_dir}: manifest lists no artifacts")
    for artifact in manifest.artifacts:
        path = backup_dir / artifact.object_key
        if not path.is_file():
            raise BackupIntegrityError(f"{backup_dir}: artifact {artifact.object_key} missing")
        if path.stat().st_size != artifact.size_bytes:
            raise BackupIntegrityError(
                f"{backup_dir}: artifact {artifact.object_key} size mismatch"
            )
        digest = sha256_file(path)
        if digest != artifact.sha256:
            raise BackupIntegrityError(
                f"{backup_dir}: artifact {artifact.object_key} hash mismatch"
            )
    return manifest


def verify_object_store(root: Path) -> list[str]:
    """Content-addressed verification (Technology spec section 8).

    Every object's SHA-256 must appear in its object key (path). Returns a
    list of problems; empty means verified.
    """
    problems: list[str] = []
    if not root.is_dir():
        return [f"object store root {root} does not exist"]
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = sha256_file(path)
        key = path.relative_to(root).as_posix()
        if digest not in key:
            problems.append(f"object {key}: content hash {digest} not reflected in object key")
    return problems


@dataclass(frozen=True, slots=True)
class RestoreStep:
    name: str
    status: str  # "ok" | "failed" | "skipped"
    detail: str = ""


@dataclass(slots=True)
class RestoreReport:
    backup_id: str
    environment: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    duration_seconds: float = 0.0
    rpo_target_minutes: int = 0
    rto_target_minutes: int = 0
    steps: list[RestoreStep] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return all(step.status != "failed" for step in self.steps)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


class PostgresRestoreTarget:
    """Restore a pg_dump custom-format artifact via ``pg_restore``.

    Same credential rules as :class:`PostgresDumpSource`: password only via
    the subprocess environment, never argv, never logged.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        subprocess_env: Mapping[str, str],
        pg_restore_binary: str = "pg_restore",
        runner: CommandRunner = _default_runner,
    ) -> None:
        self._args = [
            pg_restore_binary,
            "--no-password",
            "--exit-on-error",
            f"--host={host}",
            f"--port={port}",
            f"--username={user}",
            f"--dbname={database}",
        ]
        self._env = dict(subprocess_env)
        self._run = runner

    def restore(self, dump_file: Path) -> None:
        self._run([*self._args, str(dump_file)], self._env)


def restore_backup(
    *,
    backup_dir: Path,
    target_environment: str,
    encryptor: Encryptor,
    confirm: bool = False,
    dry_run: bool = False,
    controlled_nonempty: bool = False,
    database_is_empty: Callable[[], bool],
    restore_database: Callable[[Path], None],
    current_alembic_revision: Callable[[], str],
    verify_audit: Callable[[AuditCheckpoint], list[str]],
    restore_directory: Callable[[str, Path], Path] | None = None,
    object_store_component: str = "object_store",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RestoreReport:
    """Validate, restore and verify one backup. Fail closed at every step.

    Order (fixed): artifact/manifest/hash validation -> environment
    validation -> (dry-run stops here) -> explicit confirmation -> empty
    target check -> decrypt -> database restore -> migration-state check ->
    audit-chain-vs-checkpoint verification -> object-reference
    verification -> report. ``restore_directory(component, decrypted_path)``
    places a directory artifact and returns the restored root (the object
    store root is then reference-verified).
    """
    started = now()
    report = RestoreReport(
        backup_id="",
        environment=target_environment,
        dry_run=dry_run,
        started_at=started.isoformat(),
    )

    def step(name: str, action: Callable[[], str]) -> None:
        try:
            detail = action()
        except Exception as exc:
            report.steps.append(RestoreStep(name=name, status="failed", detail=str(exc)))
            raise
        report.steps.append(RestoreStep(name=name, status="ok", detail=detail))

    def finish() -> None:
        finished = now()
        report.finished_at = finished.isoformat()
        report.duration_seconds = (finished - started).total_seconds()

    try:
        manifest_holder: list[BackupManifest] = []

        def _validate() -> str:
            manifest_holder.append(verify_backup(backup_dir))
            return f"{len(manifest_holder[0].artifacts)} artifacts verified"

        step("validate_artifacts_and_manifest", _validate)
        manifest = manifest_holder[0]
        report.backup_id = manifest.backup_id
        report.rpo_target_minutes = manifest.policy.rpo_target_minutes
        report.rto_target_minutes = manifest.policy.rto_target_minutes

        def _environment() -> str:
            _require_environment(target_environment)
            if manifest.environment != target_environment:
                raise EnvironmentMismatchError(
                    f"backup is for {manifest.environment!r}, "
                    f"target is {target_environment!r}"
                )
            return manifest.environment

        step("validate_environment", _environment)

        if dry_run:
            report.steps.append(
                RestoreStep(name="restore", status="skipped", detail="dry run")
            )
            finish()
            return report

        def _confirmation() -> str:
            if not confirm:
                raise RestoreTargetError(
                    "restore is destructive and requires explicit confirmation"
                )
            return "explicit confirmation supplied"

        step("require_confirmation", _confirmation)

        def _target() -> str:
            if database_is_empty():
                return "target database is empty"
            if controlled_nonempty:
                return "non-empty target explicitly allowed (controlled restore)"
            raise RestoreTargetError(
                "target database is not empty; silent overwrite is forbidden"
            )

        step("validate_restore_target", _target)

        staging = backup_dir / f".restore-{uuid.uuid4().hex[:8]}"
        staging.mkdir()
        try:
            decrypted: dict[str, Path] = {}

            def _decrypt() -> str:
                for artifact in manifest.artifacts:
                    source = backup_dir / artifact.object_key
                    plain = staging / artifact.object_key.removesuffix(
                        f".{artifact.encryption}"
                    )
                    encryptor.decrypt(source, plain)
                    if not plain.exists():
                        raise BackupIntegrityError(
                            f"decryption produced no output for {artifact.object_key}"
                        )
                    decrypted[artifact.component] = plain
                return f"{len(decrypted)} artifacts decrypted"

            step("decrypt_artifacts", _decrypt)

            def _database() -> str:
                dump = decrypted.get("postgresql")
                if dump is None:
                    raise BackupIntegrityError("backup contains no postgresql artifact")
                restore_database(dump)
                return "database restored"

            step("restore_database", _database)

            def _migration_state() -> str:
                revision = current_alembic_revision()
                if revision != manifest.alembic_revision:
                    raise BackupIntegrityError(
                        f"restored migration state {revision!r} does not match "
                        f"manifest revision {manifest.alembic_revision!r}"
                    )
                return f"alembic revision {revision}"

            step("verify_migration_state", _migration_state)

            def _audit() -> str:
                problems = verify_audit(manifest.audit_checkpoint)
                if problems:
                    raise BackupIntegrityError(
                        f"audit chain failed checkpoint verification: {problems}"
                    )
                return (
                    "audit chain verified against checkpoint "
                    f"seq={manifest.audit_checkpoint.last_sequence_no}"
                )

            step("verify_audit_checkpoint", _audit)

            def _objects() -> str:
                artifact = decrypted.get(object_store_component)
                if artifact is None:
                    return "no object store artifact in backup"
                if restore_directory is None:
                    raise BackupError(
                        "object store artifact present but no restore_directory given"
                    )
                restored_root = restore_directory(object_store_component, artifact)
                problems = verify_object_store(restored_root)
                if problems:
                    raise BackupIntegrityError(
                        f"object reference verification failed: {problems}"
                    )
                return "object references verified"

            step("verify_object_references", _objects)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        finish()
        return report
    except Exception as exc:
        finish()
        if isinstance(exc, RestoreFailedError):
            raise
        raise RestoreFailedError(f"restore failed closed: {exc}", report) from exc
