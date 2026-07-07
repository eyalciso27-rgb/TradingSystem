"""Emergency control service: halt gate, RBAC, fail-closed, audit (IMP-0404)."""

from __future__ import annotations

import pytest
from trading_application.auth import InMemoryAuditSink, Principal, Role
from trading_application.emergency import (
    EmergencyControlError,
    EmergencyControlService,
    EntryCheckpoint,
    EntryContext,
    InMemoryEmergencyCommandStore,
    ReauthenticationRequiredError,
    ResumeNotAllowedError,
    match_halt,
)
from trading_contracts.enums import EmergencyCommandType, Environment, HaltScope, OrderState


def make_service() -> tuple[
    EmergencyControlService, InMemoryEmergencyCommandStore, InMemoryAuditSink
]:
    store = InMemoryEmergencyCommandStore()
    audit = InMemoryAuditSink()
    return EmergencyControlService(store, audit), store, audit


def owner() -> Principal:
    return Principal(principal_id="op-1", roles=frozenset({Role.OWNER}), mfa_verified=True)


def ops_admin() -> Principal:
    return Principal(
        principal_id="ops-1", roles=frozenset({Role.OPERATIONS_ADMIN}), mfa_verified=True
    )


PAPER = Environment.PAPER


# -- gate: blocks entries, preserves protective exits (DoD) ------------------


def test_global_halt_blocks_every_entry_and_preserves_exit() -> None:
    service, _store, _audit = make_service()
    service.activate_halt(
        owner(),
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=PAPER,
        reason_code="SEV1",
        reason_text="market anomaly",
    )
    context = EntryContext(environment=PAPER, account_ref="A1", symbol="AAPL")
    for checkpoint in EntryCheckpoint:
        decision = service.evaluate_entry(context, checkpoint)
        assert decision.blocked
        assert decision.blocking_scope is HaltScope.GLOBAL_ENTRY_HALT
    # protective exits are never blocked by a halt
    assert service.protective_exit_allowed(context) is True


def test_queued_entry_blocked_becomes_cancelled_by_policy() -> None:
    service, _store, _audit = make_service()
    service.activate_halt(
        owner(),
        scope=HaltScope.STRATEGY_HALT,
        environment=PAPER,
        target_ref="strat-7",
        reason_code="DRIFT",
        reason_text="calibration drift",
    )
    ctx = EntryContext(environment=PAPER, strategy_id="strat-7")
    queued = service.evaluate_entry(ctx, EntryCheckpoint.QUEUE_CONSUMPTION)
    assert queued.blocked
    assert queued.resulting_order_state is OrderState.CANCELLED_BY_POLICY
    # pre-submit / intent-creation blocks too but do not imply a queued state
    presubmit = service.evaluate_entry(ctx, EntryCheckpoint.PRE_SUBMIT)
    assert presubmit.blocked and presubmit.resulting_order_state is None


def test_targeted_halt_only_blocks_matching_target() -> None:
    service, _store, _audit = make_service()
    service.activate_halt(
        ops_admin(),
        scope=HaltScope.SYMBOL_HALT,
        environment=PAPER,
        target_ref="TSLA",
        reason_code="CA",
        reason_text="corporate action",
    )
    assert service.evaluate_entry(
        EntryContext(environment=PAPER, symbol="TSLA"), EntryCheckpoint.INTENT_CREATION
    ).blocked
    assert not service.evaluate_entry(
        EntryContext(environment=PAPER, symbol="AAPL"), EntryCheckpoint.INTENT_CREATION
    ).blocked


def test_halt_is_environment_scoped_except_global() -> None:
    service, _store, _audit = make_service()
    service.activate_halt(
        owner(),
        scope=HaltScope.ENVIRONMENT_HALT,
        environment=PAPER,
        reason_code="X",
        reason_text="paper only",
    )
    assert service.evaluate_entry(
        EntryContext(environment=Environment.PAPER), EntryCheckpoint.PRE_SUBMIT
    ).blocked
    assert not service.evaluate_entry(
        EntryContext(environment=Environment.RESEARCH), EntryCheckpoint.PRE_SUBMIT
    ).blocked


def test_source_halt_blocks_entries_from_that_source() -> None:
    service, _store, _audit = make_service()
    service.activate_halt(
        ops_admin(),
        scope=HaltScope.SOURCE_HALT,
        environment=PAPER,
        target_ref="src-9",
        reason_code="MANIP",
        reason_text="manipulation suspected",
    )
    assert service.evaluate_entry(
        EntryContext(environment=PAPER, source_id="src-9"), EntryCheckpoint.INTENT_CREATION
    ).blocked


# -- resume lifecycle: fail closed -------------------------------------------


