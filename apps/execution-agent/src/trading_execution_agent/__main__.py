"""``python -m trading_execution_agent probe`` — the owner-safe execution probe.

This exercises the real runtime path on the owner host as far as it can safely
go, and stops there. It resolves a contract through the IBKR Paper adapter,
takes a live quote, runs the real risk engine, builds the plan, asks the broker
to validate the order with a what-if that places nothing, computes readiness and
records the decision.

**It cannot submit an order.** Not "does not by default" — the probe constructs
the service with the production authority, which denies, and calls ``evaluate``,
which has no submission path in it at all. Two independent reasons, because one
would be a configuration mistake away from being wrong.

Its job is to find out what is actually true on the owner host, including the
answers nobody wants: a missing market-data subscription, delayed-only ticks, an
unavailable size. Those show up in the output as themselves. Nothing here
loosens a gate to make the report look better.

Paper only: the adapter refuses any port but 4002 and any account that is not a
paper account, and this entry point never offers a way to ask for Live.

**Two IBKR client sessions, deliberately.** The order/account path uses the
paper adapter's own client id and the quote path uses the market-data session's,
because they are separate clients in the existing architecture and pretending
otherwise would misdescribe what connects to the gateway. Both are disconnected
before this process exits, and so is the database engine.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from trading_application.decision_engine import DecisionExplanation, EngineDecision
from trading_application.risk_engine import RiskEngine, RiskPolicy
from trading_application.trading_readiness_runtime import MarketDataRules
from trading_broker_core.port import OrderSide
from trading_contracts.decision import DecisionResult
from trading_contracts.enums import ActionTier, DecisionOutcome, Environment, OrderType
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
)
from trading_observability.logging import configure_logging
from trading_persistence.execution_audit import (
    DurableExecutionAuditSink,
    DurableExecutionHistoryReader,
)

from trading_execution_agent.execution_service import (
    ExecutionConfig,
    ExecutionEvaluation,
    ExecutionIntent,
    ExecutionService,
    ExecutionStage,
)

#: Owner-host defaults. The account ALIAS is configuration; the account NUMBER
#: never appears here and never leaves the adapter.
ENV_ACCOUNT_ALIAS = "TRADING_PAPER_ACCOUNT_ALIAS"
ENV_GATEWAY_HOST = "TRADING_IBKR_HOST"
ENV_GATEWAY_PORT = "TRADING_IBKR_PORT"
ENV_EQUITY_CAP = "TRADING_SIMULATION_EQUITY_CAP"

DEFAULT_ACCOUNT_ALIAS = "paper-poi"
DEFAULT_HOST = "127.0.0.1"
#: 4002 is the paper API port. 4001 is live and is refused by the adapter.
DEFAULT_PAPER_PORT = 4002

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2

LOGGER = logging.getLogger("trading_execution_agent.probe")

#: Envelope values are configuration everywhere else in this system; the probe
#: needs a policy to run at all, so it uses a deliberately small one and says so.
PROBE_RISK_POLICY = RiskPolicy(
    policy_version="probe-risk-policy-1",
    risk_per_trade_pct=Decimal("0.0025"),
    max_total_open_risk_pct=Decimal("0.0100"),
    daily_soft_halt_pct=Decimal("0.0075"),
    daily_hard_halt_pct=Decimal("0.0100"),
    max_concurrent_positions=4,
    max_symbol_notional_pct=Decimal("0.10"),
    max_single_order_notional_pct=Decimal("0.075"),
    max_sector_notional_pct=Decimal("0.25"),
    max_correlated_cluster_pct=Decimal("0.30"),
    max_source_open_risk_share=Decimal("0.50"),
)


def probe_decision(symbol: str, now: datetime) -> EngineDecision:
    """A minimal APPROVE decision so the risk engine has something to judge.

    A real strategy supplies its own. This one exists because the probe is
    about the *execution path*, not about deciding to trade — and the risk
    engine still holds its veto over it, exactly as it would over a real one.
    """
    return EngineDecision(
        result=DecisionResult(
            decision_id=uuid4(),
            candidate_id=UUID(int=1),
            outcome=DecisionOutcome.APPROVE,
            action_tier=ActionTier.AUTO_FULL,
            strategy_version="1.0.0",
            decision_policy_version="1.0.0",
            decided_at=now,
            decision_hash="sha256:" + "0" * 64,
        ),
        explanation=DecisionExplanation(
            fusion_ref="probe",
            manifest_ref="probe",
            policy_ref="probe",
            engine_version="execution-probe",
            reason_codes=("OWNER_HOST_PROBE",),
            blocking_reasons=(),
            input_refs=(f"probe:{symbol}",),
            validation_note="PROBE_ONLY_NO_SUBMISSION",
        ),
    )


@dataclass
class ProbeResources:
    """Everything the probe opened, so everything can be closed again.

    A probe that leaves a gateway session or a database connection behind is a
    probe that cannot be run twice on a schedule, and a leaked transaction holds
    locks for the life of the process.

    Ownership is STAGED, not collected at the end. Construction registers each
    resource with an ``ExitStack`` the moment it opens, so a failure part-way
    through unwinds what already opened — the earlier version built everything
    and only then returned something closeable, which meant a failure in the
    second step stranded the first.
    """

    service: ExecutionService
    stack: ExitStack

    def close(self) -> None:
        """Release everything, in reverse order of acquisition."""
        self.stack.close()


def _closing(resource: object, method: str, name: str) -> Callable[[], None]:
    """A closer that logs and swallows, so one failure never strands the rest."""

    def close() -> None:
        closer = getattr(resource, method, None)
        if not callable(closer):
            return
        try:
            closer()
        except Exception as error:  # noqa: BLE001 - closing is best effort
            LOGGER.warning("could not close the %s: %s", name, type(error).__name__)

    return close


def build_service(
    *, symbol: str, account_alias: str, host: str, port: int, equity_cap: Decimal
) -> ProbeResources:
    """Wire the REAL components. Imported lazily so --help never opens a socket."""
    from pathlib import Path

    from trading_broker_ibkr.paper_adapter import build_paper_adapter
    from trading_broker_ibkr.poi_market_data_session import create_session
    from trading_persistence.db import create_sync_engine
    from trading_persistence.object_storage import FilesystemObjectStorage


    account_id = os.environ.get("TRADING_PAPER_ACCOUNT_ID", "")
    if not account_id:
        raise SystemExit(
            "TRADING_PAPER_ACCOUNT_ID must be set to the paper account this alias"
            " maps to. It is read from the environment, never written to disk,"
            " and never printed: the adapter masks it and refuses any account"
            " that is not a paper account."
        )

    # Each resource is registered for cleanup THE MOMENT it opens, so a
    # failure in any later step unwinds everything already acquired.
    stack = ExitStack()
    try:
        # The canonical builder, over the lazy SDK transport. The adapter
        # refuses any port but the paper API port, so a mistyped port cannot
        # reach live.
        adapter = build_paper_adapter(
            account_aliases={account_alias: account_id}, host=host, port=port
        )
        adapter.connect()
        stack.callback(_closing(adapter, "disconnect", "broker adapter"))

        session = create_session()
        session.connect(host, port)
        stack.callback(_closing(session, "disconnect", "market data session"))

        engine = create_sync_engine()
        stack.callback(_closing(engine, "dispose", "database engine"))

        objects = FilesystemObjectStorage(
            Path(os.environ.get("TRADING_OBJECT_STORE", "var/objects"))
        )
        service = _build(
            adapter, session, engine, objects,
            account_alias=account_alias, equity_cap=equity_cap,
        )
    except BaseException:
        # Anything at all: unwind what opened, then let it propagate.
        stack.close()
        raise
    return ProbeResources(service=service, stack=stack)


def _build(
    adapter: object,
    session: object,
    engine: object,
    objects: object,
    *,
    account_alias: str,
    equity_cap: Decimal,
) -> ExecutionService:
    """Assemble the service from already-owned resources."""
    from trading_execution_agent.ibkr_quote_source import IbkrQuoteSource
    from trading_execution_agent.reconciliation_latch import ReconciliationLatch

    # ONE latch: the agent's entry gate and the service's critical-state latch
    # are the same object, so a failed post-submit audit really does stop the
    # next entry rather than latching something nobody consults.
    latch = ReconciliationLatch()
    return ExecutionService(
        broker=adapter,  # type: ignore[arg-type]
        quotes=IbkrQuoteSource(session),  # type: ignore[arg-type]
        # The sink owns its own committed transactions; the probe hands it an
        # ENGINE and never holds a connection of its own.
        audit=DurableExecutionAuditSink(
            engine,  # type: ignore[arg-type]
            objects,  # type: ignore[arg-type]
            environment=Environment.PAPER,
        ),
        history=DurableExecutionHistoryReader(
            engine,  # type: ignore[arg-type]
            objects,  # type: ignore[arg-type]
        ),
        risk_engine=RiskEngine(PROBE_RISK_POLICY),
        config=ExecutionConfig(
            mode=TradingMode.PAPER,
            environment=Environment.PAPER,
            account_alias=account_alias,
            requirements=StrategyDataRequirements(strategy_id="owner-host-probe"),
            rules=MarketDataRules(),
            risk_policy=PROBE_RISK_POLICY,
            simulation_equity_cap=equity_cap,
            order_type=OrderType.LIMIT,
            sector="probe",
            cluster="probe",
        ),
        # ONE gate: the service builds its own agent from this object, so
        # there is no wiring in which the latched gate and the gate the agent
        # consults could be different.
        entry_gate=latch,
        # No authorization argument: the production default DENIES.
    )


def render(evaluation: ExecutionEvaluation) -> list[str]:
    """Operator-readable, and honest about what it did not do."""
    quote = evaluation.quote
    readiness = evaluation.readiness
    lines = [
        "=== EXECUTION PROBE (no order was submitted) ===",
        f"decision_id         : {evaluation.intent.decision_id}",
        f"client_order_id     : {evaluation.intent.client_order_id}",
        f"symbol              : {evaluation.intent.symbol}",
        f"mode                : {evaluation.intent_mode}",
        f"stage               : {evaluation.stage.value}",
    ]
    if evaluation.instrument is not None:
        lines += [
            f"resolved contract   : {evaluation.instrument.broker_instrument_ref}"
            f" ({evaluation.instrument.exchange} {evaluation.instrument.currency})",
        ]
    else:
        lines.append("resolved contract   : NOT RESOLVED")
    if quote is not None:
        lines += [
            f"quote bid/ask       : {quote.bid} / {quote.ask}",
            f"quote sizes         : {quote.bid_size} / {quote.ask_size}",
            f"quote observed_at   : "
            f"{quote.observed_at.isoformat() if quote.observed_at else 'NONE'}",
            f"market data state   : {quote.market_data_state.value}",
            f"quote detail        : {quote.detail or '-'}",
        ]
    else:
        lines.append("quote               : NONE")
    lines.append(
        f"universe status     : "
        f"{evaluation.universe_status.value if evaluation.universe_status else 'N/A'}"
    )
    if evaluation.risk_result is not None:
        lines += [
            f"risk outcome        : {evaluation.risk_result.outcome.value}",
            f"approved quantity   : {evaluation.risk_result.approved_quantity}",
            f"risk reasons        : {list(evaluation.risk_result.reason_codes)}",
        ]
    else:
        lines.append("risk outcome        : NOT EVALUATED")
    lines.append(f"protective stop     : {evaluation.stop_price or 'NONE'}")
    if readiness is not None:
        lines.append(f"trading readiness   : {readiness.readiness.value}")
        for gate in readiness.gates:
            mark = "PASS" if gate.satisfied else "FAIL"
            lines.append(f"  [{mark}] {gate.name}: {gate.detail}")
        if readiness.blockers:
            lines.append(f"blockers            : {list(readiness.blockers)}")
    else:
        lines.append("trading readiness   : NOT EVALUATED")
    authorization = evaluation.authorization
    lines += [
        "authorization       : "
        + (
            "DENIED / NOT_ENABLED"
            if authorization is None or not authorization.allowed
            else "ALLOWED"
        ),
        f"authorization reason: {authorization.reason if authorization else 'not reached'}",
        f"audit record        : {evaluation.audit_ref or 'NOT PERSISTED'}",
        f"reasons             : {list(evaluation.reasons)}",
        "",
        "NO ORDER SUBMITTED. This probe has no submission path; readiness is"
        " not authorization, and Paper Auto and Live remain prohibited.",
    ]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trading_execution_agent",
        description=(
            "Owner-safe execution probe: exercises the real runtime path up to"
            " the broker's what-if and stops. It cannot submit an order."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    probe = sub.add_parser("probe", help="run the read-only execution probe")
    probe.add_argument("--symbol", default="AAPL")
    probe.add_argument(
        "--mode", default="paper", choices=["paper"],
        help="paper only; there is deliberately no live option",
    )
    probe.add_argument("--strategy", default="owner-host-probe")
    probe.add_argument("--quantity", default="1")
    probe.add_argument(
        "--account-alias",
        default=os.environ.get(ENV_ACCOUNT_ALIAS, DEFAULT_ACCOUNT_ALIAS),
    )
    probe.add_argument("--host", default=os.environ.get(ENV_GATEWAY_HOST, DEFAULT_HOST))
    probe.add_argument(
        "--port", type=int,
        default=int(os.environ.get(ENV_GATEWAY_PORT, DEFAULT_PAPER_PORT)),
    )
    args = parser.parse_args(argv)

    configure_logging(service="execution-probe", environment="PAPER")
    now = datetime.now(tz=UTC)
    # ONE identity: the decision's own id is the canonical execution identity,
    # and everything downstream (risk ref, audit target, durable history,
    # client order id) derives from it rather than inventing a second name.
    decision = probe_decision(args.symbol, now)
    decision_id = str(decision.result.decision_id)

    try:
        resources = build_service(
            symbol=args.symbol,
            account_alias=args.account_alias,
            host=args.host,
            port=args.port,
            equity_cap=Decimal(os.environ.get(ENV_EQUITY_CAP, "50000")),
        )
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001
        print(f"probe could not start: {type(error).__name__}: {error}")
        return EXIT_ERROR

    try:
        evidence = resources.service.collect_evidence(
            ExecutionIntent(
                strategy_id=args.strategy,
                decision=decision,
                symbol=args.symbol,
                side=OrderSide.BUY,
                requested_quantity=Decimal(args.quantity),
                correlation_id=decision_id,
                # Probe-scoped and never reused by a real order.
                client_order_id=f"probe-{decision_id}",
                source_id="owner-host-probe",
            )
        )
    finally:
        # Everything opened above is released here, including on failure.
        resources.close()

    evaluation = evidence.evaluation
    assert evaluation is not None
    print("=== INDEPENDENT RUNTIME EVIDENCE (read-only) ===")
    for line in evidence.lines():
        print(line)
    print()
    for line in render(evaluation):
        print(line)

    if evaluation.stage in (
        ExecutionStage.AUTHORIZATION_DENIED, ExecutionStage.READY_NOT_AUTHORIZED
    ):
        # Readiness satisfied; submission correctly refused. That is a PASS.
        return EXIT_OK
    return EXIT_BLOCKED


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
