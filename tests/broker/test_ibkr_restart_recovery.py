"""D-CANCEL-04/06: restart recovery separates visibility from authority.

Two defects meet here.

1. ``get_open_orders`` recovered identity from the broker snapshot and returned
   receipts, but never hydrated the adapter's cancellation maps, so cleanup
   after a Gateway/process restart could SEE the resting order and still fail
   with ORDER_NOT_FOUND.

2. The naive fix — hydrate from ``reqAllOpenOrders`` — is wrong in the other
   direction. That request is a GLOBAL visibility feed covering every API
   client and the TWS/Gateway UI. Seeing an order there proves it exists; it
   proves nothing about whether this client may cancel it.

So visibility stays global and grants nothing, and cancellation authority comes
only from the SAME-CLIENT feed under strict, fail-closed conditions.

All tests use the deterministic fake transport: no ibapi, no network, no order.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from trading_broker_core.port import (
    BrokerErrorCode,
    BrokerPortError,
    CancelOrderRequest,
)
from trading_broker_ibkr.paper_adapter import (
    DEFAULT_CLIENT_ID,
    FOREIGN_CLIENT_ID,
    IbkrPaperAdapter,
    RawOpenOrder,
    encode_order_ref,
)
from trading_contracts.enums import OrderState

from tests.broker.test_ibkr_paper_adapter import (
    ALIASES,
    PAPER_ACCOUNT,
    FakePaperTransport,
    make_adapter,
    make_intent,
)

LIFECYCLE_ID = "poi-ibkr-001-2026-07-21-cancel-path-F-05D0E19B631C2548"
OTHER_CLIENT_ID = DEFAULT_CLIENT_ID + 5


def _row(
    broker_order_id: int = 2,
    *,
    order_ref: str | None = None,
    client_id: int = DEFAULT_CLIENT_ID,
    status: str = "Submitted",
    account: str = PAPER_ACCOUNT,
    perm_id: int = 900002,
    symbol: str = "F",
) -> RawOpenOrder:
    return RawOpenOrder(
        broker_order_id=broker_order_id,
        order_ref=encode_order_ref(LIFECYCLE_ID) if order_ref is None else order_ref,
        status=status,
        symbol=symbol,
        client_id=client_id,
        perm_id=perm_id,
        account=account,
        action="BUY",
        quantity=Decimal("1"),
        order_type="LMT",
        limit_price=Decimal("12.02"),
    )


def _restarted(
    rows: tuple[RawOpenOrder, ...],
) -> tuple[IbkrPaperAdapter, FakePaperTransport]:
    """A FRESH adapter with empty process-local maps (a restart)."""
    transport = FakePaperTransport()
    transport.snapshot_override = rows
    adapter = make_adapter(transport)
    adapter.connect()
    return adapter, transport


# -- D. restart recovery ----------------------------------------------------


def test_a_fresh_adapter_recovers_a_lifecycle_order_it_may_cancel() -> None:
    adapter, transport = _restarted((_row(),))

    recovered = adapter.recover_cancellable_orders("paper-main")

    assert [r.client_order_id for r in recovered] == [LIFECYCLE_ID]
    assert recovered[0].broker_order_ref == "ibkr-paper:2"
    assert recovered[0].state is OrderState.ACKNOWLEDGED


def test_a_recovered_lifecycle_order_can_then_be_cancelled() -> None:
    """The owner-run failure mode: cleanup after a restart must work."""
    adapter, transport = _restarted((_row(),))
    adapter.recover_cancellable_orders("paper-main")

    receipt = adapter.cancel_order(
        CancelOrderRequest(client_order_id=LIFECYCLE_ID, correlation_id="cleanup")
    )

    assert receipt.state is OrderState.CANCELLED
    # The EXACT broker order id from the snapshot was used.
    assert "cancel:2" in transport.calls


def test_without_recovery_a_fresh_adapter_cannot_cancel() -> None:
    """Proves the recovery step is what grants the ability, not visibility."""
    adapter, _transport = _restarted((_row(),))
    adapter.get_open_orders("paper-main")  # visibility only

    with pytest.raises(BrokerPortError) as error:
        adapter.cancel_order(
            CancelOrderRequest(client_order_id=LIFECYCLE_ID, correlation_id="cleanup")
        )
    assert error.value.code is BrokerErrorCode.ORDER_NOT_FOUND


def test_the_post_cancel_snapshot_is_empty() -> None:
    adapter, transport = _restarted((_row(),))
    adapter.recover_cancellable_orders("paper-main")
    adapter.cancel_order(
        CancelOrderRequest(client_order_id=LIFECYCLE_ID, correlation_id="cleanup")
    )

    transport.snapshot_override = (_row(status="Cancelled"),)
    assert adapter.get_open_orders("paper-main") == ()
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_repeated_snapshots_are_idempotent() -> None:
    adapter, _transport = _restarted((_row(),))

    first = adapter.recover_cancellable_orders("paper-main")
    second = adapter.recover_cancellable_orders("paper-main")

    assert [r.client_order_id for r in first] == [r.client_order_id for r in second]
    assert [r.broker_order_ref for r in second] == ["ibkr-paper:2"]


def test_recovery_never_overwrites_a_live_in_process_mapping() -> None:
    """A snapshot must not rewrite the broker id of an order we just sent."""
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    adapter.connect()
    receipt = adapter.submit_order(make_intent(LIFECYCLE_ID, symbol="AAPL"))
    original_ref = receipt.broker_order_ref

    adapter.recover_cancellable_orders("paper-main")

    assert adapter._orders[LIFECYCLE_ID].broker_order_ref == original_ref


# -- 10 / E. visibility is not authority ------------------------------------


def test_global_visibility_alone_cannot_make_a_foreign_client_order_cancellable() -> None:
    """reqAllOpenOrders shows it; reqOpenOrders does not; so it is untouchable."""
    transport = FakePaperTransport()
    transport.snapshot_override = (_row(client_id=OTHER_CLIENT_ID),)
    adapter = make_adapter(transport)
    adapter.connect()

    visible = adapter.get_open_orders("paper-main")
    assert [v.client_order_id for v in visible] == [LIFECYCLE_ID]

    assert adapter.recover_cancellable_orders("paper-main") == ()
    with pytest.raises(BrokerPortError) as error:
        adapter.cancel_order(
            CancelOrderRequest(client_order_id=LIFECYCLE_ID, correlation_id="cleanup")
        )
    assert error.value.code is BrokerErrorCode.ORDER_NOT_FOUND


def test_client_id_mismatch_in_the_same_client_feed_fails_closed() -> None:
    """Even on the same-client feed, a mismatched clientId is not ours."""
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(client_id=OTHER_CLIENT_ID),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_an_unattributed_client_id_is_never_cancellable() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(client_id=FOREIGN_CLIENT_ID),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_a_foreign_order_ref_is_not_lifecycle_owned() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(order_ref="manual-ui-order"),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_a_missing_order_ref_is_not_lifecycle_owned() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(order_ref=""),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_a_malformed_lifecycle_ref_is_not_treated_as_owned() -> None:
    """The poi1: prefix alone is NOT proof of ownership."""
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(order_ref="poi1:   "),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_cross_account_rows_are_ignored() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(account="DU" + "9999999"),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_a_non_working_order_is_never_recovered_as_cancellable() -> None:
    adapter, transport = _restarted(())
    for status in ("Cancelled", "Filled", "Inactive", "NotAStatus"):
        transport.same_client_override = (_row(status=status),)
        assert adapter.recover_cancellable_orders("paper-main") == ()


def test_duplicate_identical_callbacks_are_deduplicated() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(), _row(), _row())
    assert len(adapter.recover_cancellable_orders("paper-main")) == 1


# -- 15. identity collisions fail closed ------------------------------------


def test_a_zero_broker_order_id_is_never_cancellable() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(broker_order_id=0),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_a_negative_broker_order_id_is_never_cancellable() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (_row(broker_order_id=-3),)
    assert adapter.recover_cancellable_orders("paper-main") == ()


def test_two_broker_orders_claiming_one_identity_fail_closed() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (
        _row(broker_order_id=2),
        _row(broker_order_id=3, perm_id=900003),
    )
    with pytest.raises(BrokerPortError) as error:
        adapter.recover_cancellable_orders("paper-main")
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE
    assert "ambiguous" in str(error.value)
    # Nothing was hydrated: a half-recovered state would be worse than none.
    assert adapter._broker_ids == {}


def test_one_broker_order_with_two_identities_fails_closed() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (
        _row(broker_order_id=2),
        _row(broker_order_id=2, order_ref=encode_order_ref("poi-ibkr-001-other")),
    )
    with pytest.raises(BrokerPortError) as error:
        adapter.recover_cancellable_orders("paper-main")
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE


def test_conflicting_perm_ids_fail_closed() -> None:
    adapter, transport = _restarted(())
    transport.same_client_override = (
        _row(broker_order_id=2, perm_id=900002),
        _row(broker_order_id=2, perm_id=111111),
    )
    with pytest.raises(BrokerPortError) as error:
        adapter.recover_cancellable_orders("paper-main")
    assert "permId" in str(error.value)


def test_a_snapshot_conflicting_with_process_local_state_fails_closed() -> None:
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    adapter.connect()
    adapter.submit_order(make_intent(LIFECYCLE_ID, symbol="AAPL"))

    # The broker now claims a DIFFERENT broker order id for the same identity.
    transport.same_client_override = (_row(broker_order_id=4242),)
    with pytest.raises(BrokerPortError) as error:
        adapter.recover_cancellable_orders("paper-main")
    assert error.value.code is BrokerErrorCode.UNKNOWN_BROKER_RESPONSE
    assert "already knows it as" in str(error.value)


# -- E. the isolation gates still hold --------------------------------------


def test_recovery_requires_a_connection() -> None:
    transport = FakePaperTransport()
    adapter = make_adapter(transport)
    with pytest.raises(BrokerPortError) as error:
        adapter.recover_cancellable_orders("paper-main")
    assert error.value.code is BrokerErrorCode.NOT_CONNECTED


def test_recovery_rejects_an_unknown_alias() -> None:
    adapter, _transport = _restarted(())
    with pytest.raises(BrokerPortError) as error:
        adapter.recover_cancellable_orders("not-an-alias")
    assert error.value.code is BrokerErrorCode.ACCOUNT_UNKNOWN


def test_paper_gates_are_unchanged_by_recovery() -> None:
    """Localhost-only, port-4002-only and DU-only remain in force."""
    for host, port in (("10.0.0.5", 4002), ("127.0.0.1", 4001), ("127.0.0.1", 7497)):
        adapter = make_adapter(FakePaperTransport(), host=host, port=port)
        with pytest.raises(BrokerPortError):
            adapter.connect()

    with pytest.raises(BrokerPortError):
        make_adapter(FakePaperTransport(), aliases={"live": "U" + "7654321"})


def test_no_global_cancel_surface_exists() -> None:
    """reqGlobalCancel must not exist anywhere in the adapter or harness."""
    from pathlib import Path

    for path in (
        Path("packages/py/broker-ibkr/src/trading_broker_ibkr/paper_adapter.py"),
        Path("packages/py/broker-ibkr/src/trading_broker_ibkr/callback_router.py"),
        Path("scripts/poi_ibkr_lifecycle.py"),
    ):
        assert "reqGlobalCancel" not in path.read_text(encoding="utf-8")


def test_capabilities_declare_native_bracket_and_oca() -> None:
    # ADR-0019: the adapter now establishes a real native bracket atomically in
    # submit_order, so bracket/oca are NATIVE (implementation and flip together).
    adapter, _transport = _restarted(())
    capabilities = adapter.capabilities()
    assert capabilities.bracket.value == "NATIVE"
    assert capabilities.oco.value == "NATIVE"


def test_the_aliases_map_is_still_paper_only() -> None:
    assert all(account.startswith("DU") for account in ALIASES.values())
