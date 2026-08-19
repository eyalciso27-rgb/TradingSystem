"""Durable pre/post-submit audit for one execution decision.

The readiness design has always had an ``AuditSink`` seam and no production
implementation behind it, which made "the decision was recorded" a claim about
a test double. This binds it to the audit infrastructure the repository already
has, rather than growing a parallel JSON-file subsystem beside it:

* the **tamper-evident chain** (``audit_events``) gets one appended, hash-linked
  record per persisted decision, carrying the decision identity and a redacted
  one-line summary. Append-only, and any later edit or deletion is detectable;
* the **content-addressed object store** (``raw_objects`` semantics) holds the
  full decision payload — every gate outcome, the quote, the risk result, the
  plan — keyed by its own sha256. The chain row references it, so the heavy
  payload never bloats the chain and the reference cannot silently point at
  different bytes than were audited.

The split matters for one property in particular: the payload is written
*first*, and only then is the chain row appended inside the caller's
transaction. A crash between them leaves an unreferenced object, which is
harmless. The reverse order would leave a chain row pointing at nothing, which
would look like tampering.

``can_persist`` is a real probe, not a constant. Readiness must be able to
refuse *before* a decision is made because audit is down, and a sink that
answers "yes" and then throws has told the gate nothing.

No secrets: the chain's own validation rejects credential-shaped detail, and
only references, identifiers and market observations are ever written here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Connection, select
from trading_contracts.enums import Environment

from trading_persistence.audit import append_audit_record
from trading_persistence.models import AuditEvent

EXECUTION_AUDIT_VERSION = "execution-audit-1.0.0"

#: Chain actions. Distinct verbs so a query can separate "we decided" from
#: "the broker answered" without parsing payloads.
ACTION_PRE_SUBMIT = "EXECUTION_PRE_SUBMIT"
ACTION_POST_SUBMIT = "EXECUTION_POST_SUBMIT"

#: The chain's ``detail`` column is a short operator-readable summary; the full
#: record lives in the object store. Bounded so one decision cannot dominate the
#: chain, and truncation is visible rather than silent.
MAX_DETAIL_CHARS = 400


class ObjectStore(Protocol):
    """Content-addressed payload storage (``FilesystemObjectStorage`` and co.)."""

    def put(self, data: bytes) -> object: ...

    def get(self, ref: str) -> bytes: ...

    def exists(self, ref: str) -> bool: ...


class ExecutionAuditError(Exception):
    """The decision could not be durably recorded. Never a reason to submit."""


def canonical_payload(record: Mapping[str, object]) -> bytes:
    """Deterministic bytes for one decision record.

    Sorted keys and tight separators so the same decision always hashes to the
    same reference — the property that makes the object store idempotent and a
    retry cheap instead of duplicative.
    """
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")


def summarize(record: Mapping[str, object]) -> str:
    """A one-line, bounded, secret-free summary for the chain row."""
    parts = [
        f"readiness={record.get('trading_readiness', 'UNKNOWN')}",
        f"mode={record.get('mode', 'UNKNOWN')}",
        f"symbol={record.get('symbol', 'UNKNOWN')}",
        f"checkpoint={record.get('checkpoint', 'UNKNOWN')}",
    ]
    blockers = record.get("blockers")
    if isinstance(blockers, list | tuple) and blockers:
        parts.append(f"blockers={list(blockers)}")
    outcome = record.get("submission_outcome")
    if outcome:
        parts.append(f"outcome={outcome}")
    detail = " ".join(" ".join(str(part).split()) for part in parts)
    if len(detail) > MAX_DETAIL_CHARS:
        detail = detail[: MAX_DETAIL_CHARS - 3] + "..."
    return detail


class DurableExecutionAuditSink:
    """``AuditSink`` backed by the tamper-evident chain and the object store.

    One instance serves one decision path. ``persist`` returns the chain hash,
    which is the audit reference the execution path carries forward: it
    identifies the record, and it changes if anything in the record changes.
    """

    def __init__(
        self,
        connection: Connection,
        objects: ObjectStore,
        *,
        environment: Environment,
        actor_ref: str = "execution-service",
    ) -> None:
        self._connection = connection
        self._objects = objects
        self._environment = environment
        self._actor_ref = actor_ref

    # -- the readiness AuditSink protocol -------------------------------------

    def can_persist(self) -> bool:
        """Probe the real dependencies. A guess here is worth nothing.

        Both halves are checked because either can be down independently, and
        discovering that at submit time is exactly what this must prevent.
        """
        try:
            self._connection.execute(
                select(AuditEvent.sequence_no).order_by(AuditEvent.sequence_no.desc()).limit(1)
            ).one_or_none()
            probe = canonical_payload({"probe": EXECUTION_AUDIT_VERSION})
            stored = self._objects.put(probe)
        except Exception:  # noqa: BLE001 - any failure means "cannot persist"
            return False
        return bool(getattr(stored, "ref", None))

    def persist(self, record: Mapping[str, object]) -> str:
        """Write the payload, then chain it. Returns the chain hash."""
        return self._append(record, action=ACTION_PRE_SUBMIT)

    # -- post-submit ----------------------------------------------------------

    def persist_outcome(self, record: Mapping[str, object]) -> str:
        """Record what the broker answered. NEVER replaces the pre-submit row.

        Appended as its own chained record: the pre-submit record stated what we
        decided before acting, and rewriting it afterwards would destroy the one
        thing it exists to prove.
        """
        return self._append(record, action=ACTION_POST_SUBMIT)

    # -- internals ------------------------------------------------------------

    def _append(self, record: Mapping[str, object], *, action: str) -> str:
        payload = canonical_payload(record)
        try:
            stored = self._objects.put(payload)
            payload_ref = str(getattr(stored, "ref", "") or "")
        except Exception as error:  # noqa: BLE001
            raise ExecutionAuditError(
                f"decision payload could not be stored ({type(error).__name__})"
            ) from error
        if not payload_ref:
            raise ExecutionAuditError("object store returned no reference")

        occurred_raw = record.get("evaluated_at")
        occurred_at = _aware(occurred_raw) or datetime.now(tz=UTC)
        try:
            return append_audit_record(
                self._connection,
                actor_ref=self._actor_ref,
                action=action,
                environment=self._environment,
                occurred_at=occurred_at,
                target_ref=_target_ref(record),
                detail=summarize(record),
                payload_ref=payload_ref,
                correlation_id=_uuid_or_none(record.get("correlation_id")),
            )
        except Exception as error:  # noqa: BLE001
            raise ExecutionAuditError(
                f"decision could not be chained ({type(error).__name__})"
            ) from error


def _target_ref(record: Mapping[str, object]) -> str | None:
    """The decision this record belongs to, so failures stay queryable."""
    for key in ("decision_id", "client_order_id", "security_id"):
        value = record.get(key)
        if value:
            return str(value)
    return None


def _aware(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def _uuid_or_none(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def load_decision_records(
    connection: Connection, decision_id: str
) -> list[tuple[str, str, str | None]]:
    """Every chained record for one decision: (action, chain hash, payload ref).

    Queryable whether or not the decision ever reached the broker — a refusal is
    a decision, and the whole point of recording one is being able to find it.
    """
    rows = connection.execute(
        select(AuditEvent.action, AuditEvent.current_hash, AuditEvent.payload_ref)
        .where(AuditEvent.target_ref == decision_id)
        .order_by(AuditEvent.sequence_no)
    ).all()
    return [(row.action, row.current_hash, row.payload_ref) for row in rows]


__all__ = [
    "ACTION_POST_SUBMIT",
    "ACTION_PRE_SUBMIT",
    "EXECUTION_AUDIT_VERSION",
    "MAX_DETAIL_CHARS",
    "DurableExecutionAuditSink",
    "ExecutionAuditError",
    "ObjectStore",
    "canonical_payload",
    "load_decision_records",
    "summarize",
]
