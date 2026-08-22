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
from typing import TYPE_CHECKING
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trading_application.readiness_guarded_broker import QuoteSource

    from trading_execution_agent.ibkr_quote_source import MarketDataSessionLike

from trading_execution_agent.execution_service import (
    ExecutionConfig,
    ExecutionEvaluation,
    ExecutionIntent,
    ExecutionService,
    ExecutionStage,
)
from trading_execution_agent.safe_logging import (
    install_owner_output_protection,
    redact,
    register_sensitive_value,
    safe_failure,
)

#: Owner-host defaults. The account ALIAS is configuration; the account NUMBER
#: never appears here and never leaves the adapter.
ENV_ACCOUNT_ALIAS = "TRADING_PAPER_ACCOUNT_ALIAS"
#: The canonical secret store root, as the rest of the owner-host runtime uses
#: it. No default: a secret store location is deployment configuration, and
#: guessing one is how a probe ends up reading the wrong environment's secrets.
ENV_SECRETS_ROOT = "SECRETS_ROOT"
#: The reference inside the PAPER store. Not a path -- FileSecretStore resolves
#: it under $SECRETS_ROOT/PAPER/ and refuses anything that escapes that root.
PAPER_ACCOUNT_SECRET = "ibkr/paper-account-id"
#: Explicit override, retained for unusual cases and NOT the documented path:
#: exporting a raw broker account id puts it in shell history and in `ps`.
#: Takes precedence over the secret store when set.
ENV_ACCOUNT_ID_OVERRIDE = "TRADING_PAPER_ACCOUNT_ID"
ENV_DATABASE_URL = "DATABASE_URL"
ENV_OBJECT_STORE = "TRADING_OBJECT_STORE"
DEFAULT_OBJECT_STORE = "var/objects"
ENV_GATEWAY_HOST = "TRADING_IBKR_HOST"
ENV_GATEWAY_PORT = "TRADING_IBKR_PORT"
ENV_EQUITY_CAP = "TRADING_SIMULATION_EQUITY_CAP"

DEFAULT_ACCOUNT_ALIAS = "paper-poi"
DEFAULT_HOST = "127.0.0.1"
#: 4002 is the paper API port. 4001 is live and is refused by the adapter.
DEFAULT_PAPER_PORT = 4002

#: Stable owner-visible failure codes. A code plus an exception TYPE is
#: diagnosable without printing text this repository does not control.
PREFLIGHT_FAILED = "PROBE_PREFLIGHT_FAILED"
PROBE_START_FAILED = "PROBE_START_FAILED"
PROBE_RUN_FAILED = "PROBE_RUN_FAILED"

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


def _quote_source(session: MarketDataSessionLike) -> QuoteSource:
    """The production quote source, bound to the canonical contract HERE.

    Deliberately a typed builder rather than an inline construction with a
    ``type: ignore[arg-type]``. That suppression is what let the production
    quote leg drift: the application contract moved to
    ``current_quote(BrokerInstrument)`` while ``IbkrQuoteSource`` still took a
    ticker, and the ignore meant mypy never said so.

    The return annotation is the check. If ``IbkrQuoteSource`` ever stops
    satisfying ``QuoteSource`` -- a changed parameter type, a renamed method --
    this function fails to type-check, in the production wiring, before anything
    runs.
    """
    from trading_execution_agent.ibkr_quote_source import IbkrQuoteSource

    return IbkrQuoteSource(session)


