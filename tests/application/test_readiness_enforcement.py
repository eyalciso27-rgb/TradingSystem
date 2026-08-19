"""Enforcement at the REAL boundary: no order reaches the broker unready.

These matter more than another unit test of the evaluator. They drive
``submit_order`` — the single surface every order must cross — and assert on
what the *broker* saw, not on what the report said.

The test double here is deliberately not hand-shaped. It returns the canonical
``BrokerInstrument`` / ``AccountSnapshot`` / ``OrderPreview`` dataclasses and
implements the whole ``BrokerPort`` surface, and ``test_the_double_is_a_real_
broker_port`` pins that. An earlier version invented ``con_id``, ``security_type``
and ``tradable`` on its own instrument class; every test passed, and the guard
was reading fields the real port has never had. A double that is more generous
than the contract does not test the contract.

Four properties:

* a failed mandatory gate => BLOCKED, the refusal is audited, and the broker's
  ``submit_order`` is NEVER called;
* all gates satisfied => the pre-trade record is persisted BEFORE submission;
* audit persistence failure => submission refused, broker never called;
* an unprovable fact (no what-if surface) blocks — UNKNOWN is not optimism.

Offline and deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from trading_application.readiness_guarded_broker import (
    AuditPersistenceError,
    GuardConfig,
    ReadinessBlockedError,
    ReadinessGuardedBroker,
)
from trading_application.risk_engine import RiskResult
from trading_application.trading_readiness_runtime import (
    MarketDataRules,
    MarketDataState,
    QuoteState,
)
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    BrokerErrorCode,
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
    OrderSide,
    ReconciliationResult,
)
from trading_contracts.enums import (
    Environment,
    OrderState,
    OrderType,
    RiskDecisionOutcome,  # noqa: I001
)
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    UniverseListStatus,
    UniverseType,
)

NOW = datetime(2026, 8, 18, 15, 30, tzinfo=UTC)

def _quote(*args: object, **kw: object) -> QuoteState:
    """A quote that states its feed, because an unstated feed now blocks.

    ``market_data_state`` defaults to UNKNOWN in production for exactly that
    reason; these tests are about other properties, so they declare a healthy
    realtime feed rather than silently relying on a default that must not exist.
    """
    kw.setdefault("market_data_state", MarketDataState.REALTIME)
    return QuoteState(*args, **kw)  # type: ignore[arg-type]



CANONICAL_INSTRUMENT = BrokerInstrument(
    broker_instrument_ref="ibkr-paper:265598",
    symbol="AAPL",
    exchange="SMART",
    currency="USD",
)


class _Broker:
    """A stand-in ``BrokerPort`` that RECORDS whether submit was ever reached.

    Everything it returns is a canonical port dataclass, so it cannot supply a
    field the real contract lacks.
    """

    def __init__(
        self,
        *,
        healthy: bool = True,
        checked_at: datetime | None = None,
        environment: Environment = Environment.PAPER,
        instrument: BrokerInstrument | None = None,
        resolve_error: BrokerPortError | None = None,
        preview_error: BrokerPortError | None = None,
        what_if: BrokerCapabilityMode = BrokerCapabilityMode.NATIVE,
        stop_capability: BrokerCapabilityMode = BrokerCapabilityMode.NATIVE,
    ) -> None:
        self.submitted: list[OrderIntent] = []
        self.cancelled: list[CancelOrderRequest] = []
        self.previewed: list[OrderIntent] = []
        self._healthy = healthy
        self._checked_at = checked_at or (NOW - timedelta(seconds=5))
        self._environment = environment
        self._instrument = instrument or CANONICAL_INSTRUMENT
        self._resolve_error = resolve_error
        self._preview_error = preview_error
        self._what_if = what_if
        self._stop_capability = stop_capability

    # -- the surface the guard actually observes ------------------------------

    def health_check(self) -> IntegrationHealth:
        return IntegrationHealth(
            healthy=self._healthy, detail="ok", checked_at=self._checked_at
        )

    def get_account_snapshot(self, account_alias: str) -> AccountSnapshot:
        return AccountSnapshot(
            account_alias=account_alias,
            environment=self._environment,
            net_liquidation_value=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("200000"),
            observed_at=NOW - timedelta(seconds=5),
        )

    def resolve_instrument(self, query: InstrumentQuery) -> BrokerInstrument:
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._instrument

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            order_types={OrderType.STOP: self._stop_capability},
            bracket=BrokerCapabilityMode.UNSUPPORTED,
            oco=BrokerCapabilityMode.UNSUPPORTED,
            trailing=BrokerCapabilityMode.UNSUPPORTED,
            fractional=BrokerCapabilityMode.UNSUPPORTED,
            outside_rth=BrokerCapabilityMode.UNSUPPORTED,
            what_if=self._what_if,
            streaming_events=BrokerCapabilityMode.NATIVE,
        )

    def preview_order(self, intent: OrderIntent) -> OrderPreview:
        if self._preview_error is not None:
            raise self._preview_error
        self.previewed.append(intent)
        return OrderPreview(
            client_order_id=intent.client_order_id,
            estimated_notional=intent.quantity * (intent.limit_price or Decimal(0)),
            correlation_id=intent.correlation_id,
        )

    def submit_order(self, intent: OrderIntent) -> BrokerOrderReceipt:
        self.submitted.append(intent)
        return BrokerOrderReceipt(
            client_order_id=intent.client_order_id,
            broker_order_ref=f"receipt-{intent.client_order_id}",
            state=OrderState.ACKNOWLEDGED,
            authorization_ref=intent.authorization_ref,
            correlation_id=intent.correlation_id,
            received_at=NOW,
        )

    def cancel_order(self, request: CancelOrderRequest) -> CancelReceipt:
        self.cancelled.append(request)
        return CancelReceipt(
            client_order_id=request.client_order_id,
            broker_order_ref="cancelled",
            state=OrderState.CANCELLED,
            correlation_id=request.correlation_id,
            received_at=NOW,
        )

    # -- the rest of the port, so the double really is one --------------------

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def get_positions(self, account_alias: str) -> tuple[BrokerPosition, ...]:
        return ()

    def get_open_orders(self, account_alias: str) -> tuple[BrokerOrderReceipt, ...]:
        return ()

    def order_events(self) -> Iterator[BrokerOrderEvent]:
        return iter(())

    def reconcile(self, account_alias: str) -> ReconciliationResult:
        return ReconciliationResult(
            account_alias=account_alias,
            snapshot=self.get_account_snapshot(account_alias),
            positions=(),
            open_orders=(),
            reconciled_at=NOW,
        )


class _Quotes:
    def __init__(self, quote: QuoteState | None = None, raises: bool = False) -> None:
        self._quote = quote or _quote(
            bid=Decimal("100.00"), ask=Decimal("100.05"),
            observed_at=NOW - timedelta(seconds=1), bid_size=500, ask_size=500,
        )
        self._raises = raises

    def current_quote(self, symbol: str) -> QuoteState:
        if self._raises:
            raise RuntimeError("quote feed down")
        return self._quote


class _Risk:
    """Supplies a REAL ``RiskResult``.

    Not a stand-in shaped like one: the readiness path reads the risk verdict
    through its declared type, so a fake with a convenient ``outcome`` string
    would be exactly the kind of over-generous double this suite exists to
    avoid.
    """

    def __init__(
        self,
        outcome: RiskDecisionOutcome | str = RiskDecisionOutcome.APPROVE,
        quantity: Decimal = Decimal(10),
    ) -> None:
        self._outcome = outcome
        self._quantity = quantity

    def risk_result_for(self, intent: OrderIntent) -> RiskResult | None:
        if self._outcome == "NONE":
            return None
        outcome = RiskDecisionOutcome(self._outcome)
        return RiskResult(
            outcome=outcome,
            approved_quantity=self._quantity,
            approved_risk_amount=Decimal("500"),
            max_loss_amount=Decimal("500"),
            risk_budget_equity=Decimal("100000"),
            decision_ref="decision-1",
            policy_ref="policy-1",
            engine_version="test",
            checks=(),
            reason_codes=(
                () if outcome is RiskDecisionOutcome.APPROVE else ("RISK_VETO",)
            ),
            input_refs=(),
        )


class _Audit:
    def __init__(self, *, ok: bool = True, raises: bool = False, empty_ref: bool = False) -> None:
        self.records: list[Mapping[str, object]] = []
        self._ok = ok
        self._raises = raises
        self._empty_ref = empty_ref

    def can_persist(self) -> bool:
        return self._ok

    def persist(self, record: Mapping[str, object]) -> str:
        if self._raises:
            raise RuntimeError("audit store unavailable")
        self.records.append(record)
        return "" if self._empty_ref else f"audit-{len(self.records)}"


def _intent(**kw: object) -> OrderIntent:
    defaults: dict[str, object] = {
        "client_order_id": "ord-1",
        "authorization_ref": "auth-1",
        "correlation_id": "corr-1",
        "environment": Environment.PAPER,
        "account_alias": "paper-poi",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": Decimal(10),
        "limit_price": Decimal("100.00"),
        "stop_price": Decimal("95.00"),
    }
    defaults.update(kw)
    return OrderIntent(**defaults)  # type: ignore[arg-type]


def _guard(
    broker: _Broker | None = None,
    *,
    quotes: _Quotes | None = None,
    risk: _Risk | None = None,
    audit: _Audit | None = None,
    universe_status: UniverseListStatus | None = None,
    requirements: StrategyDataRequirements | None = None,
    rules: MarketDataRules | None = None,
) -> tuple[ReadinessGuardedBroker, _Broker, _Audit]:
    real = broker or _Broker()
    sink = audit or _Audit()
    guard = ReadinessGuardedBroker(
        real,
        config=GuardConfig(
            mode=TradingMode.PAPER,
            requirements=requirements
            or StrategyDataRequirements(strategy_id="baseline-trend-breakout-v1"),
            rules=rules
            or MarketDataRules(
                max_spread_fraction=Decimal("0.01"), min_price=Decimal("1.00")
            ),
            account_alias="paper-poi",
        ),
        quotes=quotes or _Quotes(),
        risk=risk or _Risk(),
        audit=sink,
        universe_status=(lambda: universe_status) if universe_status else None,
        now=lambda: NOW,
    )
    return guard, real, sink


# == the double must not be more generous than the contract ===================


def test_the_double_is_a_real_broker_port() -> None:
    """If the double drifts from ``BrokerPort``, this stops being a test."""
    port: BrokerPort = _Broker()
    assert port.capabilities().what_if is BrokerCapabilityMode.NATIVE
    # mypy accepts this assignment, which is the point: the guard IS the port.
    guarded: BrokerPort = ReadinessGuardedBroker(
        _Broker(),
        config=GuardConfig(
            mode=TradingMode.PAPER,
            requirements=StrategyDataRequirements(strategy_id="s"),
            rules=MarketDataRules(),
            account_alias="paper-poi",
        ),
        quotes=_Quotes(),
        risk=_Risk(),
        audit=_Audit(),
    )
    # The guard is itself a drop-in port: wrapping changes behaviour, not shape.
    assert isinstance(guarded, ReadinessGuardedBroker)


def test_the_guard_never_reads_a_field_the_port_does_not_define() -> None:
    """The canonical instrument has never carried these fields.

    So ``getattr(instrument, "tradable", True)`` could only ever return its
    default, on every instrument, forever. That default was ``True``: an unknown
    fact became permission, and no test could see it because the double invented
    the field. The assertion below is the defect, reproduced.
    """
    assert not hasattr(CANONICAL_INSTRUMENT, "con_id")
    assert not hasattr(CANONICAL_INSTRUMENT, "security_type")
    assert not hasattr(CANONICAL_INSTRUMENT, "tradable")
    assert getattr(CANONICAL_INSTRUMENT, "tradable", True) is True
    assert not hasattr(
        AccountSnapshot(
            account_alias="paper-poi", environment=Environment.PAPER,
            net_liquidation_value=Decimal(1), cash=Decimal(1),
            buying_power=Decimal(1), observed_at=NOW,
        ),
        "account_id",
    )


# == the happy path: audit BEFORE submit ======================================


def test_a_ready_order_reaches_the_broker() -> None:
    guard, broker, audit = _guard()
    receipt = guard.submit_order(_intent())
    assert receipt.broker_order_ref == "receipt-ord-1"
    assert len(broker.submitted) == 1
    assert guard.last_report is not None
    assert guard.last_report.readiness is TradingReadiness.PAPER_READY


def test_the_pre_trade_record_is_persisted_before_submission() -> None:
    """Ordering matters: an order must never reach the broker unrecorded."""
    guard, broker, audit = _guard()
    order: list[str] = []

    original_persist = audit.persist

    def _tracking_persist(record: Mapping[str, object]) -> str:
        order.append("audit")
        return original_persist(record)

    audit.persist = _tracking_persist  # type: ignore[method-assign]
    original_submit = broker.submit_order

    def _tracking_submit(intent: OrderIntent) -> BrokerOrderReceipt:
        order.append("submit")
        return original_submit(intent)

    broker.submit_order = _tracking_submit  # type: ignore[method-assign]

    guard.submit_order(_intent())
    assert order == ["audit", "submit"], "audit must land before submission"


def test_the_persisted_record_identifies_the_order_and_checkpoint() -> None:
    guard, _, audit = _guard()
    guard.submit_order(_intent())
    record = audit.records[0]
    assert record["checkpoint"] == "PRE_SUBMIT"
    assert record["client_order_id"] == "ord-1"
    assert record["correlation_id"] == "corr-1"
    assert record["trading_readiness"] == "PAPER_READY"
    assert len(record["gates"]) == 11  # type: ignore[arg-type]


def test_the_record_carries_the_opaque_broker_ref_not_a_decoded_id() -> None:
    guard, _, audit = _guard()
    guard.submit_order(_intent())
    refs = audit.records[0]["runtime_refs"]
    assert isinstance(refs, dict)
    assert refs["broker_instrument_ref"] == "ibkr-paper:265598"
    assert "con_id" not in refs


# == a failed gate must never reach the broker ================================


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("unhealthy session", {"broker": _Broker(healthy=False)}),
        ("stale session", {"broker": _Broker(checked_at=NOW - timedelta(minutes=30))}),
        (
            "live environment in paper mode",
            {"broker": _Broker(environment=Environment.LIVE)},
        ),
        (
            "non-trading environment",
            {"broker": _Broker(environment=Environment.RESEARCH)},
        ),
        (
            "unresolved contract",
            {
                "broker": _Broker(
                    resolve_error=BrokerPortError(
                        BrokerErrorCode.INSTRUMENT_NOT_FOUND, "unknown instrument"
                    )
                )
            },
        ),
        (
            "broker refuses the what-if",
            {
                "broker": _Broker(
                    preview_error=BrokerPortError(
                        BrokerErrorCode.BROKER_REJECTED, "instrument not tradable now"
                    )
                )
            },
        ),
        (
            "no what-if surface at all",
            {"broker": _Broker(what_if=BrokerCapabilityMode.UNSUPPORTED)},
        ),
        (
            "wrong routing",
            {
                "broker": _Broker(
                    instrument=BrokerInstrument(
                        broker_instrument_ref="ibkr-paper:1", symbol="AAPL",
                        exchange="ARCA", currency="USD",
                    )
                )
            },
        ),
        (
            "wrong currency",
            {
                "broker": _Broker(
                    instrument=BrokerInstrument(
                        broker_instrument_ref="ibkr-paper:1", symbol="AAPL",
                        exchange="SMART", currency="EUR",
                    )
                )
            },
        ),
        ("risk veto", {"risk": _Risk(outcome="VETO")}),
        ("no risk evaluation", {"risk": _Risk(outcome="NONE")}),
        ("zero size", {"risk": _Risk(quantity=Decimal(0))}),
        (
            "no protection capability",
            {"broker": _Broker(stop_capability=BrokerCapabilityMode.UNSUPPORTED)},
        ),
        ("quote feed down", {"quotes": _Quotes(raises=True)}),
        (
            "stale quote",
            {
                "quotes": _Quotes(
                    _quote(
                        Decimal("100"), Decimal("100.05"), NOW - timedelta(minutes=5)
                    )
                )
            },
        ),
        ("audit unavailable", {"audit": _Audit(ok=False)}),
    ],
)
def test_a_failed_gate_blocks_and_the_broker_is_never_called(
    label: str, kwargs: dict[str, Any]
) -> None:
    guard, broker, _ = _guard(**kwargs)
    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent())
    assert broker.submitted == [], f"{label}: submit_order must NEVER be reached"


def test_an_unprovable_tradability_blocks_rather_than_defaulting_open() -> None:
    """The exact defect: UNKNOWN must never become optimism.

    A broker with no what-if surface has told us nothing about whether this
    security can be traded right now. The only safe reading of silence is no.
    """
    guard, broker, _ = _guard(broker=_Broker(what_if=BrokerCapabilityMode.UNSUPPORTED))
    with pytest.raises(ReadinessBlockedError) as error:
        guard.submit_order(_intent())
    assert broker.submitted == []
    assert any("security_currently_tradable" in b for b in error.value.report.blockers)
    assert any("UNKNOWN" in gate.detail for gate in error.value.report.gates)


def test_the_what_if_used_to_prove_tradability_places_nothing() -> None:
    guard, broker, _ = _guard()
    guard.submit_order(_intent())
    assert len(broker.previewed) == 1, "tradability is proven by a what-if"
    assert len(broker.submitted) == 1, "and exactly one real order follows"


def test_a_plan_without_a_protective_stop_never_reaches_the_broker() -> None:
    guard, broker, _ = _guard()
    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent(order_type=OrderType.MARKET, stop_price=None))
    assert broker.submitted == []


def test_a_refusal_is_still_audited() -> None:
    guard, broker, audit = _guard(risk=_Risk(outcome="VETO"))
    with pytest.raises(ReadinessBlockedError) as error:
        guard.submit_order(_intent())
    assert broker.submitted == []
    assert audit.records, "a refusal is a decision worth recording"
    assert audit.records[0]["trading_readiness"] == "BLOCKED"
    assert error.value.report.blockers


# == audit failure must block submission ======================================


def test_audit_persistence_failure_blocks_submission() -> None:
    """The race the review identified: READY, then persistence fails."""
    guard, broker, _ = _guard(audit=_Audit(raises=True))
    with pytest.raises(AuditPersistenceError):
        guard.submit_order(_intent())
    assert broker.submitted == [], "an unrecorded order must never be submitted"


def test_an_audit_sink_returning_no_reference_blocks_submission() -> None:
    guard, broker, _ = _guard(audit=_Audit(empty_ref=True))
    with pytest.raises(AuditPersistenceError):
        guard.submit_order(_intent())
    assert broker.submitted == []


def test_a_retry_after_an_audit_failure_does_not_double_submit() -> None:
    """The record is written first, so a retry cannot duplicate an order."""
    audit = _Audit(raises=True)
    guard, broker, _ = _guard(audit=audit)
    for _ in range(3):
        with pytest.raises(AuditPersistenceError):
            guard.submit_order(_intent())
    assert broker.submitted == []


# == optional data still never blocks; other port methods are untouched =======


def test_missing_gics_and_pit_do_not_block_submission() -> None:
    guard, broker, _ = _guard(
        requirements=StrategyDataRequirements(strategy_id="s", uses_sector=True)
    )
    guard.submit_order(_intent())
    assert len(broker.submitted) == 1


def test_an_index_strategy_without_a_current_list_is_blocked() -> None:
    guard, broker, _ = _guard(
        requirements=StrategyDataRequirements(
            strategy_id="s", universe=UniverseType.SP500_CURRENT
        ),
        universe_status=UniverseListStatus.CURRENT_LIST_UNAVAILABLE,
    )
    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent())
    assert broker.submitted == []


def test_cancel_is_never_gated() -> None:
    """Reducing exposure must not be blocked by a readiness failure."""
    real = _Broker(healthy=False)
    guard, _, _ = _guard(broker=real)
    request = CancelOrderRequest(client_order_id="ord-1", correlation_id="corr-1")
    assert guard.cancel_order(request).state is OrderState.CANCELLED
    assert real.cancelled == [request]


def test_preview_places_nothing_and_is_not_gated() -> None:
    guard, real, _ = _guard(broker=_Broker(healthy=False))
    preview = guard.preview_order(_intent())
    assert preview.client_order_id == "ord-1"
    assert real.submitted == []


def test_a_configured_liquidity_rule_is_never_silently_ignored() -> None:
    """min_bid_size used to exist and never be evaluated."""
    guard, broker, _ = _guard(
        rules=MarketDataRules(min_bid_size=1000),
        quotes=_Quotes(
            _quote(
                Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1),
                bid_size=10, ask_size=10,
            )
        ),
    )
    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent())
    assert broker.submitted == []


def test_an_unknown_bid_size_fails_a_configured_liquidity_rule_closed() -> None:
    guard, broker, _ = _guard(
        rules=MarketDataRules(min_bid_size=100),
        quotes=_Quotes(
            _quote(Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1))
        ),
    )
    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent())
    assert broker.submitted == []


def test_readiness_is_still_not_authorization() -> None:
    guard, _, audit = _guard()
    guard.submit_order(_intent())
    assert "does NOT authorize" in str(audit.records[0]["authorization_note"])
