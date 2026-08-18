"""Enforcement at the REAL boundary: no order reaches the broker unready.

These matter more than another unit test of the evaluator. They drive
``submit_order`` — the single surface every order must cross — and assert on
what the *broker* saw, not on what the report said.

Three properties:

* a failed mandatory gate => BLOCKED, the refusal is audited, and the broker's
  ``submit_order`` is NEVER called;
* all gates satisfied => the pre-trade record is persisted BEFORE submission;
* audit persistence failure => submission refused, broker never called.

Offline and deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
from trading_application.trading_readiness_runtime import MarketDataRules, QuoteState
from trading_broker_core.port import (
    BrokerCapabilities,
    BrokerCapabilityMode,
    IntegrationHealth,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment, OrderType
from trading_market_data_core.trading_readiness import (
    StrategyDataRequirements,
    TradingMode,
    TradingReadiness,
    UniverseListStatus,
    UniverseType,
)

NOW = datetime(2026, 8, 18, 15, 30, tzinfo=UTC)


@dataclass
class _Instrument:
    symbol: str = "AAPL"
    con_id: int = 265598
    security_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    tradable: bool = True


@dataclass
class _Account:
    account_id: str = "DU1234567"


class _Broker:
    """A stand-in BrokerPort that RECORDS whether submit was ever reached."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        checked_at: datetime | None = None,
        account_id: str = "DU1234567",
        instrument: _Instrument | None = None,
        stop_capability: BrokerCapabilityMode = BrokerCapabilityMode.NATIVE,
    ) -> None:
        self.submitted: list[OrderIntent] = []
        self._healthy = healthy
        self._checked_at = checked_at or (NOW - timedelta(seconds=5))
        self._account_id = account_id
        self._instrument = instrument or _Instrument()
        self._stop_capability = stop_capability

    def health_check(self) -> IntegrationHealth:
        return IntegrationHealth(
            healthy=self._healthy, detail="ok", checked_at=self._checked_at
        )

    def get_account_snapshot(self, account_alias: str) -> _Account:
        return _Account(account_id=self._account_id)

    def resolve_instrument(self, query: Any) -> _Instrument:
        return self._instrument

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            order_types={OrderType.STOP: self._stop_capability},
            bracket=BrokerCapabilityMode.UNSUPPORTED,
            oco=BrokerCapabilityMode.UNSUPPORTED,
            trailing=BrokerCapabilityMode.UNSUPPORTED,
            fractional=BrokerCapabilityMode.UNSUPPORTED,
            outside_rth=BrokerCapabilityMode.UNSUPPORTED,
            what_if=BrokerCapabilityMode.NATIVE,
            streaming_events=BrokerCapabilityMode.NATIVE,
        )

    def submit_order(self, intent: OrderIntent) -> str:
        self.submitted.append(intent)
        return f"receipt-{intent.client_order_id}"


class _Quotes:
    def __init__(self, quote: QuoteState | None = None, raises: bool = False) -> None:
        self._quote = quote or QuoteState(
            bid=Decimal("100.00"), ask=Decimal("100.05"),
            observed_at=NOW - timedelta(seconds=1), bid_size=500, ask_size=500,
        )
        self._raises = raises

    def current_quote(self, symbol: str) -> QuoteState:
        if self._raises:
            raise RuntimeError("quote feed down")
        return self._quote


class _Risk:
    def __init__(self, outcome: str = "APPROVE", quantity: Decimal = Decimal(10)) -> None:
        self._outcome = outcome
        self._quantity = quantity

    def risk_result_for(self, intent: Any) -> Any:
        """Mirrors the real RiskResult shape (outcome + approved_quantity)."""
        if self._outcome == "NONE":
            return None
        result: Any = type("R", (), {})()
        result.outcome = type("O", (), {"value": self._outcome})()
        result.approved_quantity = self._quantity
        result.reason_codes = () if self._outcome == "APPROVE" else ("RISK_VETO",)
        return result


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


# == the happy path: audit BEFORE submit ======================================


def test_a_ready_order_reaches_the_broker() -> None:
    guard, broker, audit = _guard()
    receipt = guard.submit_order(_intent())
    assert receipt == "receipt-ord-1"
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

    def _tracking_submit(intent: OrderIntent) -> str:
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


# == a failed gate must never reach the broker ================================


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("unhealthy session", {"broker": _Broker(healthy=False)}),
        ("stale session", {"broker": _Broker(checked_at=NOW - timedelta(minutes=30))}),
        ("live account in paper mode", {"broker": _Broker(account_id="U7654321")}),
        (
            "unresolved contract",
            {"broker": _Broker(instrument=_Instrument(con_id=None))},  # type: ignore[arg-type]
        ),
        (
            "not tradable",
            {"broker": _Broker(instrument=_Instrument(tradable=False))},
        ),
        (
            "wrong routing",
            {"broker": _Broker(instrument=_Instrument(exchange="ARCA"))},
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
                    QuoteState(
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

    class _CancelBroker(_Broker):
        def __init__(self) -> None:
            super().__init__(healthy=False)
            self.cancelled: list[object] = []

        def cancel_order(self, request: object) -> str:
            self.cancelled.append(request)
            return "cancelled"

    real = _CancelBroker()
    guard, _, _ = _guard(broker=real)
    assert guard.cancel_order(object()) == "cancelled"
    assert real.cancelled


def test_preview_places_nothing_and_is_not_gated() -> None:
    class _PreviewBroker(_Broker):
        def preview_order(self, intent: object) -> str:
            return "preview-only"

    guard, real, _ = _guard(broker=_PreviewBroker(healthy=False))
    assert guard.preview_order(_intent()) == "preview-only"
    assert real.submitted == []


def test_a_configured_liquidity_rule_is_never_silently_ignored() -> None:
    """min_bid_size used to exist and never be evaluated."""
    guard, broker, _ = _guard(
        rules=MarketDataRules(min_bid_size=1000),
        quotes=_Quotes(
            QuoteState(
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
            QuoteState(Decimal("100"), Decimal("100.05"), NOW - timedelta(seconds=1))
        ),
    )
    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent())
    assert broker.submitted == []


def test_readiness_is_still_not_authorization() -> None:
    guard, _, audit = _guard()
    guard.submit_order(_intent())
    assert "does NOT authorize" in str(audit.records[0]["authorization_note"])
