"""Tamper-evident audit chain (IMP-0304).

Authority: Security_and_Operations_Specification_v0.6 (tamper-evident audit,
append-only, hash chain, encrypted offsite checkpoints; auth failures and
permission events are audited) and Data_Model v0.6 section 6.8 (audit_events,
Append-Only).

Chain rule: record N carries ``previous_hash`` = record N-1's
``current_hash`` (genesis: 64 zero digits) and ``current_hash`` = SHA-256
over the canonical serialization of its own fields (including sequence_no and
previous_hash). Verification recomputes every hash and the linkage, so:

- mutating any historical field breaks that record's hash;
- deleting a record breaks the sequence and the next record's linkage;
- reordering breaks the hashes (sequence_no is hashed);
- inserting a forged record breaks its neighbours' linkage.

Checkpoints (offsite copies of the tail position) let a restore prove the
chain was not truncated: the stored chain must still end at the checkpointed
sequence/hash or extend it consistently.

No secrets: only actor/action/target references, a redacted detail string and
an opaque payload_ref are ever stored. Append-only is enforced at the
application layer; any out-of-band mutation is what verification detects.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert, select
from trading_contracts.enums import Environment

from trading_persistence.models import AuditEvent

GENESIS_HASH: Final[str] = "sha256:" + "0" * 64
_SEPARATOR: Final[str] = "\x1f"

_FORBIDDEN_DETAIL_MARKERS: Final[tuple[str, ...]] = (
    "-----begin",
    "password=",
    "bearer ",
    "cookie:",
)


class AuditIntegrityError(Exception):
    """The audit chain failed verification."""


class AuditRecordLike(Protocol):
    """Structural, read-only view of one audit row (ORM row or plain object)."""

    @property
    def sequence_no(self) -> int: ...
    @property
    def actor_ref(self) -> str: ...
    @property
    def action(self) -> str: ...
    @property
    def target_ref(self) -> str | None: ...
    @property
    def environment(self) -> str: ...
    @property
    def detail(self) -> str | None: ...
    @property
    def payload_ref(self) -> str | None: ...
    @property
    def correlation_id(self) -> UUID | None: ...
    @property
    def occurred_at(self) -> datetime: ...
    @property
    def previous_hash(self) -> str: ...
    @property
    def current_hash(self) -> str: ...


@dataclass(frozen=True)
class AuditCheckpoint:
    """Offsite checkpoint: the chain tail at capture time."""

    last_sequence_no: int
    last_hash: str


def compute_audit_hash(
    *,
    sequence_no: int,
    actor_ref: str,
    action: str,
    target_ref: str | None,
    environment: str,
    detail: str | None,
    payload_ref: str | None,
    correlation_id: UUID | None,
    occurred_at: datetime,
    previous_hash: str,
) -> str:
    """Deterministic hash over the canonical field serialization."""
    material = _SEPARATOR.join(
        (
            str(sequence_no),
            actor_ref,
            action,
            target_ref or "",
            environment,
            detail or "",
            payload_ref or "",
            str(correlation_id) if correlation_id else "",
            occurred_at.isoformat(),
            previous_hash,
        )
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def verify_records(records: Sequence[AuditRecordLike]) -> list[str]:
    """Verify a full chain (ordered by sequence_no); empty list = intact."""
    problems: list[str] = []
    expected_previous = GENESIS_HASH
    expected_sequence = 1
    for record in records:
        if record.sequence_no != expected_sequence:
            problems.append(
                f"sequence gap: expected {expected_sequence}, found {record.sequence_no}"
            )
            expected_sequence = record.sequence_no
        if record.previous_hash != expected_previous:
            problems.append(f"broken linkage at sequence {record.sequence_no}")
        recomputed = compute_audit_hash(
            sequence_no=record.sequence_no,
            actor_ref=record.actor_ref,
            action=record.action,
            target_ref=record.target_ref,
            environment=record.environment,
            detail=record.detail,
            payload_ref=record.payload_ref,
            correlation_id=record.correlation_id,
            occurred_at=record.occurred_at,
            previous_hash=record.previous_hash,
        )
        if recomputed != record.current_hash:
            problems.append(f"hash mismatch at sequence {record.sequence_no}")
        expected_previous = record.current_hash
        expected_sequence += 1
    return problems


def _validate_no_secret(detail: str | None) -> str | None:
    if detail is None:
        return None
    lowered = detail.lower()
    for marker in _FORBIDDEN_DETAIL_MARKERS:
        if marker in lowered:
            raise AuditIntegrityError("audit detail may not contain secret material")
    return detail


def append_audit_record(
    connection: Connection,
    *,
    actor_ref: str,
    action: str,
    environment: Environment,
    occurred_at: datetime,
    target_ref: str | None = None,
    detail: str | None = None,
    payload_ref: str | None = None,
    correlation_id: UUID | None = None,
) -> str:
    """Append one chained record inside the caller's transaction."""
    if occurred_at.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    _validate_no_secret(detail)
    tail = connection.execute(
        select(AuditEvent.sequence_no, AuditEvent.current_hash)
        .order_by(AuditEvent.sequence_no.desc())
        .limit(1)
    ).one_or_none()
    sequence_no = 1 if tail is None else tail.sequence_no + 1
    previous_hash = GENESIS_HASH if tail is None else tail.current_hash
    current_hash = compute_audit_hash(
        sequence_no=sequence_no,
        actor_ref=actor_ref,
        action=action,
        target_ref=target_ref,
        environment=environment.value,
        detail=detail,
        payload_ref=payload_ref,
        correlation_id=correlation_id,
        occurred_at=occurred_at,
        previous_hash=previous_hash,
    )
    connection.execute(
        insert(AuditEvent).values(
            id=uuid4(),
            sequence_no=sequence_no,
            actor_ref=actor_ref,
            action=action,
            target_ref=target_ref,
            environment=environment.value,
            detail=detail,
            payload_ref=payload_ref,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
            previous_hash=previous_hash,
            current_hash=current_hash,
            created_at=occurred_at,
        )
    )
    return current_hash


