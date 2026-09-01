"""Adversarial tests for the production protected-entry lifecycle (ADR-0019).

The property under test is a safety property: ``protection_for`` returns
``established=True`` with a naming reference ONLY when a native IBKR bracket will
atomically protect the entry, and returns ``established=False`` for every way an
entry could end up unprotected. The guard and the execution service both treat
``established and protective_order_refs`` as "proven", so a False here is a
refusal there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from trading_broker_core.fake import DEFAULT_CAPABILITIES, FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityMode,
    BrokerInstrument,
    OrderIntent,
    OrderSide,
    ProtectiveBracket,
)
from trading_contracts.enums import Environment, OrderType
from trading_execution_agent.protected_entry import (
    NATIVE_BRACKET_MECHANISM,
    IbkrProtectedEntryLifecycle,
)

CONID = "conid:265598"
NOW = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)


def _broker(bracket: BrokerCapabilityMode = BrokerCapabilityMode.NATIVE) -> FakeBroker:
    caps = BrokerCapabilities(
        order_types=DEFAULT_CAPABILITIES.order_types,
        bracket=bracket,
        oco=DEFAULT_CAPABILITIES.oco,
        trailing=DEFAULT_CAPABILITIES.trailing,
        fractional=DEFAULT_CAPABILITIES.fractional,
        outside_rth=DEFAULT_CAPABILITIES.outside_rth,
        what_if=DEFAULT_CAPABILITIES.what_if,
        streaming_events=DEFAULT_CAPABILITIES.streaming_events,
    )
    account = AccountSnapshot(
        account_alias="paper-poi",
        environment=Environment.PAPER,
        net_liquidation_value=Decimal("100000"),
        cash=Decimal("100000"),
        buying_power=Decimal("200000"),
        observed_at=NOW,
        session_pnl=Decimal("0"),
    )
    return FakeBroker(
        accounts={"paper-poi": account},
        instruments={"AAPL": BrokerInstrument(CONID, "AAPL", "SMART", "USD")},
        capabilities=caps,
    )


def _intent(
    *,
    side: OrderSide = OrderSide.BUY,
    limit_price: Decimal | None = Decimal("190.00"),
    protective: ProtectiveBracket | None = None,
    environment: Environment = Environment.PAPER,
    with_default_bracket: bool = True,
) -> OrderIntent:
    if protective is None and with_default_bracket:
        # A long bracket by default; a short test overrides it.
        stop = Decimal("180.00") if side is OrderSide.BUY else Decimal("200.00")
        protective = ProtectiveBracket(
            stop_price=stop, stop_client_order_id="coid-1:stop"
        )
    return OrderIntent(
        client_order_id="coid-1",
        authorization_ref="unauthorized-probe:d1",
        correlation_id="corr-1",
        environment=environment,
        account_alias="paper-poi",
        symbol="AAPL",
        broker_instrument_ref=CONID,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=limit_price,
        protective=protective,
    )


def test_native_long_bracket_is_proven_and_names_the_stop() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    evidence = lifecycle.protection_for(_intent())
    assert evidence.established is True
    assert evidence.mechanism == NATIVE_BRACKET_MECHANISM
    # Proven REQUIRES a naming reference: a mechanism with nothing to point at is
    # a promise, not protection (guard/service check `established and refs`).
    assert evidence.protective_order_refs == ("coid-1:stop",)


def test_no_bracket_on_the_intent_is_not_protected() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    evidence = lifecycle.protection_for(_intent(with_default_bracket=False))
    assert evidence.established is False
    assert not evidence.protective_order_refs


def test_non_native_bracket_capability_is_not_protected() -> None:
    # The inert-until-adapter-ready property: while the adapter still declares
    # bracket UNSUPPORTED, the lifecycle proves nothing, so nothing changes.
    lifecycle = IbkrProtectedEntryLifecycle(_broker(BrokerCapabilityMode.UNSUPPORTED))
    evidence = lifecycle.protection_for(_intent())
    assert evidence.established is False
    assert "UNSUPPORTED" in evidence.detail


def test_emulated_bracket_is_not_native_and_is_refused() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker(BrokerCapabilityMode.EMULATED))
    evidence = lifecycle.protection_for(_intent())
    assert evidence.established is False


def test_live_environment_is_out_of_scope_and_refused() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    evidence = lifecycle.protection_for(_intent(environment=Environment.LIVE))
    assert evidence.established is False
    assert "Paper-only" in evidence.detail


def test_long_stop_at_or_above_entry_protects_nothing() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    at = _intent(
        protective=ProtectiveBracket(
            stop_price=Decimal("190.00"), stop_client_order_id="coid-1:stop"
        )
    )
    assert lifecycle.protection_for(at).established is False
    above = _intent(
        protective=ProtectiveBracket(
            stop_price=Decimal("195.00"), stop_client_order_id="coid-1:stop"
        )
    )
    assert lifecycle.protection_for(above).established is False


def test_short_entry_stop_must_sit_above_entry() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    good = _intent(
        side=OrderSide.SELL,
        protective=ProtectiveBracket(
            stop_price=Decimal("200.00"), stop_client_order_id="coid-1:stop"
        ),
    )
    assert lifecycle.protection_for(good).established is True
    bad = _intent(
        side=OrderSide.SELL,
        protective=ProtectiveBracket(
            stop_price=Decimal("185.00"), stop_client_order_id="coid-1:stop"
        ),
    )
    assert lifecycle.protection_for(bad).established is False


def test_market_entry_without_a_reference_price_is_refused() -> None:
    # A MARKET parent legitimately has no limit price, so the protective stop
    # side cannot be validated ex-ante. Out of scope for this milestone (the POI
    # is a LIMIT entry), and refusing is the fail-closed answer.
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    market = OrderIntent(
        client_order_id="coid-1",
        authorization_ref="unauthorized-probe:d1",
        correlation_id="corr-1",
        environment=Environment.PAPER,
        account_alias="paper-poi",
        symbol="AAPL",
        broker_instrument_ref=CONID,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        limit_price=None,
        protective=ProtectiveBracket(
            stop_price=Decimal("180.00"), stop_client_order_id="coid-1:stop"
        ),
    )
    evidence = lifecycle.protection_for(market)
    assert evidence.established is False
    assert "reference price" in evidence.detail


def test_capability_query_raising_fails_closed() -> None:
    class _Raising(FakeBroker):
        def capabilities(self) -> BrokerCapabilities:
            raise RuntimeError("gateway down")

    broker = _Raising(
        instruments={"AAPL": BrokerInstrument(CONID, "AAPL", "SMART", "USD")}
    )
    lifecycle = IbkrProtectedEntryLifecycle(broker)
    evidence = lifecycle.protection_for(_intent())
    assert evidence.established is False
    assert "raised" in evidence.detail


def test_take_profit_ref_is_named_when_present() -> None:
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    intent = _intent(
        protective=ProtectiveBracket(
            stop_price=Decimal("180.00"),
            stop_client_order_id="coid-1:stop",
            take_profit_price=Decimal("210.00"),
            take_profit_client_order_id="coid-1:tp",
        )
    )
    evidence = lifecycle.protection_for(intent)
    assert evidence.established is True
    assert evidence.protective_order_refs == ("coid-1:stop", "coid-1:tp")


@pytest.mark.parametrize("established_only", [True])
def test_proven_evidence_always_carries_a_reference(established_only: bool) -> None:
    """Non-vacuity: whenever the lifecycle says established, it names a ref.

    If a future edit set ``established=True`` without refs, the guard would read
    it as unproven anyway; this pins the invariant at the source.
    """
    lifecycle = IbkrProtectedEntryLifecycle(_broker())
    evidence = lifecycle.protection_for(_intent())
    assert evidence.established is established_only
    assert bool(evidence.protective_order_refs) is evidence.established
