# Notification Service (IMP-0405)

Authority: System_Functional_Specification_v0.7 §20 and
Integration_Specification_v0.6 §20 (severity levels); Technology spec §32.3
(critical alerts require dedup, correlation_id and acknowledgement; a user
reply is never a trading command); Security spec §32.2 (no full sensitive
information by default) and §33.7 (a notification channel cannot Resume,
Flatten or change Policy); Operations Console & UX §14 (severity UX and
acknowledgement). Data model: `notification_events` (§18).

## Severity and channels

`NotificationSeverity`: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` (§20).
`NotificationChannel` (MVP): `UI` (in-app alert drawer) and `TELEGRAM`
(private chat). The channel bot/token is separate from the ingestion bot/token
(§32.3); wiring a concrete Telegram provider is a later social-adapter task.

## Guarantees (DoD: critical alert delivered and correlated, no commands)

- **Outbound only.** There is no method that turns an inbound message into a
  trading command. `handle_inbound_reply` ignores ordinary replies and
  *rejects* any text that looks like `BUY`/`SELL`/`HALT`/`FLATTEN` — the
  channel can never execute an action.
- **Correlation.** A `CRITICAL` alert fails closed without a `correlation_id`,
  so every critical alert is traceable end-to-end.
- **Dedup / idempotency.** Delivery is idempotent per `(channel, dedup_key)`;
  the durable `notification_events.dedup_key` is UNIQUE and
  `record_notification` uses INSERT … ON CONFLICT DO NOTHING … RETURNING, so a
  repeated alert is never delivered twice.
- **Redaction.** Every title/body passes through the same central redaction
  used by structured logging (`trading_observability.redact`), so secrets,
  bearer tokens and URL credentials never reach a channel.
- **Acknowledgement.** `acknowledge` requires an authenticated principal and
  is audited. **Escalation.** `escalate_unacknowledged(deadline=…)` re-delivers
  critical alerts still unacknowledged after a caller-supplied deadline; the
  deadline is policy/config input, never hardcoded.

## Layers

- `trading_application.notifications` — the pure service: dedup, redaction,
  delivery via the generic `NotificationProvider` port (no single-channel
  dependency), acknowledgement, escalation, and the no-command guard; plus an
  in-memory store and a collecting provider for tests.
- `trading_persistence.notifications` — durable `notification_events`
  (migration `0007`): idempotent `record_notification` and
  `mark_delivered` / `mark_failed` / `acknowledge` / `mark_escalated`.

PAPER and LIVE never share alerts (each event carries its `environment`);
LIVE stays disabled.

## Not verified locally

The `notification_events` DDL, `alembic upgrade/downgrade`, and the durable
dedup/ack tests run against real PostgreSQL in CI (no local database). A real
Telegram provider and its separate token are not part of this task.
