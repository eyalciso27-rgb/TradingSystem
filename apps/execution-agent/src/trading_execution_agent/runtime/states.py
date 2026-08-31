"""Runtime lifecycle states and the machine-readable status (A6).

The state is DERIVED from facts, never set as a wish. ``RuntimeStatus`` carries
the facts the runtime can actually prove -- ownership, connectivity,
reconciliation, halt, authorization mode, the last successful tick and
reconciliation, and the count of positions/orders it could not attribute -- and
``derive_state`` maps those facts to exactly one lifecycle state. That ordering
matters: a state is a summary of the facts, so no code can report READY while a
fact says the runtime is blocked.

Phase A tops out at ``READY_DENYALL``: with the only production authorization
being DENY, an entry-eligible runtime is healthy but structurally unable to
submit. ``RUNNING`` is reserved for a future authorization mode and is
unreachable while the mode is DENYALL -- which is exactly the guarantee Phase A
must make visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RuntimeState(StrEnum):
    """The one lifecycle state the runtime is in, derived from its facts."""

    STARTING = "STARTING"
    RECONCILING = "RECONCILING"
    BLOCKED = "BLOCKED"
    READY_DENYALL = "READY_DENYALL"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class AuthorizationMode(StrEnum):
    """How the runtime is permitted to authorize submissions.

    Only ``DENYALL`` exists in Phase A. A future owner-gated Paper mode would add
    a second value; until then the runtime can never leave DENYALL, and the type
    makes that explicit rather than implicit.
    """

    DENYALL = "DENYALL"


class LifecyclePhase(StrEnum):
    """Where the supervised loop is in its own lifecycle (independent of gates).

    Distinct from :class:`RuntimeState`: the phase is what the loop is DOING
    (booting, ticking, stopping, dead), while the state also folds in whether an
    entry is currently permitted. ``derive_state`` combines the two.
    """

    INIT = "INIT"
    RECONCILING = "RECONCILING"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """Every fact the runtime can prove, plus the state derived from them.

    Booleans are three-valued in spirit: ``reconciliation_clean`` and the
    connectivity flags are ``None`` when the runtime has not yet been able to
    determine them, and an unknown is never treated as a pass.
    """

    phase: LifecyclePhase
    authorization_mode: AuthorizationMode
    ownership_proven: bool
    ibkr_connected: bool | None
    market_data_connected: bool | None
    reconciliation_clean: bool | None
    halt_active: bool
    unknown_positions: int | None
    unknown_orders: int | None
    last_tick_at: datetime | None
    last_reconciliation_at: datetime | None
    detail: str = ""

    @property
    def entry_eligible(self) -> bool:
        """Whether a NEW entry may even be ATTEMPTED this moment (fail closed).

        Every unknown counts against eligibility: ownership must be proven, both
        the broker and market-data sessions must be positively connected (not
        ``None``), reconciliation must be positively CLEAN (not ``None``), no
        halt may be active, and no position/order may be unattributed. A
        connection whose state is uncertain blocks new entries, exactly as the
        safety philosophy requires. This is the runtime's own gate; the
        execution path re-checks independently at the boundary.
        """
        if not self.ownership_proven:
            return False
        if self.ibkr_connected is not True:
            return False
        if self.market_data_connected is not True:
            return False
        if self.reconciliation_clean is not True:
            return False
        if self.halt_active:
            return False
        if self.unknown_positions is None or self.unknown_positions > 0:
            return False
        if self.unknown_orders is None or self.unknown_orders > 0:
            return False
        return True

    @property
    def state(self) -> RuntimeState:
        return derive_state(self)

    def as_dict(self) -> dict[str, object]:
        """Deterministic, sanitized mapping for the status CLI / health probe."""
        return {
            "state": self.state.value,
            "phase": self.phase.value,
            "authorization_mode": self.authorization_mode.value,
            "entry_eligible": self.entry_eligible,
            "ownership_proven": self.ownership_proven,
            "ibkr_connected": self.ibkr_connected,
            "market_data_connected": self.market_data_connected,
            "reconciliation_clean": self.reconciliation_clean,
            "halt_active": self.halt_active,
            "unknown_positions": self.unknown_positions,
            "unknown_orders": self.unknown_orders,
            "last_tick_at": _iso(self.last_tick_at),
            "last_reconciliation_at": _iso(self.last_reconciliation_at),
            "detail": self.detail,
        }


def derive_state(status: RuntimeStatus) -> RuntimeState:
    """Map facts to exactly one lifecycle state (deterministic, fail closed)."""
    phase = status.phase
    if phase is LifecyclePhase.FAILED:
        return RuntimeState.FAILED
    if phase is LifecyclePhase.STOPPING:
        return RuntimeState.STOPPING
    if phase is LifecyclePhase.INIT:
        return RuntimeState.STARTING
    if phase is LifecyclePhase.RECONCILING:
        return RuntimeState.RECONCILING
    # ACTIVE: eligibility decides READY vs BLOCKED; the authorization mode
    # decides READY_DENYALL vs RUNNING. Phase A never reaches RUNNING because
    # the mode is always DENYALL.
    if not status.entry_eligible:
        return RuntimeState.BLOCKED
    if status.authorization_mode is AuthorizationMode.DENYALL:
        return RuntimeState.READY_DENYALL
    return RuntimeState.RUNNING


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "AuthorizationMode",
    "LifecyclePhase",
    "RuntimeState",
    "RuntimeStatus",
    "derive_state",
]
