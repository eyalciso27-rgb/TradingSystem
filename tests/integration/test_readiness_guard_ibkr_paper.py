"""The readiness guard driven against the REAL IBKR paper adapter.

Every other enforcement test uses a double. This one wraps an actual
``IbkrPaperAdapter`` — the same class the owner host runs — over the same
deterministic fake transport the adapter's own suite uses. No ibapi import, no
socket, no real order: the seam is the transport, exactly one layer below the
port, so everything above it is the production code path.

This is the test that fails when the guard and the canonical ``BrokerPort``
disagree. A hand-written double can be given a ``con_id``, a ``security_type``
or a ``tradable`` flag, and then a guard reading those fields looks correct
forever. The real adapter returns a real ``BrokerInstrument``, which has none
of them, so the disagreement surfaces here as a failure rather than in
production as an order that should never have been submitted.

Paper-only invariants are the adapter's, and they still hold: it refuses to
construct against a non-DU account and refuses any port other than 4002.
Nothing here authorizes Paper Auto or Live.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.readiness_guarded_broker import (
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
    BrokerPort,
    BrokerPortError,
    InstrumentQuery,
    OrderIntent,
    OrderSide,
    TimeInForce,
)
from trading_broker_ibkr.paper_adapter import IbkrPaperAdapter
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
)

from tests.broker.test_ibkr_paper_adapter import (
    ALIASES,
    FakePaperTransport,
)

#: The adapter clock is an explicit test input; production uses the real clock.
ADAPTER_NOW = datetime(2026, 8, 18, 15, 30, tzinfo=UTC)
#: Readiness is evaluated a couple of seconds after the broker was observed, so
#: the session-freshness gate sees a genuinely recent observation.
GUARD_NOW = ADAPTER_NOW + timedelta(seconds=2)

def _quote(*args: object, **kw: object) -> QuoteState:
    """A quote that states its feed, because an unstated feed now blocks.

    ``market_data_state`` defaults to UNKNOWN in production for exactly that
    reason; these tests are about other properties, so they declare a healthy
    realtime feed rather than silently relying on a default that must not exist.
    """
    kw.setdefault("market_data_state", MarketDataState.REALTIME)
    return QuoteState(*args, **kw)  # type: ignore[arg-type]




class _Quotes:
    def __init__(self, quote: QuoteState | None = None) -> None:
        self._quote = quote or _quote(
            bid=Decimal("100.00"), ask=Decimal("100.05"),
            observed_at=GUARD_NOW - timedelta(seconds=1), bid_size=500, ask_size=500,
        )

    def current_quote(self, symbol: str) -> QuoteState:
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
    def __init__(self) -> None:
        self.records: list[Mapping[str, object]] = []

    def can_persist(self) -> bool:
        return True

    def persist(self, record: Mapping[str, object]) -> str:
        self.records.append(record)
        return f"audit-{len(self.records)}"


def _adapter(transport: FakePaperTransport | None = None) -> IbkrPaperAdapter:
    """A REAL adapter over the deterministic transport, connected."""
    fake = transport or FakePaperTransport()
    adapter = IbkrPaperAdapter(
        lambda: fake,
        account_aliases=dict(ALIASES),
        host="127.0.0.1",
        port=4002,  # PAPER only; 4001 (live) is refused by the adapter itself
        clock=lambda: ADAPTER_NOW,
    )
    adapter.connect()
    return adapter


def _guard(
    adapter: IbkrPaperAdapter,
    *,
    quotes: _Quotes | None = None,
    risk: _Risk | None = None,
    audit: _Audit | None = None,
) -> tuple[ReadinessGuardedBroker, _Audit]:
    sink = audit or _Audit()
    guard = ReadinessGuardedBroker(
        adapter,
        config=GuardConfig(
            mode=TradingMode.PAPER,
            requirements=StrategyDataRequirements(strategy_id="baseline-trend-breakout-v1"),
            rules=MarketDataRules(
                max_spread_fraction=Decimal("0.01"), min_price=Decimal("1.00")
            ),
            account_alias="paper-main",
        ),
        quotes=quotes or _Quotes(),
        risk=risk or _Risk(),
        audit=sink,
        now=lambda: GUARD_NOW,
    )
    return guard, sink


def _intent(symbol: str = "AAPL", **overrides: object) -> OrderIntent:
    fields: dict[str, object] = {
        "client_order_id": "guard-ord-1",
        "authorization_ref": "submit-auth:guard-ord-1",
        "correlation_id": "corr-1",
        "environment": Environment.PAPER,
        "account_alias": "paper-main",
        "symbol": symbol,
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": Decimal(10),
        "limit_price": Decimal("100.00"),
        "stop_price": Decimal("95.00"),
        "time_in_force": TimeInForce.DAY,
    }
    fields.update(overrides)
    return OrderIntent(**fields)  # type: ignore[arg-type]


# == the adapter really is the canonical port =================================


def test_the_real_adapter_satisfies_the_port_the_guard_expects() -> None:
    port: BrokerPort = _adapter()
    instrument = port.resolve_instrument(InstrumentQuery(symbol="AAPL"))
    # Exactly the four canonical fields, and no invented fifth.
    assert {field.name for field in fields(instrument)} == {
        "broker_instrument_ref", "symbol", "exchange", "currency",
    }


def test_the_guard_wraps_the_real_adapter_and_stays_a_port() -> None:
    guard, _ = _guard(_adapter())
    # Statically a BrokerPort: a drift between guard and contract is a type error.
    port: BrokerPort = guard
    assert port.capabilities().what_if.value


# == the happy path against the real adapter ==================================


def test_a_ready_order_reaches_the_real_adapter() -> None:
    transport = FakePaperTransport()
    guard, audit = _guard(_adapter(transport))

    receipt = guard.submit_order(_intent())

    assert receipt.state is OrderState.ACKNOWLEDGED
    assert any(call.startswith("submit:") for call in transport.calls)
    assert guard.last_report is not None
    assert guard.last_report.readiness is TradingReadiness.PAPER_READY
    assert audit.records[0]["checkpoint"] == "PRE_SUBMIT"


def test_the_identity_recorded_is_the_adapter_s_own_opaque_ref() -> None:
    """Whatever the adapter calls the instrument is what gets audited.

    The application never parses it, so this asserts the value round-trips
    unchanged rather than asserting any particular format.
    """
    guard, audit = _guard(_adapter())
    guard.submit_order(_intent())

    resolved = _adapter().resolve_instrument(InstrumentQuery(symbol="AAPL"))
    refs = audit.records[0]["runtime_refs"]
    assert isinstance(refs, dict)
    assert refs["broker_instrument_ref"] == resolved.broker_instrument_ref
    assert audit.records[0]["security_id"] == resolved.broker_instrument_ref


def test_tradability_is_proven_by_the_adapter_s_own_what_if() -> None:
    """The adapter's ``preview_order`` places nothing, and is what proves it."""
    transport = FakePaperTransport()
    guard, _ = _guard(_adapter(transport))

    guard.submit_order(_intent())

    submits = [call for call in transport.calls if call.startswith("submit:")]
    assert len(submits) == 1, "the what-if must not reach the transport as an order"


