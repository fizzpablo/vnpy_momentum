"""Offline exchange-calendar gate.  Any failure is represented as unknown."""

from __future__ import annotations

from datetime import datetime


def is_open(market: str, now: datetime) -> bool | None:
    """Return regular-session status, or None when calendar data is unknown."""
    try:
        import pandas_market_calendars as mcal
        calendar = mcal.get_calendar("NYSE" if market == "US" else "HKEX")
        schedule = calendar.schedule(start_date=now.date(), end_date=now.date())
        if schedule.empty:
            return False
        row = schedule.iloc[0]
        utc_now = now.astimezone(row["market_open"].tzinfo)
        if not row["market_open"] <= utc_now < row["market_close"]:
            return False
        if "break_start" in row and row["break_start"] <= utc_now < row["break_end"]:
            return False
        return True
    except Exception:
        return None
