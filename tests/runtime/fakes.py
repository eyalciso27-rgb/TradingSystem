"""In-memory collaborators for deterministic runtime tests.

Every fake satisfies exactly one runtime port so the supervised loop can be
driven without a database, a broker or a gateway. The fakes record what the
runtime asked of them, so a test can assert behaviour (how many reconciliations
ran, whether release was called) rather than timing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from trading_application.emergency import EntryContext, HaltRecord
from trading_broker_core.port import OrderSide
from trading_execution_agent.__main__ import probe_decision
from trading_execution_agent.execution_service import ExecutionIntent
from trading_execution_agent.runtime.halts import HaltStoreUnavailableError
from trading_execution_agent.runtime.reconciliation_state import ReconciliationState

NOW = datetime(2026, 8, 31, 15, 0, tzinfo=UTC)


def make_intent(symbol: str = "AAPL", *, strategy_id: str = "test-strategy") -> ExecutionIntent:
    """A minimal, valid entry intent carrying a real EngineDecision."""
    decision = probe_decision(symbol, NOW)
    decision_id = str(decision.result.decision_id)
    return ExecutionIntent(
        strategy_id=strategy_id,
        decision=decision,
        symbol=symbol,
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
        correlation_id=decision_id,
        client_order_id=f"test-{decision_id}",
        source_id="test",
    )


def clean_state(*, session: str = "session-1") -> ReconciliationState:
    return ReconciliationState(
        clean=True,
        entry_blocked=False,
        unknown_positions=0,
        unknown_orders=0,
        session_token=session,
        checked_at=NOW,
        reason="CLEAN",
    )


def blocked_state(reason: str = "UNKNOWN") -> ReconciliationState:
    return ReconciliationState(
        clean=False,
        entry_blocked=True,
        unknown_positions=None,
        unknown_orders=None,
        session_token=None,
        checked_at=NOW,
        reason=reason,
    )


class FakeOwnership:
    """A single-instance ownership guard whose held-ness a test controls."""

    def __init__(self, *, acquired: bool = True, held: bool = True) -> None:
        self._acquired = acquired
        self.held = held
        self.acquire_calls = 0
        self.is_held_calls = 0
        self.released = False

    def try_acquire(self) -> bool:
        self.acquire_calls += 1
        return self._acquired

    def is_held(self) -> bool:
        self.is_held_calls += 1
        return self.held

    def release(self) -> None:
        self.released = True


class RaisingOwnership:
    """Ownership acquisition that fails to even reach the database."""

    def try_acquire(self) -> bool:
        raise RuntimeError("database unreachable")

    def is_held(self) -> bool:
        return False

    def release(self) -> None:
        return None


class FakeReconciliationDriver:
    """Returns preset states; records how often each entry point ran."""

    def __init__(
        self,
        startup: ReconciliationState,
        periodic: ReconciliationState | None = None,
    ) -> None:
        self._startup = startup
        self._periodic = periodic if periodic is not None else startup
        self.startup_calls = 0
        self.periodic_calls = 0

    def run_startup(self) -> ReconciliationState:
        self.startup_calls += 1
        return self._startup

    def run_periodic(self) -> ReconciliationState:
        self.periodic_calls += 1
        return self._periodic


class SequencedReconciliationDriver:
    """Yields a scripted sequence of states across successive runs."""

    def __init__(self, states: Sequence[ReconciliationState]) -> None:
        self._states = list(states)
        self._index = 0
        self.startup_calls = 0
        self.periodic_calls = 0

    def _next(self) -> ReconciliationState:
        state = self._states[min(self._index, len(self._states) - 1)]
        self._index += 1
        return state

    def run_startup(self) -> ReconciliationState:
        self.startup_calls += 1
        return self._next()

    def run_periodic(self) -> ReconciliationState:
        self.periodic_calls += 1
        return self._next()


class FakeHaltGate:
    """A halt view a test can flip; can also simulate a store failure."""

    def __init__(
        self,
        *,
        halts: Sequence[HaltRecord] = (),
        blocked: bool = False,
        store_down: bool = False,
    ) -> None:
        self._halts = tuple(halts)
        self.blocked = blocked
        self.store_down = store_down
        self.entry_checks = 0

    def active_halts(self) -> Sequence[HaltRecord]:
        if self.store_down:
            raise HaltStoreUnavailableError("halt store down")
        return self._halts

    def entry_halt_active(self, context: EntryContext) -> bool:
        self.entry_checks += 1
        if self.store_down:
            return True  # fail closed
        return self.blocked


class FakeConnectionFacts:
    """Connectivity facts a test controls (and can flip across ticks)."""

    def __init__(
        self, *, ibkr: bool | None = True, market_data: bool | None = True
    ) -> None:
        self.ibkr = ibkr
        self.market_data = market_data

    def ibkr_connected(self) -> bool | None:
        return self.ibkr

    def market_data_connected(self) -> bool | None:
        return self.market_data


@dataclass
class _Result:
    submitted: bool


class RecordingExecutionPort:
    """Records every execute() call. ``submitted`` mimics the port's answer.

    The default -- ``submitted=False`` -- is the DenyAll contract: an entry is
    evaluated and refused, nothing crosses the broker boundary.
    """

    def __init__(self, *, submitted: bool = False) -> None:
        self._submitted = submitted
        self.calls: list[ExecutionIntent] = []

    def execute(self, intent: ExecutionIntent) -> _Result:
        self.calls.append(intent)
        return _Result(self._submitted)


@dataclass
class StubIntentSource:
    """Supplies a fixed set of intents every tick (tests only)."""

    intents: Sequence[ExecutionIntent] = field(default_factory=tuple)
    calls: int = 0

    def next_intents(self, now: datetime) -> Sequence[ExecutionIntent]:
        del now
        self.calls += 1
        return tuple(self.intents)


class ScriptedWait:
    """A deterministic wait: returns the scripted stop-signals, never sleeps.

    Each call pops the next boolean (True = 'stop requested during the wait').
    When the script is exhausted it returns True, so a bounded run always ends.
    """

    def __init__(self, signals: Sequence[bool] = ()) -> None:
        self._signals = list(signals)
        self.calls = 0

    def __call__(self, _seconds: float) -> bool:
        self.calls += 1
        if self._signals:
            return self._signals.pop(0)
        return True
