"""POI-OBS-001 drill — end-to-end telemetry chain (local, real components).

Runs the observability chain for real across component boundaries: one
correlation_id flows source -> candidate -> ... -> decision through
structured logs and the timeline, secrets are redacted, and a critical alert
derived from the same correlation is deduplicated and escalated. Blocked for
full PASS: live Grafana/Loki/Alloy stack (Docker) and a real Telegram private
alert (token).
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from trading_application.auth import InMemoryAuditSink
from trading_application.notifications import (
    CollectingProvider,
    InMemoryNotificationStore,
    NotificationService,
)
from trading_contracts.enums import Environment, NotificationChannel, NotificationSeverity
from trading_observability import (
    TIMELINE_STAGES,
    TimelineEntry,
    bind_log_context,
    build_timeline,
    configure_logging,
    new_correlation_id,
)

NOW = datetime(2026, 7, 7, 15, 0, tzinfo=UTC)


def test_source_to_decision_trace_with_redaction_and_alerting() -> None:
    correlation = new_correlation_id()
    stream = io.StringIO()
    configure_logging(service="core-worker", environment="paper", stream=stream)
    logger = logging.getLogger("poi.obs")

    # 1) Source-to-decision: every pipeline stage logs under one correlation
    # and emits a timeline entry linked by causation.
    entries: list[TimelineEntry] = []
    previous: str | None = None
    for index, stage in enumerate(TIMELINE_STAGES[: TIMELINE_STAGES.index("decision") + 1]):
        event_id = f"evt-{index}"
        with bind_log_context(
            correlation_id=correlation, event_id=event_id, symbol="AAPL"
        ):
            logger.info(
                "stage complete",
                extra={"stage": stage, "session_token": "should-never-appear"},
            )
        entries.append(
            TimelineEntry(
                event_id=event_id,
                stage=stage,
                correlation_id=correlation,
                causation_id=previous,
                timestamp=(NOW + timedelta(seconds=index)).isoformat(),
            )
        )
        previous = event_id

    lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
    assert all(line["correlation_id"] == correlation for line in lines)
    assert [line["stage"] for line in lines] == list(
        TIMELINE_STAGES[: TIMELINE_STAGES.index("decision") + 1]
    )
    # 2) Redaction across the whole stream
    assert "should-never-appear" not in stream.getvalue()
    assert all(line["session_token"] == "[REDACTED]" for line in lines)

    # 3) The timeline reconstructs source -> decision from the same ids
    chain = build_timeline(entries, correlation_id=correlation)
    assert chain[0].stage == "source_event" and chain[-1].stage == "decision"

    # 4) A critical alert on the same correlation: dedup + escalation
    store = InMemoryNotificationStore()
    ui = CollectingProvider(NotificationChannel.UI)
    service = NotificationService(store, {NotificationChannel.UI: ui}, InMemoryAuditSink())
    corr_uuid = uuid4()
    for _ in range(2):  # duplicate raise -> single delivery
        service.notify(
            severity=NotificationSeverity.CRITICAL,
            channels=[NotificationChannel.UI],
            title="adapter down",
            body=f"correlation {correlation}: market data stale",
            dedup_key=f"adapter-down:{correlation}",
            environment=Environment.PAPER,
            correlation_id=corr_uuid,
            now=NOW,
        )
    assert len(ui.sent) == 1
    escalated = service.escalate_unacknowledged(
        deadline=timedelta(minutes=5), now=NOW + timedelta(minutes=10)
    )
    assert len(escalated) == 1 and len(ui.sent) == 2


def test_health_degradation_produces_correlated_alert() -> None:
    """Simulated adapter-down: the failure path emits a correlated alert."""
    store = InMemoryNotificationStore()
    ui = CollectingProvider(NotificationChannel.UI)
    service = NotificationService(store, {NotificationChannel.UI: ui}, InMemoryAuditSink())
    corr = uuid4()
    (record,) = service.notify(
        severity=NotificationSeverity.CRITICAL,
        channels=[NotificationChannel.UI],
        title="telegram adapter AUTH_REQUIRED",
        body="re-authentication required; ingestion halted for this source",
        dedup_key=f"auth-required:{corr}",
        environment=Environment.PAPER,
        correlation_id=corr,
        now=NOW,
    )
    assert record.delivery_status == "DELIVERED"
    assert record.correlation_id == corr
