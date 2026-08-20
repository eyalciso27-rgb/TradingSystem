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
    RecoveredOrder,
)
from trading_contracts.enums import Environment
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
    """The canonical live-quote surface for the SECURITY being submitted.

    It used to take a ticker. That meant the quote was attributable to whatever
    the market-data feed resolved the string to, which is not necessarily the
    instrument the contract resolver returned -- so a price could belong to one
    listing while the order belonged to another, and nothing compared them.

    It now takes the resolved ``BrokerInstrument``, so the quote is attributable
    to the exact security. An implementation may interpret its own opaque
    reference; it must not re-resolve the ticker into a different instrument.
    """

    def current_quote(self, instrument: BrokerInstrument) -> QuoteState: ...


@dataclass(frozen=True, slots=True)
class ProtectionEvidence:
    """Proof that the broker will actually hold protection for this entry.

    Evidence, not capability. A capability flag says the broker *could* accept
    a protective order if one were sent; it says nothing about one existing. The
    distinction is the whole safety property, so this carries the broker's own
    references to the protective orders involved and is refused without them.
    """

    #: The lifecycle established (or will atomically establish) real
    #: broker-resident protection for this entry.
    established: bool
    #: What actually does it: "native-bracket", "oca-group", "resting-stop"...
    mechanism: str = ""
    #: Broker-side references for the protective order(s). REQUIRED when
    #: ``established`` is True: a mechanism with nothing to point at is a
    #: promise, not protection.
    protective_order_refs: tuple[str, ...] = ()
    detail: str = ""


