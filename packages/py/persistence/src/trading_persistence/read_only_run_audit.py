"""Durable immutable storage for application-neutral read-only run records.

The full canonical record is stored in the existing content-addressed object
store and referenced by one row in the existing tamper-evident audit_events
chain. No table, migration, execution lifecycle state, or submission concept is
introduced. An identity is append-once: byte-identical retries are no-ops and
conflicting bytes fail closed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, cast

from sqlalchemy import Engine, select, text
from trading_contracts.enums import Environment

from trading_persistence.audit import append_audit_record
from trading_persistence.models import AuditEvent

ACTION_READ_ONLY_RUN = "MVP_READ_ONLY_RUN_EVALUATED"
REQUIRED_RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "record_version",
        "task_id",
        "run_id",
        "run_identity",
        "run_input_ref",
        "as_of",
        "environment",
        "strategy_id",
        "strategy_version",
        "strategy_fingerprint",
        "result",
        "terminal_stage",
        "reason_codes",
        "input_snapshot_ref",
        "decision_ref",
        "risk_ref",
        "allocation_ref",
        "evaluated_quantity",
        "read_only",
        "seal_ref",
        "input_evidence",
    }
)


class ReadOnlyRunAuditError(Exception):
    """Immutable read-only evidence could not be safely stored or loaded."""


class ObjectStore(Protocol):
    def put(self, data: bytes) -> object: ...

    def get(self, ref: str) -> bytes: ...


def _canonical_payload(record: Mapping[str, object]) -> bytes:
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("ascii")


def _decode(payload: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(payload)
    except (TypeError, ValueError) as error:
        raise ReadOnlyRunAuditError(
            "stored read-only record is not valid JSON"
        ) from error
    if not isinstance(value, dict):
        raise ReadOnlyRunAuditError("stored read-only record is not an object")
    return cast("Mapping[str, object]", value)


class DurableReadOnlyRunAuditStore:
    """Structural implementation of the application read-only audit port."""

    def __init__(
        self,
        engine: Engine,
        objects: ObjectStore,
        *,
        actor_ref: str = "mvp-read-only-runtime",
    ) -> None:
        self._engine = engine
        self._objects = objects
        self._actor_ref = actor_ref

    def put_if_absent(
        self, identity: str, record: Mapping[str, object]
    ) -> Mapping[str, object]:
        self._validate_record(identity, record)
        environment = self._environment(record)
        occurred_at = self._occurred_at(record)
        payload = _canonical_payload(record)
        try:
            stored = self._objects.put(payload)
            payload_ref = str(getattr(stored, "ref", "") or "")
        except Exception as error:  # noqa: BLE001 - all storage failures fail closed
            raise ReadOnlyRunAuditError(
                f"read-only payload could not be stored ({type(error).__name__})"
            ) from error
        if not payload_ref:
            raise ReadOnlyRunAuditError("object store returned no payload reference")

        try:
            with self._engine.begin() as connection:
                # The chain has one global tail. Serialize append/check so
                # concurrent retries cannot append twice or hide a conflict.
                connection.execute(text("LOCK TABLE audit_events IN EXCLUSIVE MODE"))
                rows = connection.execute(
                    select(AuditEvent.payload_ref)
                    .where(
                        AuditEvent.action == ACTION_READ_ONLY_RUN,
                        AuditEvent.target_ref == identity,
                    )
                    .order_by(AuditEvent.sequence_no)
                ).all()
                if len(rows) > 1:
                    raise ReadOnlyRunAuditError(
                        "immutable run identity has duplicate records"
                    )
                if rows:
                    existing_ref = rows[0].payload_ref
                    if not existing_ref:
                        raise ReadOnlyRunAuditError(
                            "existing run record has no payload reference"
                        )
                    existing = self._objects.get(existing_ref)
                    if existing != payload:
                        raise ReadOnlyRunAuditError(
                            "immutable run identity has conflicting payload or provenance"
                        )
                    return _decode(existing)
                append_audit_record(
                    connection,
                    actor_ref=self._actor_ref,
                    action=ACTION_READ_ONLY_RUN,
                    environment=environment,
                    occurred_at=occurred_at,
                    target_ref=identity,
                    detail=self._detail(record),
                    payload_ref=payload_ref,
                )
        except ReadOnlyRunAuditError:
            raise
        except Exception as error:  # noqa: BLE001 - persistence failures fail closed
            raise ReadOnlyRunAuditError(
                f"read-only record could not be committed ({type(error).__name__})"
            ) from error
        return _decode(payload)

    def load(self, identity: str) -> Mapping[str, object] | None:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    select(AuditEvent.payload_ref)
                    .where(
                        AuditEvent.action == ACTION_READ_ONLY_RUN,
                        AuditEvent.target_ref == identity,
                    )
                    .order_by(AuditEvent.sequence_no)
                ).all()
            if not rows:
                return None
            if len(rows) != 1 or not rows[0].payload_ref:
                raise ReadOnlyRunAuditError(
                    "immutable run identity is ambiguous or unreadable"
                )
            record = _decode(self._objects.get(rows[0].payload_ref))
            self._validate_record(identity, record)
            self._environment(record)
            self._occurred_at(record)
            return record
        except ReadOnlyRunAuditError:
            raise
        except Exception as error:  # noqa: BLE001 - unreadable evidence fails closed
            raise ReadOnlyRunAuditError(
                f"read-only record could not be loaded ({type(error).__name__})"
            ) from error

    @staticmethod
    def _validate_record(identity: str, record: Mapping[str, object]) -> None:
        if not REQUIRED_RECORD_FIELDS.issubset(record):
            raise ReadOnlyRunAuditError("read-only record is incomplete")
        if record.get("run_identity") != identity:
            raise ReadOnlyRunAuditError(
                "record identity does not match its immutable key"
            )
        if any(key in record for key in ("submit_authorization", "executable_plan")):
            raise ReadOnlyRunAuditError(
                "execution authority may not be persisted here"
            )

    @staticmethod
    def _environment(record: Mapping[str, object]) -> Environment:
        try:
            environment = Environment(str(record["environment"]))
        except (KeyError, ValueError) as error:
            raise ReadOnlyRunAuditError(
                "record environment is missing or invalid"
            ) from error
        if environment is Environment.LIVE:
            raise ReadOnlyRunAuditError(
                "read-only runtime records may not target LIVE"
            )
        return environment

    @staticmethod
    def _occurred_at(record: Mapping[str, object]) -> datetime:
        value = record.get("as_of")
        try:
            occurred_at = datetime.fromisoformat(str(value))
        except ValueError as error:
            raise ReadOnlyRunAuditError("record as_of is invalid") from error
        if occurred_at.tzinfo is None:
            raise ReadOnlyRunAuditError("record as_of must be timezone-aware")
        return occurred_at

    @staticmethod
    def _detail(record: Mapping[str, object]) -> str:
        detail = " ".join(
            (
                f"result={record.get('result', 'UNKNOWN')}",
                f"stage={record.get('terminal_stage', 'UNKNOWN')}",
                f"strategy={record.get('strategy_id', 'UNKNOWN')}",
            )
        )
        return detail[:400]


__all__ = [
    "ACTION_READ_ONLY_RUN",
    "DurableReadOnlyRunAuditStore",
    "ReadOnlyRunAuditError",
]
