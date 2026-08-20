"""Risk engine (IMP-0904).

Authority: Decision_Contract_v0.8 DC-007 (the Risk Engine holds ABSOLUTE
veto; it may reject, reduce or tighten protection and may never expand risk
beyond policy) and section 12.3 (canonical RiskDecisionOutcome
APPROVE/REDUCE/VETO); Policy_Parameters_and_Initial_Assumptions_v0.5
section 2 (the canonical equity formula: ``risk_budget_equity =
min(broker_net_liquidation_value, configured_simulation_equity_cap)``,
POL-AC-03: the smaller of NLV and cap), section 3 (safety envelope:
per-trade risk, total open risk, daily soft/hard halts, concurrent
positions, symbol/single-order/sector notional caps, correlated-cluster and
source open-risk shares) and section 3.1 (hard rules: no averaging down, one
position per symbol, no pyramiding, no entry while a halt is active, the
Risk Engine may only REDUCE quantity — never increase — and quantity is
rounded DOWN so the risk budget is never exceeded); Security spec section 12
(kill switch blocks entries) via the IMP-0404 halt gate.

Deterministic, pure evaluation over FROZEN inputs (ADR-0014): the IMP-0903
decision, account/portfolio fixtures, active-halt fixtures and a
``RiskPolicy``. Output is a canonical risk result only — never an
Order/OrderIntent/broker action and never a submission.

Fail-closed rules: a non-APPROVE decision, an active halt, a missing
protective stop, or missing/unreliable account data can only VETO. Every
limit value comes from the policy — nothing is invented in code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading_contracts.enums import DecisionOutcome, Environment, RiskDecisionOutcome

from trading_application.decision_engine import EngineDecision
from trading_application.emergency import EntryContext, HaltRecord, match_halt
from trading_application.execution_identity import (
    decision_identity,
    risk_policy_identity,
    risk_request_identity,
)

RISK_ENGINE_VERSION = "risk-engine-1.0.0"


class RiskEngineError(Exception):
    """Fail-closed risk-engine failure (bad policy/inputs, lookahead)."""


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """The safety envelope — ALL limits are configuration (section 3)."""

    policy_version: str
    risk_per_trade_pct: Decimal
    max_total_open_risk_pct: Decimal
    daily_soft_halt_pct: Decimal
    daily_hard_halt_pct: Decimal
    max_concurrent_positions: int
    max_symbol_notional_pct: Decimal
    max_single_order_notional_pct: Decimal
    max_sector_notional_pct: Decimal
    max_correlated_cluster_pct: Decimal
    max_source_open_risk_share: Decimal

    def __post_init__(self) -> None:
        if not self.policy_version.strip():
            raise RiskEngineError("policy_version is mandatory")
        fractions = (
            self.risk_per_trade_pct,
            self.max_total_open_risk_pct,
            self.daily_soft_halt_pct,
            self.daily_hard_halt_pct,
            self.max_symbol_notional_pct,
            self.max_single_order_notional_pct,
            self.max_sector_notional_pct,
            self.max_correlated_cluster_pct,
            self.max_source_open_risk_share,
        )
        if any(fraction <= 0 for fraction in fractions):
            raise RiskEngineError("all envelope fractions must be positive")
        if self.daily_soft_halt_pct > self.daily_hard_halt_pct:
            raise RiskEngineError("soft halt must not exceed hard halt")
        if self.max_concurrent_positions < 1:
            raise RiskEngineError("max_concurrent_positions must be >= 1")


@dataclass(frozen=True, slots=True)
class PositionExposure:
    """One existing open position (portfolio fixture)."""

    symbol: str
    sector: str
    cluster: str
    source_id: str
    strategy_id: str
    open_risk_amount: Decimal
    notional: Decimal


@dataclass(frozen=True, slots=True)
class AccountState:
    """Frozen account/portfolio snapshot (fixture)."""

    broker_net_liquidation_value: Decimal | None
    simulation_equity_cap: Decimal | None  # mandatory input, no default (section 2)
    session_pnl: Decimal
    open_positions: tuple[PositionExposure, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class TradeRiskRequest:
    """The proposed entry to be risk-checked (never an order)."""

    symbol: str
    sector: str
    cluster: str
    source_id: str
    strategy_id: str
    environment: Environment
    requested_quantity: Decimal
    entry_price: Decimal
    stop_price: Decimal | None  # protective stop; None fails closed

    def __post_init__(self) -> None:
        if self.requested_quantity <= 0 or self.entry_price <= 0:
            raise RiskEngineError("quantity and entry price must be positive")


@dataclass(frozen=True, slots=True)
class RiskCheck:
    """One executed check with its limit and observation (audit/debug)."""

    name: str
    passed: bool
    limit: str
    observed: str
    effect: str  # "PASS" | "REDUCE" | "VETO"


@dataclass(frozen=True, slots=True)
class RiskResult:
    """Canonical risk evaluation (RiskDecisionOutcome 12.3 + explanation)."""

    outcome: RiskDecisionOutcome
    approved_quantity: Decimal
    approved_risk_amount: Decimal
    max_loss_amount: Decimal
    risk_budget_equity: Decimal
    decision_ref: str
    #: THE exact question this answers. Without it a result is only its output
    #: numbers, and two different requests whose numbers coincide would carry
    #: the same identity -- so an authorization derived from one could be
    #: presented for the other.
    request_ref: str
    policy_ref: str
    engine_version: str
    checks: tuple[RiskCheck, ...]
    reason_codes: tuple[str, ...]
    input_refs: tuple[str, ...]


class RiskEngine:
    """Deterministic absolute-veto risk engine over frozen inputs."""

    def __init__(self, policy: RiskPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> RiskPolicy:
        """The policy this engine actually applies.

        Supported, so a caller can verify that the policy its configuration
        names is the policy the veto is computed from, instead of reaching for
        a private attribute and hoping.
        """
        return self._policy

    def evaluate(
        self,
        decision: EngineDecision,
        request: TradeRiskRequest,
        account: AccountState,
        *,
        active_halts: Sequence[HaltRecord] = (),
        as_of: datetime,
    ) -> RiskResult:
        if as_of.tzinfo is None:
            raise RiskEngineError("as_of must be timezone-aware")
        if account.observed_at > as_of or decision.result.decided_at > as_of:
            raise RiskEngineError("lookahead: inputs are newer than as_of")

        policy = self._policy
        checks: list[RiskCheck] = []
        reasons: list[str] = []
        vetoed = False

        def veto(name: str, limit: str, observed: str, code: str) -> None:
            nonlocal vetoed
            vetoed = True
            checks.append(RiskCheck(name, False, limit, observed, "VETO"))
            reasons.append(code)

        # -- 1. only an APPROVE decision may proceed (never silently) ---------
        if decision.result.outcome is not DecisionOutcome.APPROVE:
            veto(
                "decision_gate",
                "outcome == APPROVE",
                decision.result.outcome.value,
                "DECISION_NOT_APPROVED",
            )
        else:
            checks.append(
                RiskCheck("decision_gate", True, "outcome == APPROVE", "APPROVE", "PASS")
            )

        # -- 2. kill switch / halt: absolute block (section 12) ----------------
        halt = match_halt(
            tuple(active_halts),
            EntryContext(
                environment=request.environment,
                strategy_id=request.strategy_id,
                symbol=request.symbol,
                source_id=request.source_id,
            ),
        )
        if halt is not None:
            veto("halt_gate", "no active halt", halt.scope.value, "HALT_ACTIVE")
        else:
            checks.append(RiskCheck("halt_gate", True, "no active halt", "none", "PASS"))

        # -- 3. canonical equity (section 2, POL-AC-03) — fail closed ----------
        nlv = account.broker_net_liquidation_value
        cap = account.simulation_equity_cap
        if nlv is None or cap is None or nlv <= 0 or cap <= 0:
            veto(
                "risk_budget_equity",
                "min(NLV, simulation_equity_cap) with both present",
                f"nlv={nlv} cap={cap}",
                "ACCOUNT_DATA_MISSING",
            )
            equity = Decimal(0)
        else:
            equity = min(nlv, cap)
            checks.append(
                RiskCheck(
                    "risk_budget_equity",
                    True,
                    "min(NLV, simulation_equity_cap)",
                    str(equity),
                    "PASS",
                )
            )

        # -- 4. protective stop is mandatory ------------------------------------
        stop = request.stop_price
        per_share_risk = Decimal(0)
        if stop is None or stop <= 0 or stop == request.entry_price:
            veto(
                "protective_stop",
                "stop present and distinct from entry",
                str(stop),
                "NO_PROTECTIVE_STOP",
            )
        else:
            per_share_risk = abs(request.entry_price - stop)
            checks.append(
                RiskCheck("protective_stop", True, "present", str(stop), "PASS")
            )

        # -- 5. daily loss halts (section 3) -------------------------------------
        if equity > 0:
            daily_loss_pct = min(Decimal(0), account.session_pnl / equity)
            if -daily_loss_pct >= policy.daily_hard_halt_pct:
                veto(
                    "daily_loss",
                    f"loss < {policy.daily_hard_halt_pct}",
                    str(-daily_loss_pct),
                    "DAILY_HARD_HALT",
                )
            elif -daily_loss_pct >= policy.daily_soft_halt_pct:
                veto(
                    "daily_loss",
                    f"loss < {policy.daily_soft_halt_pct}",
                    str(-daily_loss_pct),
                    "DAILY_SOFT_HALT",
                )
            else:
                checks.append(
                    RiskCheck(
                        "daily_loss",
                        True,
                        f"loss < {policy.daily_soft_halt_pct}",
                        str(-daily_loss_pct),
                        "PASS",
                    )
                )

        # -- 6. structural rules (section 3.1): one position per symbol, count --
        if any(p.symbol == request.symbol for p in account.open_positions):
            veto(
                "single_position_per_symbol",
                "no existing position in symbol",
                request.symbol,
                "SYMBOL_ALREADY_OPEN",
            )
        else:
            checks.append(
                RiskCheck("single_position_per_symbol", True, "none open", "ok", "PASS")
            )
        if len(account.open_positions) >= policy.max_concurrent_positions:
            veto(
                "max_concurrent_positions",
                str(policy.max_concurrent_positions),
                str(len(account.open_positions)),
                "MAX_POSITIONS_REACHED",
            )
        else:
            checks.append(
                RiskCheck(
                    "max_concurrent_positions",
                    True,
                    str(policy.max_concurrent_positions),
                    str(len(account.open_positions)),
                    "PASS",
                )
            )

        if vetoed:
            return self._result(
                RiskDecisionOutcome.VETO, Decimal(0), Decimal(0), equity,
                decision, request, checks, reasons,
            )

        # -- 7. quantity budgets: reduce-only, rounded DOWN (section 3.1) -------
        quantity = request.requested_quantity
        assert per_share_risk > 0 and equity > 0

        def cap_quantity(name: str, allowed: Decimal, per_unit: Decimal, code: str) -> None:
            nonlocal quantity
            if per_unit <= 0:
                return
            max_units = (allowed / per_unit).to_integral_value(rounding="ROUND_FLOOR")
            if quantity > max_units:
                checks.append(
                    RiskCheck(
                        name,
                        False,
                        f"allowed={allowed}",
                        f"requested_units={quantity}",
                        "REDUCE",
                    )
                )
                reasons.append(code)
                quantity = max_units
            else:
                checks.append(
                    RiskCheck(name, True, f"allowed={allowed}", f"units={quantity}", "PASS")
                )

        # per-trade risk budget
        cap_quantity(
            "per_trade_risk",
            policy.risk_per_trade_pct * equity,
            per_share_risk,
            "REDUCED_PER_TRADE_RISK",
        )
        # total open risk envelope
        open_risk = sum((p.open_risk_amount for p in account.open_positions), Decimal(0))
        cap_quantity(
            "total_open_risk",
            max(policy.max_total_open_risk_pct * equity - open_risk, Decimal(0)),
            per_share_risk,
            "REDUCED_TOTAL_OPEN_RISK",
        )
        # notional caps (symbol / single order / sector)
        cap_quantity(
            "single_order_notional",
            policy.max_single_order_notional_pct * equity,
            request.entry_price,
            "REDUCED_ORDER_NOTIONAL",
        )
        cap_quantity(
            "symbol_notional",
            policy.max_symbol_notional_pct * equity,
            request.entry_price,
            "REDUCED_SYMBOL_NOTIONAL",
        )
        sector_notional = sum(
            (p.notional for p in account.open_positions if p.sector == request.sector),
            Decimal(0),
        )
        cap_quantity(
            "sector_notional",
            max(policy.max_sector_notional_pct * equity - sector_notional, Decimal(0)),
            request.entry_price,
            "REDUCED_SECTOR_NOTIONAL",
        )
        # correlated cluster + source shares of the open-risk envelope
        envelope = policy.max_total_open_risk_pct * equity
        cluster_risk = sum(
            (p.open_risk_amount for p in account.open_positions if p.cluster == request.cluster),
            Decimal(0),
        )
        cap_quantity(
            "correlated_cluster_risk",
            max(policy.max_correlated_cluster_pct * envelope - cluster_risk, Decimal(0)),
            per_share_risk,
            "REDUCED_CLUSTER_RISK",
        )
        source_risk = sum(
            (
                p.open_risk_amount
                for p in account.open_positions
                if p.source_id == request.source_id
            ),
            Decimal(0),
        )
        cap_quantity(
            "source_open_risk_share",
            max(policy.max_source_open_risk_share * envelope - source_risk, Decimal(0)),
            per_share_risk,
            "REDUCED_SOURCE_RISK",
        )

        if quantity <= 0:
            reasons.append("NO_ROOM_WITHIN_ENVELOPE")
            return self._result(
                RiskDecisionOutcome.VETO, Decimal(0), Decimal(0), equity,
                decision, request, checks, reasons,
            )

        approved_risk = quantity * per_share_risk
        outcome = (
            RiskDecisionOutcome.REDUCE
            if quantity < request.requested_quantity
            else RiskDecisionOutcome.APPROVE
        )
        if outcome is RiskDecisionOutcome.APPROVE:
            reasons.append("WITHIN_SAFETY_ENVELOPE")
        return self._result(
            outcome, quantity, approved_risk, equity, decision, request, checks, reasons
        )

    def _result(
        self,
        outcome: RiskDecisionOutcome,
        quantity: Decimal,
        approved_risk: Decimal,
        equity: Decimal,
        decision: EngineDecision,
        request: TradeRiskRequest,
        checks: list[RiskCheck],
        reasons: list[str],
    ) -> RiskResult:
        return RiskResult(
            outcome=outcome,
            approved_quantity=quantity,
            approved_risk_amount=approved_risk,
            max_loss_amount=approved_risk,
            risk_budget_equity=equity,
            decision_ref=decision_identity(decision),
            request_ref=risk_request_identity(request),
            policy_ref=risk_policy_identity(self._policy),
            engine_version=RISK_ENGINE_VERSION,
            checks=tuple(checks),
            reason_codes=tuple(dict.fromkeys(reasons)),
            input_refs=tuple(decision.explanation.input_refs),
        )


__all__ = [
    "RISK_ENGINE_VERSION",
    "AccountState",
    "PositionExposure",
    "RiskCheck",
    "RiskEngine",
    "RiskEngineError",
    "RiskPolicy",
    "RiskResult",
    "TradeRiskRequest",
]
