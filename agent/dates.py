"""Small date helpers shared by the tools.

UAE documents usually write dates day-first (15/01/2026), so numeric dates
are parsed as DD/MM/YYYY, never MM/DD/YYYY.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTHS["sept"] = 9

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DAY_FIRST_NUMERIC = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$")
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?,?\s+(\d{4})$")
_MONTH_DAY_YEAR = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")


def parse_date(value: str | date | datetime | None) -> date | None:
    """Turn common date spellings into a `date`. Returns None if it can't.

    Accepted: 2026-01-15, 15/01/2026, 15.01.2026, 15-01-2026,
    15 January 2026, 15 Jan 2026, January 15, 2026.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip().rstrip(".")
    if not text:
        return None

    try:
        if m := _ISO.match(text):
            y, mo, d = (int(g) for g in m.groups())
            return date(y, mo, d)
        if m := _DAY_FIRST_NUMERIC.match(text):
            d, mo, y = (int(g) for g in m.groups())
            return date(y, mo, d)
        if m := _DAY_MONTH_YEAR.match(text):
            d, month_name, y = m.groups()
            mo = _MONTHS.get(month_name.lower())
            return date(int(y), mo, int(d)) if mo else None
        if m := _MONTH_DAY_YEAR.match(text):
            month_name, d, y = m.groups()
            mo = _MONTHS.get(month_name.lower())
            return date(int(y), mo, int(d)) if mo else None
    except ValueError:
        # e.g. 31/02/2026 matches the pattern but is not a real date.
        return None
    return None


def subtract_months(day: date, months: int) -> date:
    """Go back a whole number of calendar months.

    If the target month is shorter, clamp to its last day:
    31 March minus 1 month is 28 (or 29) February, not an error.
    """
    month_index = day.year * 12 + (day.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def long_date(value: str | date | None) -> str:
    """'2027-01-14' -> '14 January 2027' (for letters). Unparseable input is returned as-is."""
    day = parse_date(value)
    if day is None:
        return str(value or "")
    return f"{day.day} {day.strftime('%B %Y')}"


def fmt(day: date | None) -> str | None:
    """Format a date the way we show it everywhere: 2026-01-15 (Thu 15 Jan 2026)."""
    if day is None:
        return None
    return f"{day.isoformat()} ({day.strftime('%a %d %b %Y')})"
