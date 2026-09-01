"""Unit tests for the MVP safety gates (B4/B5/B6/B7)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_application.mvp_safety_gates import (
    daily_loss_within_limit,
    entry_timing_eligible,
    sector_within_caps,
)
from trading_application.position_attribution import AttributedExposure
from trading_domain.calendar import SessionTimes, TradingCalendar

ET = ZoneInfo("America/New_York")


def _calendar(holidays: frozenset[date] = frozenset()) -> TradingCalendar:
    return TradingCalendar(
        calendar_id="XTEST",
        timezone="America/New_York",
        sessions=SessionTimes(time(4, 0), time(9, 30), time(16, 0), time(20, 0), time(13, 0)),
        holidays=holidays,
        half_days=frozenset(),
    )


def _at(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=ET).astimezone(UTC)


def _exposure(sector: str, open_risk: str) -> AttributedExposure:
    return AttributedExposure(
        con_id=1, symbol="X", sector=sector, cluster="C", source_id="s",
        strategy_id="baseline-trend-breakout-v1",
        open_risk_amount=Decimal(open_risk), notional=Decimal("1000"),
    )


# -- B6: daily loss vs Start-of-Day equity ------------------------------------
def test_daily_loss_missing_sod_blocks() -> None:
    ok, reasons = daily_loss_within_limit(Decimal("-100"), None, Decimal("0.015"))
    assert not ok
    assert "START_OF_DAY_EQUITY_UNKNOWN" in reasons


def test_daily_loss_within_limit_passes() -> None:
    ok, reasons = daily_loss_within_limit(Decimal("-10000"), Decimal("1000000"), Decimal("0.015"))
    assert ok and reasons == ()


def test_daily_loss_at_limit_blocks() -> None:
    ok, reasons = daily_loss_within_limit(Decimal("-15000"), Decimal("1000000"), Decimal("0.015"))
    assert not ok
    assert "DAILY_LOSS_LIMIT_REACHED_SOD" in reasons


def test_daily_gain_never_blocks() -> None:
    ok, _ = daily_loss_within_limit(Decimal("500000"), Decimal("1000000"), Decimal("0.015"))
    assert ok


# -- B4/B5: GICS-sector caps ---------------------------------------------------
def test_sector_unknown_blocks() -> None:
    ok, reasons = sector_within_caps(
        None, Decimal("100"), (),
        max_positions_per_sector=3, max_sector_planned_risk_pct=Decimal("0.0075"),
        risk_budget_equity=Decimal("1000000"),
    )
    assert not ok and "SECTOR_UNKNOWN" in reasons


def test_sector_count_and_risk_pass_below_caps() -> None:
    exposures = [_exposure("TECH", "1000"), _exposure("FIN", "9999")]
    ok, reasons = sector_within_caps(
        "TECH", Decimal("100"), exposures,
        max_positions_per_sector=3, max_sector_planned_risk_pct=Decimal("0.0075"),
        risk_budget_equity=Decimal("1000000"),
    )
    assert ok and reasons == ()


def test_sector_max_positions_blocks() -> None:
    exposures = [_exposure("TECH", "10") for _ in range(3)]
    ok, reasons = sector_within_caps(
        "TECH", Decimal("100"), exposures,
        max_positions_per_sector=3, max_sector_planned_risk_pct=Decimal("0.0075"),
        risk_budget_equity=Decimal("1000000"),
    )
    assert not ok and "SECTOR_MAX_POSITIONS_REACHED" in reasons


def test_sector_planned_risk_cap_aggregates_clusters() -> None:
    # Two clusters in one sector aggregate: 3000 + 3000 + 2000 = 8000 > 7500.
    exposures = [_exposure("TECH", "3000"), _exposure("TECH", "3000")]
    ok, reasons = sector_within_caps(
        "TECH", Decimal("2000"), exposures,
        max_positions_per_sector=3, max_sector_planned_risk_pct=Decimal("0.0075"),
        risk_budget_equity=Decimal("1000000"),
    )
    assert not ok and "SECTOR_PLANNED_RISK_CAP_EXCEEDED" in reasons


def test_other_sector_positions_do_not_count() -> None:
    exposures = [_exposure("FIN", "3000") for _ in range(5)]
    ok, reasons = sector_within_caps(
        "TECH", Decimal("2000"), exposures,
        max_positions_per_sector=3, max_sector_planned_risk_pct=Decimal("0.0075"),
        risk_budget_equity=Decimal("1000000"),
    )
    assert ok and reasons == ()


# -- B7: next-RTH entry timing -------------------------------------------------
def test_entry_calendar_unknown_blocks() -> None:
    ok, reasons = entry_timing_eligible(
        _at(date(2025, 6, 2), time(16, 0)), _at(date(2025, 6, 3), time(10, 0)), None
    )
    assert not ok and "ENTRY_CALENDAR_UNKNOWN" in reasons


def test_next_rth_session_is_eligible() -> None:
    cal = _calendar()
    # Mon breakout close -> Tue RTH entry.
    breakout = _at(date(2025, 6, 2), time(16, 0))  # Monday
    entry = _at(date(2025, 6, 3), time(10, 0))  # Tuesday RTH
    ok, reasons = entry_timing_eligible(breakout, entry, cal)
    assert ok and reasons == ()


def test_same_session_blocked() -> None:
    cal = _calendar()
    breakout = _at(date(2025, 6, 2), time(16, 0))
    same = _at(date(2025, 6, 2), time(15, 0))
    ok, reasons = entry_timing_eligible(breakout, same, cal)
    assert not ok and "ENTRY_SAME_BREAKOUT_SESSION" in reasons


def test_not_rth_blocked() -> None:
    cal = _calendar()
    breakout = _at(date(2025, 6, 2), time(16, 0))
    after_hours = _at(date(2025, 6, 3), time(17, 0))  # after close
    ok, reasons = entry_timing_eligible(breakout, after_hours, cal)
    assert not ok and "ENTRY_NOT_RTH" in reasons


def test_weekend_between_picks_next_actual_rth() -> None:
    cal = _calendar()
    # Friday breakout -> the NEXT trading day is Monday (weekend skipped).
    friday = date(2025, 6, 6)
    assert friday.weekday() == 4
    monday = date(2025, 6, 9)
    breakout = _at(friday, time(16, 0))
    monday_rth = _at(monday, time(10, 0))
    ok, reasons = entry_timing_eligible(breakout, monday_rth, cal)
    assert ok and reasons == ()
    # Saturday is not a valid entry session at all.
    saturday_rth = _at(date(2025, 6, 7), time(10, 0))
    ok2, reasons2 = entry_timing_eligible(breakout, saturday_rth, cal)
    assert not ok2


def test_holiday_between_picks_next_actual_rth() -> None:
    # Friday breakout, Monday is a holiday -> next RTH is Tuesday.
    monday = date(2025, 6, 9)
    cal = _calendar(holidays=frozenset({monday}))
    friday = date(2025, 6, 6)
    tuesday = date(2025, 6, 10)
    breakout = _at(friday, time(16, 0))
    ok, _ = entry_timing_eligible(breakout, _at(tuesday, time(10, 0)), cal)
    assert ok
    # Monday (holiday) is CLOSED -> not RTH.
    ok2, reasons2 = entry_timing_eligible(breakout, _at(monday, time(10, 0)), cal)
    assert not ok2 and "ENTRY_NOT_RTH" in reasons2


def test_two_sessions_later_blocked() -> None:
    cal = _calendar()
    breakout = _at(date(2025, 6, 2), time(16, 0))  # Monday
    wednesday = _at(date(2025, 6, 4), time(10, 0))  # two sessions later
    ok, reasons = entry_timing_eligible(breakout, wednesday, cal)
    assert not ok and "ENTRY_NOT_IN_NEXT_RTH_SESSION" in reasons