@runtime_checkable
class ProtectedEntryLifecycle(Protocol):
    """The authority that can establish broker-resident protection.

    There is deliberately no default implementation. A guard with no lifecycle
    cannot prove protection and therefore refuses when protection is required —
    which is the correct answer today, because no adapter in this repository can
    establish protection atomically with an entry.
    """

    def protection_for(self, intent: OrderIntent) -> ProtectionEvidence: ...


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
        protection: ProtectedEntryLifecycle | None = None,
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
        # None means protection cannot be proven -- and therefore cannot pass
        # when it is required. There is no production implementation yet.
        self._protection = protection
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

    def _quote_for(self, intent: OrderIntent, contract: ContractState) -> QuoteState:
        """Quote the security this boundary just resolved, or nothing.

        An unresolved contract has no security to quote, and quoting the ticker
        instead would produce a price for something we have not identified.
        """
        if not contract.resolved or not contract.broker_instrument_ref:
            return QuoteState(
                bid=None, ask=None, observed_at=None,
                detail="no resolved security to quote",
            )
        try:
            return self._quotes.current_quote(
                BrokerInstrument(
                    broker_instrument_ref=contract.broker_instrument_ref,
                    symbol=contract.symbol or intent.symbol,
                    exchange=contract.exchange or "",
                    currency=contract.currency or "",
                )
            )
        except Exception as error:  # noqa: BLE001
            return QuoteState(
                bid=None, ask=None, observed_at=None,
                detail=f"quote source raised {type(error).__name__}",
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
        if instrument.broker_instrument_ref != intent.broker_instrument_ref:
            # THE just-in-time security check. The boundary re-resolves, and the
            # security it resolves NOW must be the security this order was
            # authorized for. Same ticker, different instrument -- a share class,
            # a second listing, a re-used symbol after a corporate action -- is a
            # different security, and the authorization does not cover it.
            return ContractState(
                resolved=False,
                broker_instrument_ref=None,
                symbol=instrument.symbol or None,
                exchange=instrument.exchange or None,
                currency=instrument.currency or None,
                detail=(
                    "the security resolved at the submission boundary is NOT the"
                    " security this order was authorized for"
                    f" ({instrument.broker_instrument_ref!r} !="
                    f" {intent.broker_instrument_ref!r}); a ticker is not a"
                    " security identity"
                ),
            )
        return ContractState(
            resolved=bool(instrument.broker_instrument_ref),
            broker_instrument_ref=instrument.broker_instrument_ref or None,
            symbol=instrument.symbol or None,
            exchange=instrument.exchange or None,
            currency=instrument.currency or None,
            detail="resolved by the broker, and it is the authorized security",
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
        """Protection is PROVEN by a lifecycle, never inferred from a flag.

        This used to read "the broker supports STOP orders" plus "the intent
        carries a stop price" as broker-side protection. Both are true of an
        entry that goes out completely unprotected: the order is a single LIMIT
        whose stop price becomes that order's aux price, and a supported order
        TYPE is a statement about what the broker would accept, not about what
        it holds.

        So the flag is gone. Evidence comes from a ``ProtectedEntryLifecycle``
        that can actually establish broker-resident protection, and it must
        name the protective order(s) it established. No lifecycle, no evidence,
        no references -> not protected. Since no adapter here can do this today,
        a protection-required entry is refused at this boundary, which is the
        intended architecture (ADR-0018, option B).
        """
        # The plan must still carry the protective level -- necessary, and
        # nowhere near sufficient.
        has_stop = intent.stop_price is not None and intent.stop_price > 0

        if self._protection is None:
            return ProtectionState(
                protective_order_supported=False,
                plan_has_protective_stop=has_stop,
                protection_required=self._config.protection_required,
                detail=(
                    "no protected-entry lifecycle is wired, so broker-side"
                    " protection cannot be established or proven; a supported"
                    " STOP order type is not a resting protective order"
                ),
            )
        try:
            evidence = self._protection.protection_for(intent)
        except Exception as error:  # noqa: BLE001 - unproven is unprotected
            return ProtectionState(
                protective_order_supported=False,
                plan_has_protective_stop=has_stop,
                protection_required=self._config.protection_required,
                detail=f"protection lifecycle raised {type(error).__name__}",
            )
        # Established WITHOUT a broker reference is a promise, not protection.
        proven = bool(evidence.established and evidence.protective_order_refs)
        detail = evidence.detail or (
            f"protection via {evidence.mechanism}:"
            f" {list(evidence.protective_order_refs)}"
            if proven
            else "the lifecycle established no broker-resident protection"
        )
        return ProtectionState(
            protective_order_supported=proven,
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
        # The contract is resolved FIRST, because the quote is now asked for
        # the resolved security rather than the ticker -- and because the
        # security this boundary resolves must be the security the order names.
        contract = self._observe_contract(intent)
        quote = self._quote_for(intent, contract)
        return RuntimeInputs(
            mode=self._config.mode,
            requirements=self._config.requirements,
            broker=self._observe_session(),
            contract=contract,
            tradability=self._observe_tradability(intent),
            quote=quote,
            rules=self._config.rules,
            protection=self._observe_protection(intent),
            risk_result=self._risk.risk_result_for(intent),
            audit=self._audit,
            evaluated_at=self._now(),
            universe_status=self._universe_status() if self._universe_status else None,
            gics_sector=(
                self._gics_sector(intent.symbol) if self._gics_sector else None
            ),
            pit_membership_available=self._pit_membership_available,
            expected_account_alias=self._config.account_alias,
            expected_symbol=intent.symbol,
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

    def evaluate_readiness(self, intent: OrderIntent) -> TradingReadinessRuntimeReport:
        """Observe and report, WITHOUT persisting or submitting anything.

        Exists so an operator probe reads the same observations the gate will
        use, rather than a parallel re-implementation that could drift from it.
        It is explicitly NOT the check: ``submit_order`` evaluates again at the
        boundary, because a report computed a second ago describes a market that
        has already moved on.
        """
        return evaluate_runtime_readiness(self._build_inputs(intent))

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

    def recover_cancellable_orders(
        self, account_alias: str
    ) -> tuple[BrokerOrderReceipt, ...]:
        """Never gated: recovering what already exists reduces uncertainty."""
        return self._broker.recover_cancellable_orders(account_alias)

    def recover_working_orders(
        self, account_alias: str
    ) -> tuple[RecoveredOrder, ...]:
        """Never gated: reading what the broker holds cannot create exposure."""
        return self._broker.recover_working_orders(account_alias)

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
        "client_order_id": "our canonical idempotency key",
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
    "ProtectedEntryLifecycle",
    "ProtectionEvidence",
    "ReadinessGuardedBroker",
    "RiskSource",
    "readiness_record_fields",
]
