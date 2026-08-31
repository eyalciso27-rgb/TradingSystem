"""Strategy-driven exit DECISIONS for baseline-trend-breakout-v1 (mission §8).

Two exit concepts are kept strictly separate (mission section 8):

* the mandatory protective STOP (broker-resident; PR #94 owns its mechanics),
  which this module never touches; and
* the STRATEGY exit — the strategy deciding that an EXISTING position should be
  exited or its protection tightened because an owner-frozen exit rule fired.

This module implements ONLY the decision/policy half of the strategy exit. It
emits a canonical :class:`~trading_application.position_coordinator.ExitTrigger`
that the existing Position Coordinator (the single writer) turns into a coherent
action; it never places, modifies or cancels a broker order and never adds OCO /
take-profit / trailing ORDER mechanics. The owner-frozen rules realized here:

* ``exit.invalidation`` — failed breakout: within the first N sessions after
  entry, a completed RTH close below the original breakout resistance => EXIT.
* ``exit.time_exit`` — if after M sessions the trade has never reached +1R =>
  EXIT; reaching +1R permanently disables the time exit.
* ``exit.target_rule`` — no fixed target; once +1R is reached, a trailing stop
  ``HighestCloseSinceEntry - k*ATR20`` that may only tighten, never widen.

Deterministic and point-in-time: same frozen position + completed-bar facts +
policy + as_of => identical decision. ``+1R`` is defined on a COMPLETED daily
close (an intraday touch is never sufficient), matching the frozen record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.enums import ExitTriggerType

from trading_application.position_coordinator import ExitTrigger

BASELINE_EXIT_VERSION = "baseline-exit-1.0.0"


class BaselineExitError(Exception):
    """Fail-closed exit-decision failure (bad inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class BaselineExitPolicy:
    """Owner-frozen exit windows/multiples — configuration input, no defaults.

    Provenance (baseline-trend-breakout-v1): ``exit.invalidation`` "first 3
    trading sessions" -> ``failed_breakout_sessions``; ``exit.time_exit`` "20
    trading sessions" -> ``time_exit_sessions``; ``exit.target_rule``
    "HighestCloseSinceEntry - 2.5 * ATR20" -> ``trail_atr_multiple``.
    """

    policy_version: str
    failed_breakout_sessions: int
    time_exit_sessions: int
    trail_atr_multiple: Decimal

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise BaselineExitError("policy_version is mandatory")
        if self.failed_breakout_sessions < 1 or self.time_exit_sessions < 1:
            raise BaselineExitError("session windows must be positive")
        if self.trail_atr_multiple <= 0:
            raise BaselineExitError("trail_atr_multiple must be positive")


@dataclass(frozen=True, slots=True)
class BaselineOpenPosition:
    """Frozen state of one open baseline position (from position tracking).

    ``initial_stop`` is the entry-time structural stop; ``current_stop`` is the
    protective stop in force now (>= initial_stop, since it only tightens).
    ``sessions_since_entry`` counts COMPLETED RTH sessions after entry, and
    ``reached_1r`` records whether a completed close has ever met +1R.
    """

    position_ref: str
    symbol: str
    entry_price: Decimal
    initial_stop: Decimal
    breakout_resistance: Decimal
    current_stop: Decimal
    sessions_since_entry: int
    highest_close_since_entry: Decimal
    reached_1r: bool

    def __post_init__(self) -> None:
        if not self.position_ref.strip():
            raise BaselineExitError("position_ref is mandatory")
        if self.entry_price <= 0 or self.initial_stop <= 0:
            raise BaselineExitError("entry and initial stop must be positive")
        if self.initial_stop >= self.entry_price:
            raise BaselineExitError("initial stop must be below entry (long)")
        if self.current_stop < self.initial_stop:
            raise BaselineExitError("current stop can never be below the initial stop")
        if self.sessions_since_entry < 0:
            raise BaselineExitError("sessions_since_entry must be non-negative")

    @property
    def initial_risk_per_share(self) -> Decimal:
        return self.entry_price - self.initial_stop

    @property
    def one_r_price(self) -> Decimal:
        return self.entry_price + self.initial_risk_per_share


class BaselineExitAction:
    """Namespace of the exit decision actions (kept as plain string tokens)."""

    HOLD = "HOLD"
    EXIT_FAILED_BREAKOUT = "EXIT_FAILED_BREAKOUT"
    EXIT_TIME = "EXIT_TIME"
    ADJUST_TRAILING_STOP = "ADJUST_TRAILING_STOP"


@dataclass(frozen=True, slots=True)
class BaselineExitDecision:
    """The strategy exit decision + the trigger it raises (never an order).

    ``exit_trigger_type`` is set for a full strategy exit or a trailing-stop
    adjustment; ``new_stop_price`` carries the (only-tightening) trailing stop
    for the downstream position manager to apply. ``reached_1r`` reflects the
    +1R state AFTER this decision (it can only latch on).
    """

    position_ref: str
    action: str
    exit_trigger_type: ExitTriggerType | None
    new_stop_price: Decimal | None
    reached_1r: bool
    reason_codes: tuple[str, ...]
    version: str = BASELINE_EXIT_VERSION

    def to_exit_trigger(self, *, trigger_id: str, observed_at: datetime) -> ExitTrigger | None:
        """Canonical Position-Coordinator trigger for this decision, or None.

        A full exit raises a full-exit trigger (``reduce_quantity=None``); a
        trailing adjustment raises a ``TRAILING_POLICY`` trigger, which the
        coordinator resolves to ADJUST_PROTECTION. HOLD raises nothing.
        """
        if self.exit_trigger_type is None:
            return None
        return ExitTrigger(
            trigger_id=trigger_id,
            trigger_type=self.exit_trigger_type,
            position_ref=self.position_ref,
            observed_at=observed_at,
            reduce_quantity=None,
            source_ref=f"baseline-exit:{self.action}",
        )


class BaselineExitEvaluator:
    """Deterministic strategy-exit decision over frozen position + bar facts."""

    def __init__(self, policy: BaselineExitPolicy) -> None:
        self._policy = policy

    def evaluate(
        self,
        position: BaselineOpenPosition,
        *,
        latest_completed_close: Decimal,
        atr_long: Decimal,
        as_of: datetime,
    ) -> BaselineExitDecision:
        if as_of.tzinfo is None:
            raise BaselineExitError("as_of must be timezone-aware")
        if latest_completed_close <= 0 or atr_long <= 0:
            raise BaselineExitError("latest close and ATR must be positive")

        policy = self._policy
        reasons: list[str] = []

        # +1R latches on a COMPLETED daily close (never an intraday touch).
        reached_1r = position.reached_1r or latest_completed_close >= position.one_r_price
        if reached_1r and not position.reached_1r:
            reasons.append("REACHED_1R")

        # -- failed breakout (highest priority full exit) ---------------------
        within_failed_window = 1 <= position.sessions_since_entry <= policy.failed_breakout_sessions
        if within_failed_window and latest_completed_close < position.breakout_resistance:
            reasons.append("FAILED_BREAKOUT_CLOSE_BELOW_RESISTANCE")
            return BaselineExitDecision(
                position_ref=position.position_ref,
                action=BaselineExitAction.EXIT_FAILED_BREAKOUT,
                exit_trigger_type=ExitTriggerType.THESIS_INVALIDATED,
                new_stop_price=None,
                reached_1r=reached_1r,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )

        # -- time exit (only while +1R was never reached) ---------------------
        if not reached_1r and position.sessions_since_entry >= policy.time_exit_sessions:
            reasons.append("TIME_EXIT_NO_1R_BY_DEADLINE")
            return BaselineExitDecision(
                position_ref=position.position_ref,
                action=BaselineExitAction.EXIT_TIME,
                exit_trigger_type=ExitTriggerType.TIME_EXIT,
                new_stop_price=None,
                reached_1r=reached_1r,
                reason_codes=tuple(dict.fromkeys(reasons)),
            )

        # -- trailing stop after +1R (tighten only, never widen) --------------
        if reached_1r:
            trailing = position.highest_close_since_entry - policy.trail_atr_multiple * atr_long
            new_stop = max(position.current_stop, trailing)
            if new_stop > position.current_stop:
                reasons.append("TRAILING_STOP_TIGHTENED")
                return BaselineExitDecision(
                    position_ref=position.position_ref,
                    action=BaselineExitAction.ADJUST_TRAILING_STOP,
                    exit_trigger_type=ExitTriggerType.TRAILING_POLICY,
                    new_stop_price=new_stop,
                    reached_1r=reached_1r,
                    reason_codes=tuple(dict.fromkeys(reasons)),
                )
            reasons.append("TRAILING_STOP_UNCHANGED")

        # -- otherwise hold: protection stays exactly as it is ----------------
        if not reasons:
            reasons.append("NO_STRATEGY_EXIT_CONDITION")
        return BaselineExitDecision(
            position_ref=position.position_ref,
            action=BaselineExitAction.HOLD,
            exit_trigger_type=None,
            new_stop_price=None,
            reached_1r=reached_1r,
            reason_codes=tuple(dict.fromkeys(reasons)),
        )


__all__ = [
    "BASELINE_EXIT_VERSION",
    "BaselineExitAction",
    "BaselineExitDecision",
    "BaselineExitError",
    "BaselineExitEvaluator",
    "BaselineExitPolicy",
    "BaselineOpenPosition",
]