def test_resume_lifts_halt_only_with_health_and_reconciliation() -> None:
    service, _store, _audit = make_service()
    service.activate_halt(
        owner(),
        scope=HaltScope.ACCOUNT_HALT,
        environment=PAPER,
        target_ref="acct-1",
        reason_code="R",
        reason_text="reason",
    )
    ctx = EntryContext(environment=PAPER, account_ref="acct-1")
    assert service.evaluate_entry(ctx, EntryCheckpoint.PRE_SUBMIT).blocked

    with pytest.raises(ResumeNotAllowedError):
        service.resume(
            owner(),
            scope=HaltScope.ACCOUNT_HALT,
            environment=PAPER,
            target_ref="acct-1",
            reason_code="R",
            reason_text="reason",
            health_ok=True,
            reconciliation_ok=False,
        )
    assert service.evaluate_entry(ctx, EntryCheckpoint.PRE_SUBMIT).blocked  # still halted

    service.resume(
        owner(),
        scope=HaltScope.ACCOUNT_HALT,
        environment=PAPER,
        target_ref="acct-1",
        reason_code="R",
        reason_text="health+recon ok",
        health_ok=True,
        reconciliation_ok=True,
    )
    assert not service.evaluate_entry(ctx, EntryCheckpoint.PRE_SUBMIT).blocked


# -- RBAC and reason requirements --------------------------------------------


def test_no_role_is_denied() -> None:
    service, _store, audit = make_service()
    with pytest.raises(PermissionError):
        service.activate_halt(
            Principal(principal_id="nobody"),
            scope=HaltScope.SYMBOL_HALT,
            environment=PAPER,
            target_ref="AAPL",
            reason_code="R",
            reason_text="t",
        )
    assert any(not d.allowed for d in audit.decisions)


def test_global_halt_requires_owner_or_explicit_grant() -> None:
    service, _store, _audit = make_service()
    # OPERATIONS_ADMIN has ACTIVATE_KILL_SWITCH but not OWNER/explicit -> denied
    with pytest.raises(EmergencyControlError):
        service.activate_halt(
            ops_admin(),
            scope=HaltScope.GLOBAL_ENTRY_HALT,
            environment=PAPER,
            reason_code="R",
            reason_text="t",
        )
    # OWNER is allowed
    service.activate_halt(
        owner(),
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=PAPER,
        reason_code="R",
        reason_text="t",
    )


def test_reason_code_and_text_are_mandatory() -> None:
    service, _store, _audit = make_service()
    with pytest.raises(EmergencyControlError):
        service.activate_halt(
            owner(),
            scope=HaltScope.SYMBOL_HALT,
            environment=PAPER,
            target_ref="AAPL",
            reason_code="  ",
            reason_text="t",
        )


# -- position emergency actions ----------------------------------------------


def test_flatten_requires_reauthentication() -> None:
    service, store, _audit = make_service()
    with pytest.raises(ReauthenticationRequiredError):
        service.issue_position_action(
            owner(),
            command=EmergencyCommandType.FLATTEN_ACCOUNT,
            environment=PAPER,
            target_ref="acct-1",
            reason_code="R",
            reason_text="t",
            reauthenticated=False,
        )
    record = service.issue_position_action(
        owner(),
        command=EmergencyCommandType.FLATTEN_ACCOUNT,
        environment=PAPER,
        target_ref="acct-1",
        reason_code="R",
        reason_text="t",
        reauthenticated=True,
    )
    assert record.command is EmergencyCommandType.FLATTEN_ACCOUNT
    assert store.log[-1].command is EmergencyCommandType.FLATTEN_ACCOUNT


def test_position_action_rejects_halt_lifecycle_commands() -> None:
    service, _store, _audit = make_service()
    with pytest.raises(ValueError):
        service.issue_position_action(
            owner(),
            command=EmergencyCommandType.ACTIVATE_HALT,
            environment=PAPER,
            reason_code="R",
            reason_text="t",
        )


# -- append-only log + audit -------------------------------------------------


def test_every_command_is_appended_and_audited() -> None:
    service, store, audit = make_service()
    service.activate_halt(
        owner(),
        scope=HaltScope.SYMBOL_HALT,
        environment=PAPER,
        target_ref="AAPL",
        reason_code="R1",
        reason_text="t1",
    )
    service.resume(
        owner(),
        scope=HaltScope.SYMBOL_HALT,
        environment=PAPER,
        target_ref="AAPL",
        reason_code="R2",
        reason_text="t2",
        health_ok=True,
        reconciliation_ok=True,
    )
    assert len(store.log) == 2  # append-only, nothing overwritten
    assert store.log[0].command is EmergencyCommandType.ACTIVATE_HALT
    assert store.log[1].command is EmergencyCommandType.RESUME
    emergency_audits = [
        d for d in audit.decisions if str(d.action).startswith("EMERGENCY:")
    ]
    assert len(emergency_audits) == 2
    # actor and auth level recorded; no secret material
    assert store.log[0].actor_ref == "op-1"
    assert store.log[0].auth_level == "OWNER"


def test_match_halt_is_pure_and_order_deterministic() -> None:
    from trading_application.emergency import HaltRecord

    halts = [
        HaltRecord(HaltScope.SYMBOL_HALT, PAPER, "AAPL", "R"),
        HaltRecord(HaltScope.ACCOUNT_HALT, PAPER, "acct-1", "R"),
    ]
    ctx = EntryContext(environment=PAPER, symbol="AAPL", account_ref="acct-1")
    assert match_halt(halts, ctx) is halts[0]
    assert match_halt(halts, EntryContext(environment=PAPER, symbol="MSFT")) is None
