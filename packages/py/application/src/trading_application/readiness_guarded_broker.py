"""The PRE_SUBMIT enforcement point: readiness is checked before submission.

An evaluable contract is not a gate. This makes readiness **enforced** by
placing it at ``BrokerPort.submit_order`` — the single surface every order must
cross (``BrokerPort`` is the canonical broker boundary, Integration 9.1). A
caller holding a guarded port cannot reach the broker without passing, because
there is no other submission path to take.

Honest scoping note: this repository has no order-submission *service* yet —
Paper Auto and Live are prohibited, so nothing in production calls
``submit_order`` today. The guard is therefore placed at the boundary itself
rather than inside a pipeline that does not exist, which is also where
``EntryCheckpoint.PRE_SUBMIT`` already says the final check belongs. When an
execution service is built, wrapping its port is the whole integration.

Everything this guard observes is read from the **declared** ``BrokerPort``
types — statically, never through ``getattr`` on a hoped-for attribute. That
matters more than it sounds: an attribute the port does not define is not a
missing feature to work around, it is a fact the broker never told us, and a
lenient default would turn silence into permission. Where the contract is
silent the guard proves the fact through a canonical surface or blocks.

The order of operations closes the audit race:

1. build ``RuntimeInputs`` INTERNALLY from canonical components;
2. evaluate every mandatory gate;
3. persist the pre-trade decision record and PROVE it succeeded;
4. only then delegate to the real broker.

A blocked decision is still persisted — a refusal is a decision worth auditing —
and a persistence failure blocks submission rather than proceeding unrecorded.
Because the record is written before submission and ``client_order_id`` is the
broker's idempotency key, an audit retry cannot cause a double submission.

**Readiness is not authorization.** Passing this guard does not authorize Paper
Auto or Live; those prohibitions are separate and unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilityMode,
    BrokerInstrument,
    BrokerOrderEvent,
    BrokerOrderReceipt,
    BrokerPort,
    BrokerPortError,
    BrokerPosition,
    CancelOrderRequest,
    CancelReceipt,
    InstrumentQuery,
    IntegrationHealth,
    OrderIntent,
    OrderPreview,
    ReconciliationResult,
)
from trading_contracts.enums import Environment, OrderType
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    UniverseListStatus,
)

from trading_application.risk_engine import RiskResult
from trading_application.trading_readiness_runtime import (
    AuditSink,
    BrokerSessionState,
    ContractState,
    MarketDataRules,
    ProtectionState,
    QuoteState,
    RuntimeInputs,
    TradabilityState,
    TradingReadinessRuntimeReport,
    evaluate_runtime_readiness,
)

GUARD_VERSION = "readiness-guarded-broker-1.0.0"

#: The canonical environment axis mapped onto the trading mode. RESEARCH is
#: deliberately absent: it is not a trading environment, so a session reporting
#: it has no trading mode at all and every mode check fails closed.
_ENVIRONMENT_TO_MODE: Mapping[Environment, TradingMode] = {
    Environment.PAPER: TradingMode.PAPER,
    Environment.LIVE: TradingMode.LIVE,
}

#: What-if support that actually proves something. Anything else means the
#: broker offers no validation surface, so tradability stays UNKNOWN.
_USABLE_WHAT_IF = (BrokerCapabilityMode.NATIVE, BrokerCapabilityMode.EMULATED)


class ReadinessBlockedError(Exception):
    """Submission refused: the runtime readiness report was BLOCKED."""

    def __init__(self, report: TradingReadinessRuntimeReport) -> None:
        super().__init__(
            f"submission blocked for {report.symbol}: {list(report.blockers)}"
        )
        self.report = report


class AuditPersistenceError(Exception):
    """Submission refused: the pre-trade decision record could not be stored."""


@runtime_checkable
class QuoteSource(Protocol):
    """The canonical live-quote surface for the symbol being submitted."""

    def current_quote(self, symbol: str) -> QuoteState: ...


@runtime_checkable
class RiskSource(Protocol):
    """Supplies the REAL RiskEngine result for this order intent.

    The guard never fabricates a risk verdict, and a caller cannot hand one in
    through the port: it is fetched here, from the component that owns it.
    """

    def risk_result_for(self, intent: OrderIntent) -> RiskResult | None: ...


@dataclass(frozen=True, slots=True)
class GuardConfig:
    """Canonical policy for one guarded broker, not per-call-site opinion.

    There is no account-prefix and no security-type policy here. Both are real
    invariants, but neither is this layer's to enforce: the raw account number
    never leaves the IBKR adapter (which refuses to construct against a non-DU
    paper account), and the same adapter is the authority on which instrument
    types it will resolve at all. Re-checking them here would mean inventing
    fields the canonical port does not expose.
    """

    mode: TradingMode
    requirements: StrategyDataRequirements
    rules: MarketDataRules
    account_alias: str
    allowed_currencies: tuple[str, ...] = ("USD",)
    allowed_exchanges: tuple[str, ...] = ("SMART",)
    #: Whether this order class requires protection at all.
    protection_required: bool = True


class ReadinessGuardedBroker:
    """A ``BrokerPort`` that refuses to submit unless readiness passes.

    Every other port method delegates unchanged; only ``submit_order`` is
    gated, because that is the only method that can reach the market. The
    wrapped broker is typed as the canonical ``BrokerPort``, so a mismatch
    between this guard and the real contract is a type error rather than
    something a permissive test double can hide.
    """

    def __init__(
        self,
        broker: BrokerPort,
        *,
        config: GuardConfig,
        quotes: QuoteSource,
        risk: RiskSource,
        audit: AuditSink,
        universe_status: Callable[[], UniverseListStatus | None] | None = None,
        gics_sector: Callable[[str], str | None] | None = None,
        pit_membership_available: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._config = config
        self._quotes = quotes
        self._risk = risk
        self._audit = audit
        self._universe_status = universe_status
        self._gics_sector = gics_sector
        self._pit_membership_available = pit_membership_available
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._last_report: TradingReadinessRuntimeReport | None = None

    # -- the gate -------------------------------------------------------------

    @property
    def last_report(self) -> TradingReadinessRuntimeReport | None:
        """The most recent readiness decision, for operator inspection."""
        return self._last_report

    def _observe_session(self) -> BrokerSessionState:
        """Session state from the broker's OWN health check and account view.

        The trading mode comes from ``AccountSnapshot.environment`` — the port's
        own statement of which environment it is connected to — not from any
        identifier this layer decodes.
        """
        try:
            health: IntegrationHealth = self._broker.health_check()
        except Exception as error:  # noqa: BLE001 - unknown broker failure => blocked
            return BrokerSessionState(
                connected=False, account_alias=None, session_mode=None,
                observed_at=None, health_ok=False,
                detail=f"health_check raised {type(error).__name__}",
            )
        try:
            snapshot: AccountSnapshot = self._broker.get_account_snapshot(
                self._config.account_alias
            )
        except Exception as error:  # noqa: BLE001
            return BrokerSessionState(
                connected=True, account_alias=self._config.account_alias,
                session_mode=None, observed_at=health.checked_at,
                health_ok=health.healthy,
                detail=f"account snapshot raised {type(error).__name__}",
            )
        # An environment outside the trading axis (RESEARCH) maps to None, which
        # blocks: it is neither paper nor live, so it cannot satisfy either.
        session_mode = _ENVIRONMENT_TO_MODE.get(snapshot.environment)
        return BrokerSessionState(
            connected=True,
            account_alias=snapshot.account_alias,
            session_mode=session_mode,
            observed_at=health.checked_at,
            health_ok=health.healthy,
            detail=health.detail,
        )

    def _observe_contract(self, intent: OrderIntent) -> ContractState:
        """Resolve the instrument through the broker's own resolver.

        ``broker_instrument_ref`` is carried through opaquely. It is the
        broker's identity for the instrument and this layer neither parses nor
        interprets it.
        """
        try:
            instrument: BrokerInstrument = self._broker.resolve_instrument(
                InstrumentQuery(symbol=intent.symbol)
            )
        except BrokerPortError as error:
            return ContractState(
                resolved=False, broker_instrument_ref=None, symbol=intent.symbol,
                exchange=None, currency=None,
                detail=f"resolve_instrument refused: {error}",
            )
        except Exception as error:  # noqa: BLE001
            return ContractState(
                resolved=False, broker_instrument_ref=None, symbol=intent.symbol,
                exchange=None, currency=None,
                detail=f"resolve_instrument raised {type(error).__name__}",
            )
        return ContractState(
            resolved=bool(instrument.broker_instrument_ref),
            broker_instrument_ref=instrument.broker_instrument_ref or None,
            symbol=instrument.symbol or None,
            exchange=instrument.exchange or None,
            currency=instrument.currency or None,
            detail="resolved by the broker",
        )

    def _observe_tradability(self, intent: OrderIntent) -> TradabilityState:
        """Prove current tradability through the broker's what-if surface.

        ``BrokerInstrument`` carries no tradability flag, so there is nothing to
        read. What exists is ``preview_order`` — a what-if that places nothing
        and that the broker refuses for an instrument it will not trade right
        now. A broker without that capability leaves the question UNKNOWN, and
        UNKNOWN blocks rather than defaulting to permission.
        """
        try:
            what_if = self._broker.capabilities().what_if
        except Exception as error:  # noqa: BLE001
            return TradabilityState(
                proven_tradable=False, evidence_available=False,
                detail=f"capabilities raised {type(error).__name__}",
            )
        if what_if not in _USABLE_WHAT_IF:
            return TradabilityState(
                proven_tradable=False, evidence_available=False,
                detail=(
                    f"broker what-if support is {what_if}; nothing can prove"
                    " current tradability, so it stays UNKNOWN"
                ),
            )
        try:
            preview: OrderPreview = self._broker.preview_order(intent)
        except BrokerPortError as error:
            return TradabilityState(
                proven_tradable=False,
                detail=f"broker refused the what-if validation: {error}",
            )
        except Exception as error:  # noqa: BLE001
            return TradabilityState(
                proven_tradable=False, evidence_available=False,
                detail=f"preview_order raised {type(error).__name__}",
            )
        detail = f"what-if accepted; estimated notional {preview.estimated_notional}"
        if preview.warnings:
            detail = f"{detail}; warnings={list(preview.warnings)}"
        return TradabilityState(proven_tradable=True, detail=detail)

    def _observe_protection(self, intent: OrderIntent) -> ProtectionState:
        """Broker capability AND the plan's own protective stop."""
        supported = False
        detail = ""
        try:
            mode = self._broker.capabilities().order_types.get(OrderType.STOP)
            supported = mode in (
                BrokerCapabilityMode.NATIVE, BrokerCapabilityMode.EMULATED
            )
            detail = f"STOP capability: {mode}"
        except Exception as error:  # noqa: BLE001
            detail = f"capabilities raised {type(error).__name__}"
        # The intent itself must carry the protective level.
        has_stop = intent.stop_price is not None and intent.stop_price > 0
        return ProtectionState(
            protective_order_supported=supported,
            plan_has_protective_stop=has_stop,
            protection_required=self._config.protection_required,
            detail=detail,
        )

    def _build_inputs(self, intent: OrderIntent) -> RuntimeInputs:
        """Construct RuntimeInputs INTERNALLY from canonical components.

        Nothing here is supplied by the caller of ``submit_order``: the session,
        contract, tradability, quote, risk verdict, protection and audit sink
        are all read from the components that own them.
        """
        symbol = intent.symbol
        try:
            quote = self._quotes.current_quote(symbol)
        except Exception as error:  # noqa: BLE001
            quote = QuoteState(
                bid=None, ask=None, observed_at=None,
                detail=f"quote source raised {type(error).__name__}",
            )
        return RuntimeInputs(
            mode=self._config.mode,
            requirements=self._config.requirements,
            broker=self._observe_session(),
            contract=self._observe_contract(intent),
            tradability=self._observe_tradability(intent),
            quote=quote,
            rules=self._config.rules,
            protection=self._observe_protection(intent),
            risk_result=self._risk.risk_result_for(intent),
            audit=self._audit,
            evaluated_at=self._now(),
            universe_status=self._universe_status() if self._universe_status else None,
            gics_sector=self._gics_sector(symbol) if self._gics_sector else None,
            pit_membership_available=self._pit_membership_available,
            expected_account_alias=self._config.account_alias,
            expected_symbol=symbol,
            allowed_currencies=self._config.allowed_currencies,
            allowed_exchanges=self._config.allowed_exchanges,
        )

    def _persist_pre_trade(
        self, report: TradingReadinessRuntimeReport, intent: OrderIntent
    ) -> str:
        """Write the pre-trade record and PROVE it landed. Failure blocks."""
        record: dict[str, object] = dict(report.to_dict())
        record["checkpoint"] = "PRE_SUBMIT"
        record["client_order_id"] = intent.client_order_id
        record["correlation_id"] = intent.correlation_id
        record["account_alias"] = self._config.account_alias
        record["guard_version"] = GUARD_VERSION
        try:
            reference = self._audit.persist(record)
        except Exception as error:  # noqa: BLE001 - any failure blocks
            raise AuditPersistenceError(
                f"pre-trade audit persistence failed ({type(error).__name__});"
                " refusing to submit an unrecorded order"
            ) from error
        if not reference:
            raise AuditPersistenceError(
                "pre-trade audit returned no reference; refusing to submit an"
                " unrecorded order"
            )
        return str(reference)

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        """THE gate. Readiness, then audit, then — only then — the broker."""
        report = evaluate_runtime_readiness(self._build_inputs(intent))
        self._last_report = report

        if report.readiness is TradingReadiness.BLOCKED:
            # A refusal is a decision worth auditing. A failure to record the
            # refusal must not turn it into a submission, so this is best-effort
            # and the raise happens regardless.
            try:
                self._persist_pre_trade(report, intent)
            except AuditPersistenceError:
                pass
            raise ReadinessBlockedError(report)

        # Persist BEFORE submitting: an order must never reach the broker
        # without its pre-trade decision already recorded.
        self._persist_pre_trade(report, intent)
        return self._broker.submit_order(intent)

    # -- everything else delegates unchanged ---------------------------------

    def connect(self) -> None:
        self._broker.connect()

    def disconnect(self) -> None:
        self._broker.disconnect()

    def capabilities(self) -> Any:
        return self._broker.capabilities()

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument:
        return self._broker.resolve_instrument(query)

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        return self._broker.get_account_snapshot(account_alias)

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        return self._broker.get_positions(account_alias)

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        return self._broker.get_open_orders(account_alias)

    def preview_order(self, intent: OrderIntent) -> OrderPreview:
        """What-if only; it places nothing, so it is not gated."""
        return self._broker.preview_order(intent)

    def cancel_order(self, request: CancelOrderRequest) -> CancelReceipt:
        """Never gated: reducing exposure must not be blocked by readiness."""
        return self._broker.cancel_order(request)

    def order_events(self) -> Iterator[BrokerOrderEvent]:
        return self._broker.order_events()

    def reconcile(self, account_alias: str) -> ReconciliationResult:
        return self._broker.reconcile(account_alias)

    def health_check(self) -> IntegrationHealth:
        return self._broker.health_check()


def readiness_record_fields() -> Mapping[str, str]:
    """The pre-trade audit fields this guard guarantees, for documentation."""
    return {
        "checkpoint": "always PRE_SUBMIT",
        "client_order_id": "the broker idempotency key",
        "correlation_id": "decision correlation",
        "trading_readiness": "PAPER_READY / LIVE_READY / BLOCKED",
        "gates": "all eleven outcomes with details",
        "blockers": "why a refusal happened",
        "runtime_refs": (
            "opaque broker instrument ref, risk outcome, approved quantity,"
            " quote time"
        ),
        "evaluated_at": "when readiness was computed",
    }


__all__ = [
    "GUARD_VERSION",
    "AuditPersistenceError",
    "GuardConfig",
    "QuoteSource",
    "ReadinessBlockedError",
    "ReadinessGuardedBroker",
    "RiskSource",
    "readiness_record_fields",
]
