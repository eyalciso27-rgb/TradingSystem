# Chaos and Recovery Drills — IMP-1303

> Scope: the **non-destructive** portion of IMP-1303. This document is the gap
> analysis, the evidence map, and the owner-run procedures for the host-only
> destructive drills. Nothing here has been executed against a real broker,
> database, or message bus. The automated drills run in CI against fakes and a
> disposable PostgreSQL service only.
>
> **POI-IBKR-001 remains BLOCKED / NOT RUN.** No order is sent, previewed,
> cancelled, replaced, or modified by any drill. Paper Auto and Live remain
> prohibited. This document changes no PoI status and no `docs/poi/STATUS.yaml`
> entry.

## 1. Principle

Every fault domain must **fail closed**: an unhealthy state can only *prevent*
action, never cause one. The binding invariants, proven by the drills below:

1. No new order intent reaches submission during an unhealthy state.
2. No blind retry — an `UNKNOWN` broker state is resolved only by
   reconciliation, never by resending.
3. No duplicate *logical* order or event, under at-least-once delivery.
4. Protective exits are never removed by a halt or by reconciliation.
5. Recovery requires canonical reconciliation before entries resume.

## 2. Gap analysis by fault domain

Legend for **Automated now**: ✅ new IMP-1303 drill · 🔁 already covered by an
existing suite (cited) · ⛔ owner-run host drill, BLOCKED by POI-IBKR-001 ·
🏗 needs external infrastructure.

| Fault domain | Fail-closed behaviour proven | Automated now | Owner-run / external |
|---|---|---|---|
| IBKR/API disconnect + reconnect | submit while down → terminal, no retry; reconnect recovery is a reconciliation, not a resend | ✅ `test_recovery_drills.py` (disconnect/reconnect) + 🔁 `test_paper_manual_lifecycle.py` sc. 10/11 | ⛔ real `kill -9` of IB Gateway mid-lifecycle (host) |
| Market-data loss + stale data | final pre-submit gate refuses on missing / stale market or account snapshot; mints no authorization | ✅ `test_recovery_drills.py` (missing/stale market, stale account) + 🔁 `test_pre_submit_check.py` | 🏗 real feed-drop on host (needs entitlement window) |
| PostgreSQL outage / restart | outbox makes domain state + event atomic (failure before commit leaves neither); delivery is **at-least-once** with a **stable message identity**, not exactly-once | ✅ `tests/db/test_chaos_recovery_db.py` (atomicity, publish-before-mark redelivery, row-transition idempotency) + 🔁 `tests/db/test_outbox.py` | ⛔/🏗 real `systemctl restart postgresql` on host |
| Duplicate / at-least-once redelivery (consumer side) | the canonical Consumer Inbox (`process_once`, IMP-0204) runs a handler once per `(consumer_id, message_id)`; a crash mid-handler rolls back the claim for a clean retry | ✅ `tests/db/test_chaos_recovery_db.py` (inbox once + crash-retry) | — |
| Content-addressed object dedup | `RawObject.content_hash` collapses identical bytes to one object (object-store dedup, **not** a consumer inbox) | ✅ `tests/db/test_chaos_recovery_db.py` | — |
| NATS outage / restart / replay (transport) | no NATS JetStream adapter exists yet (`trading_event_bus` is a skeleton); the persistence-layer idempotency it will rely on (Consumer Inbox) is proven, but end-to-end bus redelivery is **not** exercised | 🏗 **remaining gap** | 🏗 real NATS JetStream adapter (separate task) |
| Application / service restart | restarted engine with empty state never resends; open broker order rebuilt only via reconciliation | ✅ `test_recovery_drills.py` (restart) + 🔁 `test_paper_manual_lifecycle.py` sc. 16b | ⛔ real service restart against live Gateway |
| UI / dashboard outage | kill-switch / halt gate stays deterministic with no UI and no LLM; entries blocked, protective exits allowed | ✅ `test_recovery_drills.py` (halt-without-UI, protective-exit-allowed, agent halt gate) + 🔁 `test_emergency.py` | ⛔ operator halt from CLI on host during a live session |
| Duplicate delivery / replay | same queue item and same fill are idempotent; redelivered event deduped in DB | ✅ `test_recovery_drills.py` (duplicate order, duplicate fill) + `test_chaos_recovery_db.py` | — |
| Partial processing + reconciliation | unprotected position is a loud, entry-blocking finding; reconciliation recommends, never mutates | ✅ `test_recovery_drills.py` (unprotected-blocks, no-remove-protective) | ⛔ real partial fill on host (POI-IBKR-001 fill-path) |

## 3. What is completed now (automated, safe)

