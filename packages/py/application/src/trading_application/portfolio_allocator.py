"""Portfolio allocator (IMP-0905).

Authority: Portfolio_Capital_Allocation_and_Market_Regime_v0.1 section 3
(each strategy has a risk-budget share and max positions; the Portfolio
Engine may REDUCE quantity but never increase beyond the Risk Engine; unused
budget is not auto-transferred), section 4 (two symbols in one correlation
cluster -> reduce or reject; same news cluster is not independent
confirmation), section 8 (portfolio pre-check: existing positions ->
strategy budget -> symbol/sector/theme/correlation caps -> portfolio action
ALLOW | REDUCE | MANUAL | REJECT) and section 10 (no two independent
positions per symbol in the MVP; every quantity reduction is explained and
policy-versioned); Policy_Parameters section 22 (strategy_risk_budget_share,
same_symbol_multiple_strategies DISABLED, correlation_cluster_open_risk_cap).

Deterministic, pure allocation over FROZEN inputs (ADR-0014): a batch of
``AllocationCandidate`` (each carrying the IMP-0904 ``RiskResult`` as the
upper bound), the portfolio state fixture and a ``PortfolioAllocationPolicy``.
Output is an ``AllocationPlan`` only — never an Order/OrderIntent/broker
action and never a submission.

Hard rules:

- A Risk **VETO** never receives an allocation (REJECT).
- The Risk **REDUCE/APPROVE** quantity is the UPPER BOUND — the allocator
  only reduces, never increases, and rounds DOWN.
- Every cap (portfolio risk, per-symbol/sector/theme notional, strategy /
  source / correlation-cluster risk shares, max concurrent positions) is a
  policy value; nothing is invented in code. Missing/invalid policy or
  portfolio equity fails closed.
- Parallel candidates are ranked deterministically (descending priority
  score, then candidate id) — never incidental list/dict order — so the same
  inputs + policy + as_of always produce the same allocation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from trading_contracts.enums import PortfolioAction, RiskDecisionOutcome

from trading_application.risk_engine import RiskResult

ALLOCATOR_VERSION = "portfolio-allocator-1.0.0"


class PortfolioAllocatorError(Exception):
    """Fail-closed allocator failure (bad policy/inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class PortfolioAllocationPolicy:
    """All caps and rules — configuration input, nothing in code."""

    policy_version: str
    max_total_open_risk_pct: Decimal
    max_symbol_notional_pct: Decimal
    max_sector_notional_pct: Decimal
    max_theme_notional_pct: Decimal
    max_strategy_risk_share: Decimal  # of the open-risk envelope
    max_source_risk_share: Decimal
    max_cluster_risk_share: Decimal
    max_concurrent_positions: int
    insufficient_capacity_action: PortfolioAction  # REDUCE-to-zero -> this

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise PortfolioAllocatorError("policy_version is mandatory")
        fractions = (
            self.max_total_open_risk_pct,
            self.max_symbol_notional_pct,
            self.max_sector_notional_pct,
            self.max_theme_notional_pct,
            self.max_strategy_risk_share,
            self.max_source_risk_share,
            self.max_cluster_risk_share,
        )
        if any(fraction <= 0 for fraction in fractions):
            raise PortfolioAllocatorError("all cap fractions must be positive")
        if self.max_concurrent_positions < 1:
            raise PortfolioAllocatorError("max_concurrent_positions must be >= 1")
        if self.insufficient_capacity_action not in (
            PortfolioAction.REJECT,
            PortfolioAction.MANUAL,
        ):
            raise PortfolioAllocatorError(
                "insufficient_capacity_action must be REJECT or MANUAL (never ALLOW)"
            )


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    """One existing open position (portfolio fixture)."""

    symbol: str
    sector: str
    theme: str
    cluster: str
    source_id: str
    strategy_id: str
    open_risk_amount: Decimal
    notional: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Frozen portfolio snapshot (fixture)."""

    risk_budget_equity: Decimal | None
    open_positions: tuple[PortfolioPosition, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    """One risk-approved candidate to be portfolio-checked (never an order)."""

    candidate_id: str
    risk_result: RiskResult
    symbol: str
    sector: str
    theme: str
    cluster: str
    source_id: str
    strategy_id: str
    entry_price: Decimal
    per_share_risk: Decimal
    priority_score: Decimal  # upstream fusion priority (input, not invented)

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise PortfolioAllocatorError("candidate_id is mandatory")
        if self.entry_price <= 0 or self.per_share_risk <= 0:
            raise PortfolioAllocatorError("entry_price and per_share_risk must be positive")


@dataclass(frozen=True, slots=True)
class CapCheck:
    name: str
    limit: str
    observed: str
    effect: str  # "PASS" | "REDUCE" | "REJECT"


@dataclass(frozen=True, slots=True)
class CandidateAllocation:
    """The portfolio decision for one candidate + full explanation."""

    candidate_id: str
    action: PortfolioAction
    requested_quantity: Decimal
    allocated_quantity: Decimal
    allocated_risk: Decimal
    allocated_notional: Decimal
    decision_ref: str
    risk_ref: str
    policy_ref: str
    caps_evaluated: tuple[CapCheck, ...]
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    """Canonical allocation plan for a batch — never an order."""

    as_of: datetime
    policy_ref: str
    allocator_version: str
    allocations: tuple[CandidateAllocation, ...]


@dataclass
class _Running:
    """Mutable running exposure tallies while allocating a batch."""

    total_risk: Decimal
    by_sector_notional: dict[str, Decimal] = field(default_factory=dict)
    by_theme_notional: dict[str, Decimal] = field(default_factory=dict)
    by_symbol_notional: dict[str, Decimal] = field(default_factory=dict)
    by_strategy_risk: dict[str, Decimal] = field(default_factory=dict)
    by_source_risk: dict[str, Decimal] = field(default_factory=dict)
    by_cluster_risk: dict[str, Decimal] = field(default_factory=dict)
    symbols: set[str] = field(default_factory=set)
    position_count: int = 0


def _floor(value: Decimal) -> Decimal:
    return value.to_integral_value(rounding=ROUND_FLOOR)


class PortfolioAllocator:
    """Deterministic portfolio allocator over frozen candidates."""

    def __init__(self, policy: PortfolioAllocationPolicy) -> None:
        self._policy = policy

    def allocate(
        self,
        candidates: Sequence[AllocationCandidate],
        state: PortfolioState,
        *,
        as_of: datetime,
    ) -> AllocationPlan:
        if as_of.tzinfo is None:
            raise PortfolioAllocatorError("as_of must be timezone-aware")
        if state.observed_at > as_of:
            raise PortfolioAllocatorError("lookahead: portfolio state is newer than as_of")
        equity = state.risk_budget_equity
        if equity is None or equity <= 0:
            raise PortfolioAllocatorError(
                "portfolio risk_budget_equity missing/invalid (fail closed)"
            )
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise PortfolioAllocatorError("duplicate candidate_id in batch")

        policy = self._policy
        envelope = policy.max_total_open_risk_pct * equity
        running = self._seed_running(state)

        # Deterministic ranking: highest priority first, candidate_id tie-break.
        ordered = sorted(
            candidates, key=lambda c: (-c.priority_score, c.candidate_id)
        )

        allocations: list[CandidateAllocation] = []
        for candidate in ordered:
            allocations.append(
                self._allocate_one(candidate, equity, envelope, running)
            )
        # Return in the deterministic ranked order (stable, explainable).
        return AllocationPlan(
            as_of=as_of,
            policy_ref=f"portfolio-policy:{policy.policy_version}",
            allocator_version=ALLOCATOR_VERSION,
            allocations=tuple(allocations),
        )

    def _seed_running(self, state: PortfolioState) -> _Running:
        running = _Running(
            total_risk=sum(
                (p.open_risk_amount for p in state.open_positions), Decimal(0)
            )
        )
        for position in state.open_positions:
            running.by_sector_notional[position.sector] = (
                running.by_sector_notional.get(position.sector, Decimal(0)) + position.notional
            )
            running.by_theme_notional[position.theme] = (
                running.by_theme_notional.get(position.theme, Decimal(0)) + position.notional
            )
            running.by_symbol_notional[position.symbol] = (
                running.by_symbol_notional.get(position.symbol, Decimal(0)) + position.notional
            )
            running.by_strategy_risk[position.strategy_id] = (
                running.by_strategy_risk.get(position.strategy_id, Decimal(0))
                + position.open_risk_amount
            )
            running.by_source_risk[position.source_id] = (
                running.by_source_risk.get(position.source_id, Decimal(0))
                + position.open_risk_amount
            )
            running.by_cluster_risk[position.cluster] = (
                running.by_cluster_risk.get(position.cluster, Decimal(0))
                + position.open_risk_amount
            )
            running.symbols.add(position.symbol)
            running.position_count += 1
        return running

    def _allocate_one(
        self,
        candidate: AllocationCandidate,
        equity: Decimal,
        envelope: Decimal,
        running: _Running,
    ) -> CandidateAllocation:
        policy = self._policy
        risk = candidate.risk_result
        requested = risk.approved_quantity
        caps: list[CapCheck] = []
        reasons: list[str] = []

        def finish(
            action: PortfolioAction, quantity: Decimal, extra_reason: str | None = None
        ) -> CandidateAllocation:
            if extra_reason:
                reasons.append(extra_reason)
            allocated_risk = quantity * candidate.per_share_risk
            allocated_notional = quantity * candidate.entry_price
            if quantity > 0:
                self._commit(candidate, quantity, allocated_risk, allocated_notional, running)
            return CandidateAllocation(
                candidate_id=candidate.candidate_id,
                action=action,
                requested_quantity=requested,
                allocated_quantity=quantity,
                allocated_risk=allocated_risk,
                allocated_notional=allocated_notional,
                decision_ref=risk.decision_ref,
                risk_ref=f"risk:{risk.policy_ref}:{risk.outcome.value}",
                policy_ref=f"portfolio-policy:{policy.policy_version}",
                caps_evaluated=tuple(caps),
                reason_codes=tuple(dict.fromkeys(reasons)),
                input_refs=tuple(risk.input_refs),
            )

        # -- risk veto is absolute (never allocated) ---------------------------
        if risk.outcome is RiskDecisionOutcome.VETO:
            caps.append(CapCheck("risk_gate", "outcome != VETO", "VETO", "REJECT"))
            return finish(PortfolioAction.REJECT, Decimal(0), "RISK_VETOED")
        caps.append(CapCheck("risk_gate", "outcome != VETO", risk.outcome.value, "PASS"))

        # -- gates that reject rather than resize (section 8/10) ---------------
        if candidate.symbol in running.symbols:
            caps.append(
                CapCheck("single_position_per_symbol", "no existing", candidate.symbol, "REJECT")
            )
            return finish(PortfolioAction.REJECT, Decimal(0), "SYMBOL_ALREADY_ALLOCATED")
        caps.append(CapCheck("single_position_per_symbol", "no existing", "ok", "PASS"))

        if running.position_count >= policy.max_concurrent_positions:
            caps.append(
                CapCheck(
                    "max_concurrent_positions",
                    str(policy.max_concurrent_positions),
                    str(running.position_count),
                    "REJECT",
                )
            )
            return finish(
                policy.insufficient_capacity_action, Decimal(0), "MAX_POSITIONS_REACHED"
            )
        caps.append(
            CapCheck(
                "max_concurrent_positions",
                str(policy.max_concurrent_positions),
                str(running.position_count),
                "PASS",
            )
        )

        # -- resizing caps: allocated is reduce-only, rounded down -------------
        quantity = requested

        def apply_cap(name: str, allowed: Decimal, per_unit: Decimal, code: str) -> None:
            nonlocal quantity
            allowed = max(allowed, Decimal(0))
            max_units = _floor(allowed / per_unit) if per_unit > 0 else Decimal(0)
            if quantity > max_units:
                caps.append(
                    CapCheck(name, f"allowed={allowed}", f"requested_units={quantity}", "REDUCE")
                )
                reasons.append(code)
                quantity = max_units
            else:
                caps.append(CapCheck(name, f"allowed={allowed}", f"units={quantity}", "PASS"))

        apply_cap(
            "total_portfolio_risk",
            policy.max_total_open_risk_pct * equity - running.total_risk,
            candidate.per_share_risk,
            "REDUCED_PORTFOLIO_RISK",
        )
        apply_cap(
            "symbol_notional",
            policy.max_symbol_notional_pct * equity
            - running.by_symbol_notional.get(candidate.symbol, Decimal(0)),
            candidate.entry_price,
            "REDUCED_SYMBOL_NOTIONAL",
        )
        apply_cap(
            "sector_notional",
            policy.max_sector_notional_pct * equity
            - running.by_sector_notional.get(candidate.sector, Decimal(0)),
            candidate.entry_price,
            "REDUCED_SECTOR_NOTIONAL",
        )
        apply_cap(
            "theme_notional",
            policy.max_theme_notional_pct * equity
            - running.by_theme_notional.get(candidate.theme, Decimal(0)),
            candidate.entry_price,
            "REDUCED_THEME_NOTIONAL",
        )
        apply_cap(
            "strategy_risk_share",
            policy.max_strategy_risk_share * envelope
            - running.by_strategy_risk.get(candidate.strategy_id, Decimal(0)),
            candidate.per_share_risk,
            "REDUCED_STRATEGY_RISK",
        )
        apply_cap(
            "source_risk_share",
            policy.max_source_risk_share * envelope
            - running.by_source_risk.get(candidate.source_id, Decimal(0)),
            candidate.per_share_risk,
            "REDUCED_SOURCE_RISK",
        )
        apply_cap(
            "cluster_risk_share",
            policy.max_cluster_risk_share * envelope
            - running.by_cluster_risk.get(candidate.cluster, Decimal(0)),
            candidate.per_share_risk,
            "REDUCED_CLUSTER_RISK",
        )

        if quantity <= 0:
            return finish(
                policy.insufficient_capacity_action, Decimal(0), "NO_PORTFOLIO_CAPACITY"
            )
        if quantity < requested:
            return finish(PortfolioAction.REDUCE, quantity)
        return finish(PortfolioAction.ALLOW, quantity, "WITHIN_PORTFOLIO_CAPS")

    @staticmethod
    def _commit(
        candidate: AllocationCandidate,
        quantity: Decimal,
        allocated_risk: Decimal,
        allocated_notional: Decimal,
        running: _Running,
    ) -> None:
        running.total_risk += allocated_risk
        running.by_sector_notional[candidate.sector] = (
            running.by_sector_notional.get(candidate.sector, Decimal(0)) + allocated_notional
        )
        running.by_theme_notional[candidate.theme] = (
            running.by_theme_notional.get(candidate.theme, Decimal(0)) + allocated_notional
        )
        running.by_symbol_notional[candidate.symbol] = (
            running.by_symbol_notional.get(candidate.symbol, Decimal(0)) + allocated_notional
        )
        running.by_strategy_risk[candidate.strategy_id] = (
            running.by_strategy_risk.get(candidate.strategy_id, Decimal(0)) + allocated_risk
        )
        running.by_source_risk[candidate.source_id] = (
            running.by_source_risk.get(candidate.source_id, Decimal(0)) + allocated_risk
        )
        running.by_cluster_risk[candidate.cluster] = (
            running.by_cluster_risk.get(candidate.cluster, Decimal(0)) + allocated_risk
        )
        running.symbols.add(candidate.symbol)
        running.position_count += 1


__all__ = [
    "ALLOCATOR_VERSION",
    "AllocationCandidate",
    "AllocationPlan",
    "CandidateAllocation",
    "CapCheck",
    "PortfolioAllocationPolicy",
    "PortfolioAllocator",
    "PortfolioAllocatorError",
    "PortfolioPosition",
    "PortfolioState",
]
