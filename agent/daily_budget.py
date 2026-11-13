"""A daily spending limit for the whole app (all visitors together).

The per-run and per-session limits stop one analysis or one browser tab from
running up a bill, but anyone can open a new tab. On a public deploy this
limit caps the total: once today's Claude spend reaches
HAQQ_MAX_DAILY_SPEND_USD, new requests are answered in free demo mode until
midnight Dubai time. The count lives in memory, so a restart resets it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from agent.config import Settings
from agent.cost import format_usd

DUBAI = timezone(timedelta(hours=4))   # Gulf Standard Time; the UAE has no daylight saving


def dubai_today() -> date:
    return datetime.now(DUBAI).date()


class DailyBudget:
    """Adds up what the app spent on Claude today and says when the limit is reached."""

    def __init__(self, limit_usd: float, clock: Callable[[], date] = dubai_today) -> None:
        self.limit_usd = limit_usd
        self._clock = clock
        self._lock = threading.Lock()
        self._day: date | None = None
        self._spent = 0.0

    def _roll_over(self) -> None:   # call with the lock held
        today = self._clock()
        if today != self._day:
            self._day, self._spent = today, 0.0

    def spent(self) -> float:
        with self._lock:
            self._roll_over()
            return self._spent

    def exhausted(self) -> bool:
        with self._lock:
            self._roll_over()
            return self._spent >= self.limit_usd

    def add(self, cost_usd: float) -> None:
        with self._lock:
            self._roll_over()
            self._spent += max(0.0, cost_usd)

    def notice(self) -> str:
        return (f"The public app spends at most {format_usd(self.limit_usd)} a day on Claude "
                "(HAQQ_MAX_DAILY_SPEND_USD), and today's amount is used up. This answer comes from the free "
                "demo mode (the code-only specialists); the AI team is back after midnight Dubai time.")


_shared: DailyBudget | None = None
_shared_lock = threading.Lock()


def shared_budget(settings: Settings) -> DailyBudget:
    """The one budget for this process, shared by the website and the classic UI."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = DailyBudget(settings.max_daily_spend_usd)
        return _shared
