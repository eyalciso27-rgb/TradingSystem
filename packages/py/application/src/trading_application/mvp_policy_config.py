"""MVP policy configuration bundle for the baseline strategy pipeline.

Assembles the canonical engine policies (fusion, decision, risk, allocation,
pre-submit) that carry the ``baseline-trend-breakout-v1`` opportunity through
the existing decision/risk/pre-submit path. These are POLICY inputs — the
engines invent nothing — and every value is either an owner-frozen risk limit
(cited below) or an explicit MVP-conservative default.

Nothing here begins Paper Auto or Live: ``require_manual_approval`` is True (the
Paper MVP is manual), the "do not chase" price rule cancels rather than resizes,
and any adverse risk change cancels. Environments are passed by the caller and
are never LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_contracts.enums import (
    ActionTier,
    DecisionOutcome,
    PortfolioAction,
    SignalFamily,
)

from trading_application.baseline_strategy import BaselineTrendBreakoutPolicy
from trading_application.baseline_strategy_config import (
    baseline_trend_breakout_v1_manifest,
    baseline_trend_breakout_v1_policy,
)
from trading_application.decision_engine import DecisionPolicy
from trading_application.portfolio_allocator import PortfolioAllocationPolicy
from trading_application.pre_submit_check import ChangeAction, PreSubmitPolicy, StaleAction
from trading_application.risk_engine import RiskPolicy
from trading_application.signal_fusion import FusionPolicy
from trading_application.strategy_manifest import StrategyManifest

MVP_POLICY_VERSION = "1.0.0"


def mvp_fusion_policy() -> FusionPolicy:
    """A single-family technical fusion policy.

    The baseline is a pure, deterministic technical breakout with NO ML gate,
    so TECHNICAL carries full weight and the unverified multiplier is 1 (the
    calibration gate exists for probabilistic model outputs, of which there are
    none here). A TRIGGERED long assessment (score 1) reaches AUTO_FULL; a gate
    failure is forced to WATCH by the fusion engine regardless of arithmetic.
    """
    return FusionPolicy(
        policy_version=MVP_POLICY_VERSION,
        family_weights=((SignalFamily.TECHNICAL, Decimal(1)),),
        unverified_multiplier=Decimal(1),
        action_tier_rules=(
            (ActionTier.AUTO_FULL, Decimal("0.5")),
            (ActionTier.WATCH, Decimal(0)),
        ),
    )


def mvp_decision_policy() -> DecisionPolicy:
    """Tier->outcome mapping. An unsatisfied gate or a conflict never APPROVES."""
    return DecisionPolicy(
        policy_version=MVP_POLICY_VERSION,
        tier_outcomes=(
            (ActionTier.AUTO_FULL, DecisionOutcome.APPROVE),
            (ActionTier.AUTO_REDUCED, DecisionOutcome.APPROVE),
            (ActionTier.MANUAL, DecisionOutcome.NEEDS_REVIEW),
            (ActionTier.SHADOW, DecisionOutcome.SHADOW),
            (ActionTier.WATCH, DecisionOutcome.WATCH),
            (ActionTier.REJECT, DecisionOutcome.REJECT),
            (ActionTier.HALT, DecisionOutcome.REJECT),
        ),
        unsatisfied_gate_outcome=DecisionOutcome.WATCH,
        conflict_outcome=DecisionOutcome.NEEDS_REVIEW,
    )


def mvp_risk_policy() -> RiskPolicy:
    """The owner-frozen risk envelope, mapped onto the Risk Engine fields.

    Owner provenance (baseline-trend-breakout-v1):

    * ``risk.per_trade`` 0.25% -> ``risk_per_trade_pct``.
    * ``risk.max_concurrent_positions`` 8 -> ``max_concurrent_positions``;
      the implied aggregate planned risk (8 x 0.25%) -> ``max_total_open_risk_pct``.
    * ``risk.daily_loss_limit`` 1.5% (a single hard stop) -> both
      ``daily_soft_halt_pct`` and ``daily_hard_halt_pct`` (soft==hard, so only
      the hard branch fires).
    * ``risk.max_portfolio_exposure`` 75% gross -> the notional caps (no single
      name / order / sector may exceed the gross ceiling).
    * ``risk.max_concurrent_positions`` GICS-sector risk cap 0.75% of equity ->
      ``max_correlated_cluster_pct`` = 0.375 (0.75% expressed as a share of the
      2% open-risk envelope). MVP: the correlated cluster stands in for the
      owner's GICS sector risk cap.
    * No owner per-source cap for the single MVP strategy ->
      ``max_source_open_risk_share`` = 1 (non-binding).
    """
    return RiskPolicy(
        policy_version=MVP_POLICY_VERSION,
        risk_per_trade_pct=Decimal("0.0025"),
        max_total_open_risk_pct=Decimal("0.02"),
        daily_soft_halt_pct=Decimal("0.015"),
        daily_hard_halt_pct=Decimal("0.015"),
        max_concurrent_positions=8,
        max_symbol_notional_pct=Decimal("0.75"),
        max_single_order_notional_pct=Decimal("0.75"),
        max_sector_notional_pct=Decimal("0.75"),
        max_correlated_cluster_pct=Decimal("0.375"),
        max_source_open_risk_share=Decimal("1"),
    )


def mvp_allocation_policy() -> PortfolioAllocationPolicy:
    """Portfolio caps for the single-strategy MVP (mirrors the risk envelope)."""
    return PortfolioAllocationPolicy(
        policy_version=MVP_POLICY_VERSION,
        max_total_open_risk_pct=Decimal("0.02"),
        max_symbol_notional_pct=Decimal("0.75"),
        max_sector_notional_pct=Decimal("0.75"),
        max_theme_notional_pct=Decimal("0.75"),
        max_strategy_risk_share=Decimal("1"),
        max_source_risk_share=Decimal("1"),
        max_cluster_risk_share=Decimal("0.375"),
        max_concurrent_positions=8,
        insufficient_capacity_action=PortfolioAction.REJECT,
    )


def mvp_pre_submit_policy() -> PreSubmitPolicy:
    """Final pre-submit gate. Manual, fail-closed, "do not chase"."""
    return PreSubmitPolicy(
        policy_version=MVP_POLICY_VERSION,
        max_price_slippage_pct=Decimal("0.005"),
        max_risk_increase_pct=Decimal("0"),
        market_max_staleness_seconds=60,
        account_max_staleness_seconds=60,
        decision_max_age_seconds=86_400,
        approval_max_age_seconds=3_600,
        require_manual_approval=True,
        price_change_action=ChangeAction.CANCEL,
        risk_change_action=ChangeAction.CANCEL,
        stale_data_action=StaleAction.CANCEL,
    )


@dataclass(frozen=True, slots=True)
class MvpPolicyConfig:
    """The full set of policies the MVP trading policy runs with."""

    strategy_policy: BaselineTrendBreakoutPolicy
    manifest: StrategyManifest
    fusion_policy: FusionPolicy
    decision_policy: DecisionPolicy
    risk_policy: RiskPolicy
    allocation_policy: PortfolioAllocationPolicy
    pre_submit_policy: PreSubmitPolicy


def default_mvp_policy_config() -> MvpPolicyConfig:
    """The default owner-frozen MVP configuration bundle."""
    return MvpPolicyConfig(
        strategy_policy=baseline_trend_breakout_v1_policy(),
        manifest=baseline_trend_breakout_v1_manifest(),
        fusion_policy=mvp_fusion_policy(),
        decision_policy=mvp_decision_policy(),
        risk_policy=mvp_risk_policy(),
        allocation_policy=mvp_allocation_policy(),
        pre_submit_policy=mvp_pre_submit_policy(),
    )


__all__ = [
    "MVP_POLICY_VERSION",
    "MvpPolicyConfig",
    "default_mvp_policy_config",
    "mvp_allocation_policy",
    "mvp_decision_policy",
    "mvp_fusion_policy",
    "mvp_pre_submit_policy",
    "mvp_risk_policy",
]
