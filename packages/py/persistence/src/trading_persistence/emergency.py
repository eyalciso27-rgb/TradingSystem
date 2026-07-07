"""Durable emergency-command persistence (IMP-0404).

Authority: Data_Model_and_Retention_Specification_v0.6 section 18
(emergency_commands is append-only and not normally deletable) and
Security_and_Operations_Specification_v0.6 section 12. This module only ever
appends; active-halt state is derived by replaying the log (latest lifecycle
command per scope + target + environment). No update or delete is exposed.

The pure halt-matching gate and the RBAC-guarded service live in
``trading_application.emergency``; this is the infrastructure half and does
not import the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.engine import Connection
from trading_contracts.enums import EmergencyCommandType, Environment, HaltScope

from trading_persistence.models import EmergencyCommand


@dataclass(frozen=True, slots=True)
class ActiveHalt:
    """An active halt reconstructed from the append-only command log."""

    scope: HaltScope
    environment: Environment
    target_ref: str | None
    reason_code: str
    keep_protection: bool


def record_emergency_command(
    connection: Connection,
    *,
    command: EmergencyCommandType,
    environment: Environment,
    actor_ref: str,
    auth_level: str,
    reason_code: str,
    reason_text: str,
    result: str,
    issued_at: datetime,
    keep_protection: bool = True,
    halt_scope: HaltScope | None = None,
    target_ref: str | None = None,
    acknowledged: bool = False,
    acknowledged_at: datetime | None = None,
    acknowledged_by: str | None = None,
    correlation_id: UUID | None = None,
) -> UUID:
    """Append one emergency command inside the caller's transaction."""
    if issued_at.tzinfo is None:
        raise ValueError("issued_at must be timezone-aware")
    command_id = uuid4()
    connection.execute(
        insert(EmergencyCommand).values(
            id=command_id,
            command=command.value,
            halt_scope=halt_scope.value if halt_scope is not None else None,
            target_ref=target_ref,
            environment=environment.value,
            actor_ref=actor_ref,
            auth_level=auth_level,
            reason_code=reason_code,
            reason_text=reason_text,
            keep_protection=keep_protection,
            acknowledged=acknowledged,
            acknowledged_at=acknowledged_at,
            acknowledged_by=acknowledged_by,
            result=result,
            correlation_id=correlation_id,
            issued_at=issued_at,
            created_at=issued_at,
        )
    )
    return command_id


def load_active_halts(
    connection: Connection, environment: Environment | None = None
) -> list[ActiveHalt]:
    """Derive currently-active halts by replaying the append-only log.

    For each (scope, target, environment) the latest lifecycle command wins;
    the halt is active only when that latest command is ACTIVATE_HALT.
    """
    rows = connection.execute(
        select(
            EmergencyCommand.halt_scope,
            EmergencyCommand.target_ref,
            EmergencyCommand.environment,
            EmergencyCommand.command,
            EmergencyCommand.reason_code,
            EmergencyCommand.keep_protection,
            EmergencyCommand.issued_at,
        )
        .where(EmergencyCommand.halt_scope.is_not(None))
        .where(
            EmergencyCommand.command.in_(
                (
                    EmergencyCommandType.ACTIVATE_HALT.value,
                    EmergencyCommandType.RESUME.value,
                )
            )
        )
        .order_by(EmergencyCommand.issued_at, EmergencyCommand.created_at)
    ).all()

    latest: dict[tuple[str, str | None, str], tuple[str, str, bool]] = {}
    for scope, target, env, command, reason_code, keep_protection, _issued in rows:
        latest[(scope, target, env)] = (command, reason_code, keep_protection)

    halts: list[ActiveHalt] = []
    for (scope, target, env), (command, reason_code, keep_protection) in latest.items():
        if command != EmergencyCommandType.ACTIVATE_HALT.value:
            continue
        scope_enum = HaltScope(scope)
        if (
            environment is not None
            and env != environment.value
            and scope_enum is not HaltScope.GLOBAL_ENTRY_HALT
        ):
            continue
        halts.append(
            ActiveHalt(
                scope=scope_enum,
                environment=Environment(env),
                target_ref=target,
                reason_code=reason_code,
                keep_protection=keep_protection,
            )
        )
    return halts
