"""Final pre-submit check: refresh gates, cancel/resize, one-time auth (IMP-0906)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.emergency import HaltRecord
from trading_application.execution_identity import (
    decision_identity,
    risk_request_identity,
    risk_result_identity,
)
from trading_application.portfolio_allocator import CandidateAllocation
from trading_application.pre_submit_check import (
    ApprovalState,
    AuthorizationConsumedError,
    ChangeAction,
    ExecutionContext,
    FinalPreSubmitCheck,
    PreSubmitAction,
    PreSubmitCheckResult,
    PreSubmitError,
    PreSubmitPolicy,
    PreSubmitRequest,
    RefreshedAccount,
    RefreshedMarket,
    SingleUseAuthorization,
    StaleAction,
)
from trading_application.risk_engine import RiskResult, TradeRiskRequest
from trading_contracts.decision import DecisionResult
from trading_contracts.enums import (
    ActionTier,
    DecisionOutcome,
    Environment,
    HaltScope,
    OrderType,
    PortfolioAction,
    RiskDecisionOutcome,
)

AS_OF = datetime(2026, 7, 8, 21, 0, tzinfo=UTC)
RECENT = AS_OF - timedelta(seconds=10)

POLICY = PreSubmitPolicy(
    policy_version="pre-submit-policy-test-1",
    max_price_slippage_pct=Decimal("0.01"),  # 1%
    max_risk_increase_pct=Decimal("0.10"),  # 10%
    market_max_staleness_seconds=30,
    account_max_staleness_seconds=60,
    decision_max_age_seconds=300,
    approval_max_age_seconds=600,
    require_manual_approval=False,
    price_change_action=ChangeAction.RESIZE,
    risk_change_action=ChangeAction.RESIZE,
    stale_data_action=StaleAction.NEEDS_REVIEW,
)
CHECK = FinalPreSubmitCheck(POLICY)


def risk_request(
    *,
    symbol: str = "AAPL",
    sector: str = "tech",
    cluster: str = "megacap",
    source_id: str = "src-1",
    strategy_id: str = "strat-1",
    environment: Environment = Environment.PAPER,
    quantity: str = "100",
    entry: str = "10",
    stop: str = "9",
) -> TradeRiskRequest:
    """THE question the risk engine was asked.

    A result now names the request it answers, so a fixture has to state one:
    two evaluations whose output numbers coincide are no longer allowed to
    share an identity.
    """
    return TradeRiskRequest(
        symbol=symbol,
        sector=sector,
        cluster=cluster,
        source_id=source_id,
        strategy_id=strategy_id,
        environment=environment,
        requested_quantity=Decimal(quantity),
        entry_price=Decimal(entry),
        stop_price=Decimal(stop),
    )


def risk_result(
    outcome: RiskDecisionOutcome = RiskDecisionOutcome.APPROVE,
    for_decision: EngineDecision | None = None,
    for_request: TradeRiskRequest | None = None,
) -> RiskResult:
    """A risk result whose decision reference really points at its decision.

    The pre-submit check verifies lineage now, so an invented reference is not
    a valid risk result — the same property the real engine already satisfies.
    """
    return RiskResult(
        outcome=outcome,
        approved_quantity=Decimal("100"),
        approved_risk_amount=Decimal("100"),  # 100 shares x $1 risk
        max_loss_amount=Decimal("100"),
        risk_budget_equity=Decimal("50000"),
        decision_ref=decision_identity(
            for_decision if for_decision is not None else decision()
        ),
        request_ref=risk_request_identity(
            for_request if for_request is not None else risk_request()
        ),
        policy_ref="risk-policy:r1@sha256:" + "0" * 64,
        engine_version="risk-engine-1.0.0",
        checks=(),
        reason_codes=(),
        input_refs=("observation:a-1@src-1",),
    )


def decision(outcome: DecisionOutcome = DecisionOutcome.APPROVE) -> EngineDecision:
    result = DecisionResult(
        decision_id=UUID("00000000-0000-0000-0000-0000000000d1"),
        candidate_id=UUID("00000000-0000-0000-0000-0000000000c1"),
        outcome=outcome,
        action_tier=ActionTier.AUTO_FULL,
        strategy_version="1.0.0",
        decision_policy_version="1.0.0",
        decided_at=RECENT,
        decision_hash="sha256:" + "b" * 64,
    )
    return EngineDecision(
        result=result,
        explanation=DecisionExplanation(
            fusion_ref="fusion:s@1.0.0",
            manifest_ref="manifest:s@1.0.0",
            policy_ref="decision-policy:1.0.0",
            engine_version="decision-engine-1.0.0",
            reason_codes=(),
            blocking_reasons=(),
            input_refs=("observation:a-1@src-1",),
            validation_note="FIXTURES_VALIDATED_ONLY",
        ),
    )


def allocation(
    *,
    action: PortfolioAction = PortfolioAction.ALLOW,
    quantity: str = "100",
    for_decision: EngineDecision | None = None,
    for_risk: RiskResult | None = None,
) -> CandidateAllocation:
    """An allocation whose lineage really points at its decision and risk.

    The pre-submit check now VERIFIES that the allocation belongs to this
    decision and this exact risk evaluation rather than trusting its action and
    quantity, so a fixture with invented references is no longer a valid
    allocation — which is the point.
    """
    source_decision = for_decision if for_decision is not None else decision()
    source_risk = for_risk if for_risk is not None else risk_result()
    return CandidateAllocation(
        candidate_id="cand-1",
        action=action,
        requested_quantity=Decimal("100"),
        allocated_quantity=Decimal(quantity),
        allocated_risk=Decimal(quantity),
        allocated_notional=Decimal(quantity) * Decimal("10"),
        decision_ref=decision_identity(source_decision),
        risk_ref=risk_result_identity(source_risk),
        policy_ref="portfolio-policy:p1@sha256:" + "0" * 64,
        caps_evaluated=(),
        reason_codes=(),
        input_refs=("observation:a-1@src-1",),
    )


def request(
    *,
    alloc: CandidateAllocation | None = None,
    risk_outcome: RiskDecisionOutcome = RiskDecisionOutcome.APPROVE,
    decision_outcome: DecisionOutcome = DecisionOutcome.APPROVE,
    entry: str = "10",
    stop: str = "9",
) -> PreSubmitRequest:
    this_decision = decision(decision_outcome)
    this_question = risk_request(entry=entry, stop=stop)
    this_risk = risk_result(
        risk_outcome, for_decision=this_decision, for_request=this_question
    )
    return PreSubmitRequest(
        allocation=(
            alloc
            if alloc is not None
            else allocation(for_decision=this_decision, for_risk=this_risk)
        ),
        risk_result=this_risk,
        decision=this_decision,
        risk_request=this_question,
        symbol="AAPL",
        strategy_id="strat-1",
        source_id="src-1",
        environment=Environment.PAPER,
        planned_entry_price=Decimal(entry),
        planned_stop_price=Decimal(stop),
        side="BUY",
        account_alias="paper-main",
        order_type=OrderType.LIMIT,
        time_in_force="DAY",
        client_order_id="ord-1",
    )


def market(*, price: str | None = "10", observed_at: datetime = RECENT) -> RefreshedMarket:
    return RefreshedMarket(
        price=Decimal(price) if price is not None else None, observed_at=observed_at
    )


def acct(*, equity: str | None = "50000", observed_at: datetime = RECENT) -> RefreshedAccount:
    return RefreshedAccount(
        risk_budget_equity=Decimal(equity) if equity else None, observed_at=observed_at
    )


def run(
    *,
    req: PreSubmitRequest | None = None,
    mkt: RefreshedMarket | None = None,
    account: RefreshedAccount | None = None,
    approval: ApprovalState | None = None,
    context: ExecutionContext | None = None,
    check: FinalPreSubmitCheck = CHECK,
) -> PreSubmitCheckResult:
    return check.check(
        req if req is not None else request(),
        mkt if mkt is not None else market(),
        account if account is not None else acct(),
        approval if approval is not None else ApprovalState(approved=True, approved_at=RECENT),
        context if context is not None else ExecutionContext(),
        as_of=AS_OF,
    )


# -- allow + one-time authorization ----------------------------------------------


def test_clean_state_allows_and_mints_single_use_authorization() -> None:
    result = run()
    assert result.action is PreSubmitAction.ALLOW
    assert result.authorized_quantity == Decimal("100")
    assert result.authorization is not None
    assert result.authorization.authorized_quantity == Decimal("100")
    assert "not a broker submission" in result.authorization.note
    assert "PRE_SUBMIT_CLEARED" in result.reason_codes
    # single-use enforcement
    token = SingleUseAuthorization(result.authorization)
    assert token.consume() is result.authorization
    with pytest.raises(AuthorizationConsumedError):
        token.consume()


# -- kill/halt/broker ------------------------------------------------------------


def test_active_kill_switch_cancels() -> None:
    halt = HaltRecord(
        scope=HaltScope.GLOBAL_ENTRY_HALT,
        environment=Environment.PAPER,
        target_ref=None,
        reason_code="SEV1",
    )
    result = run(context=ExecutionContext(active_halts=(halt,)))
    assert result.action is PreSubmitAction.CANCEL
    assert "HALT_ACTIVE_GLOBAL_ENTRY_HALT" in result.reason_codes
    assert result.authorization is None


def test_symbol_halt_cancels() -> None:
    halt = HaltRecord(
        scope=HaltScope.SYMBOL_HALT,
        environment=Environment.PAPER,
        target_ref="AAPL",
        reason_code="CA",
    )
    assert run(context=ExecutionContext(active_halts=(halt,))).action is PreSubmitAction.CANCEL


def test_unhealthy_broker_and_reconciliation_cancel() -> None:
    assert run(context=ExecutionContext(broker_healthy=False)).action is PreSubmitAction.CANCEL
    result = run(context=ExecutionContext(reconciliation_required=True))
    assert result.action is PreSubmitAction.CANCEL
    assert "RECONCILIATION_REQUIRED" in result.reason_codes


# -- refresh: freshness / price / risk -------------------------------------------


def test_stale_market_data_uses_policy_stale_action() -> None:
    stale = market(observed_at=AS_OF - timedelta(seconds=45))
    result = run(mkt=stale)
    assert result.action is PreSubmitAction.NEEDS_REVIEW  # policy stale_data_action
    assert "MARKET_DATA_STALE" in result.reason_codes


def test_missing_account_equity_fails_closed() -> None:
    result = run(account=acct(equity=None))
    assert result.action is PreSubmitAction.NEEDS_REVIEW
    assert "ACCOUNT_DATA_MISSING" in result.reason_codes


def test_price_slippage_resizes_when_policy_says_resize() -> None:
    # price moved 10 -> 10.5 (5% > 1%); policy resize; risk 10.5-9=1.5 vs 1 =50% up
    result = run(mkt=market(price="10.5"))
    assert result.action is PreSubmitAction.RESIZE
    assert "RESIZED_PRICE_SLIPPAGE" in result.reason_codes
    assert result.authorized_quantity <= Decimal("100")  # never increased


def test_price_slippage_cancels_when_policy_says_cancel() -> None:
    cancel_policy = PreSubmitPolicy(
        policy_version="p2",
        max_price_slippage_pct=Decimal("0.01"),
        max_risk_increase_pct=Decimal("0.10"),
        market_max_staleness_seconds=30,
        account_max_staleness_seconds=60,
        decision_max_age_seconds=300,
        approval_max_age_seconds=600,
        require_manual_approval=False,
        price_change_action=ChangeAction.CANCEL,
        risk_change_action=ChangeAction.RESIZE,
        stale_data_action=StaleAction.CANCEL,
    )
    result = run(mkt=market(price="10.5"), check=FinalPreSubmitCheck(cancel_policy))
    assert result.action is PreSubmitAction.CANCEL
    assert "PRICE_SLIPPAGE_EXCEEDED" in result.reason_codes


def test_risk_increase_resizes_down_within_budget() -> None:
    # entry stays 10 (no slippage) but stop farther: risk 10-8=2 vs planned 1?
    # planned stop 9 -> risk 1; market price 10, new stop... we simulate via
    # a smaller planned stop distance so refreshed risk rises. Use planned
    # entry 10 stop 9.95 (risk 0.05); market 10 -> new risk 0.05 (no change).
    # Instead: planned entry 10, stop 9 (risk 1); market price 11.5 would be
    # slippage; keep price within 1% (10.05) and stop 9 -> risk 1.05 = 5% < 10%.
    # To force >10%: planned stop 9.5 (risk 0.5), market 10.05 -> risk 0.55 =10%
    # boundary; use market 10.06 -> 0.56/0.5-1 = 12% > 10%.
    req = request(entry="10", stop="9.5")  # planned risk 0.5
    result = run(req=req, mkt=market(price="10.06"))
    assert result.action is PreSubmitAction.RESIZE
    assert "RESIZED_RISK_INCREASE" in result.reason_codes
    # budget 100 risk / new risk 0.56 -> floor(178.5) = 178, but capped at
    # allocation 100 -> 100
    assert result.authorized_quantity == Decimal("100")


def test_risk_increase_resize_reduces_when_budget_binds() -> None:
    # large risk jump forces the budget to bind below the allocation.
    # planned entry 100 stop 99 (risk 1); market 100.5 (slippage 0.5% < 1%);
    # new risk 100.5-99 = 1.5 -> 50% > 10% -> resize; budget 100/1.5=66.6 ->66
    req = request(entry="100", stop="99")
    result = run(req=req, mkt=market(price="100.5"))
    assert result.action is PreSubmitAction.RESIZE
    assert result.authorized_quantity == Decimal("66")
    assert result.authorized_quantity < Decimal("100")


# -- decision / approval expiry ------------------------------------------------------


def test_expired_decision_cancels() -> None:
    old_decision = decision()
    object.__setattr__(
        old_decision.result, "decided_at", AS_OF - timedelta(seconds=400)
    )
    result = run(
        req=PreSubmitRequest(
            allocation=allocation(),
            risk_result=risk_result(),
            decision=old_decision,
            risk_request=risk_request(),
            symbol="AAPL",
            strategy_id="strat-1",
            source_id="src-1",
            environment=Environment.PAPER,
            planned_entry_price=Decimal("10"),
            planned_stop_price=Decimal("9"),
            side="BUY",
            account_alias="paper-main",
            order_type=OrderType.LIMIT,
            time_in_force="DAY",
            client_order_id="ord-1",
        )
    )
    assert result.action is PreSubmitAction.CANCEL
    assert "DECISION_EXPIRED" in result.reason_codes


def test_missing_and_expired_approval() -> None:
    approval_policy = PreSubmitPolicy(
        policy_version="p3",
        max_price_slippage_pct=Decimal("0.01"),
        max_risk_increase_pct=Decimal("0.10"),
        market_max_staleness_seconds=30,
        account_max_staleness_seconds=60,
        decision_max_age_seconds=300,
        approval_max_age_seconds=600,
        require_manual_approval=True,
        price_change_action=ChangeAction.RESIZE,
        risk_change_action=ChangeAction.RESIZE,
        stale_data_action=StaleAction.NEEDS_REVIEW,
    )
    check = FinalPreSubmitCheck(approval_policy)
    missing = run(approval=ApprovalState(approved=False), check=check)
    assert missing.action is PreSubmitAction.NEEDS_REVIEW
    assert "APPROVAL_MISSING" in missing.reason_codes
    expired = run(
        approval=ApprovalState(approved=True, approved_at=AS_OF - timedelta(seconds=700)),
        check=check,
    )
    assert expired.action is PreSubmitAction.NEEDS_REVIEW
    assert "APPROVAL_EXPIRED" in expired.reason_codes


# -- rejected/vetoed never allowed ---------------------------------------------------


def test_rejected_allocation_never_allowed() -> None:
    for action, qty in ((PortfolioAction.REJECT, "0"), (PortfolioAction.MANUAL, "0")):
        result = run(req=request(alloc=allocation(action=action, quantity=qty)))
        assert result.action is PreSubmitAction.CANCEL
        assert "ALLOCATION_NOT_APPROVED" in result.reason_codes


def test_risk_veto_and_non_approved_decision_never_allowed() -> None:
    assert run(req=request(risk_outcome=RiskDecisionOutcome.VETO)).action is PreSubmitAction.CANCEL
    assert (
        run(req=request(decision_outcome=DecisionOutcome.REJECT)).action is PreSubmitAction.CANCEL
    )


# -- policy / determinism / hygiene ----------------------------------------------------


def test_invalid_policy_fails_closed() -> None:
    with pytest.raises(PreSubmitError, match="policy_version"):
        PreSubmitPolicy(
            policy_version=" ",
            max_price_slippage_pct=Decimal("0.01"),
            max_risk_increase_pct=Decimal("0.10"),
            market_max_staleness_seconds=30,
            account_max_staleness_seconds=60,
            decision_max_age_seconds=300,
            approval_max_age_seconds=600,
            require_manual_approval=False,
            price_change_action=ChangeAction.RESIZE,
            risk_change_action=ChangeAction.RESIZE,
            stale_data_action=StaleAction.NEEDS_REVIEW,
        )
    with pytest.raises(PreSubmitError, match="windows must be positive"):
        PreSubmitPolicy(
            policy_version="p",
            max_price_slippage_pct=Decimal("0.01"),
            max_risk_increase_pct=Decimal("0.10"),
            market_max_staleness_seconds=0,
            account_max_staleness_seconds=60,
            decision_max_age_seconds=300,
            approval_max_age_seconds=600,
            require_manual_approval=False,
            price_change_action=ChangeAction.RESIZE,
            risk_change_action=ChangeAction.RESIZE,
            stale_data_action=StaleAction.NEEDS_REVIEW,
        )
    with pytest.raises(TypeError):
        PreSubmitPolicy(policy_version="p")  # type: ignore[call-arg]


def test_same_inputs_same_result() -> None:
    assert run() == run()


def test_lookahead_and_naive_rejected() -> None:
    future_market = market(observed_at=AS_OF + timedelta(seconds=1))
    with pytest.raises(PreSubmitError, match="lookahead"):
        run(mkt=future_market)
    with pytest.raises(PreSubmitError, match="timezone-aware"):
        CHECK.check(
            request(), market(), acct(), ApprovalState(approved=True, approved_at=RECENT),
            ExecutionContext(), as_of=datetime(2026, 7, 8, 21, 0),  # noqa: DTZ001
        )


def test_explanation_contains_full_trace() -> None:
    result = run()
    assert result.allocation_ref.startswith("allocation:cand-1@")
    assert result.risk_ref.startswith("risk-result:sha256:")
    # The SAME canonical decision reference the risk engine emits — carrying
    # the id and the hash — so a consumer can actually compare the two. A bare
    # hash could not identify which decision it belonged to.
    assert result.decision_ref.startswith("decision:")
    assert "@sha256:" in result.decision_ref
    # The PRE-SUBMIT policy identity, in its own namespace. It is deliberately
    # NOT the risk policy identity: they govern different checks, and comparing
    # them is what made a valid authorization look invalid.
    assert result.policy_ref.startswith(
        "pre-submit-policy:pre-submit-policy-test-1@sha256:"
    )
    assert not result.policy_ref.startswith("risk-policy:")
    # And the risk reference identifies the EXACT evaluation, not its outcome.
    assert result.risk_ref.startswith("risk-result:sha256:")
    assert result.risk_ref != "risk:APPROVE"
    assert result.input_refs == ("observation:a-1@src-1",)
    names = [check.name for check in result.checks]
    assert "halt_gate" in names and "price_slippage" in names


def test_check_creates_no_broker_action_or_submit() -> None:
    import trading_application.pre_submit_check as module

    source = open(module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for name in ("trading_persistence", "httpx", "requests", "subprocess", "broker_ibkr"):
            assert name not in line, line


# -- evaluation-only API (research/shadow callers, IMP-1301) ------------------------


def test_evaluate_only_matches_check_and_never_constructs_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared core drives both paths; evaluate_only never mints."""
    import trading_application.pre_submit_check as module

    production = run()
    assert production.action is PreSubmitAction.ALLOW
    assert production.authorization is not None  # production path unchanged

    class _Exploding:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError(
                "authorization constructor called on the evaluation-only path"
            )

    monkeypatch.setattr(module, "SubmitAuthorization", _Exploding)
    evaluation = CHECK.evaluate_only(
        request(),
        market(),
        acct(),
        ApprovalState(approved=True, approved_at=RECENT),
        ExecutionContext(),
        as_of=AS_OF,
    )
    # Same deterministic gate outcome, no authorization anywhere.
    assert evaluation.action is production.action
    assert evaluation.evaluated_quantity == production.authorized_quantity
    assert evaluation.checks == production.checks
    assert evaluation.reason_codes == production.reason_codes
    assert evaluation.research_only is True
    assert not hasattr(evaluation, "authorization")


def test_evaluation_dataclass_has_no_authorization_surface() -> None:
    from dataclasses import fields

    from trading_application.pre_submit_check import PreSubmitEvaluation

    names = {field.name for field in fields(PreSubmitEvaluation)}
    assert not any("authoriz" in name for name in names)
    with pytest.raises(PreSubmitError, match="research-only"):
        evaluation = CHECK.evaluate_only(
            request(), market(), acct(),
            ApprovalState(approved=True, approved_at=RECENT), ExecutionContext(),
            as_of=AS_OF,
        )
        PreSubmitEvaluation(
            candidate_id=evaluation.candidate_id,
            action=evaluation.action,
            evaluated_quantity=evaluation.evaluated_quantity,
            allocation_ref=evaluation.allocation_ref,
            risk_ref=evaluation.risk_ref,
            decision_ref=evaluation.decision_ref,
            policy_ref=evaluation.policy_ref,
            checks=evaluation.checks,
            reason_codes=evaluation.reason_codes,
            input_refs=evaluation.input_refs,
            research_only=False,
        )
