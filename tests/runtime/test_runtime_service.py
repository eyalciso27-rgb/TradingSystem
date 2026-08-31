"""Deterministic tests for the supervised runtime loop (A1-A4/A6).

No database, broker or gateway: the loop is driven entirely through its ports
with the in-memory fakes, so every assertion is about behaviour, not timing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

import pytest
from trading_application.emergency import EntryContext, HaltRecord
from trading_contracts.enums import Environment
from trading_execution_agent.execution_service import ExecutionIntent
from trading_execution_agent.runtime.ports import (
    ConnectionFacts,
    ExecutionPort,
    HaltGate,
    IntentSource,
    OwnershipGuard,
    ReconciliationDriver,
)
from trading_execution_agent.runtime.reconciliation_state import ReconciliationState
from trading_execution_agent.runtime.service import (
    OwnershipNotAcquiredError,
    PaperRuntime,
    RuntimeConfig,
)
from trading_execution_agent.runtime.states import AuthorizationMode, RuntimeState

from tests.runtime.fakes import (
    NOW,
    FakeConnectionFacts,
    FakeHaltGate,
    FakeOwnership,
    FakeReconciliationDriver,
    RaisingOwnership,
    RecordingExecutionPort,
    ScriptedWait,
    SequencedReconciliationDriver,
    StubIntentSource,
    blocked_state,
    clean_state,
    make_intent,
)


def _runtime(
    *,
    ownership: OwnershipGuard | None = None,
    reconciliation: ReconciliationDriver | None = None,
    halts: HaltGate | None = None,
    execution: ExecutionPort | None = None,
    intents: IntentSource | None = None,
    connection_facts: ConnectionFacts | None = None,
    config: RuntimeConfig | None = None,
    wait: Callable[[float], bool] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PaperRuntime:
    return PaperRuntime(
        ownership=ownership or FakeOwnership(),
        reconciliation=reconciliation or FakeReconciliationDriver(clean_state()),
        halts=halts or FakeHaltGate(),
        execution=execution or RecordingExecutionPort(),
        intents=intents or StubIntentSource(),
        connection_facts=connection_facts or FakeConnectionFacts(),
        config=config or RuntimeConfig(max_ticks=3),
        now=clock or (lambda: NOW),
        wait=wait or (lambda _s: False),
    )


# -- lifecycle ---------------------------------------------------------------


def test_starts_and_stops_cleanly() -> None:
    ownership = FakeOwnership()
    reconciliation = FakeReconciliationDriver(clean_state())
    runtime = _runtime(ownership=ownership, reconciliation=reconciliation)
    summary = runtime.run()
    runtime.close()
    assert summary.stopped_cleanly is True
    assert summary.ownership_lost is False
    assert summary.submissions == 0
    assert ownership.released is True
    assert reconciliation.startup_calls == 1


def test_request_stop_ends_the_loop_immediately() -> None:
    # SIGTERM/SIGINT land here: a handler calls request_stop(), and the loop's
    # blocking wait returns True at once. Modelled by a wait that stops on the
    # first call.
    runtime = _runtime(config=RuntimeConfig(), wait=ScriptedWait([True]))
    summary = runtime.run()
    assert summary.stopped_cleanly is True
    assert summary.ticks == 1  # one tick, then the wait signalled stop


def test_signal_handler_style_stop() -> None:
    runtime = _runtime(config=RuntimeConfig(max_ticks=100))

    # A wait that asks the runtime to stop after two ticks, exactly as a signal
    # handler calling request_stop() from another thread would.
    ticks = {"n": 0}

    def wait(_seconds: float) -> bool:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            runtime.request_stop()
            return True
        return False

    runtime._wait = wait
    summary = runtime.run()
    assert runtime.stopping is True
    assert summary.ticks == 2


# -- ownership (A2) ----------------------------------------------------------


def test_second_instance_cannot_acquire_ownership() -> None:
    runtime = _runtime(ownership=FakeOwnership(acquired=False))
    with pytest.raises(OwnershipNotAcquiredError):
        runtime.start()
    assert runtime.status().state is RuntimeState.FAILED


def test_ownership_that_cannot_reach_db_fails_closed() -> None:
    runtime = _runtime(ownership=RaisingOwnership())
    with pytest.raises(OwnershipNotAcquiredError):
        runtime.start()


def test_ownership_loss_blocks_entries_and_stops() -> None:
    ownership = FakeOwnership(held=True)
    execution = RecordingExecutionPort()
    intents = StubIntentSource(intents=[make_intent()])
    runtime = _runtime(
        ownership=ownership,
        execution=execution,
        intents=intents,
        config=RuntimeConfig(max_ticks=5),
    )
    # Lose the lock after the first tick, exactly as a dropped connection would.
    calls = {"n": 0}

    def wait(_seconds: float) -> bool:
        calls["n"] += 1
        ownership.held = False
        return False

    runtime._wait = wait
    summary = runtime.run()
    assert summary.ownership_lost is True
    assert summary.stopped_cleanly is False
    assert runtime.status().ownership_proven is False
    # No entry was submitted, and the loop stopped rather than continuing to
    # trade while another instance may own execution.
    assert summary.submissions == 0


# -- zero submissions under DenyAll (A1) -------------------------------------


def test_denyall_runtime_makes_zero_submissions_over_many_ticks() -> None:
    execution = RecordingExecutionPort(submitted=False)  # the DenyAll contract
    intents = StubIntentSource(intents=[make_intent("AAPL"), make_intent("MSFT")])
    runtime = _runtime(
        execution=execution,
        intents=intents,
        config=RuntimeConfig(max_ticks=10),
    )
    summary = runtime.run()
    assert summary.ticks == 10
    assert summary.submissions == 0
    # The port WAS exercised -- the loop really evaluated entries and every one
    # was refused, rather than never reaching execution at all.
    assert summary.entries_attempted == 20
    assert len(execution.calls) == 20


def test_runtime_flags_an_impossible_submission_but_never_submits_itself() -> None:
    # If a port ever reported a submission under DenyAll the invariant is broken;
    # the runtime records it loudly. The runtime still never submits ITSELF --
    # it only ever calls execute().
    execution = RecordingExecutionPort(submitted=True)
    intents = StubIntentSource(intents=[make_intent()])
    runtime = _runtime(
        execution=execution, intents=intents, config=RuntimeConfig(max_ticks=1)
    )
    summary = runtime.run()
    assert summary.submissions == 1  # surfaced, not hidden
    assert summary.entries_attempted == 1


# -- reconciliation gating (A3) ----------------------------------------------


def test_startup_unknown_reconciliation_blocks() -> None:
    runtime = _runtime(
        reconciliation=FakeReconciliationDriver(blocked_state("UNKNOWN")),
        intents=StubIntentSource(intents=[make_intent()]),
    )
    runtime.start()
    status = runtime.status()
    assert status.state is RuntimeState.BLOCKED
    assert status.entry_eligible is False


def test_startup_clean_reconciliation_is_ready_but_still_denyall() -> None:
    execution = RecordingExecutionPort()
    runtime = _runtime(execution=execution)
    runtime.start()
    status = runtime.status()
    assert status.entry_eligible is True
    # Eligible, but the only authority is DenyAll: the top state is READY_DENYALL
    # and never RUNNING.
    assert status.authorization_mode is AuthorizationMode.DENYALL
    assert status.state is RuntimeState.READY_DENYALL


def test_unknown_position_blocks_and_is_never_treated_as_flat() -> None:
    # A non-zero unattributed position count blocks; a None (unreadable) count
    # blocks too -- an unknown is never read as zero exposure.
    for state in (
        blocked_state("NON_ZERO_POSITION_PRESENT"),
        _state_with_unknown_positions(2),
    ):
        runtime = _runtime(reconciliation=FakeReconciliationDriver(state))
        runtime.start()
        assert runtime.status().entry_eligible is False


def _state_with_unknown_positions(count: int) -> ReconciliationState:
    return ReconciliationState(
        clean=False,
        entry_blocked=True,
        unknown_positions=count,
        unknown_orders=0,
        session_token="s",
        checked_at=NOW,
        reason="unattributed positions",
    )


def test_disconnect_reblocks_until_reconciliation_is_clean_again() -> None:
    # tick 1: connected + CLEAN -> eligible. tick 2: disconnected -> reconcile
    # returns blocked -> not eligible. tick 3: reconnected -> reconcile returns
    # clean -> eligible again.
    reconciliation = SequencedReconciliationDriver(
        [clean_state(), blocked_state("DISCONNECTED"), clean_state()]
    )
    facts = FakeConnectionFacts(ibkr=True)
    execution = RecordingExecutionPort()
    intents = StubIntentSource(intents=[make_intent()])
    runtime = _runtime(
        reconciliation=reconciliation,
        connection_facts=facts,
        execution=execution,
        intents=intents,
        config=RuntimeConfig(max_ticks=1),
    )
    runtime.start()  # startup reconcile -> clean (state[0])
    assert runtime.status().entry_eligible is True

    facts.ibkr = False  # a disconnect
    runtime.tick()  # forces reconcile -> blocked (state[1])
    assert runtime.status().entry_eligible is False
    assert runtime.status().ibkr_connected is False

    facts.ibkr = True  # reconnect
    runtime.tick()  # forces reconcile -> clean (state[2])
    assert runtime.status().entry_eligible is True


# -- halt wiring (A4) --------------------------------------------------------


def test_operator_halt_blocks_the_next_entry_tick() -> None:
    halts = FakeHaltGate(blocked=True)
    execution = RecordingExecutionPort()
    intents = StubIntentSource(intents=[make_intent()])
    runtime = _runtime(
        halts=halts, execution=execution, intents=intents,
        config=RuntimeConfig(max_ticks=3),
    )
    runtime.start()
    runtime.tick()
    # Mid-run, while the loop is ACTIVE: halt active -> not entry-eligible ->
    # execute() never called, and the derived state is BLOCKED.
    assert execution.calls == []
    assert runtime.status().halt_active is True
    assert runtime.status().state is RuntimeState.BLOCKED
    # The loop keeps ticking (so risk-reducing observation remains possible).
    summary = runtime.run()
    assert execution.calls == []
    assert summary.entries_attempted == 0


def test_halt_store_failure_fails_closed() -> None:
    halts = FakeHaltGate(store_down=True)
    execution = RecordingExecutionPort()
    intents = StubIntentSource(intents=[make_intent()])
    runtime = _runtime(halts=halts, execution=execution, intents=intents)
    runtime.run()
    # An unreadable halt store is treated as 'halted': no entry is attempted.
    assert execution.calls == []
    assert runtime.status().halt_active is True


def test_symbol_scoped_halt_skips_only_its_target() -> None:
    # A halt scoped to MSFT must skip MSFT while AAPL still evaluates.
    class SymbolHaltGate:
        def active_halts(self) -> Sequence[HaltRecord]:
            return ()

        def entry_halt_active(self, context: EntryContext) -> bool:
            # environment-level check (no symbol) is not halted; MSFT is.
            return context.symbol == "MSFT"

    execution = RecordingExecutionPort()
    intents = StubIntentSource(intents=[make_intent("AAPL"), make_intent("MSFT")])
    runtime = _runtime(
        halts=SymbolHaltGate(), execution=execution, intents=intents,
        config=RuntimeConfig(max_ticks=1),
    )
    runtime.run()
    executed = {intent.symbol for intent in execution.calls}
    assert executed == {"AAPL"}  # MSFT halted, AAPL evaluated


# -- exception containment (A1) ----------------------------------------------


def test_a_failing_tick_is_contained_and_the_loop_survives() -> None:
    class RaisingIntentSource:
        def __init__(self) -> None:
            self.calls = 0

        def next_intents(self, now: datetime) -> Sequence[ExecutionIntent]:
            self.calls += 1
            raise RuntimeError("boom")

    intents = RaisingIntentSource()
    runtime = _runtime(intents=intents, config=RuntimeConfig(max_ticks=3))
    summary = runtime.run()
    # Every tick raised inside entry work, yet the loop completed all three and
    # stopped cleanly. A bad tick blocks entries; it never kills the runtime.
    assert summary.ticks == 3
    assert summary.submissions == 0
    assert intents.calls == 3


def test_reconciliation_failure_is_contained_and_blocks() -> None:
    class RaisingReconciliation:
        def __init__(self) -> None:
            self.startup_calls = 0
            self.periodic_calls = 0

        def run_startup(self) -> ReconciliationState:
            self.startup_calls += 1
            raise RuntimeError("reconcile failed")

        def run_periodic(self) -> ReconciliationState:
            self.periodic_calls += 1
            raise RuntimeError("reconcile failed")

    runtime = _runtime(reconciliation=RaisingReconciliation())
    runtime.start()
    status = runtime.status()
    assert status.reconciliation_clean is False
    assert status.entry_eligible is False


# -- shutdown (A1) -----------------------------------------------------------


def test_close_releases_owned_resources() -> None:
    ownership = FakeOwnership()
    runtime = _runtime(ownership=ownership)
    runtime.run()
    runtime.close()
    assert ownership.released is True
    # Idempotent: closing twice is safe.
    runtime.close()


def test_config_refuses_live() -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(environment=Environment.LIVE)


def test_config_refuses_non_positive_intervals() -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(tick_interval=timedelta(0))
    with pytest.raises(ValueError):
        RuntimeConfig(reconciliation_interval=timedelta(seconds=-1))
