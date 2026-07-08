"""Deterministic trading calendar (IMP-0704).

Authority: System_Functional_Specification_v0.7 section 40.5 (the
authoritative calendar defines Open, Close, Early Close, Holiday and Halt;
policies are expressed in America/New_York and stored in UTC; hours are
NEVER computed with a fixed offset because of DST; a half day automatically
shortens the session) and Integration_Specification_v0.6 sections 7.2
(session statuses) and 20.3 (Trading Calendar — America/New_York).

Pure domain logic: no I/O, no network, no provider. Calendar *data*
(holidays, half days, session windows, halt windows) is input — nothing is
invented here; the known-fixture data lives with the tests/config. The
calendar is keyed by a canonical ``calendar_id`` through a fail-closed
registry, and every computation converts the queried instant into the
exchange timezone via ``zoneinfo`` (DST-correct by construction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trading_contracts.enums import MarketSessionStatus


class CalendarError(Exception):
    """Base class for fail-closed calendar failures."""


class UnknownCalendarError(CalendarError):
    """No calendar registered under the requested id: fail closed."""


def _require_aware(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("calendar queries require timezone-aware datetimes")
    return at


@dataclass(frozen=True, slots=True)
class SessionTimes:
    """Local-time session template (exchange timezone, e.g. America/New_York)."""

    pre_market_open: time
    regular_open: time
    regular_close: time
    after_hours_close: time
    early_close: time  # regular close on half days (shortens the session)


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """One market's calendar: weekly schedule + holidays + half days + halts."""

    calendar_id: str
    timezone: str
    sessions: SessionTimes
    holidays: frozenset[date]
    half_days: frozenset[date]
    weekend: frozenset[int] = frozenset({5, 6})  # Sat/Sun (date.weekday())
    halts: tuple[tuple[datetime, datetime], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for start, end in self.halts:
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("halt windows must be timezone-aware")
            if end <= start:
                raise ValueError("halt window must end after it starts")
        overlap = self.holidays & self.half_days
        if overlap:
            raise ValueError(f"dates cannot be both holiday and half day: {sorted(overlap)}")

    def _tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_trading_day(self, day: date) -> bool:
        """A holiday or weekend is never a trading day (never mistaken open)."""
        return day.weekday() not in self.weekend and day not in self.holidays

    def regular_close_time(self, day: date) -> time:
        """Half days close early — automatically shortening the session."""
        return self.sessions.early_close if day in self.half_days else self.sessions.regular_close

    def rth_window(self, day: date) -> tuple[datetime, datetime] | None:
        """The regular-trading-hours window for ``day`` in UTC (None if closed).

        Computed through the exchange timezone — never a fixed UTC offset —
        so DST transitions are handled by ``zoneinfo``.
        """
        if not self.is_trading_day(day):
            return None
        tz = self._tz()
        open_local = datetime.combine(day, self.sessions.regular_open, tzinfo=tz)
        close_local = datetime.combine(day, self.regular_close_time(day), tzinfo=tz)
        return open_local.astimezone(ZoneInfo("UTC")), close_local.astimezone(ZoneInfo("UTC"))

    def session_status(self, at: datetime) -> MarketSessionStatus:
        """Deterministic session status at ``at`` (any tz-aware instant)."""
        _require_aware(at)
        for start, end in self.halts:
            if start <= at < end:
                return MarketSessionStatus.HALTED
        local = at.astimezone(self._tz())
        day = local.date()
        if not self.is_trading_day(day):
            return MarketSessionStatus.CLOSED
        now = local.time()
        close = self.regular_close_time(day)
        if self.sessions.pre_market_open <= now < self.sessions.regular_open:
            return MarketSessionStatus.PRE_MARKET
        if self.sessions.regular_open <= now < close:
            return MarketSessionStatus.REGULAR
        # A half day truncates after-hours too: nothing reopens after the
        # early close (a shortened day must never be mistaken for open).
        if day not in self.half_days and close <= now < self.sessions.after_hours_close:
            return MarketSessionStatus.AFTER_HOURS
        return MarketSessionStatus.CLOSED

    def is_open_for_new_entries(self, at: datetime) -> bool:
        """RTH-only convenience used by future order-eligibility checks."""
        return self.session_status(at) is MarketSessionStatus.REGULAR


class CalendarRegistry:
    """Fail-closed lookup of calendars by canonical calendar_id."""

    def __init__(self, calendars: tuple[TradingCalendar, ...] = ()) -> None:
        self._by_id: dict[str, TradingCalendar] = {}
        for calendar in calendars:
            if calendar.calendar_id in self._by_id:
                raise ValueError(f"duplicate calendar id {calendar.calendar_id!r}")
            self._by_id[calendar.calendar_id] = calendar

    def get(self, calendar_id: str) -> TradingCalendar:
        calendar = self._by_id.get(calendar_id)
        if calendar is None:
            raise UnknownCalendarError(
                f"no calendar registered for {calendar_id!r} (fail closed; "
                "an unknown market is never assumed open)"
            )
        return calendar


__all__ = [
    "CalendarError",
    "CalendarRegistry",
    "SessionTimes",
    "TradingCalendar",
    "UnknownCalendarError",
]
