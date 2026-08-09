"""Paper-Manual lifecycle drill — POI-IBKR-001 / IMP-1302 preparation.

A deterministic, FAKES-ONLY harness that drives the REAL execution stack
(ExecutionAgent + ProtectionManager + ReconciliationEngine, plus the
watchdog's reconciliation gate) through the full order lifecycle a real
Paper Manual run will need — with zero IBKR contact, zero real orders and
zero LIVE/PAPER_AUTO surface. The broker is the canonical ``FakeBroker``
(the same one the conformance suite runs against the IBKR adapter), so
what passes here is the exact behavior expected at owner-run time.

Scenarios covered (the POI-IBKR-001 / IMP-1302 matrix):
 1. owner approval gate            9. cancel / replace
 2. pre-submit validation         10. disconnect / reconnect
 3. submit intent                 11. reconciliation
 4. broker acknowledgement        12. duplicate delivery / retry
 5. partial fill                  13. stale order event
 6. full fill                     14. broker rejection
 7. protection creation           15. protection failure
 8. stop/target acknowledgement   16. restart during lifecycle

DoD invariants proven with fakes (IMP-1302: "Zero duplicate orders and
zero required unprotected positions"): idempotency at the agent AND the
broker, duplicate events never create duplicate orders or duplicate
protection, every required fill gets protection, reconciliation detects
mismatches and fails closed (recommends, never acts), no blind retry
anywhere, and the watchdog restart gate honors order-lifecycle.active.

This file does NOT mark POI-IBKR-001 PASS — that requires the owner's
manual run against the real IBKR Paper gateway (see
docs/poi/POI-IBKR-001-READINESS.md).
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from trading_application.pre_submit_check import (
    PreSubmitAction,
    PreSubmitCheckResult,
    SingleUseAuthorization,
    SubmitAuthorization,
)
from trading_broker_core.fake import DEFAULT_CAPABILITIES, FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderState, OrderType
from trading_execution_agent.agent import (
    ExecutionAgent,
    ExecutionRequest,
    ExecutionStatus,
)
from trading_execution_agent.protection import (
    FillEvent,
    MissingCapabilityAction,
    PlanStatus,
    ProtectionActionType,
    ProtectionManager,
    ProtectionPolicy,
    ProtectionStructure,
)
from trading_execution_agent.reconciliation import (
    FindingKind,
    InternalOrderExpectation,
    InternalState,
    Recommendation,
    ReconciliationEngine,
    ReconciliationPolicy,
    ReconciliationTrigger,
)
from trading_execution_agent.reconciliation_latch import (
    ALWAYS_PERMIT,
    CLEAN,
    ReconciliationLatch,
)

AS_OF = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)

ACCOUNT = AccountSnapshot(
    account_alias="paper-main",
    environment=Environment.PAPER,
    net_liquidation_value=Decimal("100000"),
    cash=Decimal("60000"),
    buying_power=Decimal("120000"),
    observed_at=AS_OF - timedelta(minutes=1),
)

RECON_POLICY = ReconciliationPolicy(
    policy_version="recon-1",
    blocking_kinds=frozenset(
        {FindingKind.POSITION_MISMATCH, FindingKind.PROTECTION_MISMATCH}
    ),
)


@dataclass
class LifecycleDrill:
    """Deterministic wiring of the REAL stack over the canonical FakeBroker.

    ``owner_approved`` models the IMP-1302 rule that a SubmitAuthorization
    exists only after the owner approved the complete plan: without
    approval no authorization is minted, and the agent refuses.
    """

    broker: FakeBroker = field(
        default_factory=lambda: FakeBroker(accounts={"paper-main": ACCOUNT})
    )
    protection_structure: ProtectionStructure = ProtectionStructure.BRACKET

    def __post_init__(self) -> None:
        self.broker.connect()
        self.agent = ExecutionAgent(self.broker, entry_gate=ALWAYS_PERMIT, clock=lambda: AS_OF)
        self.protection = ProtectionManager(
            ProtectionPolicy(
                policy_version="prot-1",
                structure=self.protection_structure,
                missing_capability_action=MissingCapabilityAction.NEEDS_REVIEW,
            )
        )
        self.reconciliation = ReconciliationEngine(RECON_POLICY)

    # -- request building (sealed pipeline output + owner approval) ------------
    def request(
        self,
        client_order_id: str,
        *,
        owner_approved: bool = True,
        pre_submit_action: PreSubmitAction = PreSubmitAction.ALLOW,
        quantity: str = "1",
    ) -> ExecutionRequest:
        allow = pre_submit_action in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE)
        authorization = (
            SubmitAuthorization(
                candidate_id=f"cand-{client_order_id}",
                authorized_quantity=Decimal(quantity),
                authorized_at=AS_OF,
            )
            if owner_approved and allow
            else None
        )
        pre_submit = PreSubmitCheckResult(
            candidate_id=f"cand-{client_order_id}",
            action=pre_submit_action,
            authorized_quantity=Decimal(quantity) if authorization else Decimal(0),
            authorization=authorization,
            allocation_ref=f"allocation:cand-{client_order_id}@ALLOW",
            risk_ref="risk:APPROVE",
            decision_ref="sha256:" + "c" * 64,
            policy_ref="pre-submit-policy:p1",
            checks=(),
            reason_codes=("PRE_SUBMIT_CLEARED",) if allow else ("PRE_SUBMIT_CANCELLED",),
            input_refs=("observation:a-1@src-1",),
        )
        return ExecutionRequest(
            client_order_id=client_order_id,
            correlation_id=f"corr-{client_order_id}",
            pre_submit=pre_submit,
            authorization=SingleUseAuthorization(authorization) if authorization else None,
            environment=Environment.PAPER,
            account_alias="paper-main",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            limit_price=Decimal("10"),
            stop_price=None,
            strategy_id="strat-1",
            source_id="src-1",
        )

    def fill(
        self, fill_id: str, quantity: str, *, position_ref: str = "pos-AAPL"
    ) -> FillEvent:
        return FillEvent(
            fill_id=fill_id,
            position_ref=position_ref,
            symbol="AAPL",
            environment=Environment.PAPER,
            entry_side=OrderSide.BUY,
            filled_quantity=Decimal(quantity),
            stop_price=Decimal("9"),
            target_price=Decimal("12"),
            observed_at=AS_OF,
            correlation_id="corr-fill",
        )

    def processed_event_ids(self) -> frozenset[str]:
        """The engine's canonical id for every broker event seen so far —
        what a healthy internal inbox would have processed."""
        return frozenset(
            f"{event.sequence}:{event.client_order_id}:{event.state.value}"
            for event in self.broker.order_events()
        )

    def reconcile(
        self,
        *,
        open_orders: tuple[InternalOrderExpectation, ...] = (),
        positions: dict[str, Decimal] | None = None,
        protected: dict[str, Decimal] | None = None,
        processed: frozenset[str] | None = None,
        trigger: ReconciliationTrigger = ReconciliationTrigger.EVENT_DRIVEN,
    ) -> Any:
        return self.reconciliation.run(
            self.broker,
            InternalState(
                account_alias="paper-main",
                environment=Environment.PAPER,
                open_orders=open_orders,
                positions=positions or {},
                protected_quantities=protected or {},
                processed_fill_event_ids=(
                    processed if processed is not None else self.processed_event_ids()
                ),
                as_of=AS_OF,
            ),
            trigger=trigger,
            as_of=AS_OF + timedelta(seconds=1),
        )


# -- 1+2: owner approval gate + pre-submit validation --------------------------------


def test_scenario_01_no_owner_approval_means_no_submit() -> None:
    drill = LifecycleDrill()
    outcome = drill.agent.process(drill.request("ord-1", owner_approved=False))
    assert outcome.status is ExecutionStatus.REFUSED
    assert "AUTHORIZATION_MISSING" in outcome.reason_codes
    assert drill.broker.get_open_orders("paper-main") == ()


def test_scenario_02_cancelled_pre_submit_refused_nothing_sent() -> None:
    drill = LifecycleDrill()
    outcome = drill.agent.process(
        drill.request("ord-2", pre_submit_action=PreSubmitAction.CANCEL)
    )
    assert outcome.status is ExecutionStatus.REFUSED
    assert drill.broker.get_open_orders("paper-main") == ()


# -- 3+4: submit + acknowledgement ----------------------------------------------------


def test_scenario_03_04_submit_and_broker_acknowledgement() -> None:
    drill = LifecycleDrill()
    outcome = drill.agent.process(drill.request("ord-3"))
    assert outcome.status is ExecutionStatus.SUBMITTED
    assert outcome.receipt is not None
    assert outcome.receipt.state is OrderState.ACKNOWLEDGED
    events = tuple(drill.broker.order_events())
    assert any(
        event.client_order_id == "ord-3" and event.state is OrderState.ACKNOWLEDGED
        for event in events
    )
    assert outcome.authorization_ref.startswith("submit-auth:")


# -- 5+6+7+8: partial fill -> full fill -> protection with stop+target ----------------


def test_scenario_05_to_08_fills_protection_and_stop_target() -> None:
    drill = LifecycleDrill()
    partial = drill.protection.on_fill(
        drill.fill("fill-1", "40"), DEFAULT_CAPABILITIES, as_of=AS_OF
    )
    assert partial.status is PlanStatus.OK
    assert partial.protected_quantity == Decimal("40")
    place_actions = {action.action for action in partial.actions}
    assert ProtectionActionType.PLACE_STOP in place_actions
    assert ProtectionActionType.PLACE_TARGET in place_actions  # bracket ack

    full = drill.protection.on_fill(
        drill.fill("fill-2", "60"), DEFAULT_CAPABILITIES, as_of=AS_OF
    )
    assert full.protected_quantity == Decimal("100")  # protection follows fills
    replace_actions = {action.action for action in full.actions}
    assert ProtectionActionType.REPLACE_STOP in replace_actions
    assert drill.protection.unprotected_positions() == ()
    stop_specs = [
        action.spec for action in full.actions if action.spec.order_type is OrderType.STOP
    ]
    assert stop_specs and stop_specs[0].quantity == Decimal("100")
    assert stop_specs[0].side is OrderSide.SELL  # opposite of the BUY entry


# -- 9: cancel / replace ----------------------------------------------------------------


def test_scenario_09_cancel_and_replace_via_fresh_authorization() -> None:
    drill = LifecycleDrill()
    drill.agent.process(drill.request("ord-9"))
    cancel_outcome, new_outcome = drill.agent.replace(
        "ord-9", drill.request("ord-9r", quantity="1")
    )
    assert cancel_outcome.status is ExecutionStatus.CANCEL_SENT
    assert new_outcome.status is ExecutionStatus.SUBMITTED
    states = {
        receipt.client_order_id: receipt.state
        for receipt in (drill.broker.get_open_orders("paper-main"))
    }
    assert states.get("ord-9r") is OrderState.ACKNOWLEDGED
    assert "ord-9" not in states  # cancelled leg is closed


def test_scenario_09b_replace_refused_when_cancel_leg_unknown() -> None:
    drill = LifecycleDrill()
    cancel_outcome, new_outcome = drill.agent.replace(
        "never-submitted", drill.request("ord-9x")
    )
    assert cancel_outcome.status is ExecutionStatus.REFUSED
    assert new_outcome.status is ExecutionStatus.REFUSED
    assert "REPLACE_CANCELLED_LEG_NOT_CONFIRMED" in new_outcome.reason_codes
    assert drill.broker.get_open_orders("paper-main") == ()  # nothing leaked


# -- 10+11: disconnect / reconnect + reconciliation --------------------------------------


def test_scenario_10_11_disconnect_reconnect_and_clean_reconciliation() -> None:
    drill = LifecycleDrill()
    drill.agent.process(drill.request("ord-10"))
    drill.broker.disconnect()
    # A submit while disconnected fails terminally — no blind retry.
    outcome = drill.agent.process(drill.request("ord-10b"))
    assert outcome.status in (ExecutionStatus.FAILED, ExecutionStatus.NEEDS_REVIEW)
    drill.broker.connect()  # reconnect
    report = drill.reconcile(
        open_orders=(
            InternalOrderExpectation(
                client_order_id="ord-10", expected_state=OrderState.ACKNOWLEDGED
            ),
        ),
        trigger=ReconciliationTrigger.STARTUP,
    )
    assert report.reconciled is True
    assert report.findings == ()
    assert report.entry_block_required is False


# -- 12: duplicate delivery / retry -------------------------------------------------------


def test_scenario_12_duplicate_delivery_never_duplicates_orders_or_protection() -> None:
    drill = LifecycleDrill()
    request = drill.request("ord-12")
    first = drill.agent.process(request)
    second = drill.agent.process(request)  # duplicate queue delivery
    assert second is first  # idempotent outcome, nothing re-sent
    assert len(drill.broker.get_open_orders("paper-main")) == 1  # zero duplicates

    fill = drill.fill("fill-12", "1")
    plan_a = drill.protection.on_fill(fill, DEFAULT_CAPABILITIES, as_of=AS_OF)
    plan_b = drill.protection.on_fill(fill, DEFAULT_CAPABILITIES, as_of=AS_OF)
    assert plan_b is plan_a  # duplicate fill never duplicates protection
    assert plan_a.protected_quantity == Decimal("1")


# -- 13: stale order event ------------------------------------------------------------------


def test_scenario_13_stale_internal_order_detected_needs_review_no_retry() -> None:
    drill = LifecycleDrill()
    # Internal state still believes an order is open; the broker has no such
    # order (stale event / lost order) -> explicit finding, never a resend.
    report = drill.reconcile(
        open_orders=(
            InternalOrderExpectation(
                client_order_id="ord-stale", expected_state=OrderState.ACKNOWLEDGED
            ),
        ),
    )
    assert report.reconciled is False
    kinds = {finding.kind for finding in report.findings}
    assert FindingKind.MISSING_BROKER_ORDER in kinds
    finding = next(iter(report.findings))
    assert finding.recommendation is Recommendation.NEEDS_REVIEW
    assert len(drill.broker.get_open_orders("paper-main")) == 0  # nothing re-sent


# -- 14: broker rejection ---------------------------------------------------------------------


def test_scenario_14_broker_rejection_is_terminal_without_retry() -> None:
    drill = LifecycleDrill(
        broker=FakeBroker(accounts={})  # unknown account -> broker rejects
    )
    outcome = drill.agent.process(drill.request("ord-14"))
    assert outcome.status in (ExecutionStatus.FAILED, ExecutionStatus.REFUSED)
    assert outcome.receipt is None
    # Re-delivery of the same request returns the SAME terminal outcome —
    # a rejection is never blindly retried.
    again = drill.agent.process(drill.request("ord-14"))
    assert again.status is outcome.status


# -- 15: protection failure ---------------------------------------------------------------------


def test_scenario_15_protection_failure_is_loud_and_blocks() -> None:
    no_stop = BrokerCapabilities(
        order_types={
            OrderType.LIMIT: BrokerCapabilityMode.NATIVE,
            OrderType.STOP: BrokerCapabilityMode.UNSUPPORTED,
            OrderType.STOP_LIMIT: BrokerCapabilityMode.UNSUPPORTED,
            OrderType.MARKET: BrokerCapabilityMode.NATIVE,
        },
        bracket=BrokerCapabilityMode.UNSUPPORTED,
        oco=BrokerCapabilityMode.UNSUPPORTED,
        trailing=BrokerCapabilityMode.UNSUPPORTED,
        fractional=BrokerCapabilityMode.DISABLED,
        outside_rth=BrokerCapabilityMode.DISABLED,
        what_if=BrokerCapabilityMode.NATIVE,
        streaming_events=BrokerCapabilityMode.NATIVE,
    )
    drill = LifecycleDrill()
    plan = drill.protection.on_fill(drill.fill("fill-15", "1"), no_stop, as_of=AS_OF)
    assert plan.status is PlanStatus.NEEDS_REVIEW
    assert "POSITION_WOULD_BE_UNPROTECTED" in plan.reason_codes
    assert drill.protection.unprotected_positions() == ("pos-AAPL",)  # loud
    # Reconciliation turns the same gap into a blocking finding.
    report = drill.reconcile(positions={"AAPL": Decimal("1")}, protected={})
    kinds = {finding.kind for finding in report.findings}
    assert FindingKind.POSITION_MISMATCH in kinds or FindingKind.PROTECTION_MISMATCH in kinds
    assert report.entry_block_required is True  # fail closed


# -- 16: restart during lifecycle -----------------------------------------------------------------


def _load_watchdog() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "ib_gateway_watchdog_lifecycle", repo_root / "scripts" / "ib_gateway_watchdog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_scenario_16_restart_gate_honors_active_order_lifecycle() -> None:
    wd = _load_watchdog()
    # order-lifecycle.active exists -> the watchdog must NOT kill/restart the
    # gateway mid-lifecycle; it waits and alerts (reconciliation gate).
    plan = wd.decide_action(
        wd.WatchdogState.API_DOWN,
        wd.LoginMarker.LOGGED_IN,
        restart_count=0,
        relogin_count=0,
        policy=wd.Policy(),
        restart_inhibited=True,
    )
    assert plan.action is wd.Action.WAIT
    assert plan.exit_code == wd.ExitCode.RESTART_INHIBITED


def test_scenario_16b_state_rebuild_after_restart_via_reconciliation() -> None:
    drill = LifecycleDrill()
    drill.agent.process(drill.request("ord-16"))
    # Simulated process restart: a FRESH agent with empty internal state.
    restarted_agent = ExecutionAgent(drill.broker, entry_gate=ALWAYS_PERMIT, clock=lambda: AS_OF)
    del restarted_agent  # its state is empty; recovery goes via reconciliation
    report = drill.reconcile(open_orders=())  # internal lost the open order
    kinds = {finding.kind for finding in report.findings}
    assert FindingKind.UNEXPECTED_BROKER_ORDER in kinds  # detected, not ignored
    # Startup reconciliation recommends review/resync — it never cancels or
    # resubmits by itself.
    for finding in report.findings:
        assert finding.recommendation in (
            Recommendation.NEEDS_REVIEW,
            Recommendation.RESYNC_STATE,
            Recommendation.BLOCK_NEW_ENTRIES,
        )
    assert len(drill.broker.get_open_orders("paper-main")) == 1  # untouched


# -- 17: entry-reconciliation latch (no new orders after restart until CLEAN) ----------


def test_scenario_17_entry_latch_blocks_new_orders_after_restart_until_clean(
    tmp_path: Path,
) -> None:
    """IMP-1302 execution/reconciliation DoD, with the merged final-boundary
    reconciliation latch: after a Gateway restart (a new broker-session token) a
    new Paper-Manual entry is REFUSED until a CURRENT-session account-safety
    CLEAN clears the latch — so no new/duplicate order is ever submitted into a
    freshly recovered session. cancel()/protective paths are never gated."""
    drill = LifecycleDrill()  # supplies the FakeBroker + request builder
    session = tmp_path / "gateway-session"
    session.write_text("gw-1\n", encoding="utf-8")
    latch = ReconciliationLatch(
        session_path=session, latch_path=tmp_path / "reconciliation-required.json"
    )
    agent = ExecutionAgent(drill.broker, entry_gate=latch, clock=lambda: AS_OF)

    # (a) latch not yet cleared for the current session -> entry REFUSED, no order
    blocked = agent.process(drill.request("ord-17a"))
    assert blocked.status is ExecutionStatus.NEEDS_REVIEW
    assert any(code.startswith("RECONCILIATION_REQUIRED") for code in blocked.reason_codes)
    assert "RECONCILE_BEFORE_ANY_SUBMIT" in blocked.reason_codes
    assert drill.broker.get_open_orders("paper-main") == ()

    # (b) a CURRENT-session account-safety CLEAN clears the latch -> entry proceeds
    assert latch.try_clear(CLEAN, "gw-1") is True
    ok = agent.process(drill.request("ord-17b"))
    assert ok.status is ExecutionStatus.SUBMITTED
    assert len(drill.broker.get_open_orders("paper-main")) == 1

    # (c) a restart rotates the session token -> latch blocks again; ZERO new orders
    session.write_text("gw-2\n", encoding="utf-8")
    after_restart = agent.process(drill.request("ord-17c"))
    assert after_restart.status is ExecutionStatus.NEEDS_REVIEW
    assert len(drill.broker.get_open_orders("paper-main")) == 1  # still only ord-17b

    # (d) a stale pre-restart CLEAN cannot clear the new session; a fresh one does
    assert latch.try_clear(CLEAN, "gw-1") is False  # old-session proof rejected
    assert latch.try_clear(CLEAN, "gw-2") is True
    resumed = agent.process(drill.request("ord-17d"))
    assert resumed.status is ExecutionStatus.SUBMITTED
    assert len(drill.broker.get_open_orders("paper-main")) == 2

    # (e) the entry latch never gates a protective cancel.
    cancel = agent.cancel("ord-17b", correlation_id="corr-17")
    assert cancel.status is ExecutionStatus.CANCEL_SENT


# -- cross-cutting safety -----------------------------------------------------------------------


def test_authorization_is_single_use_across_the_lifecycle() -> None:
    drill = LifecycleDrill()
    request = drill.request("ord-au")
    outcome = drill.agent.process(request)
    assert outcome.status is ExecutionStatus.SUBMITTED
    assert request.authorization is not None
    with pytest.raises(Exception, match="consumed"):
        request.authorization.consume()  # cannot be spent twice


def test_no_live_environment_anywhere_in_the_drill() -> None:
    drill = LifecycleDrill()
    assert ACCOUNT.environment is Environment.PAPER
    request = drill.request("ord-env")
    assert request.environment is Environment.PAPER
    assert "LIVE" not in {ACCOUNT.environment.value, request.environment.value}


def test_drill_uses_no_ibkr_sdk_or_network() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in ("ib" + "api", "trading_broker_ibkr", "httpx", "requests", "socket"):
            assert forbidden not in line, line
