"""Shared deterministic fixtures for the MVP strategy/trading-policy tests.

Everything here is a frozen, hand-constructed market-data fixture (ADR-0014):
a rising-SMA150 uptrend, a genuinely contracting base and a completed breakout
bar that make ``baseline-trend-breakout-v1`` fire, plus the account / execution
inputs the orchestrator needs. Mutation tests perturb ONE element of the spec to
prove each mandatory predicate is load-bearing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from trading_application.mvp_policy_config import MvpPolicyConfig, default_mvp_policy_config
from trading_application.mvp_trading_policy import (
    MvpAccountSnapshot,
    MvpCandidateFacts,
    MvpExecutionInputs,
    MvpPolicyOutcome,
    MvpTradingPolicy,
)
from trading_application.pre_submit_check import (
    ApprovalState,
    RefreshedAccount,
    RefreshedMarket,
)
from trading_broker_core.port import SessionPnlStatus
from trading_contracts.enums import Environment, MarketSessionStatus, OrderType
from trading_market_data_core.types import Bar, BarSeries

EPOCH = datetime(2025, 1, 1, tzinfo=UTC)
INSTRUMENT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "mvp-baseline-test-instrument")

#: A (close, day_range) spec that triggers a clean breakout. Index 170 is the
#: completed breakout bar; 151..169 are the contracting base; 0..150 the trend.
CloseRange = tuple[float, float]


def triggered_spec() -> list[CloseRange]:
    """171 (close, range) pairs producing a single valid long breakout."""
    spec: list[CloseRange] = []
    for i in range(151):  # rising trend -> SMA150 rises, close ends well above it
        spec.append((100 + i * 0.4307, 2.0))
    for i in range(151, 170):  # contracting base, flat closes above SMA150
        day_range = 2.4 if i <= 160 else (2.0 if i <= 164 else 1.6)
        spec.append((165.0, day_range))
    spec.append((166.5, 2.2))  # breakout: completed close just above resistance
    return spec


@pytest.fixture
def instrument_id() -> uuid.UUID:
    return INSTRUMENT_ID


@pytest.fixture
def make_bar() -> Callable[..., Bar]:
    def _make_bar(
        index: int,
        close: float,
        day_range: float,
        *,
        freshness: object | None = None,
        provider: str = "fixture",
        instrument: uuid.UUID = INSTRUMENT_ID,
    ) -> Bar:
        from trading_contracts.enums import FreshnessClass

        resolved = freshness if freshness is not None else FreshnessClass.REALTIME
        close_dec = Decimal(str(close))
        half = Decimal(str(day_range)) / 2
        start = EPOCH + timedelta(days=index)
        return Bar(
            instrument_id=str(instrument),
            provider_id=provider,
            interval="1D",
            open=close_dec,
            high=close_dec + half,
            low=close_dec - half,
            close=close_dec,
            volume=Decimal(1_000_000),
            start_at=start,
            end_at=start + timedelta(hours=6),
            observed_at=start + timedelta(hours=6),
            freshness=resolved,  # type: ignore[arg-type]
        )

    return _make_bar


@pytest.fixture
def build_series(make_bar: Callable[..., Bar]) -> Callable[..., BarSeries]:
    def _build(
        spec: list[CloseRange],
        *,
        provider: str = "fixture",
        instrument: uuid.UUID = INSTRUMENT_ID,
        last_freshness: object | None = None,
    ) -> BarSeries:
        bars = []
        for index, (close, day_range) in enumerate(spec):
            fresh = last_freshness if (index == len(spec) - 1 and last_freshness) else None
            bars.append(
                make_bar(
                    index, close, day_range, freshness=fresh, provider=provider,
                    instrument=instrument,
                )
            )
        return BarSeries(
            instrument_id=str(instrument), provider_id=provider, interval="1D",
            bars=tuple(bars),
        )

    return _build


@pytest.fixture
def base_spec() -> list[CloseRange]:
    """A fresh copy of the triggering spec, for mutation tests to perturb."""
    return triggered_spec()


@pytest.fixture
def triggered_series(build_series: Callable[..., BarSeries]) -> BarSeries:
    return build_series(triggered_spec())


@pytest.fixture
def as_of_after() -> Callable[[BarSeries], datetime]:
    def _as_of(series: BarSeries) -> datetime:
        return series.bars[-1].observed_at + timedelta(minutes=1)

    return _as_of


@pytest.fixture
def mvp_config() -> MvpPolicyConfig:
    return default_mvp_policy_config()


@pytest.fixture
def candidate_facts() -> MvpCandidateFacts:
    return MvpCandidateFacts(symbol="MVPX", sector="TECH", cluster="SEMI", source_id="scanner")


@pytest.fixture
def account_ok() -> Callable[[datetime], MvpAccountSnapshot]:
    def _account(observed_at: datetime) -> MvpAccountSnapshot:
        return MvpAccountSnapshot(
            broker_net_liquidation_value=Decimal(1_000_000),
            simulation_equity_cap=Decimal(1_000_000),
            session_pnl=Decimal("-100"),
            session_pnl_status=SessionPnlStatus.OK,
            observed_at=observed_at,
        )

    return _account


@pytest.fixture
def execution_inputs() -> Callable[..., MvpExecutionInputs]:
    def _execution(
        *,
        entry_price: Decimal,
        as_of: datetime,
        environment: Environment = Environment.PAPER,
        market_price: Decimal | None = None,
        approved: bool = True,
        broker_healthy: bool = True,
        reconciliation_required: bool = False,
    ) -> MvpExecutionInputs:
        return MvpExecutionInputs(
            environment=environment,
            account_alias="paper-mvp",
            side="BUY",
            order_type=OrderType.LIMIT,
            time_in_force="DAY",
            client_order_id="mvp-entry-1",
            broker_instrument_ref="brkr:conid:987654",
            broker_exchange="SMART",
            broker_currency="USD",
            refreshed_market=RefreshedMarket(
                price=market_price if market_price is not None else entry_price,
                observed_at=as_of,
            ),
            refreshed_account=RefreshedAccount(
                risk_budget_equity=Decimal(1_000_000), observed_at=as_of
            ),
            approval=ApprovalState(approved=approved, approved_at=as_of if approved else None),
            broker_healthy=broker_healthy,
            reconciliation_required=reconciliation_required,
        )

    return _execution


@pytest.fixture
def run_policy(
    mvp_config: MvpPolicyConfig,
    instrument_id: uuid.UUID,
    candidate_facts: MvpCandidateFacts,
    account_ok: Callable[[datetime], MvpAccountSnapshot],
    execution_inputs: Callable[..., MvpExecutionInputs],
    as_of_after: Callable[[BarSeries], datetime],
) -> Callable[..., MvpPolicyOutcome]:
    """Run the orchestrator on a series with sensible, overridable defaults."""

    def _run(
        series: BarSeries,
        *,
        account: MvpAccountSnapshot | None = None,
        candidate: MvpCandidateFacts | None = None,
        held_positions: object = (),
        system_records: object = (),
        environment: Environment = Environment.PAPER,
        market_price: Decimal | None = None,
        approved: bool = True,
        broker_healthy: bool = True,
        reconciliation_required: bool = False,
        active_halts: object = (),
        session: MarketSessionStatus | None = MarketSessionStatus.REGULAR,
    ) -> MvpPolicyOutcome:
        policy = MvpTradingPolicy(mvp_config)
        as_of = as_of_after(series)
        # The planned entry price is the strategy's marketable-limit cap; derive
        # it from a dry strategy run so the execution refresh matches.
        evaluation = policy._strategy.evaluate(  # noqa: SLF001 - test helper
            instrument_id, bars=series, as_of=as_of, session=session
        )
        entry_price = (
            evaluation.signal.planned_entry_price
            if evaluation.signal is not None
            else Decimal("100")
        )
        execution = execution_inputs(
            entry_price=entry_price,
            as_of=as_of,
            environment=environment,
            market_price=market_price,
            approved=approved,
            broker_healthy=broker_healthy,
            reconciliation_required=reconciliation_required,
        )
        return policy.evaluate(
            instrument_id,
            bars=series,
            candidate=candidate if candidate is not None else candidate_facts,
            account=account if account is not None else account_ok(as_of),
            held_positions=held_positions,  # type: ignore[arg-type]
            system_records=system_records,  # type: ignore[arg-type]
            execution=execution,
            as_of=as_of,
            active_halts=active_halts,  # type: ignore[arg-type]
            session=session,
        )

    return _run
