"""The narrow ports the runtime depends on (so it is testable in isolation).

The supervised loop is written against these Protocols, never against the IBKR
stack directly. Production wires the real collaborators (the advisory lock, the
account-safety-backed reconciliation driver, the durable halt source, the
canonical ``ExecutionService``); deterministic tests wire in-memory fakes. That
separation is what lets Phase A prove loop behaviour -- ownership, reconciliation
gating, halt response, zero submissions -- without a broker, a gateway or a
market feed.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading_application.emergency import EntryContext, HaltRecord

from trading_execution_agent.execution_service import ExecutionIntent
from trading_execution_agent.runtime.reconciliation_state import ReconciliationState


@runtime_checkable
class OwnershipGuard(Protocol):
    """Single-instance ownership. Fail closed: unproven ownership blocks work.

    ``AdvisoryLock`` satisfies this structurally. ``is_held`` must actively
    re-verify, never assume: a dropped connection means ownership was lost.
    """

    def try_acquire(self) -> bool: ...

    def is_held(self) -> bool: ...

    def release(self) -> None: ...


@runtime_checkable
class ReconciliationDriver(Protocol):
    """Drives startup / periodic reconciliation and returns the current state."""

    def run_startup(self) -> ReconciliationState: ...

    def run_periodic(self) -> ReconciliationState: ...


@runtime_checkable
class HaltGate(Protocol):
    """The durable halt/kill-switch view, loaded fresh -- never cached."""

    def active_halts(self) -> Sequence[HaltRecord]:
        """Every active halt, re-read from the durable store on every call."""
        ...

    def entry_halt_active(self, context: EntryContext) -> bool:
        """Whether an active halt blocks a NEW entry in ``context`` right now."""
        ...


@runtime_checkable
class ConnectionFacts(Protocol):
    """Observed connectivity of the broker and market-data sessions.

    Returns ``None`` when a fact is not yet known -- which the status treats as
    'not connected', never as connected. The live wiring lands in Phase B; Phase
    A uses an explicit provider so the fact is always sourced, never guessed.
    """

    def ibkr_connected(self) -> bool | None: ...

    def market_data_connected(self) -> bool | None: ...


@runtime_checkable
class IntentSource(Protocol):
    """Supplies the entry intents to evaluate this tick.

    In Phase A this is a null or a test stub only: no real strategy is wired,
    and the real Strategy -> EngineDecision -> ExecutionIntent source lands after
    the strategy branch merges.
    """

    def next_intents(self, now: datetime) -> Sequence[ExecutionIntent]: ...


class ExecutionResultView(Protocol):
    """The minimum the runtime reads back from an execution attempt."""

    @property
    def submitted(self) -> bool: ...


@runtime_checkable
class ExecutionPort(Protocol):
    """Evaluate-and-maybe-submit for ONE intent. The ONLY submission authority.

    ``ExecutionService.execute`` satisfies this. The runtime NEVER submits; it
    hands an intent to this port and reads back whether anything was submitted.
    With the DenyAll authority the port refuses every submission, which is what
    Phase A asserts.
    """

    def execute(self, intent: ExecutionIntent) -> ExecutionResultView: ...


__all__ = [
    "ConnectionFacts",
    "ExecutionPort",
    "ExecutionResultView",
    "HaltGate",
    "IntentSource",
    "OwnershipGuard",
    "ReconciliationDriver",
]
