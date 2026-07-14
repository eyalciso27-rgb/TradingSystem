"""Backtest engine (IMP-1202) — costs, fills and walk-forward over replay.

Authority: CLAUDE.md point-in-time/no-lookahead invariants; Policy
Parameters (execution policy per broker profile: order types, timeouts,
slippage, partial fills; estimated slippage baseline max(0.05%,
half_spread) — supplied AS POLICY INPUT, never hardcoded). Task scope:
costs, spread, slippage, fill assumptions and walk-forward splits.
DoD: look-ahead and leakage tests pass.

The engine is a CONSUMER injected into the IMP-1201 ``ReplayRunner`` — it
is not a parallel runner. It sees only (event, snapshot, as_of, manifest)
from the runner, so canonical ordering, as-of snapshots, exact-version
enforcement, dedup and the deterministic result hash all come from the
runner and cannot be bypassed. The engine never touches the raw dataset,
never reads a wall clock and keeps every financial number as ``Decimal``
(string-serialized in outputs).

Execution timing (no lookahead):

- A BAR event becomes knowable at its ``available_at`` (the bar CLOSE).
  Its open/high/low/close are never visible before that.
- An INTENT created at some ``available_at`` is eligible for fills only on
  bars with a STRICTLY LATER ``available_at`` — an intent formed on a
  bar's close can never be filled retroactively inside that same bar.
- Exits (protective stop / target) of a position filled during bar M are
  evaluated from bar M+1 onwards, for the same reason.

Same-bar ambiguity: when one bar touches BOTH the stop and the target and
no tick ordering exists, the outcome is decided by the explicit,
versioned ``same_bar_fill_policy`` — never by picking the more profitable
outcome. The safe default is ``STOP_FIRST`` (adverse-first). The policy
fingerprint is embedded in every step output, so changing it always
changes the replay ``result_hash``, and every ambiguous bar is recorded as
a finding in the report.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from trading_application.replay_runner import (
    Payload,
    ReplayEvent,
    ReplayRunManifest,
    ReplaySnapshot,
)

BACKTEST_VERSION = "backtest-engine-1.0.0"

_CENT = Decimal("0.0001")


class BacktestError(Exception):
    """Fail-closed backtest failure (bad policy/inputs, unsupported data)."""


class SameBarFillPolicy(StrEnum):
    """Deterministic resolution when stop AND target touch in one bar."""

    STOP_FIRST = "STOP_FIRST"  # conservative / adverse-first (default)
    TARGET_FIRST = "TARGET_FIRST"  # must be chosen explicitly


@dataclass(frozen=True, slots=True)
class BacktestCostPolicy:
    """All cost/fill assumptions — versioned configuration, nothing in code."""

    policy_version: str
    commission_per_share: Decimal
    minimum_commission: Decimal
    spread_half_pct: Decimal  # half-spread as a fraction of price
    slippage_pct: Decimal  # adverse slippage for market/stop executions
    max_participation_pct: Decimal  # fraction of bar volume fillable
    time_in_force_bars: int  # eligible bars before an intent expires
    same_bar_fill_policy: SameBarFillPolicy

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise BacktestError("policy_version is mandatory")
        for name in (
            "commission_per_share",
            "minimum_commission",
            "spread_half_pct",
            "slippage_pct",
        ):
            if getattr(self, name) < 0:
                raise BacktestError(f"{name} must be non-negative")
        if not Decimal("0") < self.max_participation_pct <= Decimal("1"):
            raise BacktestError("max_participation_pct must be in (0, 1]")
        if self.time_in_force_bars < 1:
            raise BacktestError("time_in_force_bars must be >= 1")

    def fingerprint(self) -> str:
        material = "|".join(
            [
                self.policy_version,
                str(self.commission_per_share),
                str(self.minimum_commission),
                str(self.spread_half_pct),
                str(self.slippage_pct),
                str(self.max_participation_pct),
                str(self.time_in_force_bars),
                self.same_bar_fill_policy.value,
            ]
        )
        return "costpolicy:" + hashlib.sha256(material.encode("ascii")).hexdigest()[:16]

    def commission(self, quantity: Decimal) -> Decimal:
        return max(self.minimum_commission, quantity * self.commission_per_share)

    def adverse(self, price: Decimal, side: str) -> Decimal:
        """Market/stop execution price after adverse spread+slippage."""
        factor = self.spread_half_pct + self.slippage_pct
        adjusted = price * (Decimal(1) + factor) if side == "BUY" else price * (Decimal(1) - factor)
        return adjusted.quantize(_CENT, rounding=ROUND_HALF_EVEN)


# -- walk-forward -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WalkForwardSplit:
    """Half-open windows: train [train_start, train_end), test [test_start, test_end)."""

    split_id: str
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    def __post_init__(self) -> None:
        for name in ("train_start", "train_end", "test_start", "test_end"):
            stamp: datetime = getattr(self, name)
            if stamp.tzinfo is None:
                raise BacktestError(f"{name} must be timezone-aware")
        if not (self.train_start < self.train_end <= self.test_start < self.test_end):
            raise BacktestError(
                f"split {self.split_id!r} invalid: require train_start < train_end"
                " <= test_start < test_end (test strictly after train; no overlap)"
            )


def validate_splits(splits: Sequence[WalkForwardSplit]) -> None:
    """Fail closed on overlapping test windows or unordered splits."""
    if not splits:
        raise BacktestError("at least one walk-forward split is required")
    previous_test_end: datetime | None = None
    for split in splits:
        if previous_test_end is not None and split.test_start < previous_test_end:
            raise BacktestError(
                f"split {split.split_id!r}: test windows overlap the previous split"
            )
        previous_test_end = split.test_end


def assert_no_leakage(split: WalkForwardSplit, learned_at: Iterable[datetime]) -> None:
    """Every learned parameter must originate strictly inside the TRAIN window."""
    for stamp in learned_at:
        if stamp.tzinfo is None:
            raise BacktestError("learned_at timestamps must be timezone-aware")
        if not (split.train_start <= stamp < split.train_end):
            raise BacktestError(
                f"LEAKAGE: parameter learned from data at {stamp.isoformat()} is"
                f" outside the train window of split {split.split_id!r}; fail closed"
            )


# -- internal simulation state -------------------------------------------------------


@dataclass(slots=True)
class _Bar:
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(slots=True)
class _PendingIntent:
    intent_id: str
    symbol: str
    side: str  # BUY | SELL
    order_type: str  # MARKET | LIMIT | STOP
    quantity: Decimal
    remaining: Decimal
    limit_price: Decimal | None
    stop_price: Decimal | None
    protective_stop: Decimal | None
    target: Decimal | None
    created_at: datetime
    bars_seen: int = 0


@dataclass(slots=True)
class _Position:
    intent_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    entry_commission: Decimal
    protective_stop: Decimal | None
    target: Decimal | None
    opened_at: datetime


@dataclass(slots=True)
class _ClosedTrade:
    intent_id: str
    symbol: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    exit_reason: str
    gross_pnl: Decimal
    costs: Decimal


@dataclass(frozen=True, slots=True)
class BacktestReport:
    """Human-facing summary; the canonical record is the replay steps."""

    policy_ref: str
    trades: int
    fills: int
    partial_fills: int
    unfilled: int
    expired: int
    gross_pnl: str
    total_costs: str
    net_pnl: str
    hit_rate: str
    max_drawdown: str
    ambiguous_bars: tuple[str, ...]
    findings: tuple[str, ...]


def _decimal(payload: Payload, key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BacktestError(f"payload[{key!r}] must be a string decimal")
    return Decimal(value)


class BacktestEngine:
    """ReplayRunner consumer simulating fills under a versioned cost policy."""

    def __init__(self, policy: BacktestCostPolicy) -> None:
        self._policy = policy
        self._pending: list[_PendingIntent] = []
        self._positions: list[_Position] = []
        self._closed: list[_ClosedTrade] = []
        self._fill_count = 0
        self._partial_count = 0
        self._unfilled_count = 0
        self._expired_count = 0
        self._ambiguous: list[str] = []
        self._findings: list[str] = []

    # -- the injected consumer ------------------------------------------------
    def consume(
        self,
        event: ReplayEvent,
        snapshot: ReplaySnapshot,
        as_of: datetime,
        manifest: ReplayRunManifest,
    ) -> Payload:
        del snapshot, manifest  # ordering/versions are the runner's concern
        notes: list[str] = []
        if event.kind == "INTENT":
            self._on_intent(event)
            notes.append(f"intent:{event.payload.get('intent_id')}")
        elif event.kind == "BAR":
            notes.extend(self._on_bar(event, as_of))
        elif event.kind == "CORPORATE_ACTION":
            # Never silently adjusted: unsupported in this task's scope.
            raise BacktestError(
                f"corporate action at {as_of.isoformat()} is not supported by the"
                " backtest engine; fail closed (no silent adjustment)"
            )
        else:
            raise BacktestError(f"unsupported event kind {event.kind!r}; fail closed")
        return {
            "policy": self._policy.fingerprint(),
            "notes": ";".join(notes) if notes else None,
        }

    # -- intents ----------------------------------------------------------------
    def _on_intent(self, event: ReplayEvent) -> None:
        payload = event.payload
        side = str(payload.get("side"))
        order_type = str(payload.get("order_type"))
        if side not in ("BUY", "SELL"):
            raise BacktestError(f"intent side {side!r} invalid")
        if order_type not in ("MARKET", "LIMIT", "STOP"):
            raise BacktestError(f"order_type {order_type!r} unsupported; fail closed")

        def optional(key: str) -> Decimal | None:
            raw = payload.get(key)
            return Decimal(str(raw)) if isinstance(raw, str) and raw.strip() else None

        self._pending.append(
            _PendingIntent(
                intent_id=str(payload.get("intent_id")),
                symbol=str(payload.get("symbol")),
                side=side,
                order_type=order_type,
                quantity=_decimal(payload, "quantity"),
                remaining=_decimal(payload, "quantity"),
                limit_price=optional("limit_price"),
                stop_price=optional("stop_price"),
                protective_stop=optional("protective_stop"),
                target=optional("target"),
                created_at=event.available_at,
            )
        )

    # -- bars ---------------------------------------------------------------------
    def _on_bar(self, event: ReplayEvent, as_of: datetime) -> list[str]:
        payload = event.payload
        bar = _Bar(
            symbol=str(payload.get("symbol")),
            open=_decimal(payload, "open"),
            high=_decimal(payload, "high"),
            low=_decimal(payload, "low"),
            close=_decimal(payload, "close"),
            volume=_decimal(payload, "volume"),
        )
        notes: list[str] = []
        notes.extend(self._process_exits(bar, as_of))
        notes.extend(self._process_entries(bar, as_of, event.available_at))
        return notes

    def _process_entries(
        self, bar: _Bar, as_of: datetime, bar_available_at: datetime
    ) -> list[str]:
        policy = self._policy
        notes: list[str] = []
        still_pending: list[_PendingIntent] = []
        for intent in self._pending:
            if intent.symbol != bar.symbol:
                still_pending.append(intent)
                continue
            # NO LOOKAHEAD / NO RETROACTIVE FILL: an intent formed at a bar
            # close is eligible only on strictly later bars.
            if bar_available_at <= intent.created_at:
                still_pending.append(intent)
                continue
            intent.bars_seen += 1
            base = self._entry_base_price(intent, bar)
            if base is None:
                if intent.bars_seen >= policy.time_in_force_bars:
                    self._expired_count += 1
                    self._unfilled_count += 1
                    notes.append(f"expired:{intent.intent_id}:remaining={intent.remaining}")
                else:
                    still_pending.append(intent)
                continue
            capacity = (bar.volume * policy.max_participation_pct).to_integral_value(
                rounding="ROUND_FLOOR"
            )
            fill_quantity = min(intent.remaining, capacity)
            if fill_quantity <= 0:
                if intent.bars_seen >= policy.time_in_force_bars:
                    self._expired_count += 1
                    self._unfilled_count += 1
                    notes.append(f"expired:{intent.intent_id}:remaining={intent.remaining}")
                else:
                    still_pending.append(intent)
                continue
            fill_price = (
                base
                if intent.order_type == "LIMIT"
                else policy.adverse(base, intent.side)
            )
            commission = policy.commission(fill_quantity)
            self._positions.append(
                _Position(
                    intent_id=intent.intent_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=fill_quantity,
                    entry_price=fill_price,
                    entry_commission=commission,
                    protective_stop=intent.protective_stop,
                    target=intent.target,
                    opened_at=as_of,
                )
            )
            self._fill_count += 1
            intent.remaining -= fill_quantity
            if intent.remaining > 0:
                self._partial_count += 1
                notes.append(
                    f"partial:{intent.intent_id}:{fill_quantity}@{fill_price}"
                    f":remaining={intent.remaining}"
                )
                if intent.bars_seen >= policy.time_in_force_bars:
                    self._expired_count += 1
                    notes.append(f"expired:{intent.intent_id}:remaining={intent.remaining}")
                else:
                    still_pending.append(intent)
            else:
                notes.append(f"fill:{intent.intent_id}:{fill_quantity}@{fill_price}")
        self._pending = still_pending
        return notes

    def _entry_base_price(self, intent: _PendingIntent, bar: _Bar) -> Decimal | None:
        if intent.order_type == "MARKET":
            return bar.open
        if intent.order_type == "LIMIT":
            limit = intent.limit_price
            if limit is None:
                raise BacktestError(f"LIMIT intent {intent.intent_id!r} without limit_price")
            if intent.side == "BUY":
                if bar.open <= limit:
                    return bar.open
                if bar.low <= limit:
                    return limit
                return None  # limit never crossed -> UNFILLED this bar
            if bar.open >= limit:
                return bar.open
            if bar.high >= limit:
                return limit
            return None
        # STOP entry
        stop = intent.stop_price
        if stop is None:
            raise BacktestError(f"STOP intent {intent.intent_id!r} without stop_price")
        if intent.side == "BUY":
            if bar.high >= stop:
                return max(bar.open, stop)
            return None
        if bar.low <= stop:
            return min(bar.open, stop)
        return None

    def _process_exits(self, bar: _Bar, as_of: datetime) -> list[str]:
        policy = self._policy
        notes: list[str] = []
        still_open: list[_Position] = []
        for position in self._positions:
            if position.symbol != bar.symbol or as_of <= position.opened_at:
                # Exits are evaluated from the bar AFTER the entry fill.
                still_open.append(position)
                continue
            long = position.side == "BUY"
            stop = position.protective_stop
            target = position.target
            stop_hit = stop is not None and (bar.low <= stop if long else bar.high >= stop)
            target_hit = target is not None and (
                bar.high >= target if long else bar.low <= target
            )
            if stop_hit and target_hit:
                # AMBIGUOUS BAR: resolved only by the versioned policy —
                # never by picking the profitable outcome. Recorded.
                self._ambiguous.append(f"{position.intent_id}@{as_of.isoformat()}")
                if policy.same_bar_fill_policy is SameBarFillPolicy.STOP_FIRST:
                    target_hit = False
                else:
                    stop_hit = False
            exit_side = "SELL" if long else "BUY"
            if stop_hit and stop is not None:
                trigger = min(bar.open, stop) if long else max(bar.open, stop)
                exit_price = policy.adverse(trigger, exit_side)
                notes.append(self._close(position, exit_price, "STOP", as_of))
            elif target_hit and target is not None:
                notes.append(self._close(position, target, "TARGET", as_of))
            else:
                still_open.append(position)
        self._positions = still_open
        return notes

    def _close(
        self, position: _Position, exit_price: Decimal, reason: str, as_of: datetime
    ) -> str:
        del as_of
        direction = Decimal(1) if position.side == "BUY" else Decimal(-1)
        gross = (exit_price - position.entry_price) * position.quantity * direction
        costs = position.entry_commission + self._policy.commission(position.quantity)
        self._closed.append(
            _ClosedTrade(
                intent_id=position.intent_id,
                symbol=position.symbol,
                side=position.side,
                quantity=position.quantity,
                entry_price=position.entry_price,
                exit_price=exit_price,
                exit_reason=reason,
                gross_pnl=gross,
                costs=costs,
            )
        )
        return f"exit:{position.intent_id}:{reason}:{position.quantity}@{exit_price}"

    # -- report ---------------------------------------------------------------------
    def report(self) -> BacktestReport:
        gross = sum((trade.gross_pnl for trade in self._closed), Decimal(0))
        costs = sum((trade.costs for trade in self._closed), Decimal(0))
        net = gross - costs
        wins = sum(1 for trade in self._closed if trade.gross_pnl - trade.costs > 0)
        hit_rate = (
            (Decimal(wins) / Decimal(len(self._closed))).quantize(_CENT)
            if self._closed
            else Decimal(0)
        )
        running = Decimal(0)
        peak = Decimal(0)
        drawdown = Decimal(0)
        for trade in self._closed:
            running += trade.gross_pnl - trade.costs
            peak = max(peak, running)
            drawdown = max(drawdown, peak - running)
        return BacktestReport(
            policy_ref=self._policy.fingerprint(),
            trades=len(self._closed),
            fills=self._fill_count,
            partial_fills=self._partial_count,
            unfilled=self._unfilled_count,
            expired=self._expired_count,
            gross_pnl=str(gross),
            total_costs=str(costs),
            net_pnl=str(net),
            hit_rate=str(hit_rate),
            max_drawdown=str(drawdown),
            ambiguous_bars=tuple(self._ambiguous),
            findings=tuple(self._findings),
        )


__all__ = [
    "BACKTEST_VERSION",
    "BacktestCostPolicy",
    "BacktestEngine",
    "BacktestError",
    "BacktestReport",
    "SameBarFillPolicy",
    "WalkForwardSplit",
    "assert_no_leakage",
    "validate_splits",
]
