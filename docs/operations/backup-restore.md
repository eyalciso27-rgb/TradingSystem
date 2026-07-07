# Backup and Restore (IMP-0403)

Authority: Security and Operations spec §13 (components, initial policy,
RPO/RTO, restore rules), Technology spec §7 (pg_dump), §8 (SHA-256
integrity, artifact metadata), §14 (SOPS/age encryption), §30.7 (audit
checkpoints outside the DB failure domain).

## What is backed up

| Component | Mechanism |
|---|---|
| PostgreSQL | `pg_dump --format=custom` (consistent snapshot; live files are never copied) |
| Object store | tar.gz of the content-addressed volume |
| Runtime sessions | tar.gz of the encrypted per-environment session directory (blobs are already Fernet-encrypted; raw tokens are never stored — IMP-0303) |
| Configuration references | tar.gz of `config/` (policies, strategies, prompts, active pointers — no secrets) |
| NATS JetStream | tar.gz of a **quiesced** copy of the file store (a running server's directory is not consistent) |
| Audit checkpoint | embedded in the manifest (chain tail: `last_sequence_no`, `last_hash`) — stored with the backup, outside the DB failure domain |

Secret **values** are not backed up here (separate secure mechanism per
spec §13); infrastructure (compose, migrations, runbooks) lives in git.

## Layout and manifest

```
<root>/<environment>/<backup_id>/
  postgresql.dump.age
  object_store.tar.gz.age
  ...
  manifest.json
  manifest.json.sha256
```

dev/test/paper never share a namespace; LIVE is rejected. The manifest
records: manifest_version, backup_id, environment, created_at,
alembic_revision (migration state), policy (frequencies, retention,
RPO/RTO targets — configuration input per §13.1/13.2, not code constants),
audit_checkpoint, and per artifact: component, object_key, sha256,
size_bytes, encryption.

## Security

Every artifact is encrypted with age (recipient public key at backup
time; identity file for restore comes from the IMP-0302 secret store —
only its path is handled). Credentials arrive via `DATABASE_URL` /
`PGPASSWORD` in the process environment, never argv, never manifests,
never logs. Verification recomputes all hashes and fails closed; there is
no silent fallback to an older backup and no backup is ever deleted by
the tool.

## Restore procedure

`python scripts/backup_tool.py restore --backup-dir ... --environment ... --confirm`

Fixed order: validate artifacts+manifest+hashes → validate environment
(backup env must equal target env) → explicit `--confirm` → empty target
database (non-empty requires `--controlled-nonempty`; silent overwrite is
forbidden) → decrypt → `pg_restore --exit-on-error` → verify restored
alembic revision matches the manifest → verify audit chain against the
checkpoint → verify object references (content hash must match the
object key) → JSON restore report (steps, duration, RPO/RTO targets).
`verify-restore` runs the validation steps only (dry run, no writes).

Per spec §13.3: restore into an isolated environment before replacing
production, broker reconciliation is mandatory afterwards, and Live
execution stays disabled until the recovery checklist is approved.

## What CI/server must still prove

Unit tests cover the mechanism with injected fakes. A live drill —
pg_dump/pg_restore against a real PostgreSQL 18, age binary, restore
duration vs the documented RPO/RTO targets (monthly in Paper per §13.1) —
must run in an environment with Docker/PostgreSQL and produce the report.
Offsite target selection remains open (TECH-OPEN-06).