def load_chain(connection: Connection) -> list[AuditRecordLike]:
    rows = connection.execute(
        select(AuditEvent).order_by(AuditEvent.sequence_no)
    ).scalars()
    return list(rows)


def verify_audit_chain(connection: Connection) -> list[str]:
    """Verify the persisted chain; empty list means intact."""
    return verify_records(load_chain(connection))


def export_checkpoint(connection: Connection) -> AuditCheckpoint:
    """Capture the chain tail for offsite storage."""
    tail = connection.execute(
        select(AuditEvent.sequence_no, AuditEvent.current_hash)
        .order_by(AuditEvent.sequence_no.desc())
        .limit(1)
    ).one_or_none()
    if tail is None:
        return AuditCheckpoint(last_sequence_no=0, last_hash=GENESIS_HASH)
    return AuditCheckpoint(last_sequence_no=tail.sequence_no, last_hash=tail.current_hash)


def verify_against_checkpoint(
    connection: Connection, checkpoint: AuditCheckpoint
) -> list[str]:
    """Verify the chain and that it still contains the checkpointed tail.

    Detects truncation/deletion after a restore: the record at the
    checkpointed sequence must carry exactly the checkpointed hash.
    """
    problems = verify_audit_chain(connection)
    if checkpoint.last_sequence_no == 0:
        return problems
    row = connection.execute(
        select(AuditEvent.current_hash).where(
            AuditEvent.sequence_no == checkpoint.last_sequence_no
        )
    ).scalar_one_or_none()
    if row is None:
        problems.append(
            f"checkpointed record {checkpoint.last_sequence_no} is missing (truncated chain)"
        )
    elif row != checkpoint.last_hash:
        problems.append(
            f"checkpointed record {checkpoint.last_sequence_no} does not match the checkpoint"
        )
    return problems


class PersistentAuditSink:
    """RBAC AuditSink (IMP-0301) backed by the tamper-evident chain.

    Structurally compatible with trading_application.auth.AuditSink without
    importing the application layer; RBAC semantics are unchanged — every
    authorization decision becomes one chained record.
    """

    def __init__(self, connection: Connection, environment: Environment) -> None:
        self._connection = connection
        self._environment = environment

    def record(self, decision: object) -> None:
        allowed = bool(getattr(decision, "allowed"))  # noqa: B009
        principal = str(getattr(decision, "principal_id"))  # noqa: B009
        action = getattr(decision, "action")  # noqa: B009
        reason = str(getattr(decision, "reason"))  # noqa: B009
        from datetime import UTC
        from datetime import datetime as _datetime

        append_audit_record(
            self._connection,
            actor_ref=principal,
            action=f"AUTHZ_{getattr(action, 'value', action)}",
            environment=self._environment,
            occurred_at=_datetime.now(UTC),
            detail=("ALLOWED: " if allowed else "DENIED: ") + reason,
        )
