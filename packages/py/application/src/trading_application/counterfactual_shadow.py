"""Counterfactual shadow (IMP-1204) — virtual outcomes for untaken candidates.

Authority: Master Architecture ("Opportunity funnel, rejected shadow"),
Decision Contract (SHADOW = hypothetical trade, measured without an
order), Data Model ("Counterfactual Shadow never changes a real
Order/Position and is never merged into account P&L"). Task scope: track
rejected/watch/manual candidates with virtual outcomes. DoD: no
counterfactual event affects live/paper state.

The tracker is a CONSUMER injected into the IMP-1201 ``ReplayRunner`` and
sees only ``(event, snapshot, as_of, manifest)`` over frozen events —
canonical ordering, as-of snapshots, exact versions, dedup and the
deterministic result hash all come from the runner and cannot be
bypassed. It follows the IMP-1202 execution-timing patterns without
depending on the backtest engine's state.

Absolute isolation (the DoD, enforced by construction):

- There is NO order intent, NO submit authorization, NO broker call and
  NO execution-agent import anywhere in this module. A MANUAL origin is a
  research classification of the candidate only — it never triggers a
  real manual action.
- The tracker holds no reference to any live or paper store; there is no
  injection point through which one could be mutated.
- Every fill, outcome and report is explicitly marked VIRTUAL/SHADOW
  (``counterfactual_only=True``, ``affects_live_state=False``,
  ``affects_paper_state=False``) so downstream consumers cannot mistake
  it for real state.

Point-in-time rules (no lookahead):

- A candidate opens for tracking at its ``decision_as_of`` (the event's
  ``available_at``). A virtual entry may come only from a price bar with
  a STRICTLY LATER ``available_at`` — never retroactively from the bar on
  which the decision itself was made.
- The virtual exit is at a FIXED, policy-declared horizon (the close of
  the Nth bar after the entry bar). There is no maximum-favorable-
  excursion or any other hindsight-optimal entry/exit selection.
- A limit that is never crossed within the entry window is UNFILLED; not
  enough bars to reach the horizon is an explicit EXPIRED/INCOMPLETE —
  nothing is silently dropped.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum

from trading_application.replay_runner import (
    Payload,
    ReplayEvent,
    ReplayRunManifest,
    ReplaySnapshot,
)

SHADOW_VERSION = "counterfactual-shadow-1.0.0"

_QUANT = Decimal("0.0001")


class ShadowError(Exception):
    """Fail-closed counterfactual-shadow failure (bad input, unsupported case)."""


def _require_aware(stamp: datetime, name: str) -> None:
    if stamp.tzinfo is None:
        raise ShadowError(f"{name} must be timezone-aware")


class ShadowOrigin(StrEnum):
    """Why the candidate was not executed — the population being shadowed."""

    REJECTED = "REJECTED"
    WATCH = "WATCH"
    MANUAL = "MANUAL"  # research classification only; never a real action


class VirtualOutcomeStatus(StrEnum):
    """Terminal report status of one shadowed candidate. All virtual."""

    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    UNFILLED = "UNFILLED"  # entry window elapsed without any virtual fill
    EXPIRED = "EXPIRED"  # data ended before the entry window elapsed
    INCOMPLETE = "INCOMPLETE"  # virtually filled but horizon never reached


@dataclass(frozen=True, slots=True)
class ShadowCandidate:
    """One untaken candidate to shadow. Frozen; attribution is mandatory."""

    candidate_id: str
    origin: ShadowOrigin
    reason_code: str
    attributed_engine: str
    attributed_version: str
    decision_as_of: datetime
    symbol: str
    direction: str  # BUY | SELL (of the hypothetical position)
    virtual_quantity: Decimal
    reference_price: Decimal
    virtual_limit_price: Decimal | None = None
    run_id: str = ""
    split_id: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ShadowError("candidate_id is mandatory")
        if not self.symbol.strip():
            raise ShadowError("symbol is mandatory")
        if self.direction not in ("BUY", "SELL"):
            raise ShadowError(f"direction {self.direction!r} invalid; use BUY or SELL")
        _require_aware(self.decision_as_of, "decision_as_of")
        # Attribution is mandatory for EVERY shadowed origin — a candidate
        # without reason/engine/version cannot be analyzed; no silent
        # fallback.
        if not self.reason_code.strip():
            raise ShadowError(
                f"candidate {self.candidate_id!r} ({self.origin}) lacks"
                " reason_code; fail closed (no silent fallback)"
            )
        if not self.attributed_engine.strip() or not self.attributed_version.strip():
            raise ShadowError(
                f"candidate {self.candidate_id!r} ({self.origin}) lacks"
                " engine/version attribution; fail closed (no silent fallback)"
            )
        if self.virtual_quantity <= 0:
            raise ShadowError("virtual_quantity must be positive")
        if self.reference_price <= 0:
            raise ShadowError("reference_price must be positive")

    def canonical(self) -> tuple[object, ...]:
        return (
            self.candidate_id,
            self.origin.value,
            self.reason_code,
            self.attributed_engine,
            self.attributed_version,
            self.decision_as_of.isoformat(),
            self.symbol,
            self.direction,
            str(self.virtual_quantity),
            str(self.reference_price),
            str(self.virtual_limit_price) if self.virtual_limit_price is not None else None,
            self.run_id,
            self.split_id,
        )


@dataclass(frozen=True, slots=True)
class ShadowPolicy:
    """All virtual-execution assumptions — frozen, versioned, explicit."""

    policy_version: str
    commission_per_share: Decimal
    minimum_commission: Decimal
    spread_half_pct: Decimal
    slippage_pct: Decimal
    entry_window_bars: int  # bars in which a virtual entry may occur
    horizon_bars: int  # FIXED exit: close of the Nth bar after entry
    allow_partial_fills: bool = False
    max_participation_pct: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise ShadowError("policy_version is mandatory")
        for name in (
            "commission_per_share",
            "minimum_commission",
            "spread_half_pct",
            "slippage_pct",
        ):
            if getattr(self, name) < 0:
                raise ShadowError(f"{name} must be non-negative")
        if self.entry_window_bars < 1:
            raise ShadowError("entry_window_bars must be >= 1")
        if self.horizon_bars < 1:
            raise ShadowError("horizon_bars must be >= 1")
        if not Decimal("0") < self.max_participation_pct <= Decimal("1"):
            raise ShadowError("max_participation_pct must be in (0, 1]")

    def fingerprint(self) -> str:
        material = "|".join(
            [
                self.policy_version,
                str(self.commission_per_share),
                str(self.minimum_commission),
                str(self.spread_half_pct),
                str(self.slippage_pct),
                str(self.entry_window_bars),
                str(self.horizon_bars),
                str(self.allow_partial_fills),
                str(self.max_participation_pct),
            ]
        )
        return "shadowpolicy:" + hashlib.sha256(material.encode("ascii")).hexdigest()[:16]

    def commission(self, quantity: Decimal) -> Decimal:
        return max(self.minimum_commission, quantity * self.commission_per_share)

    def adverse(self, price: Decimal, side: str) -> Decimal:
        """Virtual market execution price after adverse spread+slippage."""
        factor = self.spread_half_pct + self.slippage_pct
        adjusted = price * (Decimal(1) + factor) if side == "BUY" else price * (Decimal(1) - factor)
        return adjusted.quantize(_QUANT, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class VirtualFill:
    """A SHADOW fill — never a real one."""

    candidate_id: str
    quantity: Decimal
    price: Decimal
    filled_as_of: datetime
    counterfactual_only: bool = True

    def __post_init__(self) -> None:
        _require_aware(self.filled_as_of, "filled_as_of")
        if not self.counterfactual_only:
            raise ShadowError("VirtualFill must be counterfactual_only")


@dataclass(frozen=True, slots=True)
class VirtualOutcome:
    """The terminal virtual result of one shadowed candidate."""

    candidate_id: str
    origin: ShadowOrigin
    reason_code: str
    attributed_engine: str
    attributed_version: str
    status: VirtualOutcomeStatus
    filled_quantity: str
    virtual_entry: str | None
    virtual_exit: str | None
    gross_pnl: str
    costs: str
    net_pnl: str
    counterfactual_only: bool = True

    def __post_init__(self) -> None:
        if not self.counterfactual_only:
            raise ShadowError("VirtualOutcome must be counterfactual_only")


@dataclass(frozen=True, slots=True)
class ShadowGroup:
    """Aggregate for one (origin, reason, engine, version) attribution group."""

    origin: ShadowOrigin
    reason_code: str
    engine: str
    version: str
    candidates: int
    net_pnl: str


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """Virtual outcomes only. Conservation-checked; never live/paper state."""

    policy_ref: str
    total_tracked: int
    filled: int
    partial: int
    unfilled: int
    expired: int
    incomplete: int
    outcomes: tuple[VirtualOutcome, ...]
    groups: tuple[ShadowGroup, ...]
    findings: tuple[str, ...]
    counterfactual_only: bool = True
    affects_live_state: bool = False
    affects_paper_state: bool = False

    def __post_init__(self) -> None:
        if not self.counterfactual_only or self.affects_live_state or self.affects_paper_state:
            raise ShadowError("ShadowReport must be counterfactual-only")
        accounted = self.filled + self.partial + self.unfilled + self.expired + self.incomplete
        if self.total_tracked != accounted or self.total_tracked != len(self.outcomes):
            raise ShadowError(
                "conservation violated: total tracked != sum of reported outcomes"
            )


# -- internal tracking state ------------------------------------------------------------


@dataclass(slots=True)
class _Tracked:
    candidate: ShadowCandidate
    bars_seen: int = 0
    filled_quantity: Decimal = Decimal(0)
    entry_price: Decimal | None = None
    entry_as_of: datetime | None = None
    bars_after_fill: int = 0
    exit_price: Decimal | None = None
    entry_window_elapsed: bool = False
    fills: list[VirtualFill] = field(default_factory=list)


def _decimal(payload: Payload, key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ShadowError(f"payload[{key!r}] must be a string decimal")
    return Decimal(value)


def _text(payload: Payload, key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


class CounterfactualShadowTracker:
    """ReplayRunner consumer measuring untaken candidates virtually.

    Holds NO live or paper state and offers no injection point for one —
    the only inputs are frozen replay events and the frozen policy, and
    the only output is a virtual, explicitly marked report.
    """

    def __init__(self, policy: ShadowPolicy, *, run_id: str = "", split_id: str = "") -> None:
        self._policy = policy
        self._run_id = run_id
        self._split_id = split_id
        self._tracked: dict[str, _Tracked] = {}
        self._order: list[str] = []
        self._seen: dict[str, tuple[object, ...]] = {}
        self._findings: list[str] = []

    # -- the injected consumer ------------------------------------------------------
    def consume(
        self,
        event: ReplayEvent,
        snapshot: ReplaySnapshot,
        as_of: datetime,
        manifest: ReplayRunManifest,
    ) -> Payload:
        del snapshot, manifest  # ordering/versions are the runner's concern
        if event.kind == "SHADOW_CANDIDATE":
            note = self._on_candidate(event)
        elif event.kind == "BAR":
            note = ";".join(self._on_bar(event)) or "no-op"
        else:
            raise ShadowError(f"unsupported event kind {event.kind!r}; fail closed")
        return {
            "virtual": True,
            "policy": self._policy.fingerprint(),
            "as_of": as_of.isoformat(),
            "note": f"SHADOW:{note}",
        }

    # -- candidates -----------------------------------------------------------------
    def _on_candidate(self, event: ReplayEvent) -> str:
        payload = event.payload
        origin_raw = _text(payload, "origin")
        try:
            origin = ShadowOrigin(origin_raw)
        except ValueError:
            raise ShadowError(
                f"origin {origin_raw!r} is outside the shadowed population"
                " (REJECTED/WATCH/MANUAL); fail closed"
            ) from None
        limit_raw = _text(payload, "virtual_limit_price")
        candidate = ShadowCandidate(
            candidate_id=_text(payload, "candidate_id"),
            origin=origin,
            reason_code=_text(payload, "reason_code"),
            attributed_engine=_text(payload, "engine"),
            attributed_version=_text(payload, "version"),
            decision_as_of=event.available_at,
            symbol=_text(payload, "symbol"),
            direction=_text(payload, "direction"),
            virtual_quantity=_decimal(payload, "virtual_quantity"),
            reference_price=_decimal(payload, "reference_price"),
            virtual_limit_price=Decimal(limit_raw) if limit_raw.strip() else None,
            run_id=_text(payload, "run_id"),
            split_id=_text(payload, "split_id"),
        )
        return self.track(candidate)

    def track(self, candidate: ShadowCandidate) -> str:
        if (candidate.run_id, candidate.split_id) != (self._run_id, self._split_id):
            raise ShadowError(
                f"candidate {candidate.candidate_id!r} belongs to group"
                f" (run_id={candidate.run_id!r}, split_id={candidate.split_id!r})"
                f" but this tracker aggregates (run_id={self._run_id!r},"
                f" split_id={self._split_id!r}); replay runs and walk-forward"
                " splits never mix without explicit grouping; fail closed"
            )
        canonical = candidate.canonical()
        previous = self._seen.get(candidate.candidate_id)
        if previous is not None:
            if previous == canonical:
                return f"dedup:{candidate.candidate_id}"
            raise ShadowError(
                f"candidate {candidate.candidate_id!r} appears with different"
                " content; corrupt input — fail closed"
            )
        self._seen[candidate.candidate_id] = canonical
        self._tracked[candidate.candidate_id] = _Tracked(candidate=candidate)
        self._order.append(candidate.candidate_id)
        return f"tracking:{candidate.candidate_id}:{candidate.origin}"

    # -- bars --------------------------------------------------------------------------
    def _on_bar(self, event: ReplayEvent) -> list[str]:
        payload = event.payload
        symbol = _text(payload, "symbol")
        bar_open = _decimal(payload, "open")
        bar_high = _decimal(payload, "high")
        bar_low = _decimal(payload, "low")
        bar_close = _decimal(payload, "close")
        bar_volume = _decimal(payload, "volume")
        notes: list[str] = []
        for candidate_id in self._order:
            state = self._tracked[candidate_id]
            candidate = state.candidate
            if candidate.symbol != symbol or state.exit_price is not None:
                continue
            # NO LOOKAHEAD / NO RETROACTIVE FILL: only bars strictly after
            # the decision may interact with the candidate.
            if event.available_at <= candidate.decision_as_of:
                continue
            if state.entry_price is None and not state.entry_window_elapsed:
                notes.extend(
                    self._try_virtual_entry(
                        state, bar_open, bar_high, bar_low, bar_volume, event.available_at
                    )
                )
            elif state.entry_price is not None:
                # FIXED horizon: the exit bar is determined by count alone —
                # never by looking at which bar would have been profitable.
                state.bars_after_fill += 1
                if state.bars_after_fill >= self._policy.horizon_bars:
                    exit_side = "SELL" if candidate.direction == "BUY" else "BUY"
                    state.exit_price = self._policy.adverse(bar_close, exit_side)
                    notes.append(
                        f"virtual-exit:{candidate_id}:{state.filled_quantity}"
                        f"@{state.exit_price}"
                    )
        return notes

    def _try_virtual_entry(
        self,
        state: _Tracked,
        bar_open: Decimal,
        bar_high: Decimal,
        bar_low: Decimal,
        bar_volume: Decimal,
        bar_available_at: datetime,
    ) -> list[str]:
        policy = self._policy
        candidate = state.candidate
        state.bars_seen += 1
        base = self._entry_base_price(candidate, bar_open, bar_high, bar_low)
        notes: list[str] = []
        if base is not None:
            if policy.allow_partial_fills:
                capacity = (bar_volume * policy.max_participation_pct).to_integral_value(
                    rounding="ROUND_FLOOR"
                )
                quantity = min(candidate.virtual_quantity, capacity)
            else:
                quantity = candidate.virtual_quantity
            if quantity > 0:
                price = (
                    base
                    if candidate.virtual_limit_price is not None
                    else policy.adverse(base, candidate.direction)
                )
                state.entry_price = price
                state.entry_as_of = bar_available_at
                state.filled_quantity = quantity
                state.fills.append(
                    VirtualFill(
                        candidate_id=candidate.candidate_id,
                        quantity=quantity,
                        price=price,
                        filled_as_of=bar_available_at,
                    )
                )
                notes.append(f"virtual-fill:{candidate.candidate_id}:{quantity}@{price}")
                return notes
        if state.bars_seen >= policy.entry_window_bars:
            state.entry_window_elapsed = True
            notes.append(f"virtual-unfilled:{candidate.candidate_id}")
        return notes

    def _entry_base_price(
        self,
        candidate: ShadowCandidate,
        bar_open: Decimal,
        bar_high: Decimal,
        bar_low: Decimal,
    ) -> Decimal | None:
        limit = candidate.virtual_limit_price
        if limit is None:
            return bar_open  # virtual market entry at next bar's open
        # Virtual limit: filled at limit-or-better, otherwise not this bar.
        if candidate.direction == "BUY":
            if bar_open <= limit:
                return bar_open
            if bar_low <= limit:
                return limit
            return None
        if bar_open >= limit:
            return bar_open
        if bar_high >= limit:
            return limit
        return None

    # -- report -----------------------------------------------------------------------
    def report(self) -> ShadowReport:
        outcomes: list[VirtualOutcome] = []
        counts = dict.fromkeys(VirtualOutcomeStatus, 0)
        findings = list(self._findings)
        for candidate_id in self._order:
            outcome = self._finalize(self._tracked[candidate_id], findings)
            counts[outcome.status] += 1
            outcomes.append(outcome)

        grouped: dict[tuple[ShadowOrigin, str, str, str], list[VirtualOutcome]] = {}
        for outcome in outcomes:
            key = (
                outcome.origin,
                outcome.reason_code,
                outcome.attributed_engine,
                outcome.attributed_version,
            )
            grouped.setdefault(key, []).append(outcome)
        groups = tuple(
            ShadowGroup(
                origin=key[0],
                reason_code=key[1],
                engine=key[2],
                version=key[3],
                candidates=len(members),
                net_pnl=str(sum((Decimal(m.net_pnl) for m in members), Decimal(0))),
            )
            for key, members in sorted(
                grouped.items(), key=lambda item: tuple(str(part) for part in item[0])
            )
        )
        return ShadowReport(
            policy_ref=self._policy.fingerprint(),
            total_tracked=len(outcomes),
            filled=counts[VirtualOutcomeStatus.FILLED],
            partial=counts[VirtualOutcomeStatus.PARTIAL],
            unfilled=counts[VirtualOutcomeStatus.UNFILLED],
            expired=counts[VirtualOutcomeStatus.EXPIRED],
            incomplete=counts[VirtualOutcomeStatus.INCOMPLETE],
            outcomes=tuple(outcomes),
            groups=groups,
            findings=tuple(findings),
        )

    def _finalize(self, state: _Tracked, findings: list[str]) -> VirtualOutcome:
        candidate = state.candidate
        policy = self._policy

        def outcome(
            status: VirtualOutcomeStatus,
            *,
            entry: Decimal | None = None,
            exit_price: Decimal | None = None,
            quantity: Decimal = Decimal(0),
            gross: Decimal = Decimal(0),
            costs: Decimal = Decimal(0),
        ) -> VirtualOutcome:
            return VirtualOutcome(
                candidate_id=candidate.candidate_id,
                origin=candidate.origin,
                reason_code=candidate.reason_code,
                attributed_engine=candidate.attributed_engine,
                attributed_version=candidate.attributed_version,
                status=status,
                filled_quantity=str(quantity),
                virtual_entry=str(entry) if entry is not None else None,
                virtual_exit=str(exit_price) if exit_price is not None else None,
                gross_pnl=str(gross),
                costs=str(costs),
                net_pnl=str(gross - costs),
            )

        if state.entry_price is None:
            if state.entry_window_elapsed:
                return outcome(VirtualOutcomeStatus.UNFILLED)
            findings.append(
                f"{candidate.candidate_id}: data ended before the entry window"
                " elapsed; reported EXPIRED, not dropped"
            )
            return outcome(VirtualOutcomeStatus.EXPIRED)

        entry_costs = policy.commission(state.filled_quantity)
        if state.exit_price is None:
            findings.append(
                f"{candidate.candidate_id}: virtually filled but the fixed"
                f" horizon ({policy.horizon_bars} bars) was never reached;"
                " reported INCOMPLETE, not dropped"
            )
            return outcome(
                VirtualOutcomeStatus.INCOMPLETE,
                entry=state.entry_price,
                quantity=state.filled_quantity,
                costs=entry_costs,
            )

        direction = Decimal(1) if candidate.direction == "BUY" else Decimal(-1)
        gross = (state.exit_price - state.entry_price) * state.filled_quantity * direction
        costs = entry_costs + policy.commission(state.filled_quantity)
        status = (
            VirtualOutcomeStatus.FILLED
            if state.filled_quantity == candidate.virtual_quantity
            else VirtualOutcomeStatus.PARTIAL
        )
        return outcome(
            status,
            entry=state.entry_price,
            exit_price=state.exit_price,
            quantity=state.filled_quantity,
            gross=gross,
            costs=costs,
        )


__all__ = [
    "SHADOW_VERSION",
    "CounterfactualShadowTracker",
    "ShadowCandidate",
    "ShadowError",
    "ShadowGroup",
    "ShadowOrigin",
    "ShadowPolicy",
    "ShadowReport",
    "VirtualFill",
    "VirtualOutcome",
    "VirtualOutcomeStatus",
]