`tests/chaos/test_recovery_drills.py` — deterministic, fakes only. Reuses the
real `ExecutionAgent`, `ProtectionManager`, `ReconciliationEngine`,
`FinalPreSubmitCheck`, and `EmergencyControlService` over the canonical
`FakeBroker` and in-memory stores. It imports no IBKR SDK, opens no socket, and
cannot reach any broker execution surface (asserted by a self-check test).

`tests/db/test_chaos_recovery_db.py` — PostgreSQL-gated (skips without
`DATABASE_URL`, runs in CI). Proves outbox atomicity; that external delivery is
at-least-once with a stable message identity (a publish-before-mark crash
redelivers the same id); that `mark_published` transitions the outbox row only
once (DB-row idempotency, not exactly-once delivery); that the canonical
Consumer Inbox (`process_once`) runs a handler once per message and retries
cleanly after a mid-handler crash; and that `RawObject.content_hash` provides
content-addressed object dedup only.

## 4. Owner-run host drills — BLOCKED by POI-IBKR-001

These are **destructive** and require a real IB Gateway session, so they cannot
run until the POI-IBKR-001 owner-run lifecycle has passed. **Do not perform any
of them now.** They are recorded here so the sequence is ready afterwards. Each
must be run only in the Paper environment, on localhost, with the safety
envelope of `docs/runbooks/POI_IBKR_001_OWNER_RUN.md` in force.

> Precondition for every drill below: POI-IBKR-001 cancel-path and fill-path
> have passed and been recorded, and the account is flat and clean.

1. **Gateway kill mid-lifecycle** — with a resting protective stop open,
   `kill -9` the IB Gateway process (or `systemctl stop ibgateway`). Expected:
   the watchdog's reconciliation gate INHIBITS any restart while
   `order-lifecycle.active` exists; on reconnect, run
   `--scenario reconnect` and confirm the position and its stop are still
   reported, and that reconciliation is clean before any new entry.
2. **API disconnect without process kill** — disable/re-enable the Gateway's
   API socket. Expected: `--scenario reconnect` recovers via reconciliation;
   no order is resent.
3. **PostgreSQL restart** — `systemctl restart postgresql` while the stack is
   idle (no in-flight order). Expected: the app reconnects; the outbox holds
   any unpublished event; no duplicate is published; reconciliation is clean.
4. **Operator halt from CLI with the dashboard down** — simulate a UI outage
   and activate a `GLOBAL_ENTRY_HALT` from the emergency CLI. Expected: entries
   blocked at all checkpoints, protective exits still allowed, resume refused
   until health and reconciliation both pass.

Each drill's evidence goes to `docs/poi/evidence/` (masked account id; no
credentials, no full account identifiers), and none of them may enable Paper
Auto or Live.

## 5. Drills requiring external infrastructure

- **Real NATS JetStream** outage/restart — the event-bus adapter is a
  placeholder today (`trading_event_bus` is a skeleton). The consumer-side
  idempotency contract it will rely on (the Consumer Inbox, `process_once`) is
  proven at the persistence layer now, but no message actually flows through a
  bus, so end-to-end bus redelivery is a **remaining gap**; the live adapter
  drill belongs to the task that implements it. Not started here (out of
  IMP-1303 scope).
- **Real external market-data provider** feed-drop — the external-provider
  work is deferred by the **Track A / A2 owner decision (2026-07-21)** recorded
  in `docs/roadmaps/EVIDENCE_ROADMAP.md` and reflected in
  `docs/poi/STATUS.yaml` (POI-MD-001 `blocked_on`): *"External provider not
  selected; benchmark/fallback/provider ADR deferred until owner-approved
  provider availability."* ADR-0016 explicitly **does not** select a provider —
  it only narrows POI-IBKR-001's dependency. Not started here.

## 6. Remaining owner actions

1. Complete the POI-IBKR-001 owner-run lifecycle (cancel-path, fill-path,
   protection, reconnect, reconciliation, cleanup) per
   `docs/runbooks/POI_IBKR_001_OWNER_RUN.md`.
2. Only then perform the host drills in §4, recording masked evidence.
3. Leave §5 to its dedicated task; do not select a provider or build the NATS
   adapter under IMP-1303.

## 7. Test commands

```bash
# automated safe drills (fakes only)
uv run pytest tests/chaos/test_recovery_drills.py -q

# DB-backed drills (skip locally; run in CI where DATABASE_URL is set)
uv run pytest tests/db/test_chaos_recovery_db.py -q

# the wider fail-closed surface these drills build on
uv run pytest tests/execution tests/application/test_pre_submit_check.py \
              tests/application/test_emergency.py tests/poi/test_paper_manual_lifecycle.py -q
```
