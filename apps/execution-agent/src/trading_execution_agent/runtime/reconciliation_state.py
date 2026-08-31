"""The value a reconciliation run returns to the runtime.

Kept in its own module so both the driver and the ports can depend on it without
a cycle. It is a snapshot of what the last reconciliation could prove -- never a
promise. ``entry_permitted`` is True only on a positively CLEAN result with no
unattributed exposure; every unknown fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ReconciliationState:
    """What one reconciliation run proved about the account, right now."""

    #: True only when account safety was proven CLEAN for the CURRENT broker
    #: session AND the entry latch is clear. Any UNKNOWN / NOT_CLEAN is False.
    clean: bool
    #: The entry latch's own verdict, restated so the runtime and the execution
    #: path can be shown to agree. ``entry_blocked`` fails closed.
    entry_blocked: bool
    #: Non-zero positions the runtime could not attribute to a known owner. On a
    #: fresh paper account any open position is unattributed until the strategy
    #: branch supplies an owner, so a non-zero count blocks new entries. ``None``
    #: means the count could not even be determined -- also blocking.
    unknown_positions: int | None
    #: Working orders the runtime could not attribute. Same fail-closed rule.
    unknown_orders: int | None
    #: The current broker-session token the proof was taken against, if known.
    session_token: str | None
    checked_at: datetime
    #: Sanitized reason, safe to log/print. Never an account id or a secret.
    reason: str = ""

    @property
    def entry_permitted(self) -> bool:
        """Whether reconciliation permits a NEW entry (fail closed on unknowns)."""
        if not self.clean or self.entry_blocked:
            return False
        if self.unknown_positions is None or self.unknown_positions > 0:
            return False
        if self.unknown_orders is None or self.unknown_orders > 0:
            return False
        return True


__all__ = ["ReconciliationState"]
