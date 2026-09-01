"""Owner-frozen safety gates the generic engines do not cover (B4/B5/B6/B7).

Pure, deterministic, fail-closed gates that enforce baseline-trend-breakout-v1
rules the shared RiskEngine/allocator do not express directly:

- **Daily loss vs Start-of-Day equity (B6).** The frozen 1.5% daily-loss limit is
  measured against START-OF-DAY equity (daily P&L includes unrealized), NOT the
  current risk-budget equity. Missing SOD equity blocks.
- **Max 3 open positions per GICS sector (B4)** and **max 0.75% planned risk per
  GICS sector (B5).** These use the AUTHORITATIVE GICS sector — never a proxy
  like the correlation cluster, and clusters within one sector aggregate
  together. Unknown sector blocks.
- **Next-RTH entry timing (B7).** A breakout confirmed at a completed RTH daily
  close may only be entered in the NEXT RTH session per the canonical trading
  calendar. Same session, extended hours, a later session, or an unknown
  session all block.

Each gate returns ``(ok, reason_codes)``; the orchestrator turns a not-ok gate
into NO_NEW_ENTRY. Nothing here sizes, authorizes or submits.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading_contracts.enums import MarketSessionStatus
from trading_domain.calendar import TradingCalendar

from trading_application.position_attribution import AttributedExposure

SAFETY_GATES_VERSION = "mvp-safety-gates-1.0.0"

#: Bounded horizon for the next-trading-day search (weekends + a holiday run).
_NEXT_SESSION_HORIZON_DAYS = 30


def daily_loss_within_limit(
    session_pnl: Decimal,
    start_of_day_equity: Decimal | None,
    limit_pct: Decimal,
) -> tuple[bool, tuple[str, ...]]:
    """True while the daily loss is within ``limit_pct`` of START-OF-DAY equity.

    Fail closed on missing/invalid SOD equity. The threshold is independent of
    current NLV, so a change in current equity never moves it.
    """
    if start_of_day_equity is None or start_of_day_equity <= 0:
        return False, ("START_OF_DAY_EQUITY_UNKNOWN",)
    loss = -session_pnl if session_pnl < 0 else Decimal(0)
    loss_fraction = loss / start_of_day_equity
    if loss_fraction >= limit_pct:
        return False, ("DAILY_LOSS_LIMIT_REACHED_SOD",)
    return True, ()


def sector_within_caps(
    candidate_sector: str | None,
    approved_risk_amount: Decimal,
    attributed_exposures: Sequence[AttributedExposure],
    *,
    max_positions_per_sector: int,
    max_sector_planned_risk_pct: Decimal,
    risk_budget_equity: Decimal,
) -> tuple[bool, tuple[str, ...]]:
    """Enforce the GICS-sector position-count and planned-risk caps.

    ``candidate_sector`` MUST be the authoritative GICS sector; a missing sector
    fails closed. Position count and planned risk aggregate every attributed
    position in the same sector (all clusters within it), and the candidate's
    approved risk may never push the sector total above the owner cap.
    """
    if candidate_sector is None or not candidate_sector.strip():
        return False, ("SECTOR_UNKNOWN",)

    same_sector = [e for e in attributed_exposures if e.sector == candidate_sector]
    reasons: list[str] = []
    if len(same_sector) >= max_positions_per_sector:
        reasons.append("SECTOR_MAX_POSITIONS_REACHED")

    existing_sector_risk = sum((e.open_risk_amount for e in same_sector), Decimal(0))
    sector_budget = max_sector_planned_risk_pct * risk_budget_equity
    if existing_sector_risk + approved_risk_amount > sector_budget:
        reasons.append("SECTOR_PLANNED_RISK_CAP_EXCEEDED")

    return (not reasons), tuple(reasons)


def _next_trading_day(calendar: TradingCalendar, after: date) -> date | None:
    probe = after + timedelta(days=1)
    for _ in range(_NEXT_SESSION_HORIZON_DAYS):
        if calendar.is_trading_day(probe):
            return probe
        probe += timedelta(days=1)
    return None


def entry_timing_eligible(
    breakout_end_at: datetime,
    as_of: datetime,
    calendar: TradingCalendar | None,
) -> tuple[bool, tuple[str, ...]]:
    """True when ``as_of`` is the NEXT RTH session after the breakout session.

    Fail closed when the calendar is unavailable, when ``as_of`` is not in RTH,
    when it is still the breakout session, or when it is any session other than
    the immediately following trading day.
    """
    if calendar is None:
        return False, ("ENTRY_CALENDAR_UNKNOWN",)
    if breakout_end_at.tzinfo is None or as_of.tzinfo is None:
        return False, ("ENTRY_TIMESTAMP_NAIVE",)

    if calendar.session_status(as_of) is not MarketSessionStatus.REGULAR:
        return False, ("ENTRY_NOT_RTH",)

    tz = ZoneInfo(calendar.timezone)
    breakout_date = breakout_end_at.astimezone(tz).date()
    as_of_date = as_of.astimezone(tz).date()

    if as_of_date == breakout_date:
        return False, ("ENTRY_SAME_BREAKOUT_SESSION",)
    next_session = _next_trading_day(calendar, breakout_date)
    if next_session is None:
        return False, ("ENTRY_NEXT_SESSION_UNKNOWN",)
    if as_of_date != next_session:
        return False, ("ENTRY_NOT_IN_NEXT_RTH_SESSION",)
    return True, ()


__all__ = [
    "SAFETY_GATES_VERSION",
    "daily_loss_within_limit",
    "entry_timing_eligible",
    "sector_within_caps",
]
