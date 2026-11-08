"""Tool: extract_contract_terms - the contract's key terms as compact JSON.

Two ways to fill it:
- Demo mode (no key): regex, here. Predictable and free, but it misses
  unusual layouts, so the result lists `missing_fields`.
- LLM mode: Claude reads the contract ONCE at the start of the session and
  returns a schema-validated JSON (agent/extraction.py). Both paths go
  through summarize_terms(), so the agent sees the same shape either way.
"""

from __future__ import annotations

import re
from typing import Any

from agent.contract import Clause, split_clauses
from agent.dates import parse_date

# Each label pattern matches "Label: value" at the start of a line.
# (?im) = case-insensitive, ^ and $ match at every line.
_LANDLORD = re.compile(r"(?im)^\s*(?:landlord|lessor|owner)(?:'s)?(?:\s+name)?\s*:\s*(.+)$")
_TENANT = re.compile(r"(?im)^\s*(?:tenant|lessee)(?:'s)?(?:\s+name)?\s*:\s*(.+)$")
_PROPERTY = re.compile(r"(?im)^\s*(?:property(?:\s+address)?|premises|unit|leased\s+unit)\s*:\s*(.+)$")
_PROPERTY_TYPE = re.compile(r"(?im)^\s*(?:property|unit)\s+type\s*:\s*(.+)$")

_AMOUNT = r"(?:AED|Dhs?\.?|DH)\s*([\d,]+(?:\.\d+)?)"
_ANNUAL_RENT = re.compile(rf"(?i)(?:annual|yearly)\s+rent(?:al)?(?:\s+amount)?\s*[:\-]?\s*{_AMOUNT}")
_ANNUAL_RENT_AFTER = re.compile(r"(?i)(?:annual|yearly)\s+rent(?:al)?\s*[:\-]?\s*([\d,]+(?:\.\d+)?)\s*(?:AED|Dhs?)")
_DEPOSIT = re.compile(rf"(?i)security\s+deposit(?:\s+amount)?\s*[:\-]?\s*{_AMOUNT}")

_PERIOD = re.compile(r"(?im)^\s*(?:contract\s+period|tenancy\s+period|period|term)\s*:\s*from\s+(.+?)\s+to\s+(.+?)\s*$")
_START = re.compile(r"(?im)^\s*(?:start\s+date|commencement\s+date|contract\s+start)\s*:\s*(.+?)\s*$")
_END = re.compile(r"(?im)^\s*(?:end\s+date|expiry\s+date|expiration\s+date|contract\s+end)\s*:\s*(.+?)\s*$")

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_CHEQUES = re.compile(r"(?i)\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:\(\d{1,2}\)\s*)?(?:post[-\s]?dated\s+)?(?:cheques?|checks?)\b")

_SPECIAL_SECTION = re.compile(r"(?i)additional|special|addendum")


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    """First match whose value isn't a blank signature line like '_____'."""
    for match in pattern.finditer(text):
        value = match.group(1).strip()
        if value and not re.fullmatch(r"[_\-\s.]+(?:date:.*)?", value, flags=re.IGNORECASE):
            return value
    return None


def _money(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def word_or_digit_to_int(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def regex_terms(contract_text: str) -> dict[str, Any]:
    """The key terms, read with regex. Keys match the LLM extraction's terms."""
    text = contract_text

    # "Annual Rent: AED 85,000" or "Annual Rent: 85,000 AED"
    rent_match = _ANNUAL_RENT.search(text) or _ANNUAL_RENT_AFTER.search(text)
    annual_rent = _money(rent_match.group(1)) if rent_match else None

    deposit_match = _DEPOSIT.search(text)
    deposit = _money(deposit_match.group(1)) if deposit_match else None

    start = end = None
    if m := _PERIOD.search(text):
        start, end = parse_date(m.group(1)), parse_date(m.group(2))
    if start is None and (m := _START.search(text)):
        start = parse_date(m.group(1))
    if end is None and (m := _END.search(text)):
        end = parse_date(m.group(1))

    cheques = None
    if m := _CHEQUES.search(text):
        cheques = word_or_digit_to_int(m.group(1))

    return {
        "landlord": _first(_LANDLORD, text),
        "tenant": _first(_TENANT, text),
        "property": _first(_PROPERTY, text),
        "property_type": _first(_PROPERTY_TYPE, text),
        "annual_rent_aed": annual_rent,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "security_deposit_aed": deposit,
        "number_of_cheques": cheques,
        "mentions_ejari": bool(re.search(r"(?i)\bejari\b", text)),
    }


def summarize_terms(terms: dict[str, Any], clauses: list[Clause], method: str, note: str) -> dict[str, Any]:
    """Shared output of extract_contract_terms: terms + Python-derived numbers.

    The arithmetic (monthly rent, per-cheque amount, deposit %) is done here
    in Python whichever way the terms were read (regex or Claude).
    """
    annual_rent = terms.get("annual_rent_aed")
    cheques = terms.get("number_of_cheques")
    deposit = terms.get("security_deposit_aed")
    start, end = parse_date(terms.get("start_date")), parse_date(terms.get("end_date"))

    derived: dict[str, Any] = {}
    if annual_rent:
        derived["monthly_rent_aed"] = round(annual_rent / 12, 2)
        if cheques:
            derived["amount_per_cheque_aed"] = round(annual_rent / cheques, 2)
        if deposit is not None:
            derived["deposit_percent_of_annual_rent"] = round(deposit / annual_rent * 100, 2)
    if start and end:
        derived["contract_length_days"] = (end - start).days + 1  # both days inclusive

    special = [c for c in clauses if c.section and _SPECIAL_SECTION.search(c.section)]
    return {
        "terms": terms,
        "derived": derived,
        "missing_fields": [key for key, value in terms.items() if value is None],
        "special_clause_numbers": [c.number for c in special],
        "clauses": [
            {"number": c.number, "section": c.section, "text": c.text[:700]} for c in clauses
        ],
        "method": method,
        "note": note,
    }


REGEX_METHOD = "regex extraction (deterministic)"
REGEX_NOTE = (
    "Values were read with pattern matching. Check them against the contract; "
    "fill any missing_fields from the clause text or ask the tenant."
)


def extract_contract_terms(contract_text: str) -> dict[str, Any]:
    """Return key terms, derived numbers, and the numbered clauses (regex path)."""
    return summarize_terms(regex_terms(contract_text), split_clauses(contract_text), REGEX_METHOD, REGEX_NOTE)
