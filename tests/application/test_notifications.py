"""Notification service: dedup, correlation, ack, escalation, no-command."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from trading_application.auth import InMemoryAuditSink, Principal, Role
from trading_application.notifications import (
    CollectingProvider,
    InMemoryNotificationStore,
    MissingCorrelationError,
    NotificationError,
    NotificationService,
    RemoteCommandRejectedError,
)
from trading_contracts.enums import Environment, NotificationChannel, NotificationSeverity

NOW = datetime(2026, 7, 7, 10, 0, tzinfo=UTC)
PAPER = Environment.PAPER


def make_service(
    *, ui_healthy: bool = True, tg_healthy: bool = True
) -> tuple[
    NotificationService,
    InMemoryNotificationStore,
    dict[NotificationChannel, CollectingProvider],
    InMemoryAuditSink,
]:
    store = InMemoryNotificationStore()
    providers = {
        NotificationChannel.UI: CollectingProvider(NotificationChannel.UI, healthy=ui_healthy),
        NotificationChannel.TELEGRAM: CollectingProvider(
            NotificationChannel.TELEGRAM, healthy=tg_healthy
        ),
    }
    audit = InMemoryAuditSink()
    return NotificationService(store, providers, audit), store, providers, audit


def operator() -> Principal:
    return Principal(
        principal_id="op-1", roles=frozenset({Role.OPERATIONS_ADMIN}), mfa_verified=True
    )


# -- DoD: critical delivered and correlated ----------------------------------


def test_critical_alert_delivered_and_correlated() -> None:
    service, _store, providers, _audit = make_service()
    corr = uuid4()
    records = service.notify(
        severity=NotificationSeverity.CRITICAL,
        channels=[NotificationChannel.UI, NotificationChannel.TELEGRAM],
        title="Kill switch engaged",
        body="global entry halt active",
        dedup_key="evt-1",
        environment=PAPER,
        correlation_id=corr,
        now=NOW,
    )
    assert {r.channel for r in records} == {NotificationChannel.UI, NotificationChannel.TELEGRAM}
    assert all(r.correlation_id == corr for r in records)
    assert all(r.delivery_status == "DELIVERED" for r in records)
    assert len(providers[NotificationChannel.UI].sent) == 1
    assert len(providers[NotificationChannel.TELEGRAM].sent) == 1


def test_critical_requires_correlation_id() -> None:
    service, _store, _providers, _audit = make_service()
    with pytest.raises(MissingCorrelationError):
        service.notify(
            severity=NotificationSeverity.CRITICAL,
            channels=[NotificationChannel.UI],
            title="x",
            body="y",
            dedup_key="evt-2",
            environment=PAPER,
            now=NOW,
        )


# -- dedup -------------------------------------------------------------------


def test_duplicate_dedup_key_not_delivered_twice() -> None:
    service, _store, providers, _audit = make_service()
    for _ in range(2):
        service.notify(
            severity=NotificationSeverity.HIGH,
            channels=[NotificationChannel.UI],
            title="risk limit",
            body="near limit",
            dedup_key="evt-3",
            environment=PAPER,
            now=NOW,
        )
    assert len(providers[NotificationChannel.UI].sent) == 1  # second is a dedup no-op


def test_same_key_reaches_both_channels_once_each() -> None:
    service, _store, providers, _audit = make_service()
    service.notify(
        severity=NotificationSeverity.HIGH,
        channels=[NotificationChannel.UI, NotificationChannel.TELEGRAM],
        title="t",
        body="b",
        dedup_key="evt-4",
        environment=PAPER,
        now=NOW,
    )
    assert len(providers[NotificationChannel.UI].sent) == 1
    assert len(providers[NotificationChannel.TELEGRAM].sent) == 1


# -- redaction ---------------------------------------------------------------


def test_secrets_are_redacted_before_delivery() -> None:
    service, _store, providers, _audit = make_service()
    service.notify(
        severity=NotificationSeverity.MEDIUM,
        channels=[NotificationChannel.UI],
        title="auth failed",
        body="header was Bearer eyJabc.def.ghi for the session",
        dedup_key="evt-5",
        environment=PAPER,
        now=NOW,
    )
    delivered = providers[NotificationChannel.UI].sent[0]
    assert "eyJabc" not in delivered.body
    assert "[REDACTED]" in delivered.body


# -- acknowledgement ---------------------------------------------------------


def test_acknowledge_is_authenticated_and_audited() -> None:
    service, _store, _providers, audit = make_service()
    (record,) = service.notify(
        severity=NotificationSeverity.CRITICAL,
        channels=[NotificationChannel.UI],
        title="t",
        body="b",
        dedup_key="evt-6",
        environment=PAPER,
        correlation_id=uuid4(),
        now=NOW,
    )
    acked = service.acknowledge(record.id, operator(), now=NOW)
    assert acked.acknowledged and acked.acknowledged_by == "op-1"
    assert any(str(d.action).startswith("ACK_NOTIFICATION:") and d.allowed for d in audit.decisions)


def test_unauthenticated_acknowledge_denied() -> None:
    service, _store, _providers, audit = make_service()
    (record,) = service.notify(
        severity=NotificationSeverity.LOW,
        channels=[NotificationChannel.UI],
        title="t",
        body="b",
        dedup_key="evt-7",
        environment=PAPER,
        now=NOW,
    )
    with pytest.raises(NotificationError):
        service.acknowledge(record.id, Principal(principal_id="nobody"), now=NOW)
    assert any(not d.allowed for d in audit.decisions)


# -- escalation --------------------------------------------------------------


def test_unacknowledged_critical_escalates_after_deadline() -> None:
    service, _store, providers, _audit = make_service()
    (record,) = service.notify(
        severity=NotificationSeverity.CRITICAL,
        channels=[NotificationChannel.UI],
        title="t",
        body="b",
        dedup_key="evt-8",
        environment=PAPER,
        correlation_id=uuid4(),
        now=NOW,
    )
    # not yet past deadline
    assert service.escalate_unacknowledged(deadline=timedelta(minutes=5), now=NOW) == []
    later = NOW + timedelta(minutes=10)
    escalated = service.escalate_unacknowledged(deadline=timedelta(minutes=5), now=later)
    assert [e.id for e in escalated] == [record.id]
    assert len(providers[NotificationChannel.UI].sent) == 2  # re-delivered on escalation
    # acknowledged alerts are not escalated
    service.acknowledge(record.id, operator(), now=later)
    assert service.escalate_unacknowledged(deadline=timedelta(minutes=5), now=later) == []


# -- no remote commands (DoD) ------------------------------------------------


def test_inbound_reply_is_never_a_command() -> None:
    service, _store, _providers, _audit = make_service()
    for verb in ("BUY", "sell", " Halt ", "FLATTEN"):
        with pytest.raises(RemoteCommandRejectedError):
            service.handle_inbound_reply(verb)
    # ordinary replies are simply ignored, never actioned
    assert service.handle_inbound_reply("thanks, acknowledged") is None


def test_failed_provider_marks_delivery_failed() -> None:
    service, _store, _providers, _audit = make_service(ui_healthy=False)
    (record,) = service.notify(
        severity=NotificationSeverity.MEDIUM,
        channels=[NotificationChannel.UI],
        title="t",
        body="b",
        dedup_key="evt-9",
        environment=PAPER,
        now=NOW,
    )
    assert record.delivery_status == "FAILED"
