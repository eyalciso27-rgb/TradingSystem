"""Portfolio allocator: caps, deterministic ranking, reduce-only (IMP-0905)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.portfolio_allocator import (
    AllocationCandidate,
    AllocationPlan,
    PortfolioAllocationPolicy,
    PortfolioAllocator,
    PortfolioAllocatorError,
    PortfolioPosition,
    PortfolioState,
)
from trading_application.risk_engine import RiskResult
from trading_contracts.enums import PortfolioAction, RiskDecisionOutcome

AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=5)
EQUITY = Decimal("50000")

POLICY = PortfolioAllocationPolicy(
    policy_version="portfolio-policy-test-1",
    max_total_open_risk_pct=Decimal("0.0100"),  # envelope = 500
    max_symbol_notional_pct=Decimal("0.10"),  # 5000
    max_sector_notional_pct=Decimal("0.25"),  # 12500
    max_theme_notional_pct=Decimal("0.20"),  # 10000
    max_strategy_risk_share=Decimal("0.50"),  # 250 of envelope
    max_source_risk_share=Decimal("0.50"),  # 250
    max_cluster_risk_share=Decimal("0.40"),  # 200
    max_concurrent_positions=4,
    insufficient_capacity_action=PortfolioAction.REJECT,
)
ALLOCATOR = PortfolioAllocator(POLICY)


def risk_result(
    *,
    outcome: RiskDecisionOutcome = RiskDecisionOutcome.APPROVE,
    quantity: str = "100",
    per_share_risk: Decimal = Decimal("1"),
) -> RiskResult:
    return RiskResult(
        outcome=outcome,
        approved_quantity=Decimal(quantity),
        approved_risk_amount=Decimal(quantity) * per_share_risk,
        max_loss_amount=Decimal(quantity) * per_share_risk,
        risk_budget_equity=EQUITY,
        decision_ref="decision:d1@sha256:" + "a" * 64,
        policy_ref="risk-policy:risk-policy-test-1",
        engine_version="risk-engine-1.0.0",
        checks=(),
        reason_codes=("WITHIN_SAFETY_ENVELOPE",),
        input_refs=("observation:a-1@src-1",),
    )


def candidate(
    cid: str,
    *,
    outcome: RiskDecisionOutcome = RiskDecisionOutcome.APPROVE,
    quantity: str = "100",
    symbol: str | None = None,
    sector: str = "tech",
    theme: str = "ai",
    cluster: str = "megacap",
    source: str = "src-1",
    strategy: str = "strat-1",
    entry: str = "10",
    per_share_risk: str = "1",
    priority: str = "1.0",
) -> AllocationCandidate:
    return AllocationCandidate(
        candidate_id=cid,
        risk_result=risk_result(
            outcome=outcome, quantity=quantity, per_share_risk=Decimal(per_share_risk)
        ),
        symbol=symbol if symbol is not None else cid.upper(),
        sector=sector,
        theme=theme,
        cluster=cluster,
        source_id=source,
        strategy_id=strategy,
        entry_price=Decimal(entry),
        per_share_risk=Decimal(per_share_risk),
        priority_score=Decimal(priority),
    )


def state(*positions: PortfolioPosition, equity: str | None = "50000") -> PortfolioState:
    return PortfolioState(
        risk_budget_equity=Decimal(equity) if equity else None,
        open_positions=positions,
        observed_at=EARLIER,
    )


def position(
    *,
    symbol: str = "MSFT",
    sector: str = "other",
    theme: str = "other",
    cluster: str = "other",
    source: str = "src-9",
    strategy: str = "strat-9",
    risk: str = "0",
    notional: str = "0",
) -> PortfolioPosition:
    return PortfolioPosition(
        symbol=symbol,
        sector=sector,
        theme=theme,
        cluster=cluster,
        source_id=source,
        strategy_id=strategy,
        open_risk_amount=Decimal(risk),
        notional=Decimal(notional),
    )


def allocate(candidates: tuple[AllocationCandidate, ...], st: PortfolioState) -> AllocationPlan:
    return ALLOCATOR.allocate(candidates, st, as_of=AS_OF)


def by_id(plan: AllocationPlan, cid: str):  # type: ignore[no-untyped-def]
    return next(a for a in plan.allocations if a.candidate_id == cid)


# -- golden allocation ----------------------------------------------------------


def test_multiple_valid_candidates_allocated() -> None:
    plan = allocate((candidate("a"), candidate("b", symbol="B", cluster="c2")), state())
    for cid in ("a", "b"):
        alloc = by_id(plan, cid)
        assert alloc.action is PortfolioAction.ALLOW
        assert alloc.allocated_quantity == Decimal("100")
        assert alloc.decision_ref.startswith("decision:")
        assert "WITHIN_PORTFOLIO_CAPS" in alloc.reason_codes


def test_risk_veto_never_allocated() -> None:
    plan = allocate((candidate("a", outcome=RiskDecisionOutcome.VETO),), state())
    alloc = by_id(plan, "a")
    assert alloc.action is PortfolioAction.REJECT
    assert alloc.allocated_quantity == Decimal("0")
    assert "RISK_VETOED" in alloc.reason_codes


def test_risk_reduce_quantity_is_upper_bound() -> None:
    # risk approved 40; allocator can never allocate more than 40
    plan = allocate((candidate("a", outcome=RiskDecisionOutcome.REDUCE, quantity="40"),), state())
    alloc = by_id(plan, "a")
    assert alloc.allocated_quantity <= Decimal("40")
    assert alloc.allocated_quantity == Decimal("40")  # fits all caps


# -- caps ------------------------------------------------------------------------


def test_total_portfolio_risk_cap_reduces() -> None:
    # envelope 500; existing risk 480 -> room 20 -> 20 shares at $1 risk
    existing = position(risk="480", symbol="ZZZ", cluster="z", source="s9", strategy="s9")
    plan = allocate((candidate("a", quantity="100"),), state(existing))
    alloc = by_id(plan, "a")
    assert alloc.action is PortfolioAction.REDUCE
    assert alloc.allocated_quantity == Decimal("20")
    assert "REDUCED_PORTFOLIO_RISK" in alloc.reason_codes


def test_symbol_cap_via_existing_position_rejects_duplicate() -> None:
    existing = position(symbol="A")
    plan = allocate((candidate("a", symbol="A"),), state(existing))
    assert by_id(plan, "a").action is PortfolioAction.REJECT
    assert "SYMBOL_ALREADY_ALLOCATED" in by_id(plan, "a").reason_codes


def test_sector_theme_strategy_source_cluster_caps() -> None:
    # sector cap 12500; existing tech notional 12300 -> room 200 -> 20 shares @10
    sector_state = state(position(sector="tech", notional="12300"))
    assert by_id(allocate((candidate("a"),), sector_state), "a").allocated_quantity == Decimal("20")
    # theme cap 10000; existing ai theme 9900 -> room 100 -> 10 shares
    theme_state = state(position(theme="ai", notional="9900"))
    assert by_id(allocate((candidate("a"),), theme_state), "a").allocated_quantity == Decimal("10")
    # strategy risk share 250 of envelope; existing strat-1 risk 245 -> 5 shares
    strat_state = state(position(strategy="strat-1", risk="245"))
    alloc = by_id(allocate((candidate("a"),), strat_state), "a")
    assert alloc.allocated_quantity == Decimal("5")
    assert "REDUCED_STRATEGY_RISK" in alloc.reason_codes
    # source risk share 250; existing src-1 risk 246 -> 4 shares
    source_state = state(position(source="src-1", risk="246"))
    assert by_id(allocate((candidate("a"),), source_state), "a").allocated_quantity == Decimal("4")
    # cluster risk share 200; existing megacap risk 197 -> 3 shares
    cluster_state = state(position(cluster="megacap", risk="197"))
    assert by_id(allocate((candidate("a"),), cluster_state), "a").allocated_quantity == Decimal("3")


def test_max_concurrent_positions_rejects_by_policy() -> None:
    full = state(*[position(symbol=f"P{i}") for i in range(4)])
    plan = allocate((candidate("a", symbol="A"),), full)
    assert by_id(plan, "a").action is PortfolioAction.REJECT
    assert "MAX_POSITIONS_REACHED" in by_id(plan, "a").reason_codes


def test_no_capacity_uses_policy_action() -> None:
    exhausted = state(position(risk="500", symbol="ZZZ", cluster="z"))
    plan = allocate((candidate("a"),), exhausted)
    alloc = by_id(plan, "a")
    assert alloc.action is PortfolioAction.REJECT  # policy insufficient_capacity_action
    assert alloc.allocated_quantity == Decimal("0")
    assert "NO_PORTFOLIO_CAPACITY" in alloc.reason_codes


def test_insufficient_capacity_manual_when_policy_says_so() -> None:
    manual_policy = PortfolioAllocationPolicy(
        policy_version="p2",
        max_total_open_risk_pct=Decimal("0.01"),
        max_symbol_notional_pct=Decimal("0.10"),
        max_sector_notional_pct=Decimal("0.25"),
        max_theme_notional_pct=Decimal("0.20"),
        max_strategy_risk_share=Decimal("0.50"),
        max_source_risk_share=Decimal("0.50"),
        max_cluster_risk_share=Decimal("0.40"),
        max_concurrent_positions=4,
        insufficient_capacity_action=PortfolioAction.MANUAL,
    )
    exhausted = state(position(risk="500", symbol="ZZZ", cluster="z"))
    plan = PortfolioAllocator(manual_policy).allocate((candidate("a"),), exhausted, as_of=AS_OF)
    assert plan.allocations[0].action is PortfolioAction.MANUAL


# -- batch competition + deterministic ranking -----------------------------------


def test_parallel_candidates_ranked_by_priority_then_id() -> None:
    # cluster cap 200; each candidate risk 100/share*... use per_share_risk 1,
    # qty 150 -> 150 risk each; two in same cluster: first gets 150, second 50.
    high = candidate("low_id", symbol="H", cluster="shared", quantity="150", priority="2.0")
    low = candidate("zzz_id", symbol="L", cluster="shared", quantity="150", priority="1.0")
    plan = allocate((low, high), state())
    # higher priority allocated first and fully; lower priority reduced to fit
    assert by_id(plan, "low_id").allocated_quantity == Decimal("150")
    assert by_id(plan, "zzz_id").allocated_quantity == Decimal("50")
    # ranking is deterministic regardless of input order
    plan2 = allocate((high, low), state())
    assert [a.candidate_id for a in plan.allocations] == [a.candidate_id for a in plan2.allocations]


def test_tie_break_is_deterministic_by_candidate_id() -> None:
    a = candidate("aaa", symbol="A", cluster="shared", quantity="150", priority="1.0")
    b = candidate("bbb", symbol="B", cluster="shared", quantity="150", priority="1.0")
    plan = allocate((b, a), state())
    # equal priority -> candidate_id ascending wins the capacity first
    assert by_id(plan, "aaa").allocated_quantity == Decimal("150")
    assert by_id(plan, "bbb").allocated_quantity == Decimal("50")


def test_same_inputs_same_plan() -> None:
    candidates = (candidate("a"), candidate("b", symbol="B", cluster="c2"))
    assert allocate(candidates, state()) == allocate(candidates, state())


# -- fail-closed / hygiene --------------------------------------------------------


def test_invalid_policy_fails_closed() -> None:
    with pytest.raises(PortfolioAllocatorError, match="policy_version"):
        PortfolioAllocationPolicy(
            policy_version=" ",
            max_total_open_risk_pct=Decimal("0.01"),
            max_symbol_notional_pct=Decimal("0.10"),
            max_sector_notional_pct=Decimal("0.25"),
            max_theme_notional_pct=Decimal("0.20"),
            max_strategy_risk_share=Decimal("0.50"),
            max_source_risk_share=Decimal("0.50"),
            max_cluster_risk_share=Decimal("0.40"),
            max_concurrent_positions=4,
            insufficient_capacity_action=PortfolioAction.REJECT,
        )
    with pytest.raises(PortfolioAllocatorError, match="REJECT or MANUAL"):
        PortfolioAllocationPolicy(
            policy_version="p",
            max_total_open_risk_pct=Decimal("0.01"),
            max_symbol_notional_pct=Decimal("0.10"),
            max_sector_notional_pct=Decimal("0.25"),
            max_theme_notional_pct=Decimal("0.20"),
            max_strategy_risk_share=Decimal("0.50"),
            max_source_risk_share=Decimal("0.50"),
            max_cluster_risk_share=Decimal("0.40"),
            max_concurrent_positions=4,
            insufficient_capacity_action=PortfolioAction.ALLOW,
        )
    with pytest.raises(TypeError):
        PortfolioAllocationPolicy(policy_version="p")  # type: ignore[call-arg]


def test_missing_portfolio_equity_fails_closed() -> None:
    with pytest.raises(PortfolioAllocatorError, match="risk_budget_equity"):
        allocate((candidate("a"),), state(equity=None))


def test_duplicate_candidate_id_rejected() -> None:
    with pytest.raises(PortfolioAllocatorError, match="duplicate candidate_id"):
        allocate((candidate("a"), candidate("a", symbol="B")), state())


def test_lookahead_and_naive_rejected() -> None:
    with pytest.raises(PortfolioAllocatorError, match="lookahead"):
        ALLOCATOR.allocate((candidate("a"),), state(), as_of=EARLIER - timedelta(minutes=1))
    with pytest.raises(PortfolioAllocatorError, match="timezone-aware"):
        ALLOCATOR.allocate((candidate("a"),), state(), as_of=datetime(2026, 7, 8))  # noqa: DTZ001


def test_explanation_contains_full_trace() -> None:
    existing = position(risk="480", symbol="ZZZ", cluster="z")
    alloc = by_id(allocate((candidate("a"),), state(existing)), "a")
    assert alloc.decision_ref.startswith("decision:")
    assert alloc.risk_ref.startswith("risk:")
    assert alloc.policy_ref == "portfolio-policy:portfolio-policy-test-1"
    assert alloc.input_refs == ("observation:a-1@src-1",)
    names = [cap.name for cap in alloc.caps_evaluated]
    assert "risk_gate" in names and "total_portfolio_risk" in names
    assert alloc.requested_quantity == Decimal("100")


def test_allocator_creates_no_order_or_broker_action() -> None:
    import trading_application.portfolio_allocator as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        forbidden_imports = (
            "trading_persistence", "httpx", "requests", "subprocess",
            "OrderIntent", "broker",
        )
        for name in forbidden_imports:
            assert name not in line, line
