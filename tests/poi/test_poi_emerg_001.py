"""POI-EMERG-001 drill — emergency halt path (real code-path run, no UI).

Executes the executable canonical steps against the REAL IMP-0404 service:

- Set halt via emergency endpoint/CLI equivalent (direct authorized call).
- Queue order then halt -> queued entry becomes CANCELLED_BY_POLICY.
- Verify pre-submit check blocks after halt.
- Protective exits remain allowed under every halt.
- "Restart" within process: a fresh service instance over the persisted log
  derives the same active halts (cross-process restart on PostgreSQL is
  covered by tests/db/test_emergency.py in CI).
- Resume requires MFA principal + health + reconciliation; full audit trail.

NOT covered here (documented in evidence): halt via real UI (no UI exists
yet) and cross-process restart against PostgreSQL (CI-only).
"""

from __future__ import annotations

import pytest
from trading_application.auth import InMemoryAuditSink, Principal, Role
from trading_application.emergency import (
    EmergencyControlService,
    EntryCheckpoint,
    EntryContext,
    InMemoryEmergencyCommandStore,
    ResumeNotAllowedError,
)
from trading_contracts.enums import Environment, HaltScope, OrderState

PAPER = Environment.PAPER
OWNER = Principal(principal_id="owner-1", roles=frozenset({Role.OWNER}), mfa_verified=True)


def test_full_halt_drill() -> None:
    store = InMemoryEmergencyCommandStore()
    audit = InMemoryAuditSink()
    service = EmergencyControlService(store, audit)
    entry = EntryContext(environment=PAPER, account_ref="acct-1", symbol="AAPL")

    # Baseline: entries flow before the halt.
    assert not service.evaluate_entry(entry, EntryCheckpoint.INTENT_CREATION).blocked

    # Step: activate GLOBAL_ENTRY_HALT via the emergency (CLI/endpoint) path.
    service.activate_halt(
        OWNER,
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=PAPER,
        reason_code="POI_DRILL",
        reason_text="POI-EMERG-001 drill",
    )

    # Step: a queued order is stopped and mapped to CANCELLED_BY_POLICY.
    queued = service.evaluate_entry(entry, EntryCheckpoint.QUEUE_CONSUMPTION)
    assert queued.blocked
    assert queued.resulting_order_state is OrderState.CANCELLED_BY_POLICY

    # Step: pre-submit check blocks new submissions.
    assert service.evaluate_entry(entry, EntryCheckpoint.PRE_SUBMIT).blocked

    # Protective exits are never blocked by the halt.
    assert service.protective_exit_allowed(entry) is True

    # Step: "restart" — a fresh service over the persisted command log
    # derives the same halt state (nothing lives only in service memory).
    restarted = EmergencyControlService(store, InMemoryAuditSink())
    assert restarted.evaluate_entry(entry, EntryCheckpoint.PRE_SUBMIT).blocked

    # Step: resume fails closed without reconciliation, then succeeds.
    with pytest.raises(ResumeNotAllowedError):
        service.resume(
            OWNER,
            scope=HaltScope.GLOBAL_ENTRY_HALT,
            environment=PAPER,
            reason_code="POI_DRILL",
            reason_text="attempt without reconciliation",
            health_ok=True,
            reconciliation_ok=False,
        )
    service.resume(
        OWNER,
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=PAPER,
        reason_code="POI_DRILL",
        reason_text="health and reconciliation verified",
        health_ok=True,
        reconciliation_ok=True,
    )
    assert not service.evaluate_entry(entry, EntryCheckpoint.PRE_SUBMIT).blocked

    # Full audit: every lifecycle command was audited and logged append-only.
    emergency_actions = [
        d for d in audit.decisions if str(d.action).startswith("EMERGENCY:")
    ]
    assert len(emergency_actions) == 2
    assert len(store.log) == 2


def test_resume_requires_mfa_principal() -> None:
    store = InMemoryEmergencyCommandStore()
    service = EmergencyControlService(store, InMemoryAuditSink())
    no_mfa_owner = Principal(
        principal_id="owner-2", roles=frozenset({Role.OWNER}), mfa_verified=False
    )
    with pytest.raises(PermissionError):
        service.resume(
            no_mfa_owner,
            scope=HaltScope.GLOBAL_ENTRY_HALT,
            environment=PAPER,
            reason_code="POI_DRILL",
            reason_text="no MFA",
            health_ok=True,
            reconciliation_ok=True,
        )
