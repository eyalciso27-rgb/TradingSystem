"""Autonomous-Paper runtime orchestration (Phase A).

The missing orchestration layer: a supervised loop that owns single-instance
ownership, startup/periodic reconciliation, the durable halt view and the tick
cadence, and drives the canonical ``ExecutionService`` -- which stays the ONLY
component that may submit. Phase A runs on DenyAll and submits nothing.
"""

from trading_execution_agent.runtime.service import (
    PaperRuntime,
    RuntimeConfig,
    RuntimeRunSummary,
)
from trading_execution_agent.runtime.states import (
    AuthorizationMode,
    RuntimeState,
    RuntimeStatus,
)

__all__ = [
    "AuthorizationMode",
    "PaperRuntime",
    "RuntimeConfig",
    "RuntimeRunSummary",
    "RuntimeState",
    "RuntimeStatus",
]
