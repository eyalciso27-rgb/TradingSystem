"""Durable ``EmergencyCommandStore`` for the operator CLI (A4).

Bridges the application's emergency-control service to the persistence layer's
append-only ``emergency_commands`` log. It lives in the app (not persistence)
because it is the one place both layers meet: the persistence module deliberately
does not import the application's ``EmergencyCommandRecord`` / ``HaltRecord``.

Bound to ONE ``Connection`` so a halt written through
``EmergencyControlService`` -- the command row AND its tamper-evident audit
record -- commits atomically in the caller's transaction. The same halt is read
back by the runtime's ``DurableHaltSource`` through its own connection, so the
operator and the running loop share exactly one source of truth.
"""

from __future__ import annotations

from collections.abc import Sequence

from trading_application.emergency import EmergencyCommandRecord, HaltRecord
from trading_contracts.enums import Environment
from trading_persistence.emergency import load_active_halts, record_emergency_command


class DurableEmergencyCommandStore:
    """Append-only emergency store backed by ``emergency_commands`` (one conn).

    ``connection`` is an opaque SQLAlchemy ``Connection`` -- the app never names
    the driver type -- forwarded to the persistence functions that do name it.
    """

    def __init__(self, connection: object) -> None:
        self._connection = connection

    def append(self, record: EmergencyCommandRecord) -> None:
        record_emergency_command(
            self._connection,  # type: ignore[arg-type]
            command=record.command,
            environment=record.environment,
            actor_ref=record.actor_ref,
            auth_level=record.auth_level,
            reason_code=record.reason_code,
            reason_text=record.reason_text,
            result=record.result,
            issued_at=record.issued_at,
            keep_protection=record.keep_protection,
            halt_scope=record.halt_scope,
            target_ref=record.target_ref,
            acknowledged=record.acknowledged,
            acknowledged_at=record.acknowledged_at,
            acknowledged_by=record.acknowledged_by,
            correlation_id=record.correlation_id,
        )

    def active_halts(
        self, environment: Environment | None = None
    ) -> Sequence[HaltRecord]:
        return tuple(
            HaltRecord(
                scope=halt.scope,
                environment=halt.environment,
                target_ref=halt.target_ref,
                reason_code=halt.reason_code,
                keep_protection=halt.keep_protection,
            )
            for halt in load_active_halts(self._connection, environment)  # type: ignore[arg-type]
        )


__all__ = ["DurableEmergencyCommandStore"]
