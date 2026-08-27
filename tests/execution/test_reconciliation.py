"""Reconciliation drills (IMP-1006): FakeBroker only, no network.

DoD: a mismatch creates a controlled state (report + findings +
recommendations) and blocks new entries when the policy requires it. The
engine never fixes anything: no submit, no cancel, recommendations only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_broker_core.fake import FakeBroker
from trading_broker_core.port import (
    AccountSnapshot,
    BrokerPosition,
    OrderIntent,
    OrderSide,
)
from trading_contracts.enums import Environment, HaltScope, OrderState, OrderType
from trading_execution_agent.reconciliation import (
    FindingKind,
    InternalOrderExpectation,
    InternalState,
    Recommendation,
    ReconciliationEngine,
    ReconciliationError,
    ReconciliationPolicy,
    ReconciliationTrigger,
)

AS_OF = datetime(2026, 7, 10, 17, 0, tzinfo=UTC)
EARLIER = AS_OF - timedelta(minutes=1)

ACCOUNT = AccountSnapshot(
    account_alias="paper-main",
    environment=Environment.PAPER,
    net_liquidation_value=Decimal("100000"),
    cash=Decimal("60000"),
    buying_power=Decimal("120000"),
    observed_at=EARLIER,
)

POLICY = ReconciliationPolicy(
    policy_version="reconciliation-policy-test-1",
    blocking_kinds=frozenset(
        {FindingKind.POSITION_MISMATCH, FindingKind.UNEXPECTED_BROKER_ORDER}
    ),
)


def make_broker(
    *, positions: tuple[BrokerPosition, ...] = ()
) -> FakeBroker:
    broker = FakeBroker(
        accounts={"paper-main": ACCOUNT}, positions={"paper-main": positions}
    )
    broker.connect()
    return broker


def submit(broker: FakeBroker, client_order_id: str) -> None:
    broker.submit_order(
        OrderIntent(
            client_order_id=client_order_id,
            authorization_ref=f"submit-auth:{client_order_id}",
            correlation_id=f"corr-{client_order_id}",
            environment=Environment.PAPER,
            account_alias="paper-main",
            symbol="AAPL",
            broker_instrument_ref="conid:265598",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            limit_price=Decimal("10"),
        )
    )


def internal(
    *,
    orders: tuple[InternalOrderExpectation, ...] = (),
    positions: dict[str, Decimal] | None = None,
    protected: dict[str, Decimal] | None = None,
    fill_ids: frozenset[str] = frozenset(),
) -> InternalState:
    return InternalState(
        account_alias="paper-main",
        environment=Environment.PAPER,
        open_orders=orders,
        positions=positions or {},
        protected_quantities=protected or {},
        processed_fill_event_ids=fill_ids,
        as_of=EARLIER,
    )


def run(broker: FakeBroker, state: InternalState) -> object:
    return ReconciliationEngine(POLICY).run(
        broker, state, trigger=ReconciliationTrigger.STARTUP, as_of=AS_OF
    )


def event_ids(broker: FakeBroker) -> frozenset[str]:
    return frozenset(
        f"{e.sequence}:{e.client_order_id}:{e.state.value}" for e in broker.order_events()
    )


# -- clean state -------------------------------------------------------------------


def test_matching_state_is_reconciled_no_block() -> None:
    broker = make_broker(
        positions=(
            BrokerPosition(symbol="MSFT", quantity=Decimal("10"), average_price=Decimal("400")),
        )
    )
    submit(broker, "ord-1")
    state = internal(
        orders=(InternalOrderExpectation("ord-1", OrderState.ACKNOWLEDGED),),
        positions={"MSFT": Decimal("10")},
        protected={"MSFT": Decimal("10")},
        fill_ids=event_ids(broker),
    )
    report = run(broker, state)
    assert report.reconciled  # type: ignore[attr-defined]
    assert report.findings == ()  # type: ignore[attr-defined]
    assert not report.entry_block_required  # type: ignore[attr-defined]
    assert report.recommended_halt is None  # type: ignore[attr-defined]


# -- order mismatches -----------------------------------------------------------------


def test_missing_broker_order_needs_review() -> None:
    broker = make_broker()
    state = internal(orders=(InternalOrderExpectation("ghost", OrderState.ACKNOWLEDGED),))
    report = run(broker, state)
    finding = next(f for f in report.findings if f.kind is FindingKind.MISSING_BROKER_ORDER)  # type: ignore[attr-defined]
    assert finding.recommendation is Recommendation.NEEDS_REVIEW
    assert not report.reconciled  # type: ignore[attr-defined]


def test_unexpected_broker_order_needs_review_and_blocks_per_policy() -> None:
    broker = make_broker()
    submit(broker, "rogue-1")
    report = run(broker, internal(fill_ids=event_ids(broker)))
    finding = next(f for f in report.findings if f.kind is FindingKind.UNEXPECTED_BROKER_ORDER)  # type: ignore[attr-defined]
    assert finding.recommendation is Recommendation.NEEDS_REVIEW
    # policy lists UNEXPECTED_BROKER_ORDER as blocking -> controlled state
    assert report.entry_block_required  # type: ignore[attr-defined]
    halt = report.recommended_halt  # type: ignore[attr-defined]
    assert halt is not None and halt.scope is HaltScope.ACCOUNT_HALT
    assert halt.keep_protection  # protection never dropped by the block
    assert halt.target_ref == "paper-main"  # alias only, no account id


def test_order_state_mismatch_recommends_resync() -> None:
    broker = make_broker()
    submit(broker, "ord-1")
    state = internal(
        orders=(InternalOrderExpectation("ord-1", OrderState.SUBMITTING),),
        fill_ids=event_ids(broker),
    )
    report = run(broker, state)
    finding = next(f for f in report.findings if f.kind is FindingKind.ORDER_STATE_MISMATCH)  # type: ignore[attr-defined]
    assert finding.recommendation is Recommendation.RESYNC_STATE
    assert finding.expected == "SUBMITTING" and finding.observed == "ACKNOWLEDGED"


# -- position / protection mismatches ----------------------------------------------------


def test_position_mismatch_blocks_new_entries() -> None:
    broker = make_broker(
        positions=(
            BrokerPosition(symbol="MSFT", quantity=Decimal("10"), average_price=Decimal("400")),
        )
    )
    report = run(broker, internal(positions={"MSFT": Decimal("25")}))
    finding = next(f for f in report.findings if f.kind is FindingKind.POSITION_MISMATCH)  # type: ignore[attr-defined]
    assert finding.recommendation is Recommendation.BLOCK_NEW_ENTRIES
    assert report.entry_block_required  # type: ignore[attr-defined]
    assert report.recommended_halt is not None  # type: ignore[attr-defined]


def test_protection_mismatch_needs_review() -> None:
    broker = make_broker(
        positions=(
            BrokerPosition(symbol="MSFT", quantity=Decimal("10"), average_price=Decimal("400")),
        )
    )
    report = run(
        broker,
        internal(positions={"MSFT": Decimal("10")}, protected={"MSFT": Decimal("4")}),
    )
    finding = next(f for f in report.findings if f.kind is FindingKind.PROTECTION_MISMATCH)  # type: ignore[attr-defined]
    assert finding.recommendation is Recommendation.NEEDS_REVIEW
    assert "10" in finding.expected and "4" in finding.observed


# -- fills ---------------------------------------------------------------------------------


def test_unprocessed_fill_event_recommends_resync() -> None:
    broker = make_broker()
    submit(broker, "ord-1")
    state = internal(orders=(InternalOrderExpectation("ord-1", OrderState.ACKNOWLEDGED),))
    report = run(broker, state)  # fill_ids empty: event not processed internally
    finding = next(f for f in report.findings if f.kind is FindingKind.MISSING_FILL_EVENT)  # type: ignore[attr-defined]
    assert finding.recommendation is Recommendation.RESYNC_STATE


# -- no auto-fix, no retry, no submit -----------------------------------------------------


def test_engine_never_submits_or_cancels() -> None:
    broker = make_broker()
    submit(broker, "rogue-1")
    events_before = list(broker.order_events())
    orders_before = broker.get_open_orders("paper-main")
    run(broker, internal(fill_ids=event_ids(broker)))
    # broker state untouched: same orders, same events - nothing was fixed
    assert broker.get_open_orders("paper-main") == orders_before
    assert list(broker.order_events()) == events_before


def test_engine_source_has_no_submit_cancel_or_secret_access() -> None:
    from pathlib import Path

    import trading_execution_agent.reconciliation as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    for fragment in (
        "submit_order", "cancel_order", "preview_order",
        "os.environ", "getenv", "getpass", "input(", "passw" + "ord",
    ):
        assert fragment not in source, fragment
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        for forbidden in ("ib" + "api", "httpx", "requests", "socket", "trading_broker_ibkr"):
            assert forbidden not in line, line


# -- determinism / fail-closed ---------------------------------------------------------------


def test_same_state_same_report() -> None:
    def build() -> object:
        broker = make_broker(
            positions=(
                BrokerPosition(
                    symbol="MSFT", quantity=Decimal("10"), average_price=Decimal("400")
                ),
            )
        )
        submit(broker, "rogue-1")
        return run(broker, internal(positions={"MSFT": Decimal("25")}))

    assert build() == build()


def test_invalid_policy_and_lookahead_fail_closed() -> None:
    with pytest.raises(ReconciliationError, match="policy_version"):
        ReconciliationPolicy(policy_version=" ", blocking_kinds=frozenset())
    broker = make_broker()
    late = InternalState(
        account_alias="paper-main",
        environment=Environment.PAPER,
        open_orders=(),
        positions={},
        protected_quantities={},
        processed_fill_event_ids=frozenset(),
        as_of=AS_OF + timedelta(minutes=1),
    )
    with pytest.raises(ReconciliationError, match="lookahead"):
        ReconciliationEngine(POLICY).run(
            broker, late, trigger=ReconciliationTrigger.PERIODIC, as_of=AS_OF
        )
    with pytest.raises(ReconciliationError, match="timezone-aware"):
        ReconciliationEngine(POLICY).run(
            broker,
            internal(),
            trigger=ReconciliationTrigger.EVENT_DRIVEN,
            as_of=datetime(2026, 7, 10, 17, 0),  # noqa: DTZ001
        )