class ProbePreflightError(Exception):
    """Local configuration is not usable. Raised BEFORE anything connects."""


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Every local fact the probe needs, validated before any socket opens."""

    account_alias: str
    #: The broker account, resolved from the canonical PAPER secret store.
    #: Held as a plain string only because the adapter needs it; it is never
    #: logged, never printed and never written anywhere.
    account_id: str
    database_url_present: bool
    object_store: object
    host: str
    port: int
    equity_cap: Decimal


def _resolve_account_id(account_alias: str) -> str:
    """The paper account for this alias, from the canonical secret store.

    Reuses ``FileSecretStore`` -- the same PAPER/LIVE-separated loader the rest
    of the owner-host runtime already uses (``scripts/poi_ibkr_lifecycle.py``,
    ``scripts/poi_tg_user.py``, ``scripts/ib_gateway_watchdog.py``) with the
    same ``SECRETS_ROOT`` convention. There is deliberately no second account
    secret location and no path hard-coded here: the owner host configures
    ``SECRETS_ROOT`` and the store resolves
    ``$SECRETS_ROOT/PAPER/ibkr/paper-account-id``.

    ``TRADING_PAPER_ACCOUNT_ID`` is still honoured as an explicit override for
    unusual cases, and takes precedence when set -- but it is NOT the documented
    path, because requiring the owner to export a raw broker account id makes it
    visible in the shell history and in ``ps``.
    """
    from pathlib import Path

    from trading_config.secrets import FileSecretStore, SecretError

    override = os.environ.get(ENV_ACCOUNT_ID_OVERRIDE, "").strip()
    if override:
        # Registered the moment it is known, whatever its format. Pattern
        # matching is defence in depth; this is the guarantee.
        register_sensitive_value(override)
        return override

    secrets_root = os.environ.get(ENV_SECRETS_ROOT, "").strip()
    if not secrets_root:
        raise ProbePreflightError(
            f"{ENV_SECRETS_ROOT} is not set, so the paper account for alias"
            f" {account_alias!r} cannot be resolved. The owner host keeps it in"
            f" the canonical secret store at"
            f" $" + f"{ENV_SECRETS_ROOT}/PAPER/{PAPER_ACCOUNT_SECRET}."
            f" (An explicit {ENV_ACCOUNT_ID_OVERRIDE} also works, but exporting"
            " a raw account id puts it in your shell history.)"
        )
    try:
        store = FileSecretStore(Path(secrets_root), Environment.PAPER)
        account_id = store.get(PAPER_ACCOUNT_SECRET).reveal().strip()
    except SecretError as error:
        # Names the reference, never the value.
        raise ProbePreflightError(
            f"the paper account for alias {account_alias!r} could not be"
            f" resolved from the secret store: {error}"
        ) from error
    if not account_id:
        raise ProbePreflightError(
            f"the canonical paper account secret {PAPER_ACCOUNT_SECRET!r} is"
            " empty; refusing to connect without knowing which account this is"
        )
    # THE defence that does not depend on recognising a format. The first real
    # owner-host run produced an account identifier the original pattern did not
    # match, and no regex written today can anticipate the formats IBKR
    # introduces later. This removes the exact value, wherever it appears.
    register_sensitive_value(account_id)
    return account_id


def preflight(
    *, account_alias: str, host: str, port: int, equity_cap: Decimal
) -> ProbeConfig:
    """Validate ALL local configuration before opening ANY network resource.

    The first real owner-host run opened two IBKR sessions and only then
    discovered ``DATABASE_URL`` was unset -- which aborted startup mid-flight
    and left the SDK thread tearing down into a ``serverVersion() == None``
    exception. Nothing about that failure needed a socket to detect.

    So every purely local fact is checked here, first: the account secret, the
    database configuration, the object store path, and the paper-only endpoint.
    Failing here costs one clear message and zero connections.
    """
    from pathlib import Path

    from trading_broker_ibkr.poi_readonly_session import _validate_endpoint

    # 1. Paper-only endpoint. Purely local, and the most important refusal.
    try:
        _validate_endpoint(host, port)
    except Exception as error:  # noqa: BLE001 - normalised into one message
        raise ProbePreflightError(f"refused endpoint: {error}") from error

    # 2. The account, from the canonical secret store.
    account_id = _resolve_account_id(account_alias)

    # 3. The database. Checked for PRESENCE only -- the value is a DSN with a
    #    password in it and never appears in a message.
    if not os.environ.get(ENV_DATABASE_URL, "").strip():
        raise ProbePreflightError(
            f"{ENV_DATABASE_URL} is not set, so the durable execution audit"
            " cannot be reached. The paper stack publishes no database port by"
            " design; use scripts/run_execution_probe_paper.sh, which supplies"
            " it from the paper environment without printing it."
        )

    # 4. The object store path must be constructible.
    raw_store = os.environ.get(ENV_OBJECT_STORE, DEFAULT_OBJECT_STORE)
    try:
        object_store = Path(raw_store)
    except (TypeError, ValueError) as error:
        raise ProbePreflightError(
            f"{ENV_OBJECT_STORE} is not a usable path ({type(error).__name__})"
        ) from error

    return ProbeConfig(
        account_alias=account_alias,
        account_id=account_id,
        database_url_present=True,
        object_store=object_store,
        host=host,
        port=port,
        equity_cap=equity_cap,
    )


def build_service(
    *, symbol: str, account_alias: str, host: str, port: int, equity_cap: Decimal
) -> ProbeResources:
    """Wire the REAL components. Imported lazily so --help never opens a socket."""
    from trading_broker_ibkr.paper_adapter import build_paper_adapter
    from trading_broker_ibkr.poi_market_data_session import create_session
    from trading_persistence.db import create_sync_engine
    from trading_persistence.object_storage import FilesystemObjectStorage

    # EVERYTHING local first. No socket is opened until this returns.
    config = preflight(
        account_alias=account_alias, host=host, port=port, equity_cap=equity_cap
    )
    account_id = config.account_id

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

        objects = FilesystemObjectStorage(config.object_store)  # type: ignore[arg-type]
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
    # Typed, not `object`. The quote leg is where production drifted away from
    # the canonical contract while a `type: ignore` kept mypy quiet, so this
    # parameter carries its real protocol and `_quote_source` below re-states
    # the contract as its return type. If IbkrQuoteSource stops satisfying
    # QuoteSource, the production wiring stops type-checking.
    session: MarketDataSessionLike,
    engine: object,
    objects: object,
    *,
    account_alias: str,
    equity_cap: Decimal,
) -> ExecutionService:
    """Assemble the service from already-owned resources."""
    from trading_execution_agent.reconciliation_latch import ReconciliationLatch

    # ONE latch: the agent's entry gate and the service's critical-state latch
    # are the same object, so a failed post-submit audit really does stop the
    # next entry rather than latching something nobody consults.
    latch = ReconciliationLatch()
    return ExecutionService(
        broker=adapter,  # type: ignore[arg-type]
        quotes=_quote_source(session),
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


def _print_debug_detail(error: BaseException, *, debug: bool) -> None:
    """Detail for deliberate troubleshooting, through the SAME redactor.

    Debug does not mean secrets are allowed. The message is redacted with the
    registered literals and the patterns before it is printed, and the traceback
    is still not shown -- a traceback carries frames, locals in some formats,
    and the same uncontrolled message text.
    """
    if not debug:
        return
    detail = redact(str(error)).strip()
    if detail:
        print(f"  detail (redacted): {detail}")


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
    probe.add_argument(
        "--debug-broker-logging",
        action="store_true",
        help=(
            "leave the IBKR SDK's protocol loggers at their own level for"
            " troubleshooting. Account identifiers are still redacted."
        ),
    )
    probe.add_argument("--host", default=os.environ.get(ENV_GATEWAY_HOST, DEFAULT_HOST))
    probe.add_argument(
        "--port", type=int,
        default=int(os.environ.get(ENV_GATEWAY_PORT, DEFAULT_PAPER_PORT)),
    )
    args = parser.parse_args(argv)

    configure_logging(service="execution-probe", environment="PAPER")
    # BEFORE anything can log. The broker SDK echoes its own protocol payloads
    # at INFO -- including the managed-account list, DU number and all -- so the
    # promise that the account id is never printed has to be enforced against
    # third-party loggers, not just against our own render().
    install_owner_output_protection(debug=args.debug_broker_logging)
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
    except ProbePreflightError as error:
        # OUR message, written to be shown: it names what to configure and
        # never carries a value. Redacted anyway, because a future edit to one
        # of these messages should not be able to leak.
        print(f"{PREFLIGHT_FAILED}: {redact(str(error))}")
        return EXIT_ERROR
    except Exception as error:  # noqa: BLE001 - see safe_failure
        # Anything else is dependency-controlled text. `print` bypasses every
        # logging filter, so a psycopg OperationalError would have put the DSN
        # and its password straight on the terminal. Type and code only.
        print(safe_failure(PROBE_START_FAILED, error))
        _print_debug_detail(error, debug=args.debug_broker_logging)
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
    except Exception as error:  # noqa: BLE001 - see safe_failure
        # The evidence path was guarded for CLEANUP but not for OUTPUT: an
        # exception here escaped main and Python printed the traceback, message
        # included. A traceback is dependency-controlled text on the terminal.
        print(safe_failure(PROBE_RUN_FAILED, error))
        _print_debug_detail(error, debug=args.debug_broker_logging)
        return EXIT_ERROR
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
