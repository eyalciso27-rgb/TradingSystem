"""Entry-intent sources for the runtime (A1).

Phase A wires no real strategy. ``NullIntentSource`` -- the production default --
supplies nothing, so the loop runs, gates, reconciles and reports without ever
attempting an entry. The real Strategy -> EngineDecision -> ExecutionIntent
source is owned by the strategy branch and is wired in Phase B.

A deterministic test stub lives in the test suite, not here, so no stub can be
wired into a production process by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading_execution_agent.execution_service import ExecutionIntent


class NullIntentSource:
    """Supplies no intents. The safe Phase A default: nothing to enter."""

    def next_intents(self, now: datetime) -> Sequence[ExecutionIntent]:
        del now
        return ()


__all__ = ["NullIntentSource"]
