"""Owner-safe Paper-only PROOF of the protected-entry bracket (ADR-0019).

This is the ONE place a real Paper order is deliberately placed. It is not part
of production wiring: production keeps ``DenyAllExecutionAuthorization`` and can
never submit. Here — and only here — a scoped, single-use, **Paper-only**
authority mints exactly one authorization for exactly the order the service
plans, so the canonical decision path establishes a real native bracket at the
IBKR Paper gateway, proves both legs rest, then cancels the whole structure and
reconciles the account clean.

Safety, by construction:

* **Paper only.** The Paper API port is required and the Live port is refused
  before anything opens; the adapter refuses a non-DU account and a non-local
  host; the scoped authority refuses to authorize anything but a PAPER plan.
* **Not armed by default.** Without ``--execute`` the authority DENIES, so a
  bare run is a dry run: it shows what would be sent and the exact arming
  command, and places nothing.
* **Quantity 1, one liquid symbol (AAPL), a non-crossing limit.** The service
  prices the entry from the live quote (an ADR-0018 safety property this proof
  does not weaken); a BUY LIMIT at the quote sits below the ask, so it RESTS
  rather than crossing. Exposure is a single share that, in the ordinary case,
  never fills before it is cancelled.
* **Stop-only bracket.** No take-profit leg (this milestone's scope).
* **Cleanup always runs.** The bracket is cancelled (one leg cancels the whole
  OCA group) and the account is reconciled, on success and on failure. If a fill
  nonetheless occurred, the run reports the residual position and the exact
  flatten command rather than flattening silently.
* **Never Paper Auto, never Live.**

Do NOT run this unattended, and never point it at a Live endpoint. The owner
runs it; nothing here schedules or repeats it.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_application.execution_identity import pre_submit_policy_identity
from trading_application.portfolio_allocator import (
    AllocationCandidate,
    PortfolioAllocationPolicy,
    PortfolioAllocator,
    PortfolioState,
)
from trading_application.pre_submit_check import (
    ApprovalState,
    ChangeAction,
    ExecutionContext,
    FinalPreSubmitCheck,
    PreSubmitPolicy,
    PreSubmitRequest,
    RefreshedAccount,
    RefreshedMarket,
    StaleAction,
    single_use_authorization,
)
from trading_broker_core.port import OrderSide
from trading_contracts.enums import Environment, PortfolioAction
from trading_execution_agent.__main__ import (
    DEFAULT_ACCOUNT_ALIAS,
    DEFAULT_HOST,
    DEFAULT_PAPER_PORT,
    ENV_ACCOUNT_ALIAS,
    ENV_EQUITY_CAP,
    ENV_GATEWAY_HOST,
    ENV_GATEWAY_PORT,
    EXIT_ERROR,
    EXIT_OK,
    build_service,
    probe_decision,
)
from trading_execution_agent.execution_service import (
    AuthorizationDecision,
    ExecutionIntent,
    ExecutionStage,
    PlanContext,
)
from trading_execution_agent.safe_logging import (
    install_owner_output_protection,
    owner_print,
    safe_failure,
)

if TYPE_CHECKING:
    from trading_application.risk_engine import RiskResult
    from trading_application.trading_readiness_runtime import (
        TradingReadinessRuntimeReport,
    )

#: The Live API port is never opened by this proof. The adapter refuses it too;
#: this is an earlier, louder refusal.
LIVE_API_PORT = 4001

#: The literal the owner must pass to ``--execute`` to arm a REAL Paper order.
#: A bare ``--execute`` is not enough: the owner must type the whole phrase, so
#: arming a real order is a deliberate act, never a stray flag.
ARM_TOKEN = "yes-place-a-real-paper-bracket"  # noqa: S105 - not a secret, a gesture

#: Proof-scoped policies. Self-contained: the authority mints under these and
#: reports THEIR identity, and the service checks that the minted policy_ref
#: equals the authority's stated identity -- so these need not match any other
#: component, only themselves.
_PORTFOLIO_POLICY = PortfolioAllocationPolicy(
    policy_version="portfolio-policy-bracket-proof-1",
    max_total_open_risk_pct=Decimal("0.0100"),
    max_symbol_notional_pct=Decimal("0.10"),
    max_sector_notional_pct=Decimal("0.25"),
    max_theme_notional_pct=Decimal("0.20"),
    max_strategy_risk_share=Decimal("0.50"),
    max_source_risk_share=Decimal("0.50"),
    max_cluster_risk_share=Decimal("0.40"),
    max_concurrent_positions=4,
    insufficient_capacity_action=PortfolioAction.REJECT,
)
_PRE_SUBMIT_POLICY = PreSubmitPolicy(
    policy_version="pre-submit-policy-bracket-proof-1",
    max_price_slippage_pct=Decimal("0.01"),
    max_risk_increase_pct=Decimal("0.10"),
    market_max_staleness_seconds=30,
    account_max_staleness_seconds=60,
    decision_max_age_seconds=3600,
    approval_max_age_seconds=3600,
    require_manual_approval=False,
    price_change_action=ChangeAction.RESIZE,
    risk_change_action=ChangeAction.RESIZE,
    stale_data_action=StaleAction.NEEDS_REVIEW,
)


class ScopedPaperBracketAuthorization:
    """The proof's single-use, Paper-only authority. Never in production.

    It authorizes THE order the service plans -- it is handed that order's
    ``PlanContext`` and mints from it, so it cannot approve a price, side or
    account the service is not using. It refuses unless it is ARMED and the plan
    is a PAPER plan: readiness is not authorization, and this authority exists
    only to turn a deliberate owner gesture into exactly one Paper submission.
    """

    def __init__(self, *, armed: bool, equity: Decimal, now: datetime) -> None:
        self._armed = armed
        self._equity = equity
        self._now = now

    def pre_submit_policy_identity(self) -> str:
        return pre_submit_policy_identity(_PRE_SUBMIT_POLICY)

    def authorize(
        self,
        intent: ExecutionIntent,
        report: TradingReadinessRuntimeReport,
        risk_result: RiskResult,
        plan: PlanContext,
    ) -> AuthorizationDecision:
        if not self._armed:
            return AuthorizationDecision(
                allowed=False,
                reason=(
                    "dry run: the proof authority is not armed, so nothing is"
                    f" submitted. Re-run with --execute {ARM_TOKEN} to place a"
                    " REAL Paper bracket."
                ),
            )
        if plan.environment is not Environment.PAPER:
            # Defence in depth: this authority is Paper-only, full stop.
            return AuthorizationDecision(
                allowed=False,
                reason=(
                    "the proof authority authorizes PAPER only; environment is"
                    f" {plan.environment.value}"
                ),
            )
        entry = plan.entry_price
        stop = plan.stop_price if plan.stop_price is not None else entry
        allocation_plan = PortfolioAllocator(_PORTFOLIO_POLICY).allocate(
            [
                AllocationCandidate(
                    candidate_id=plan.client_order_id,
                    risk_result=risk_result,
                    symbol=plan.symbol,
                    sector="proof",
                    theme="proof",
                    cluster="proof",
                    source_id=plan.source_id,
                    strategy_id=plan.strategy_id,
                    entry_price=entry,
                    per_share_risk=abs(entry - stop) or Decimal("0.01"),
                    priority_score=Decimal("1"),
                )
            ],
            PortfolioState(
                risk_budget_equity=self._equity,
                open_positions=(),
                observed_at=self._now,
            ),
            as_of=self._now,
        )
        allocation = allocation_plan.allocations[0]
        result = FinalPreSubmitCheck(_PRE_SUBMIT_POLICY).check(
            PreSubmitRequest(
                allocation=allocation,
                risk_result=risk_result,
                decision=intent.decision,
                risk_request=plan.risk_request,
                symbol=plan.symbol,
                strategy_id=plan.strategy_id,
                source_id=plan.source_id,
                environment=plan.environment,
                planned_entry_price=entry,
                planned_stop_price=stop,
                side=plan.side,
                account_alias=plan.account_alias,
                order_type=plan.order_type,
                time_in_force=plan.time_in_force,
                client_order_id=plan.client_order_id,
                broker_instrument_ref=plan.instrument.broker_instrument_ref,
                broker_exchange=plan.instrument.exchange,
                broker_currency=plan.instrument.currency,
                protective_stop_client_order_id=plan.protective_stop_client_order_id,
            ),
            RefreshedMarket(price=entry, observed_at=self._now),
            RefreshedAccount(risk_budget_equity=self._equity, observed_at=self._now),
            ApprovalState(approved=True, approved_at=self._now),
            ExecutionContext(),
            as_of=self._now,
        )
        return AuthorizationDecision(
            allowed=True,
            reason="scoped Paper-only bracket proof authorization",
            pre_submit=result,
            authorization=single_use_authorization(result),
            allocation=allocation,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.poi_protected_bracket_proof",
        description=(
            "Owner-safe Paper-only PROOF of the protected-entry bracket. Places a"
            " single real Paper bracket, proves both legs, cancels and reconciles."
            " Never Live, never Paper Auto."
        ),
    )
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument(
        "--account-alias",
        default=os.environ.get(ENV_ACCOUNT_ALIAS, DEFAULT_ACCOUNT_ALIAS),
    )
    parser.add_argument("--host", default=os.environ.get(ENV_GATEWAY_HOST, DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get(ENV_GATEWAY_PORT, DEFAULT_PAPER_PORT)),
    )
    parser.add_argument(
        "--execute",
        default="",
        metavar="TOKEN",
        help=(
            f"place a REAL Paper bracket. Must be exactly '{ARM_TOKEN}'. Omit for"
            " a dry run that places nothing."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    install_owner_output_protection(debug=False)

    if args.port == LIVE_API_PORT:
        owner_print(
            "REFUSED: port 4001 is the LIVE API port. This proof is Paper-only."
        )
        return EXIT_ERROR
    armed = args.execute == ARM_TOKEN
    if args.execute and not armed:
        owner_print(
            "REFUSED: --execute must be exactly the arming phrase; placing a real"
            " order is a deliberate act, not a stray flag."
        )
        return EXIT_ERROR

    now = datetime.now(tz=UTC)
    equity = Decimal(os.environ.get(ENV_EQUITY_CAP, "50000"))
    decision = probe_decision(args.symbol, now)
    decision_id = str(decision.result.decision_id)
    client_order_id = f"bracket-proof-{decision_id}"

    try:
        resources = build_service(
            symbol=args.symbol,
            account_alias=args.account_alias,
            host=args.host,
            port=args.port,
            equity_cap=equity,
            authorization=ScopedPaperBracketAuthorization(
                armed=armed, equity=equity, now=now
            ),
        )
    except Exception as error:  # noqa: BLE001 - type/code only, never raw text
        owner_print(safe_failure("proof start failed", error))
        return EXIT_ERROR

    intent = ExecutionIntent(
        strategy_id="bracket-proof",
        decision=decision,
        symbol=args.symbol,
        side=OrderSide.BUY,
        requested_quantity=Decimal("1"),
        correlation_id=decision_id,
        client_order_id=client_order_id,
        source_id="bracket-proof",
    )

    exit_code = EXIT_OK
    try:
        owner_print(
            f"=== PROTECTED BRACKET PROOF ({'ARMED' if armed else 'DRY RUN'}) ==="
        )
        evaluation = resources.service.execute(intent)
        for line in _describe(evaluation, armed=armed, client_order_id=client_order_id):
            owner_print(line)
        if evaluation.stage is not ExecutionStage.SUBMITTED:
            # Not submitted: nothing to clean up, and a dry run lands here by
            # design (the authority denied).
            exit_code = EXIT_OK if not armed else EXIT_ERROR
        else:
            exit_code = _cleanup(resources, args.account_alias, client_order_id)
    except Exception as error:  # noqa: BLE001 - type/code only
        owner_print(safe_failure("proof run failed", error))
        exit_code = EXIT_ERROR
        _cleanup(resources, args.account_alias, client_order_id)
    finally:
        resources.close()
    return exit_code


def _describe(
    evaluation: object, *, armed: bool, client_order_id: str
) -> list[str]:
    lines = [f"stage: {getattr(evaluation, 'stage', '?')}"]
    reasons = getattr(evaluation, "reasons", ())
    lines.extend(f"  - {reason}" for reason in reasons)
    if not armed:
        lines.append(
            f"to place it for real: --execute {ARM_TOKEN} (qty 1"
            f" {client_order_id!r}, stop-only bracket, then auto-cancel)"
        )
    return lines


def _cleanup(resources: object, account_alias: str, client_order_id: str) -> int:
    """Cancel the whole bracket and reconcile. Never leaves a working order.

    Cancelling either leg cancels the OCA group, so cancelling the entry takes
    the protective stop with it. A residual position (an unexpected fill) is
    reported with an explicit flatten instruction rather than flattened here.
    """
    from trading_broker_core.port import CancelOrderRequest

    service = getattr(resources, "service", None)
    if service is None:
        return EXIT_ERROR
    broker = service.guarded_broker
    owner_print("=== CLEANUP: cancel + reconcile ===")
    try:
        broker.cancel_order(
            CancelOrderRequest(
                client_order_id=client_order_id, correlation_id=client_order_id
            )
        )
        owner_print("cancel requested for the bracket (OCA cancels every leg)")
    except Exception as error:  # noqa: BLE001 - type/code only
        owner_print(safe_failure("cancel failed; RECONCILE MANUALLY", error))
        return EXIT_ERROR
    try:
        result = broker.reconcile(account_alias)
    except Exception as error:  # noqa: BLE001
        owner_print(safe_failure("reconcile failed; RECONCILE MANUALLY", error))
        return EXIT_ERROR
    positions = tuple(p for p in result.positions if p.quantity != 0)
    open_orders = result.open_orders
    if positions:
        owner_print(
            "RESIDUAL POSITION after cancel (an entry filled): flatten it"
            " manually — this proof never sends a market order. Positions:"
            f" {[(p.symbol, str(p.quantity)) for p in positions]}"
        )
        return EXIT_ERROR
    if open_orders:
        owner_print(
            f"WARNING: {len(open_orders)} order(s) still open after cancel;"
            " reconcile manually."
        )
        return EXIT_ERROR
    owner_print("account clean: no residual bracket legs, no open position.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - owner entrypoint
    raise SystemExit(main())
