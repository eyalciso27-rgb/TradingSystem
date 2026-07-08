"""Trading calendar: known fixtures, DST, half days, fail-closed (IMP-0704)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from trading_contracts.enums import MarketSessionStatus
from trading_domain.calendar import (
    CalendarRegistry,
    SessionTimes,
    TradingCalendar,
    UnknownCalendarError,
)

NY = ZoneInfo("America/New_York")

# Known-fixture calendar data (test/config input — the calendar code itself
# invents no dates): standard US equity session template plus a few known
# 2026 dates: New Year (Thu 2026-01-01), Independence Day observed
# (Fri 2026-07-03), Thanksgiving (Thu 2026-11-26) and the half day after it
# (Fri 2026-11-27, 13:00 early close).
US_SESSIONS = SessionTimes(
    pre_market_open=time(4, 0),
    regular_open=time(9, 30),
    regular_close=time(16, 0),
    after_hours_close=time(20, 0),
    early_close=time(13, 0),
)
US_CALENDAR = TradingCalendar(
    calendar_id="us_equities",
    timezone="America/New_York",
    sessions=US_SESSIONS,
    holidays=frozenset({date(2026, 1, 1), date(2026, 7, 3), date(2026, 11, 26)}),
    half_days=frozenset({date(2026, 11, 27)}),
)


def ny(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


def test_regular_trading_day_statuses() -> None:
    day = date(2026, 7, 6)  # ordinary Monday
    assert US_CALENDAR.is_trading_day(day)
    assert US_CALENDAR.session_status(ny(2026, 7, 6, 8, 0)) is MarketSessionStatus.PRE_MARKET
    assert US_CALENDAR.session_status(ny(2026, 7, 6, 10, 0)) is MarketSessionStatus.REGULAR
    assert US_CALENDAR.session_status(ny(2026, 7, 6, 17, 0)) is MarketSessionStatus.AFTER_HOURS
    assert US_CALENDAR.session_status(ny(2026, 7, 6, 22, 0)) is MarketSessionStatus.CLOSED
    assert US_CALENDAR.is_open_for_new_entries(ny(2026, 7, 6, 10, 0))
    assert not US_CALENDAR.is_open_for_new_entries(ny(2026, 7, 6, 8, 0))


def test_weekend_and_holiday_are_closed_never_open() -> None:
    assert not US_CALENDAR.is_trading_day(date(2026, 7, 4))  # Saturday
    assert US_CALENDAR.session_status(ny(2026, 7, 4, 10, 0)) is MarketSessionStatus.CLOSED
    # holiday during what would be RTH — must never be mistaken for open
    assert not US_CALENDAR.is_trading_day(date(2026, 7, 3))
    assert US_CALENDAR.session_status(ny(2026, 7, 3, 10, 0)) is MarketSessionStatus.CLOSED
    assert US_CALENDAR.rth_window(date(2026, 7, 3)) is None


def test_half_day_closes_early_and_does_not_reopen() -> None:
    half = date(2026, 11, 27)
    assert US_CALENDAR.is_trading_day(half)
    assert US_CALENDAR.regular_close_time(half) == time(13, 0)
    assert US_CALENDAR.session_status(ny(2026, 11, 27, 12, 0)) is MarketSessionStatus.REGULAR
    # 13:30 NY on a half day is CLOSED - not REGULAR and not AFTER_HOURS
    assert US_CALENDAR.session_status(ny(2026, 11, 27, 13, 30)) is MarketSessionStatus.CLOSED
    window = US_CALENDAR.rth_window(half)
    assert window is not None
    _open_utc, close_utc = window
    assert close_utc == ny(2026, 11, 27, 13, 0).astimezone(UTC)


def test_dst_no_fixed_offset() -> None:
    """RTH open in UTC differs between summer (EDT) and winter (EST)."""
    summer = US_CALENDAR.rth_window(date(2026, 7, 6))
    winter = US_CALENDAR.rth_window(date(2026, 1, 5))
    assert summer is not None and winter is not None
    assert summer[0].hour == 13 and summer[0].minute == 30  # 09:30 EDT = 13:30Z
    assert winter[0].hour == 14 and winter[0].minute == 30  # 09:30 EST = 14:30Z


def test_query_in_any_timezone_is_equivalent() -> None:
    ny_instant = ny(2026, 7, 6, 10, 0)
    utc_instant = ny_instant.astimezone(UTC)
    israel_instant = ny_instant.astimezone(ZoneInfo("Asia/Jerusalem"))
    statuses = {
        US_CALENDAR.session_status(ny_instant),
        US_CALENDAR.session_status(utc_instant),
        US_CALENDAR.session_status(israel_instant),
    }
    assert statuses == {MarketSessionStatus.REGULAR}


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        US_CALENDAR.session_status(datetime(2026, 7, 6, 10, 0))  # noqa: DTZ001


def test_halt_window_overrides_open() -> None:
    halted = TradingCalendar(
        calendar_id="us_equities_halted",
        timezone="America/New_York",
        sessions=US_SESSIONS,
        holidays=frozenset(),
        half_days=frozenset(),
        halts=((ny(2026, 7, 6, 10, 0), ny(2026, 7, 6, 10, 30)),),
    )
    assert halted.session_status(ny(2026, 7, 6, 10, 15)) is MarketSessionStatus.HALTED
    assert halted.session_status(ny(2026, 7, 6, 10, 45)) is MarketSessionStatus.REGULAR


def test_unknown_calendar_fails_closed() -> None:
    registry = CalendarRegistry((US_CALENDAR,))
    assert registry.get("us_equities") is US_CALENDAR
    with pytest.raises(UnknownCalendarError, match="fail closed"):
        registry.get("xetra")


def test_replay_determinism() -> None:
    instants = [ny(2026, 11, 27, h, m) for h in range(4, 21) for m in (0, 30)]
    first = [US_CALENDAR.session_status(i) for i in instants]
    second = [US_CALENDAR.session_status(i) for i in instants]
    assert first == second


def test_invalid_calendar_data_rejected() -> None:
    with pytest.raises(ValueError, match="both holiday and half day"):
        TradingCalendar(
            calendar_id="bad",
            timezone="America/New_York",
            sessions=US_SESSIONS,
            holidays=frozenset({date(2026, 11, 27)}),
            half_days=frozenset({date(2026, 11, 27)}),
        )
    with pytest.raises(ValueError, match="duplicate calendar id"):
        CalendarRegistry((US_CALENDAR, US_CALENDAR))


def test_calendar_creates_no_candidate_decision_or_order() -> None:
    import trading_domain.calendar as calendar_module

    source = open(calendar_module.__file__, encoding="utf-8").read()  # noqa: SIM115
    import_lines = [
        line for line in source.splitlines() if line.strip().startswith(("import", "from"))
    ]
    for line in import_lines:
        for forbidden in ("trading_persistence", "trading_application", "broker", "httpx"):
            assert forbidden not in line, line
