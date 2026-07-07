"""Notification service (IMP-0405).

Authority: System_Functional_Specification_v0.7 section 20 and
Integration_Specification_v0.6 section 20 (severity levels); Technology spec
section 32.3 (critical alerts require dedup, correlation_id and
acknowledgement; a user reply is never parsed as a trading command);
Security_and_Operations_Specification_v0.6 section 32.2 (no full sensitive
information by default) and section 33.7 (a notification channel cannot
Resume, Flatten or change Policy); Operations_Console_and_UX_Specification_v0.4
section 14 (severity UX and acknowledgement). Data model: notification_events
(section 18).

Guarantees:

- Outbound only. There is no method that turns an inbound message into a
  trading command; ``handle_inbound_reply`` deliberately ignores replies.
- Critical notifications fail closed without a correlation_id.
- Delivery is idempotent per (channel, dedup_key); a duplicate is never sent
  twice.
- Every notification body/title is passed through the central redaction used
  by structured logging, so secrets never reach a channel.
- Acknowledgement is authenticated and audited; unacknowledged critical
  alerts can be escalated after a caller-supplied deadline (policy input,
  never hardcoded).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID, uuid4

from trading_contracts.enums import (
    Environment,
    NotificationChannel,
    NotificationSeverity,
)
from trading_observability import redact

from trading_application.auth import AuditSink, AuthorizationDecision, Principal

# A notification channel is never a command channel (Security spec 33.7):
# these verbs are refused if anything ever tries to route them through it.
FORBIDDEN_COMMANDS: Final[frozenset[str]] = frozenset({"BUY", "SELL", "HALT", "FLATTEN"})


class NotificationError(Exception):
    """Base class for fail-closed notification refusals."""


class MissingCorrelationError(NotificationError):
    """A critical notification was raised without a correlation_id."""


class RemoteCommandRejectedError(NotificationError):
    """An inbound reply was (incorrectly) routed as a trading command."""


def _redact_text(value: str) -> str:
    redacted = redact(value)
    return redacted if isinstance(redacted, str) else str(redacted)


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    """One outbound notification (mirrors notification_events)."""

    id: UUID
    severity: NotificationSeverity
    channel: NotificationChannel
    dedup_key: str
    environment: Environment
    title: str
    body: str
    created_at: datetime
    correlation_id: UUID | None = None
    delivery_status: str = "PENDING"
    acknowledged: bool = False
    acknowledged_at: datetime | None = None
    acknowledged_by: str | None = None
    escalated: bool = False
    escalated_at: datetime | None = None


class NotificationProvider(Protocol):
    """Generic outbound channel port (Integration spec: no single-channel
    dependency). Returns True when the notification was delivered."""

    @property
    def channel(self) -> NotificationChannel: ...

    def send(self, record: NotificationRecord) -> bool: ...


class CollectingProvider:
    """Test/bootstrap provider that records what it was asked to deliver."""

    def __init__(self, channel: NotificationChannel, *, healthy: bool = True) -> None:
        self._channel = channel
        self._healthy = healthy
        self.sent: list[NotificationRecord] = []

    @property
    def channel(self) -> NotificationChannel:
        return self._channel

    def send(self, record: NotificationRecord) -> bool:
        self.sent.append(record)
        return self._healthy


class NotificationStore(Protocol):
    """Persistence port: idempotent record + lifecycle updates."""

    def record(self, record: NotificationRecord) -> tuple[NotificationRecord, bool]: ...

    def update(self, record: NotificationRecord) -> None: ...

    def get(self, event_id: UUID) -> NotificationRecord | None: ...

    def all_records(self) -> Sequence[NotificationRecord]: ...


class InMemoryNotificationStore:
    """Append-with-dedup in-memory store keyed by dedup_key."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, NotificationRecord] = {}
        self._by_key: dict[str, UUID] = {}

    def record(self, record: NotificationRecord) -> tuple[NotificationRecord, bool]:
        existing_id = self._by_key.get(record.dedup_key)
        if existing_id is not None:
            return self._by_id[existing_id], False
        self._by_id[record.id] = record
        self._by_key[record.dedup_key] = record.id
        return record, True

    def update(self, record: NotificationRecord) -> None:
        self._by_id[record.id] = record

    def get(self, event_id: UUID) -> NotificationRecord | None:
        return self._by_id.get(event_id)

    def all_records(self) -> Sequence[NotificationRecord]:
        return tuple(self._by_id.values())


