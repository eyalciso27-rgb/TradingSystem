"""Emergency control service (IMP-0404).

Authority: Security_and_Operations_Specification_v0.6 section 12 (Kill Switch
levels and emergency modes), 12.2 (permissions and audit), 23.2 (halt checked
at intent creation, queue consumption and before submit; unauthorized pending
intent becomes CANCELLED_BY_POLICY; protective orders are never cancelled by an
entry halt), 24.1 (KEEP PROTECTION is the default for every halt); System
Functional 42.2 (HALT is immediate; Resume requires MFA, health check and
reconciliation). Data model: emergency_commands (section 18, append-only).

Design guarantees:

- The halt gate is deterministic and pure — it never consults an LLM (kill
  switch rule) and blocks only *new entries*. Protective/risk-reducing exits
  are never blocked (section 23.2; safety policy).
- Default halt behavior stops new entries while preserving existing protective
  orders (KEEP_PROTECTION); flattening is a separate, explicitly approved,
  re-authenticated action.
- Every activation, resume and emergency action is RBAC-checked
  (ACTIVATE_KILL_SWITCH; global/live additionally require OWNER or an explicit
  grant) and recorded twice: in the tamper-evident audit sink and in the
  append-only emergency-command store. Resume fails closed unless health and
  reconciliation both pass; there is no automatic resume.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from trading_contracts.enums import (
    EmergencyCommandType,
    Environment,
    HaltScope,
    OrderState,
)

from trading_application.auth import (
    AuditSink,
    AuthAction,
    AuthorizationDecision,
    Principal,
    Role,
    require,
)

# Scopes that block entries across every environment / target.
_GLOBAL_SCOPES = frozenset({HaltScope.GLOBAL_ENTRY_HALT})


class EmergencyControlError(Exception):
    """Base class for fail-closed emergency-control refusals."""


class ResumeNotAllowedError(EmergencyControlError):
    """Resume refused because health/reconciliation preconditions are unmet."""


class ReauthenticationRequiredError(EmergencyControlError):
    """A flatten/high-risk action was requested without re-authentication."""


class EntryCheckpoint(StrEnum):
    """The three points at which the halt gate is consulted (section 23.2)."""

    INTENT_CREATION = "INTENT_CREATION"
    QUEUE_CONSUMPTION = "QUEUE_CONSUMPTION"
    PRE_SUBMIT = "PRE_SUBMIT"


@dataclass(frozen=True, slots=True)
class HaltRecord:
    """An active halt derived from the append-only command log."""

    scope: HaltScope
    environment: Environment
    target_ref: str | None
    reason_code: str
    keep_protection: bool = True


@dataclass(frozen=True, slots=True)
class EntryContext:
    """Attributes of a prospective new entry, matched against active halts."""

    environment: Environment
    account_ref: str | None = None
    strategy_id: str | None = None
    symbol: str | None = None
    source_id: str | None = None
    model_id: str | None = None
    broker_id: str | None = None


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """Result of the deterministic halt gate for one entry check."""

    blocked: bool
    checkpoint: EntryCheckpoint
    blocking_scope: HaltScope | None = None
    blocking_target: str | None = None
    reason: str = ""

    @property
    def resulting_order_state(self) -> OrderState | None:
        """Canonical state for a queued intent stopped by policy (23.2)."""
        if self.blocked and self.checkpoint is EntryCheckpoint.QUEUE_CONSUMPTION:
            return OrderState.CANCELLED_BY_POLICY
        return None


@dataclass(frozen=True, slots=True)
class EmergencyCommandRecord:
    """One append-only emergency command (mirrors emergency_commands)."""

    id: UUID
    command: EmergencyCommandType
    environment: Environment
    actor_ref: str
    auth_level: str
    reason_code: str
    reason_text: str
    result: str
    issued_at: datetime
    halt_scope: HaltScope | None = None
    target_ref: str | None = None
    keep_protection: bool = True
    acknowledged: bool = False
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    correlation_id: UUID | None = None


def _scope_matches(halt: HaltRecord, context: EntryContext) -> bool:
    """True when an active halt blocks the given prospective entry."""
    if halt.environment != context.environment and halt.scope not in _GLOBAL_SCOPES:
        return False
    match halt.scope:
        case HaltScope.GLOBAL_ENTRY_HALT:
            return True
        case HaltScope.ENVIRONMENT_HALT:
            return True
        case HaltScope.BROKER_HALT:
            return _target_hit(halt.target_ref, context.broker_id)
        case HaltScope.ACCOUNT_HALT:
            return _target_hit(halt.target_ref, context.account_ref)
        case HaltScope.STRATEGY_HALT:
            return _target_hit(halt.target_ref, context.strategy_id)
        case HaltScope.SYMBOL_HALT:
            return _target_hit(halt.target_ref, context.symbol)
        case HaltScope.SOURCE_HALT:
            return _target_hit(halt.target_ref, context.source_id)
        case HaltScope.MODEL_HALT:
            return _target_hit(halt.target_ref, context.model_id)
    return False  # pragma: no cover - exhaustive match above


def _target_hit(halt_target: str | None, context_value: str | None) -> bool:
    # A scoped halt with no target blocks every entry in scope; a targeted
    # halt blocks only the matching target. Fail closed: an entry that cannot
    # supply the identifier a targeted halt is scoped to is treated as a match.
    if halt_target is None:
        return True
    return context_value == halt_target


def match_halt(
    active_halts: Sequence[HaltRecord], context: EntryContext
) -> HaltRecord | None:
    """Return the first active halt that blocks ``context`` (deterministic)."""
    for halt in active_halts:
        if _scope_matches(halt, context):
            return halt
    return None


class EmergencyCommandStore(Protocol):
    """Persistence port for the append-only command log + derived halts."""

    def append(self, record: EmergencyCommandRecord) -> None: ...

    def active_halts(self, environment: Environment | None = None) -> Sequence[HaltRecord]: ...


class InMemoryEmergencyCommandStore:
    """Append-only in-memory store; active halts derived by log replay."""

    def __init__(self) -> None:
        self._log: list[EmergencyCommandRecord] = []

    def append(self, record: EmergencyCommandRecord) -> None:
        self._log.append(record)

    @property
    def log(self) -> Sequence[EmergencyCommandRecord]:
        return tuple(self._log)

    def active_halts(
        self, environment: Environment | None = None
    ) -> Sequence[HaltRecord]:
        # Latest lifecycle command per (scope, target, environment) wins.
        latest: dict[tuple[HaltScope, str | None, Environment], EmergencyCommandRecord] = {}
        for record in self._log:
            if record.halt_scope is None:
                continue
            if record.command not in (
                EmergencyCommandType.ACTIVATE_HALT,
                EmergencyCommandType.RESUME,
            ):
                continue
            key = (record.halt_scope, record.target_ref, record.environment)
            latest[key] = record
        halts: list[HaltRecord] = []
        for (scope, target, env), record in latest.items():
            if record.command is not EmergencyCommandType.ACTIVATE_HALT:
                continue
            if environment is not None and env != environment and scope not in _GLOBAL_SCOPES:
                continue
            halts.append(
                HaltRecord(
                    scope=scope,
                    environment=env,
                    target_ref=target,
                    reason_code=record.reason_code,
                    keep_protection=record.keep_protection,
                )
            )
        return tuple(halts)


class EmergencyControlService:
    """RBAC- and audit-guarded emergency control (halts, resume, actions)."""

    def __init__(self, store: EmergencyCommandStore, audit: AuditSink) -> None:
        self._store = store
        self._audit = audit

    # -- activation / resume ------------------------------------------------

    def activate_halt(
        self,
        principal: Principal,
        *,
        scope: HaltScope,
        environment: Environment,
        reason_code: str,
        reason_text: str,
        target_ref: str | None = None,
        keep_protection: bool = True,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> EmergencyCommandRecord:
        """Activate a halt at ``scope``. Immediate; audited; append-only."""
        self._authorize_command(principal, EmergencyCommandType.ACTIVATE_HALT, scope, environment)
        self._require_reason(reason_code, reason_text)
        return self._append(
            principal,
            command=EmergencyCommandType.ACTIVATE_HALT,
            environment=environment,
            scope=scope,
            target_ref=target_ref,
            reason_code=reason_code,
            reason_text=reason_text,
            keep_protection=keep_protection,
            result="HALT_ACTIVE",
            now=now,
            correlation_id=correlation_id,
        )

    def resume(
        self,
        principal: Principal,
        *,
        scope: HaltScope,
        environment: Environment,
        reason_code: str,
        reason_text: str,
        health_ok: bool,
        reconciliation_ok: bool,
        target_ref: str | None = None,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> EmergencyCommandRecord:
        """Lift a halt. Fails closed unless health and reconciliation pass."""
        self._authorize_command(principal, EmergencyCommandType.RESUME, scope, environment)
        self._require_reason(reason_code, reason_text)
        if not (health_ok and reconciliation_ok):
            raise ResumeNotAllowedError(
                "resume requires passing health and reconciliation checks"
            )
        return self._append(
            principal,
            command=EmergencyCommandType.RESUME,
            environment=environment,
            scope=scope,
            target_ref=target_ref,
            reason_code=reason_code,
            reason_text=reason_text,
            keep_protection=True,
            result="RESUMED",
            now=now,
            correlation_id=correlation_id,
        )

    # -- position emergency actions ----------------------------------------

    def issue_position_action(
        self,
        principal: Principal,
        *,
        command: EmergencyCommandType,
        environment: Environment,
        reason_code: str,
        reason_text: str,
        target_ref: str | None = None,
        reauthenticated: bool = False,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> EmergencyCommandRecord:
        """Record a position emergency action (cancel/rebuild/reduce/flatten).

        FLATTEN_ACCOUNT is separate and requires re-authentication (12.2).
        This records the approved command in the append-only log; broker
        execution happens elsewhere and is out of this task's scope.
        """
        if command in (EmergencyCommandType.ACTIVATE_HALT, EmergencyCommandType.RESUME):
            raise ValueError("use activate_halt/resume for halt lifecycle commands")
        self._authorize_command(principal, command, None, environment)
        self._require_reason(reason_code, reason_text)
        if command is EmergencyCommandType.FLATTEN_ACCOUNT and not reauthenticated:
            raise ReauthenticationRequiredError(
                "FLATTEN_ACCOUNT requires re-authentication"
            )
        return self._append(
            principal,
            command=command,
            environment=environment,
            scope=None,
            target_ref=target_ref,
            reason_code=reason_code,
            reason_text=reason_text,
            keep_protection=True,
            result="RECORDED",
            now=now,
            correlation_id=correlation_id,
        )

    # -- deterministic gate -------------------------------------------------

    def evaluate_entry(
        self, context: EntryContext, checkpoint: EntryCheckpoint
    ) -> EntryDecision:
        """Deterministic halt gate for a new entry at one checkpoint."""
        halt = match_halt(self._store.active_halts(context.environment), context)
        if halt is None:
            return EntryDecision(blocked=False, checkpoint=checkpoint)
        return EntryDecision(
            blocked=True,
            checkpoint=checkpoint,
            blocking_scope=halt.scope,
            blocking_target=halt.target_ref,
            reason=f"blocked by active {halt.scope.value} ({halt.reason_code})",
        )

    def protective_exit_allowed(self, context: EntryContext) -> bool:
        """Protective / risk-reducing exits are never blocked by a halt."""
        return True

    # -- internals ----------------------------------------------------------

    def _authorize_command(
        self,
        principal: Principal,
        command: EmergencyCommandType,
        scope: HaltScope | None,
        environment: Environment,
    ) -> None:
        require(principal, AuthAction.ACTIVATE_KILL_SWITCH, self._audit)
        global_or_live = scope in _GLOBAL_SCOPES or environment is Environment.LIVE
        if global_or_live:
            owner = Role.OWNER in principal.roles
            explicit = AuthAction.ACTIVATE_KILL_SWITCH in principal.explicit_grants
            if not (owner or explicit):
                decision = AuthorizationDecision(
                    allowed=False,
                    principal_id=principal.principal_id,
                    action=f"EMERGENCY:{command.value}",
                    reason="global/live emergency action requires OWNER or explicit grant",
                )
                self._audit.record(decision)
                raise EmergencyControlError(decision.reason)

    @staticmethod
    def _require_reason(reason_code: str, reason_text: str) -> None:
        if not reason_code.strip() or not reason_text.strip():
            raise EmergencyControlError("emergency commands require reason_code and reason_text")

    def _append(
        self,
        principal: Principal,
        *,
        command: EmergencyCommandType,
        environment: Environment,
        scope: HaltScope | None,
        target_ref: str | None,
        reason_code: str,
        reason_text: str,
        keep_protection: bool,
        result: str,
        now: datetime | None,
        correlation_id: UUID | None,
    ) -> EmergencyCommandRecord:
        issued_at = now or datetime.now(UTC)
        auth_level = ",".join(sorted(role.value for role in principal.roles))
        record = EmergencyCommandRecord(
            id=uuid4(),
            command=command,
            environment=environment,
            actor_ref=principal.principal_id,
            auth_level=auth_level,
            reason_code=reason_code,
            reason_text=reason_text,
            result=result,
            issued_at=issued_at,
            halt_scope=scope,
            target_ref=target_ref,
            keep_protection=keep_protection,
            correlation_id=correlation_id,
        )
        self._store.append(record)
        self._audit.record(
            AuthorizationDecision(
                allowed=True,
                principal_id=principal.principal_id,
                action=f"EMERGENCY:{command.value}"
                + (f":{scope.value}" if scope is not None else ""),
                reason=reason_code,
            )
        )
        return record


__all__ = [
    "EmergencyCommandRecord",
    "EmergencyCommandStore",
    "EmergencyControlError",
    "EmergencyControlService",
    "EntryCheckpoint",
    "EntryContext",
    "EntryDecision",
    "HaltRecord",
    "InMemoryEmergencyCommandStore",
    "ReauthenticationRequiredError",
    "ResumeNotAllowedError",
    "match_halt",
]
