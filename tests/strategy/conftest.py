"""Shared deterministic fixtures for the MVP strategy/trading-policy tests.

Everything here is a frozen, hand-constructed fixture (ADR-0014): a
rising-SMA150 uptrend, a genuinely contracting base and a completed breakout
bar that make ``baseline-trend-breakout-v1`` fire, laid out on REAL trading
days of a test calendar so the NEXT-RTH entry-timing gate is exercised, plus
the universe / account / execution inputs the orchestrator needs. Mutation
tests perturb ONE element to prove each mandatory predicate is load-bearing.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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
from trading_application.universe_eligibility import UniverseFacts
from trading_broker_core.port import SessionPnlStatus
from trading_contracts.enums import Environment, MarketSessionStatus, OrderType
from trading_domain.calendar import SessionTimes, TradingCalendar
from trading_market_data_core.types import Bar, BarSeries

EXCHANGE_TZ = ZoneInfo("America/New_York")
INSTRUMENT_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "mvp-baseline-test-instrument")
CALENDAR_ID = "XNYS-TEST"


def _weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:  # Mon-Fri (the test calendar has no holidays)
            days.append(day)
        day += timedelta(days=1)
    return days


#: Enough consecutive trading days for a 171-bar series plus the next session.
TRADING_DAYS = _weekdays(date(2024, 1, 1), 220)

CloseRange = tuple[float, float]


def make_test_calendar() -> TradingCalendar:
    return TradingCalendar(
        calendar_id=CALENDAR_ID,
        timezone="America/New_York",
        sessions=SessionTimes(
            pre_market_open=time(4, 0),
            regular_open=time(9, 30),
            regular_close=time(16, 0),
            after_hours_close=time(20, 0),
            early_close=time(13, 0),
        ),
        holidays=frozenset(),
        half_days=frozenset(),
    )


def _at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=EXCHANGE_TZ).astimezone(UTC)


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
def test_calendar() -> TradingCalendar:
    return make_test_calendar()


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
        day = TRADING_DAYS[index]
        close_at = _at(day, time(16, 0))  # completed RTH daily bar (at the close)
        return Bar(
            instrument_id=str(instrument),
            provider_id=provider,
            interval="1D",
            open=close_dec,
            high=close_dec + half,
            low=close_dec - half,
            close=close_dec,
            volume=Decimal(1_000_000),
            start_at=_at(day, time(9, 30)),
            end_at=close_at,
            observed_at=close_at,
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
                    index,
                    close,
                    day_range,
                    freshness=fresh,
                    provider=provider,
                    instrument=instrument,
                )
            )
        return BarSeries(
            instrument_id=str(instrument),
            provider_id=provider,
            interval="1D",
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
    """A minute after the last completed bar (for standalone strategy tests)."""

    def _as_of(series: BarSeries) -> datetime:
        return series.bars[-1].observed_at + timedelta(minutes=1)

    return _as_of


@pytest.fixture
def at_trading_day() -> Callable[[int, time], datetime]:
    """An exchange-local time on the Nth trading day (for entry-timing tests)."""

    def _at_day(day_index: int, at_time: time) -> datetime:
        return _at(TRADING_DAYS[day_index], at_time)

    return _at_day


@pytest.fixture
def next_rth_as_of() -> Callable[[BarSeries], datetime]:
    """10:00 ET on the NEXT trading day after the last bar (a valid RTH entry)."""

    def _as_of(series: BarSeries) -> datetime:
        next_day = TRADING_DAYS[len(series.bars)]
        return _at(next_day, time(10, 0))

    return _as_of


@pytest.fixture
def mvp_config() -> MvpPolicyConfig:
    return default_mvp_policy_config()


@pytest.fixture
def candidate_facts() -> MvpCandidateFacts:
    return MvpCandidateFacts(symbol="MVPX", cluster="SEMI", source_id="scanner")


@pytest.fixture
def universe_facts() -> Callable[..., UniverseFacts]:
    def _facts(
        *,
        observed_at: datetime,
        is_sp500_member: bool | None = True,
        membership_is_point_in_time: bool = True,
        gics_sector: str | None = "Information Technology",
        adv20_usd: Decimal | None = Decimal("100000000"),
        last_price: Decimal | None = Decimal("166"),
        spread_bps: Decimal | None = Decimal("5"),
    ) -> UniverseFacts:
        return UniverseFacts(
            symbol="MVPX",
            is_sp500_member=is_sp500_member,
            membership_is_point_in_time=membership_is_point_in_time,
            gics_sector=gics_sector,
            adv20_usd=adv20_usd,
            last_price=last_price,
            spread_bps=spread_bps,
            observed_at=observed_at,
        )

    return _facts


@pytest.fixture
def account_ok() -> Callable[..., MvpAccountSnapshot]:
    def _account(observed_at: datetime, **overrides: object) -> MvpAccountSnapshot:
        base: dict[str, object] = dict(
            broker_net_liquidation_value=Decimal(1_000_000),
            simulation_equity_cap=Decimal(1_000_000),
            session_pnl=Decimal("-100"),
            session_pnl_status=SessionPnlStatus.OK,
            start_of_day_equity=Decimal(1_000_000),
            observed_at=observed_at,
        )
        base.update(overrides)
        return MvpAccountSnapshot(**base)  # type: ignore[arg-type]

    return _account


@pytest.fixture
def execution_inputs() -> Callable[..., MvpExecutionInputs]:
    def _execution(
        *,
        entry_price: Decimal,
        as_of: datetime,
        environment: Environment = Environment.PAPER,
        market_price: Decimal | None = None,
        market_observed_at: datetime | None = None,
        approved: bool = True,
        broker_healthy: bool = True,
        reconciliation_required: bool = False,
        client_order_id: str = "mvp-entry-1",
    ) -> MvpExecutionInputs:
        return MvpExecutionInputs(
            environment=environment,
            account_alias="paper-mvp",
            side="BUY",
            order_type=OrderType.LIMIT,
            time_in_force="DAY",
            client_order_id=client_order_id,
            broker_instrument_ref="brkr:conid:987654",
            broker_exchange="SMART",
            broker_currency="USD",
            refreshed_market=RefreshedMarket(
                price=market_price if market_price is not None else entry_price,
                observed_at=market_observed_at if market_observed_at is not None else as_of,
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
    account_ok: Callable[..., MvpAccountSnapshot],
    execution_inputs: Callable[..., MvpExecutionInputs],
    universe_facts: Callable[..., UniverseFacts],
    next_rth_as_of: Callable[[BarSeries], datetime],
    test_calendar: TradingCalendar,
) -> Callable[..., MvpPolicyOutcome]:
    """Run the orchestrator on a series with sensible, overridable defaults."""

    def _run(
        series: BarSeries,
        *,
        account: MvpAccountSnapshot | None = None,
        candidate: MvpCandidateFacts | None = None,
        universe: UniverseFacts | None = None,
        held_positions: object = (),
        system_records: object = (),
        environment: Environment = Environment.PAPER,
        market_price: Decimal | None = None,
        market_observed_at: datetime | None = None,
        approved: bool = True,
        broker_healthy: bool = True,
        reconciliation_required: bool = False,
        client_order_id: str = "mvp-entry-1",
        read_only: bool = False,
        active_halts: object = (),
        calendar: TradingCalendar | None = None,
        as_of: datetime | None = None,
    ) -> MvpPolicyOutcome:
        policy = MvpTradingPolicy(mvp_config)
        cal = calendar if calendar is not None else test_calendar
        run_as_of = as_of if as_of is not None else next_rth_as_of(series)
        observed = series.bars[-1].observed_at
        # Dry strategy run (in RTH) to recover the marketable-limit cap so the
        # execution refresh matches the planned entry.
        evaluation = policy._strategy.evaluate(  # noqa: SLF001 - test helper
            instrument_id, bars=series, as_of=run_as_of, session=MarketSessionStatus.REGULAR
        )
        entry_price = (
            evaluation.signal.planned_entry_price
            if evaluation.signal is not None
            else Decimal("100")
        )
        execution = execution_inputs(
            entry_price=entry_price,
            as_of=run_as_of,
            environment=environment,
            market_price=market_price,
            market_observed_at=market_observed_at,
            approved=approved,
            broker_healthy=broker_healthy,
            reconciliation_required=reconciliation_required,
            client_order_id=client_order_id,
        )
        evaluate = policy.evaluate_read_only if read_only else policy.evaluate
        return evaluate(
            instrument_id,
            bars=series,
            candidate=candidate if candidate is not None else candidate_facts,
            universe=universe if universe is not None else universe_facts(observed_at=observed),
            account=account if account is not None else account_ok(run_as_of),
            held_positions=held_positions,  # type: ignore[arg-type]
            system_records=system_records,  # type: ignore[arg-type]
            execution=execution,
            as_of=run_as_of,
            calendar=cal,
            active_halts=active_halts,  # type: ignore[arg-type]
        )

    return _run
