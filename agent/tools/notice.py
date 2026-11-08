"""Tool: notice_deadline_check - date math for the two notice rules.

* Changing terms (including rent) at renewal: written notice at least
  90 days before expiry - Law No. 26 of 2007, Art. 14.
* Eviction at expiry for the owner's own use, sale, demolition or major
  renovation: 12 months' notice through a notary public or registered
  mail - Art. 25(2).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from agent.dates import fmt, parse_date, subtract_months
from agent.knowledge import citation_for

CHANGE_TERMS_NOTICE_DAYS = 90
EVICTION_NOTICE_MONTHS = 12

# What the caller can ask about. "rent_increase" is just the most common
# kind of "change_terms", kept separate so the trace reads naturally.
NOTICE_TYPES = ("rent_increase", "change_terms", "eviction")
DELIVERY_METHODS = (
    "notary_public", "registered_mail", "email", "whatsapp_or_sms",
    "hand_delivered", "other", "unknown",
)
_VALID_EVICTION_METHODS = {"notary_public", "registered_mail"}


def _to_date(value: str | date | None, field: str) -> date | None:
    if value is None or value == "":
        return None
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{field} '{value}' is not a date I can read. Use YYYY-MM-DD.")
    return parsed


def notice_deadline_check(
    contract_end_date: str | date,
    notice_type: str,
    notice_date: str | date | None = None,
    delivery_method: str | None = None,
    today: str | date | None = None,
) -> dict[str, Any]:
    """Compute the last valid notice date and judge a notice if one was given."""
    if notice_type not in NOTICE_TYPES:
        raise ValueError(f"notice_type must be one of {', '.join(NOTICE_TYPES)}.")
    if delivery_method is not None and delivery_method not in DELIVERY_METHODS:
        raise ValueError(f"delivery_method must be one of {', '.join(DELIVERY_METHODS)}.")

    end = _to_date(contract_end_date, "contract_end_date")
    if end is None:
        raise ValueError("contract_end_date is required.")
    notice = _to_date(notice_date, "notice_date")
    today_d = _to_date(today, "today") or date.today()

    if notice_type == "eviction":
        deadline = subtract_months(end, EVICTION_NOTICE_MONTHS)
        rule_id = "eviction_12_months_notice"
        requirement = (
            "12 months' notice before the eviction date, delivered through a notary public "
            "or by registered mail"
        )
    else:
        deadline = end - timedelta(days=CHANGE_TERMS_NOTICE_DAYS)
        rule_id = "notice_change_terms_90_days"
        requirement = "written notice at least 90 days before the contract expires"

    result: dict[str, Any] = {
        "notice_type": notice_type,
        "contract_end_date": fmt(end),
        "requirement": requirement,
        "last_valid_notice_date": fmt(deadline),
        "today": fmt(today_d),
        "days_until_contract_end": (end - today_d).days,
    }

    if notice is not None:
        days_before_expiry = (end - notice).days
        on_time = notice <= deadline
        result.update(
            notice_date=fmt(notice),
            notice_days_before_expiry=days_before_expiry,
            notice_on_time=on_time,
        )
        if notice > end:
            verdict = "The notice is dated after the contract end date; check the dates."
        elif on_time:
            verdict = (
                f"The notice was given {days_before_expiry} days before expiry, on or before the "
                f"last valid date ({deadline.isoformat()}), so it meets the timing rule."
            )
        else:
            late_by = (notice - deadline).days
            verdict = (
                f"The notice is LATE by {late_by} days: it came {days_before_expiry} days before expiry, "
                f"but the last valid date was {deadline.isoformat()}."
            )
            if notice_type != "eviction":
                verdict += " A change notified this late generally cannot be imposed at this renewal."
    else:
        days_left = (deadline - today_d).days
        result["days_until_notice_deadline"] = days_left
        if days_left >= 0:
            verdict = (
                f"No notice yet. The landlord has until {deadline.isoformat()} ({days_left} days from today) "
                f"to give valid notice for this renewal."
            )
        else:
            verdict = (
                f"No notice yet, and the deadline passed on {deadline.isoformat()}. A notice sent now "
                f"would be late for a {end.isoformat()} expiry."
            )

    if notice_type == "eviction":
        if delivery_method in (None, "unknown"):
            result["delivery_method_ok"] = None
            result["delivery_note"] = "Check how the notice was sent: it must go through a notary public or registered mail."
        else:
            ok = delivery_method in _VALID_EVICTION_METHODS
            result["delivery_method_ok"] = ok
            result["delivery_note"] = (
                "Delivery method meets Art. 25(2)." if ok else
                f"Delivery by '{delivery_method}' does not meet Art. 25(2), which requires a notary public or registered mail."
            )
            if not ok:
                verdict += " The delivery method also does not meet the rule."

    result["verdict"] = verdict
    result["rule_ids"] = [rule_id]
    result["citations"] = [citation_for(rule_id)]
    return result
