"""Non-interactive backup/restore CLI (IMP-0403).

Subcommands: ``backup``, ``verify``, ``restore``, ``verify-restore``
(dry-run alias: validations only, no writes).

Credentials arrive exclusively through the environment / mounted secret
files per IMP-0302 (``DATABASE_URL``, ``AGE_IDENTITY_FILE``); they are never
command-line arguments and never printed. Restore is destructive and
requires ``--confirm``; a non-empty target additionally requires
``--controlled-nonempty``. Nothing ever deletes an existing backup.

Usage examples (non-interactive):

    python scripts/backup_tool.py backup --root /backups --environment paper \\
        --object-store /data/objects --sessions /data/sessions \\
        --config-dir config --age-recipient age1... \\
        --retention-days 30 --database-full-frequency daily \\
        --offsite-frequency daily --rpo-minutes 15 --rto-minutes 240
    python scripts/backup_tool.py verify --backup-dir /backups/paper/<id>
    python scripts/backup_tool.py verify-restore --backup-dir ... --environment paper
    python scripts/backup_tool.py restore --backup-dir ... --environment paper --confirm
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from trading_persistence.audit import (
    AuditCheckpoint,
    export_checkpoint,
    verify_against_checkpoint,
)
from trading_persistence.backup import (
    AgeEncryptor,
    BackupPolicy,
    BackupSource,
    DirectoryArchiveSource,
    PostgresDumpSource,
    PostgresRestoreTarget,
    RestoreFailedError,
    create_backup,
    restore_backup,
    verify_backup,
)


def _database_url() -> URL:
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise SystemExit("DATABASE_URL is required (per IMP-0302; never an argument)")
    return make_url(raw)


def _pg_subprocess_env(url: URL) -> dict[str, str]:
    """Password travels only via PGPASSWORD in the child environment."""
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return env


def _engine(url: URL) -> Engine:
    return create_engine(url)


def _encryptor(args: argparse.Namespace) -> AgeEncryptor:
    identity = os.environ.get("AGE_IDENTITY_FILE")
    return AgeEncryptor(
        recipient=getattr(args, "age_recipient", "") or "",
        identity_file=Path(identity) if identity else None,
    )


def cmd_backup(args: argparse.Namespace) -> int:
    url = _database_url()
    engine = _engine(url)
    with engine.connect() as connection:
        revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        checkpoint = export_checkpoint(connection)
    sources: list[BackupSource] = [
        PostgresDumpSource(
            host=url.host or "localhost",
            port=url.port or 5432,
            database=url.database or "",
            user=url.username or "",
            subprocess_env=_pg_subprocess_env(url),
        )
    ]
    for component, option in (
        ("object_store", args.object_store),
        ("sessions", args.sessions),
        ("configuration", args.config_dir),
        ("nats_jetstream", args.nats_store),
    ):
        if option:
            sources.append(DirectoryArchiveSource(component, Path(option)))
    namespace = create_backup(
        root=Path(args.root),
        environment=args.environment,
        sources=sources,
        encryptor=_encryptor(args),
        alembic_revision=str(revision),
        audit_checkpoint=checkpoint,
        policy=BackupPolicy(
            database_full_frequency=args.database_full_frequency,
            retention_days=args.retention_days,
            offsite_frequency=args.offsite_frequency,
            rpo_target_minutes=args.rpo_minutes,
            rto_target_minutes=args.rto_minutes,
        ),
    )
    print(json.dumps({"status": "ok", "backup_dir": str(namespace)}))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    manifest = verify_backup(Path(args.backup_dir))
    print(
        json.dumps(
            {
                "status": "ok",
                "backup_id": manifest.backup_id,
                "environment": manifest.environment,
                "artifacts": len(manifest.artifacts),
            }
        )
    )
    return 0


def _run_restore(args: argparse.Namespace, *, dry_run: bool) -> int:
    url = _database_url()
    engine = _engine(url)

    def database_is_empty() -> bool:
        with engine.connect() as connection:
            return not inspect(connection).get_table_names()

    def restore_database(dump: Path) -> None:
        PostgresRestoreTarget(
            host=url.host or "localhost",
            port=url.port or 5432,
            database=url.database or "",
            user=url.username or "",
            subprocess_env=_pg_subprocess_env(url),
        ).restore(dump)

    def current_alembic_revision() -> str:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
            )

    def verify_audit(checkpoint: AuditCheckpoint) -> list[str]:
        with engine.connect() as connection:
            return verify_against_checkpoint(connection, checkpoint)

    def restore_directory(component: str, artifact: Path) -> Path:
        target_root = Path(args.restore_data_root) / component
        if target_root.exists():
            raise SystemExit(f"{target_root} already exists; silent overwrite is forbidden")
        with tarfile.open(artifact, "r:gz") as archive:
            archive.extractall(Path(args.restore_data_root), filter="data")
        return target_root

    try:
        report = restore_backup(
            backup_dir=Path(args.backup_dir),
            target_environment=args.environment,
            encryptor=_encryptor(args),
            confirm=bool(getattr(args, "confirm", False)),
            dry_run=dry_run,
            controlled_nonempty=bool(getattr(args, "controlled_nonempty", False)),
            database_is_empty=database_is_empty,
            restore_database=restore_database,
            current_alembic_revision=current_alembic_revision,
            verify_audit=verify_audit,
            restore_directory=restore_directory if args.restore_data_root else None,
        )
    except RestoreFailedError as exc:
        print(exc.report.to_json())
        print(f"restore failed closed: {exc}", file=sys.stderr)
        return 1
    print(report.to_json())
    return 0


def main(argv: list[str] | None = None) -> int:
    if shutil.which("pg_dump") is None:
        print("note: pg_dump/pg_restore must be on PATH for database steps", file=sys.stderr)
    parser = argparse.ArgumentParser(prog="backup_tool", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="create an encrypted backup namespace")
    backup.add_argument("--root", required=True)
    backup.add_argument("--environment", required=True, choices=("dev", "test", "paper"))
    backup.add_argument("--age-recipient", required=True, help="age public key (not a secret)")
    backup.add_argument("--object-store", default="")
    backup.add_argument("--sessions", default="")
    backup.add_argument("--config-dir", default="")
    backup.add_argument("--nats-store", default="", help="quiesced JetStream store copy")
    backup.add_argument("--database-full-frequency", required=True)
    backup.add_argument("--retention-days", type=int, required=True)
    backup.add_argument("--offsite-frequency", required=True)
    backup.add_argument("--rpo-minutes", type=int, required=True)
    backup.add_argument("--rto-minutes", type=int, required=True)
    backup.set_defaults(func=cmd_backup)

    verify = sub.add_parser("verify", help="verify manifest and artifact integrity")
    verify.add_argument("--backup-dir", required=True)
    verify.set_defaults(func=cmd_verify)

    for name, dry in (("restore", False), ("verify-restore", True)):
        cmd = sub.add_parser(name)
        cmd.add_argument("--backup-dir", required=True)
        cmd.add_argument("--environment", required=True, choices=("dev", "test", "paper"))
        cmd.add_argument("--restore-data-root", default="")
        if not dry:
            cmd.add_argument("--confirm", action="store_true")
            cmd.add_argument("--controlled-nonempty", action="store_true")
        cmd.set_defaults(func=lambda a, _dry=dry: _run_restore(a, dry_run=_dry))

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
