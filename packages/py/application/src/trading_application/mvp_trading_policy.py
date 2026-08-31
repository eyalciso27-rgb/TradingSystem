"""MVP trading policy — strategy -> canonical decision/risk/execution intent.

This is the MVP "trading policy half": a THIN, deterministic orchestration that
carries ONE real strategy (``baseline-trend-breakout-v1``) through the existing
canonical engines and proves the mission's conceptual path::

    MARKET DATA -> STRATEGY -> SIGNAL / NO TRADE -> ENGINE DECISION
      -> RISK + SAFETY -> APPROVED/REDUCED/DENIED -> CANONICAL EXECUTION INTENT

It re-implements none of the engines. It composes:

    BaselineTrendBreakoutStrategy -> StrategyManifestEvaluator -> SignalFusion
      -> DecisionEngine
      -> universe eligibility (S&P/ADV20/price/spread)
      -> entry timing (NEXT RTH session, canonical calendar)
      -> position attribution (conId)
      -> account unknown-state gate (NLV/cap/P&L-status/Start-of-Day equity)
      -> daily-loss gate (1.5% of Start-of-Day equity)
      -> RiskEngine -> GICS-sector caps (max 3 / 0.75% planned risk)
      -> PortfolioAllocator -> FinalPreSubmitCheck.

Ownership boundaries (mission section 3), enforced by construction:

- The strategy answers only "is there an opportunity?". A NO-TRADE evaluation
  is routed through the canonical decision path as a non-APPROVE decision and
  can NEVER become an execution intent.
- Risk/portfolio/safety decide "are we allowed, and at what size?". They only
  reduce, never invent a trade and never enlarge risk. Any unknown critical
  state (P&L, account equity, an unattributable position) fails closed to
  NO_NEW_ENTRY before the Risk Engine is even asked.
- The terminal artifact is the canonical ``ExecutablePlan`` (the sealed
  execution intent). This module NEVER submits it, never consumes the
  single-use authorization the pre-submit check mints, never imports a broker
  adapter and never enables Paper Auto or Live. Execution is the responsibility
  of the canonical execution runtime and the protected-lifecycle work (PR #94).

The execution authority downstream may reduce an approved quantity N to the MVP
executable quantity 1 (the protected single-share entry) without violating any
strategy semantics — reducing risk is always allowed here; increasing it never
is. See ``docs/mvp/mvp-strategy-trading-policy.md`` for the post-#94 integration
seam.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from uuid import UUID

from trading_broker_core.port import SessionPnlStatus
from trading_contracts.enums import (
    DecisionOutcome,
    Environment,
    OrderType,
    PortfolioAction,
    RiskDecisionOutcome,
)
from trading_domain.calendar import TradingCalendar
from trading_market_data_core.types import BarSeries

from trading_application.baseline_strategy import (
    TECHNICAL_BREAKOUT_KEY,
    BaselineEvaluation,
    BaselineTrendBreakoutStrategy,
)
from trading_application.decision_engine import DecisionEngine, EngineDecision
from trading_application.emergency import HaltRecord
from trading_application.execution_plan import ExecutablePlan
from trading_application.mvp_policy_config import MvpPolicyConfig
from trading_application.mvp_safety_gates import (
    daily_loss_within_limit,
    entry_timing_eligible,
    sector_within_caps,
)
from trading_application.portfolio_allocator import (
    AllocationCandidate,
    CandidateAllocation,
    PortfolioAllocator,
    PortfolioPosition,
    PortfolioState,
)
from trading_application.position_attribution import (
    BrokerHeldPosition,
    PortfolioClassification,
    SystemPositionRecord,
    classify_positions,
)
from trading_application.pre_submit_check import (
    ApprovalState,
    ExecutionContext,
    FinalPreSubmitCheck,
    PreSubmitAction,
    PreSubmitCheckResult,
    PreSubmitRequest,
    RefreshedAccount,
    RefreshedMarket,
)
from trading_application.risk_engine import (
    AccountState,
    PositionExposure,
    RiskEngine,
    RiskResult,
    TradeRiskRequest,
)
from trading_application.signal_fusion import FusionInput, SignalFusion
from trading_application.strategy_manifest import StrategyManifestEvaluator
from trading_application.universe_eligibility import (
    UniverseEligibility,
    UniverseFacts,
    evaluate_universe_eligibility,
)

MVP_POLICY_ENGINE_VERSION = "mvp-trading-policy-1.0.0"


class MvpTradingPolicyError(Exception):
    """Fail-closed orchestration failure (bad inputs, lookahead, LIVE misuse)."""


class MvpResult(StrEnum):
    """The three terminal shapes of an MVP evaluation."""

    #: The strategy found no valid opportunity (all mandatory predicates AND'd).
    NO_TRADE = "NO_TRADE"
    #: There is an opportunity, but risk/safety/portfolio/unknown-state blocks a
    #: new entry. Never converted into a trade.
    NO_NEW_ENTRY = "NO_NEW_ENTRY"
    #: Approved (possibly reduced) into a sealed canonical execution intent.
    EXECUTION_INTENT = "EXECUTION_INTENT"


class MvpStage(StrEnum):
    """The pipeline stage an evaluation reached (audit/debug)."""

    STRATEGY = "STRATEGY"
    DECISION = "DECISION"
    UNIVERSE = "UNIVERSE"
    ENTRY_TIMING = "ENTRY_TIMING"
    POSITION_ATTRIBUTION = "POSITION_ATTRIBUTION"
    ACCOUNT_STATE = "ACCOUNT_STATE"
    DAILY_LOSS = "DAILY_LOSS"
    SIZING = "SIZING"
    RISK = "RISK"
    SECTOR = "SECTOR"
    ALLOCATION = "ALLOCATION"
    PRE_SUBMIT = "PRE_SUBMIT"
    EXECUTION_INTENT = "EXECUTION_INTENT"


@dataclass(frozen=True, slots=True)
class MvpAccountSnapshot:
    """Account state with UNKNOWN-representable critical fields.

    Every critical field is nullable so an unknown value can never silently
    become zero (mission section 9). A missing NLV, equity cap or session P&L —
    or a session P&L whose status is anything but OK — fails the new entry
    closed before the Risk Engine is consulted.
    """

    broker_net_liquidation_value: Decimal | None
    simulation_equity_cap: Decimal | None
    session_pnl: Decimal | None
    session_pnl_status: SessionPnlStatus | None
    #: Authoritative START-OF-DAY equity for the daily-loss limit (B6). The
    #: frozen 1.5% daily-loss limit is measured against SOD equity, not current
    #: NLV; a missing value fails the new entry closed.
    start_of_day_equity: Decimal | None
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise MvpTradingPolicyError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MvpCandidateFacts:
    """Attribution facts of the NEW candidate (never copied onto positions).

    The GICS ``sector`` is deliberately NOT here — it must be the authoritative
    sector from the universe eligibility facts, never a free-text candidate
    field, because the sector caps depend on it.
    """

    symbol: str
    cluster: str
    source_id: str

    def __post_init__(self) -> None:
        for name in ("symbol", "cluster", "source_id"):
            if not str(getattr(self, name)).strip():
                raise MvpTradingPolicyError(f"candidate {name} is mandatory")


@dataclass(frozen=True, slots=True)
class MvpExecutionInputs:
    """The order shape, broker resolution and refreshed pre-submit state.

    Environment is passed explicitly and must be RESEARCH or PAPER — never
    LIVE. These describe the order that WOULD be submitted by the execution
    runtime; this module only produces the sealed intent, it submits nothing.
    """

    environment: Environment
    account_alias: str
    side: str
    order_type: OrderType
    time_in_force: str
    client_order_id: str
    broker_instrument_ref: str
    broker_exchange: str
    broker_currency: str
    refreshed_market: RefreshedMarket
    refreshed_account: RefreshedAccount
    approval: ApprovalState
    broker_healthy: bool = True
    reconciliation_required: bool = False

    def __post_init__(self) -> None:
        if self.environment is Environment.LIVE:
            raise MvpTradingPolicyError(
                "the MVP trading policy never operates on a LIVE environment"
            )


@dataclass(frozen=True, slots=True)
class MvpPolicyOutcome:
    """The full, deterministic result of one MVP evaluation."""

    result: MvpResult
    stage: MvpStage
    reason_codes: tuple[str, ...]
    strategy_evaluation: BaselineEvaluation
    as_of: datetime
    classification: PortfolioClassification | None = None
    decision: EngineDecision | None = None
    universe: UniverseEligibility | None = None
    risk_result: RiskResult | None = None
    allocation: CandidateAllocation | None = None
    pre_submit: PreSubmitCheckResult | None = None
    executable_plan: ExecutablePlan | None = None
    engine_version: str = MVP_POLICY_ENGINE_VERSION

    @property
    def is_execution_intent(self) -> bool:
        return self.result is MvpResult.EXECUTION_INTENT

    @property
    def is_no_trade(self) -> bool:
        return self.result is MvpResult.NO_TRADE

    @property
    def is_no_new_entry(self) -> bool:
        return self.result is MvpResult.NO_NEW_ENTRY


def _to_position_exposure(
    *,
    symbol: str,
    sector: str,
    cluster: str,
    source_id: str,
    strategy_id: str,
    open_risk_amount: Decimal,
    notional: Decimal,
) -> PositionExposure:
    """Build a Risk Engine ``PositionExposure`` from scalar values only.

    The fields arrive as parameters (never as candidate attribute accesses), so
    an existing position's classification is taken from the attributed system
    record, exactly as the portfolio-fabrication guardrail requires.
    """
    return PositionExposure(
        symbol=symbol,
        sector=sector,
        cluster=cluster,
        source_id=source_id,
        strategy_id=strategy_id,
        open_risk_amount=open_risk_amount,
        notional=notional,
    )


class MvpTradingPolicy:
    """Deterministic strategy -> canonical execution-intent orchestrator."""

    def __init__(self, config: MvpPolicyConfig) -> None:
        self._config = config
        self._strategy = BaselineTrendBreakoutStrategy(config.strategy_policy)
        self._manifest_evaluator = StrategyManifestEvaluator()
        self._fusion = SignalFusion(config.fusion_policy)
        self._decision = DecisionEngine(config.decision_policy)
        self._risk = RiskEngine(config.risk_policy)
        self._allocator = PortfolioAllocator(config.allocation_policy)
        self._pre_submit = FinalPreSubmitCheck(config.pre_submit_policy)

    def evaluate(
        self,
        instrument_id: UUID,
        *,
        bars: BarSeries,
        candidate: MvpCandidateFacts,
        universe: UniverseFacts,
        account: MvpAccountSnapshot,
        held_positions: Sequence[BrokerHeldPosition],
        system_records: Sequence[SystemPositionRecord],
        execution: MvpExecutionInputs,
        as_of: datetime,
        calendar: TradingCalendar | None = None,
        active_halts: Sequence[HaltRecord] = (),
    ) -> MvpPolicyOutcome:
        if as_of.tzinfo is None:
            raise MvpTradingPolicyError("as_of must be timezone-aware")

        config = self._config

        # -- 1. STRATEGY: is there a valid opportunity? -----------------------
        # The session comes from the canonical calendar (fail closed on an
        # unknown session: the strategy requires an authoritative REGULAR
        # session and treats None as SESSION_UNKNOWN).
        session = calendar.session_status(as_of) if calendar is not None else None
        evaluation = self._strategy.evaluate(
            instrument_id, bars=bars, as_of=as_of, session=session
        )

        # -- 2. canonical decision path (NO TRADE is routed, never dropped) ---
        assessment_ref = f"assessment:{evaluation.candidate_id}:{TECHNICAL_BREAKOUT_KEY}"
        assessments = {TECHNICAL_BREAKOUT_KEY: evaluation.assessment}
        manifest_evaluation = self._manifest_evaluator.evaluate(
            config.manifest, assessments, as_of=as_of
        )
        fusion = self._fusion.fuse(
            (
                FusionInput(
                    assessment_ref=assessment_ref,
                    assessment=evaluation.assessment,
                    source_id=candidate.source_id,
                    cluster_key=assessment_ref,
                ),
            ),
            manifest_evaluation,
            as_of=as_of,
        )
        decision = self._decision.decide(
            evaluation.candidate_id, fusion, manifest_evaluation, as_of=as_of
        )

        # A NO-TRADE strategy result (no triggered signal) or any non-APPROVE
        # decision terminates as NO_TRADE. The strategy decided "no opportunity"
        # — nothing downstream can manufacture one.
        if evaluation.signal is None or decision.result.outcome is not DecisionOutcome.APPROVE:
            reasons = (
                *evaluation.no_trade_reasons,
                *decision.explanation.blocking_reasons,
                f"DECISION_{decision.result.outcome.value}",
            )
            return MvpPolicyOutcome(
                result=MvpResult.NO_TRADE,
                stage=MvpStage.DECISION,
                reason_codes=tuple(dict.fromkeys(reasons)),
                strategy_evaluation=evaluation,
                decision=decision,
                as_of=as_of,
            )
        signal = evaluation.signal

        # -- 3. UNIVERSE eligibility (S&P membership, ADV20, price, spread) ----
        eligibility = evaluate_universe_eligibility(
            universe, config.universe_policy, as_of=as_of
        )
        if not eligibility.eligible:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.UNIVERSE,
                reason_codes=eligibility.reason_codes,
                strategy_evaluation=evaluation,
                decision=decision,
                universe=eligibility,
                as_of=as_of,
            )
        assert eligibility.gics_sector is not None
        gics_sector = eligibility.gics_sector

        # -- 4. ENTRY TIMING: only the NEXT RTH session may enter -------------
        timing_ok, timing_reasons = entry_timing_eligible(
            signal.breakout_bar_end_at, as_of, calendar
        )
        if not timing_ok:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.ENTRY_TIMING,
                reason_codes=timing_reasons,
                strategy_evaluation=evaluation,
                decision=decision,
                universe=eligibility,
                as_of=as_of,
            )

        # -- 5. POSITION ATTRIBUTION: unknown position blocks a new entry -----
        classification = classify_positions(held_positions, system_records, as_of=as_of)
        if classification.new_entry_blocked:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.POSITION_ATTRIBUTION,
                reason_codes=classification.block_reason_codes,
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                as_of=as_of,
            )

        # -- 6. ACCOUNT STATE: any unknown critical fact fails closed ---------
        account_reasons = self._account_unknown_reasons(account)
        if account_reasons:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.ACCOUNT_STATE,
                reason_codes=account_reasons,
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                as_of=as_of,
            )
        assert account.broker_net_liquidation_value is not None
        assert account.simulation_equity_cap is not None
        assert account.session_pnl is not None
        assert account.start_of_day_equity is not None
        equity = min(account.broker_net_liquidation_value, account.simulation_equity_cap)

        # -- 7. DAILY LOSS vs START-OF-DAY equity (authoritative 1.5%) --------
        daily_ok, daily_reasons = daily_loss_within_limit(
            account.session_pnl,
            account.start_of_day_equity,
            config.safety_policy.daily_loss_limit_pct,
        )
        if not daily_ok:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.DAILY_LOSS,
                reason_codes=daily_reasons,
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                as_of=as_of,
            )

        # -- 8. SIZING: fixed-fractional PROPOSAL (risk decides the final size)
        risk_budget = config.risk_policy.risk_per_trade_pct * equity
        requested_quantity = (risk_budget / signal.per_share_risk).to_integral_value(
            rounding=ROUND_FLOOR
        )
        if requested_quantity < 1:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.SIZING,
                reason_codes=("INSUFFICIENT_RISK_BUDGET_FOR_ONE_SHARE",),
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                as_of=as_of,
            )

        # -- 9. RISK: absolute veto, reduce-only -------------------------------
        exposures = tuple(
            _to_position_exposure(
                symbol=exposure.symbol,
                sector=exposure.sector,
                cluster=exposure.cluster,
                source_id=exposure.source_id,
                strategy_id=exposure.strategy_id,
                open_risk_amount=exposure.open_risk_amount,
                notional=exposure.notional,
            )
            for exposure in classification.attributed_exposures
        )
        risk_account = AccountState(
            broker_net_liquidation_value=account.broker_net_liquidation_value,
            simulation_equity_cap=account.simulation_equity_cap,
            session_pnl=account.session_pnl,
            open_positions=exposures,
            observed_at=account.observed_at,
        )
        risk_request = TradeRiskRequest(
            symbol=candidate.symbol,
            sector=gics_sector,
            cluster=candidate.cluster,
            source_id=candidate.source_id,
            strategy_id=config.strategy_policy.strategy_id,
            environment=execution.environment,
            requested_quantity=requested_quantity,
            entry_price=signal.planned_entry_price,
            stop_price=signal.structural_stop,
        )
        risk_result = self._risk.evaluate(
            decision, risk_request, risk_account, active_halts=active_halts, as_of=as_of
        )
        if risk_result.outcome is RiskDecisionOutcome.VETO:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.RISK,
                reason_codes=risk_result.reason_codes,
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                risk_result=risk_result,
                as_of=as_of,
            )

        # -- 10. GICS-SECTOR caps: max 3 positions and 0.75% planned risk -----
        sector_ok, sector_reasons = sector_within_caps(
            gics_sector,
            risk_result.approved_risk_amount,
            classification.attributed_exposures,
            max_positions_per_sector=config.safety_policy.max_positions_per_sector,
            max_sector_planned_risk_pct=config.safety_policy.max_sector_planned_risk_pct,
            risk_budget_equity=equity,
        )
        if not sector_ok:
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.SECTOR,
                reason_codes=sector_reasons,
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                risk_result=risk_result,
                as_of=as_of,
            )

        # -- 11. PORTFOLIO ALLOCATION -----------------------------------------
        portfolio_positions = tuple(
            PortfolioPosition(
                symbol=exposure.symbol,
                sector=exposure.sector,
                theme=exposure.cluster,
                cluster=exposure.cluster,
                source_id=exposure.source_id,
                strategy_id=exposure.strategy_id,
                open_risk_amount=exposure.open_risk_amount,
                notional=exposure.notional,
            )
            for exposure in classification.attributed_exposures
        )
        allocation_plan = self._allocator.allocate(
            [
                AllocationCandidate(
                    candidate_id=str(evaluation.candidate_id),
                    risk_result=risk_result,
                    symbol=candidate.symbol,
                    sector=gics_sector,
                    theme=candidate.cluster,
                    cluster=candidate.cluster,
                    source_id=candidate.source_id,
                    strategy_id=config.strategy_policy.strategy_id,
                    entry_price=signal.planned_entry_price,
                    per_share_risk=signal.per_share_risk,
                    priority_score=fusion.score,
                )
            ],
            PortfolioState(
                risk_budget_equity=equity,
                open_positions=portfolio_positions,
                observed_at=account.observed_at,
            ),
            as_of=as_of,
        )
        allocation = allocation_plan.allocations[0]
        if allocation.action in (PortfolioAction.REJECT, PortfolioAction.MANUAL) or (
            allocation.allocated_quantity <= 0
        ):
            return MvpPolicyOutcome(
                result=MvpResult.NO_NEW_ENTRY,
                stage=MvpStage.ALLOCATION,
                reason_codes=allocation.reason_codes or ("ALLOCATION_BLOCKED",),
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                risk_result=risk_result,
                allocation=allocation,
                as_of=as_of,
            )

        # -- 12. FINAL PRE-SUBMIT: mint the sealed canonical execution intent -
        pre_submit_request = PreSubmitRequest(
            allocation=allocation,
            risk_result=risk_result,
            decision=decision,
            risk_request=risk_request,
            symbol=candidate.symbol,
            strategy_id=config.strategy_policy.strategy_id,
            source_id=candidate.source_id,
            environment=execution.environment,
            planned_entry_price=signal.planned_entry_price,
            planned_stop_price=signal.structural_stop,
            side=execution.side,
            account_alias=execution.account_alias,
            order_type=execution.order_type,
            time_in_force=execution.time_in_force,
            client_order_id=execution.client_order_id,
            broker_instrument_ref=execution.broker_instrument_ref,
            broker_exchange=execution.broker_exchange,
            broker_currency=execution.broker_currency,
        )
        pre_submit = self._pre_submit.check(
            pre_submit_request,
            execution.refreshed_market,
            execution.refreshed_account,
            execution.approval,
            ExecutionContext(
                active_halts=tuple(active_halts),
                broker_healthy=execution.broker_healthy,
                reconciliation_required=execution.reconciliation_required,
            ),
            as_of=as_of,
        )
        if (
            pre_submit.action in (PreSubmitAction.ALLOW, PreSubmitAction.RESIZE)
            and pre_submit.authorized_plan is not None
        ):
            return MvpPolicyOutcome(
                result=MvpResult.EXECUTION_INTENT,
                stage=MvpStage.EXECUTION_INTENT,
                reason_codes=pre_submit.reason_codes,
                strategy_evaluation=evaluation,
                classification=classification,
                decision=decision,
                universe=eligibility,
                risk_result=risk_result,
                allocation=allocation,
                pre_submit=pre_submit,
                executable_plan=pre_submit.authorized_plan,
                as_of=as_of,
            )
        return MvpPolicyOutcome(
            result=MvpResult.NO_NEW_ENTRY,
            stage=MvpStage.PRE_SUBMIT,
            reason_codes=pre_submit.reason_codes,
            strategy_evaluation=evaluation,
            classification=classification,
            decision=decision,
            universe=eligibility,
            risk_result=risk_result,
            allocation=allocation,
            pre_submit=pre_submit,
            as_of=as_of,
        )

    @staticmethod
    def _account_unknown_reasons(account: MvpAccountSnapshot) -> tuple[str, ...]:
        """Reason codes for any unknown critical account fact (fail closed).

        A numeric session P&L is usable ONLY with an authoritative ``PNL_OK``
        status: a finite value with no status, or any non-OK status
        (TIMEOUT / REQUEST_ERROR / UNSET / STALE), is UNKNOWN and blocks.
        """
        reasons: list[str] = []
        if account.broker_net_liquidation_value is None:
            reasons.append("NLV_UNKNOWN")
        if account.simulation_equity_cap is None:
            reasons.append("EQUITY_CAP_UNKNOWN")
        if account.start_of_day_equity is None:
            reasons.append("START_OF_DAY_EQUITY_UNKNOWN")
        if account.session_pnl is None:
            reasons.append("SESSION_PNL_UNKNOWN")
        elif account.session_pnl_status is not SessionPnlStatus.OK:
            # A finite P&L without an authoritative OK status is not usable.
            status = (
                account.session_pnl_status.value
                if account.session_pnl_status is not None
                else "STATUS_MISSING"
            )
            reasons.append(f"SESSION_PNL_NOT_OK_{status}")
        return tuple(reasons)


__all__ = [
    "MVP_POLICY_ENGINE_VERSION",
    "MvpAccountSnapshot",
    "MvpCandidateFacts",
    "MvpExecutionInputs",
    "MvpPolicyOutcome",
    "MvpResult",
    "MvpStage",
    "MvpTradingPolicy",
    "MvpTradingPolicyError",
]
