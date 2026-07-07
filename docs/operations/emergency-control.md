# Emergency Control Service (IMP-0404)

Authority: Security_and_Operations_Specification_v0.6 §12 (Kill Switch levels
and emergency modes), §12.2 (permissions and audit), §23.2 (halt checked at
intent creation, queue consumption and before submit; protective orders are
never cancelled by an entry halt), §24.1 (KEEP PROTECTION default); System
Functional §42.2 (HALT immediate; Resume needs MFA + health + reconciliation);
Data Model §18 (`emergency_commands`, append-only).

## What it does

A Kill Switch **stops new entries**; it is not "sell everything". Flatten is a
separate, explicitly approved, re-authenticated action.

Halt scopes (`HaltScope`): `GLOBAL_ENTRY_HALT`, `ENVIRONMENT_HALT`,
`BROKER_HALT`, `ACCOUNT_HALT`, `STRATEGY_HALT`, `SYMBOL_HALT`, `SOURCE_HALT`,
`MODEL_HALT`.

## Deterministic halt gate (DoD)

`EmergencyControlService.evaluate_entry(context, checkpoint)` is pure and
never consults an LLM. It is consulted at all three checkpoints of §23.2 —
`INTENT_CREATION`, `QUEUE_CONSUMPTION`, `PRE_SUBMIT` — and blocks a prospective
entry when any active halt matches its scope/target/environment. A global halt
blocks every environment; other scopes are environment- and target-scoped (an
untargeted scoped halt blocks all entries in scope; fail-closed matching). A
queued entry blocked at `QUEUE_CONSUMPTION` maps to
`OrderState.CANCELLED_BY_POLICY`. Already-`ACKNOWLEDGED` broker orders are not
auto-cancelled (§23.2) — the gate applies only to entries not yet submitted.

`protective_exit_allowed(...)` always returns `True`: protective and
risk-reducing exits are never blocked by a halt.

## Lifecycle, RBAC and audit

- **Activate** (`activate_halt`) is immediate. **Resume** (`resume`) fails
  closed unless both `health_ok` and `reconciliation_ok`; there is no
  automatic resume after SEV-1.
- Every command requires `ACTIVATE_KILL_SWITCH` (server-side RBAC, deny by
  default). `GLOBAL_ENTRY_HALT` and any LIVE-environment action additionally
  require the `OWNER` role or an explicit `ACTIVATE_KILL_SWITCH` grant (§12.2).
- `reason_code` and `reason_text` are mandatory. `FLATTEN_ACCOUNT` requires
  re-authentication.
- Each command is recorded twice: in the tamper-evident audit sink and in the
  append-only `emergency_commands` log.

## Persistence and state

`emergency_commands` (migration `0006`, append-only, never updated or deleted)
stores command, scope, target, actor, auth level, reason, ack and result.
Active-halt state is **derived by replaying** the log (latest lifecycle
command per scope+target+environment wins), so persistent state cannot drift
from the audit trail. `trading_persistence.emergency` provides the durable
`record_emergency_command` / `load_active_halts` half;
`trading_application.emergency` provides the pure gate, the RBAC-guarded
service and an in-memory store for the service abstraction.

## Boundaries

Telegram/WhatsApp/Discord messages never invoke HALT/FLATTEN; the service is
driven only via authorized API/RBAC actions. LIVE stays disabled; PAPER and
LIVE never share halt state (each command carries its `environment`). Broker
execution of FLATTEN/REDUCE is out of scope here — the service records the
approved command; execution wiring is a later task.

## Not verified locally

`emergency_commands` DDL, `alembic upgrade/downgrade`, and the durable
append/replay tests run against real PostgreSQL in CI (no local database).