# == the real adapter's refusals block, and nothing is submitted ==============


def test_an_instrument_the_real_adapter_cannot_resolve_blocks() -> None:
    """The fake transport resolves AAPL and nothing else, as IBKR would."""
    transport = FakePaperTransport()
    guard, _ = _guard(_adapter(transport))

    with pytest.raises(ReadinessBlockedError) as error:
        guard.submit_order(_intent(symbol="NOSUCH", client_order_id="guard-ord-2"))

    assert not [call for call in transport.calls if call.startswith("submit:")]
    assert any("contract_identity_valid" in b for b in error.value.report.blockers)


def test_an_intent_the_adapter_refuses_blocks_before_submission() -> None:
    """An order the adapter will not accept must never reach submission.

    Fractional quantities are DISABLED in the MVP, and the adapter says so from
    ``preview_order`` — which is exactly the surface tradability is proven on.
    The refusal therefore arrives as a readiness block rather than as an
    exception from a half-attempted submit.
    """
    transport = FakePaperTransport()
    guard, _ = _guard(_adapter(transport))

    with pytest.raises(ReadinessBlockedError) as error:
        guard.submit_order(
            _intent(quantity=Decimal("10.5"), client_order_id="guard-ord-3")
        )

    assert not [call for call in transport.calls if call.startswith("submit:")]
    assert any("security_currently_tradable" in b for b in error.value.report.blockers)


def test_a_disconnected_adapter_blocks_and_never_submits() -> None:
    transport = FakePaperTransport()
    adapter = _adapter(transport)
    adapter.disconnect()
    guard, _ = _guard(adapter)

    with pytest.raises(ReadinessBlockedError) as error:
        guard.submit_order(_intent(client_order_id="guard-ord-4"))

    assert not [call for call in transport.calls if call.startswith("submit:")]
    assert any("ibkr_connectivity" in b for b in error.value.report.blockers)


def test_a_risk_veto_blocks_the_real_adapter_too() -> None:
    transport = FakePaperTransport()
    guard, audit = _guard(_adapter(transport), risk=_Risk(outcome="VETO"))

    with pytest.raises(ReadinessBlockedError):
        guard.submit_order(_intent(client_order_id="guard-ord-5"))

    assert not [call for call in transport.calls if call.startswith("submit:")]
    assert audit.records[0]["trading_readiness"] == "BLOCKED"


# == the adapter's own paper invariants are untouched by the guard ============


def test_the_adapter_still_refuses_a_non_paper_account() -> None:
    """The DU invariant lives in the adapter, and the guard did not move it."""
    with pytest.raises(BrokerPortError, match="non-paper account"):
        IbkrPaperAdapter(
            lambda: FakePaperTransport(),
            account_aliases={"live-main": "U" + "7654321"},
        )


def test_the_adapter_still_refuses_the_live_port() -> None:
    adapter = IbkrPaperAdapter(
        lambda: FakePaperTransport(),
        account_aliases=dict(ALIASES),
        host="127.0.0.1",
        port=4001,
    )
    with pytest.raises(BrokerPortError):
        adapter.connect()