class NotificationService:
    """Dedup + delivery + acknowledgement + escalation of alerts."""

    def __init__(
        self,
        store: NotificationStore,
        providers: Mapping[NotificationChannel, NotificationProvider],
        audit: AuditSink,
    ) -> None:
        self._store = store
        self._providers = dict(providers)
        self._audit = audit

    def notify(
        self,
        *,
        severity: NotificationSeverity,
        channels: Sequence[NotificationChannel],
        title: str,
        body: str,
        dedup_key: str,
        environment: Environment,
        correlation_id: UUID | None = None,
        now: datetime | None = None,
    ) -> list[NotificationRecord]:
        """Deliver one alert to each channel, deduplicated and redacted."""
        if severity is NotificationSeverity.CRITICAL and correlation_id is None:
            raise MissingCorrelationError(
                "critical notifications require a correlation_id"
            )
        if not channels:
            raise NotificationError("at least one channel is required")
        issued_at = now or datetime.now(UTC)
        safe_title = _redact_text(title)
        safe_body = _redact_text(body)

        results: list[NotificationRecord] = []
        for channel in channels:
            record = NotificationRecord(
                id=uuid4(),
                severity=severity,
                channel=channel,
                # dedup is per channel so the same event can reach both UI and
                # Telegram, while a repeat on the same channel is suppressed.
                dedup_key=f"{channel.value}:{dedup_key}",
                environment=environment,
                title=safe_title,
                body=safe_body,
                created_at=issued_at,
                correlation_id=correlation_id,
            )
            stored, created = self._store.record(record)
            if not created:
                results.append(stored)  # duplicate: never delivered twice
                continue
            delivered = self._deliver(stored)
            results.append(delivered)
        return results

    def acknowledge(
        self, event_id: UUID, principal: Principal, *, now: datetime | None = None
    ) -> NotificationRecord:
        """Authenticated, audited acknowledgement of an alert."""
        if not principal.roles:
            self._audit.record(
                AuthorizationDecision(
                    allowed=False,
                    principal_id=principal.principal_id,
                    action=f"ACK_NOTIFICATION:{event_id}",
                    reason="unauthenticated acknowledgement",
                )
            )
            raise NotificationError("acknowledgement requires an authenticated principal")
        record = self._store.get(event_id)
        if record is None:
            raise NotificationError(f"unknown notification {event_id}")
        acked = replace(
            record,
            acknowledged=True,
            acknowledged_at=now or datetime.now(UTC),
            acknowledged_by=principal.principal_id,
        )
        self._store.update(acked)
        self._audit.record(
            AuthorizationDecision(
                allowed=True,
                principal_id=principal.principal_id,
                action=f"ACK_NOTIFICATION:{event_id}",
                reason="acknowledged",
            )
        )
        return acked

    def escalate_unacknowledged(
        self, *, deadline: timedelta, now: datetime | None = None
    ) -> list[NotificationRecord]:
        """Escalate critical alerts unacknowledged past ``deadline`` (policy input)."""
        moment = now or datetime.now(UTC)
        escalated: list[NotificationRecord] = []
        for record in self._store.all_records():
            if record.severity is not NotificationSeverity.CRITICAL:
                continue
            if record.acknowledged or record.escalated:
                continue
            if moment - record.created_at < deadline:
                continue
            updated = replace(record, escalated=True, escalated_at=moment)
            self._store.update(updated)
            self._deliver(updated)  # re-deliver on escalation
            escalated.append(updated)
        return escalated

    @staticmethod
    def handle_inbound_reply(text: str) -> None:
        """Inbound replies are never parsed as commands (Technology 32.3).

        A reply that happens to contain a trading verb is rejected loudly so
        it can never be mistaken for an authorized action; anything else is
        ignored (returns None).
        """
        if text.strip().upper() in FORBIDDEN_COMMANDS:
            raise RemoteCommandRejectedError(
                "notification channel is read-only; commands are never executed"
            )
        return None

    def _deliver(self, record: NotificationRecord) -> NotificationRecord:
        provider = self._providers.get(record.channel)
        delivered = False
        if provider is not None:
            try:
                delivered = provider.send(record)
            except Exception:  # noqa: BLE001 - provider failure must fail closed
                delivered = False
        updated = replace(
            record,
            delivery_status="DELIVERED" if delivered else "FAILED",
        )
        self._store.update(updated)
        return updated


__all__ = [
    "FORBIDDEN_COMMANDS",
    "CollectingProvider",
    "InMemoryNotificationStore",
    "MissingCorrelationError",
    "NotificationError",
    "NotificationProvider",
    "NotificationRecord",
    "NotificationService",
    "NotificationStore",
    "RemoteCommandRejectedError",
]
