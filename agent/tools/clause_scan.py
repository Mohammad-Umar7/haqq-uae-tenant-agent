"""Tool: clause_risk_scan - find red-flag clauses with keyword/regex rules.

Each red flag is a small rule: a regex (or a function) that runs against one
clause at a time. Adding a new red flag = adding one RedFlag entry below
and a test for it. No LLM is involved, so results are repeatable.

A scan like this misses creative wording and sometimes flags harmless text.
In LLM mode the agent reads the clauses too and can catch what regex misses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.contract import Clause, split_clauses
from agent.knowledge import citation_for
from agent.tools.extract import word_or_digit_to_int

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# Period like "four (4) months'" or "30 days". Group "n" = word/number,
# group "paren" = digit in brackets (preferred when present), "unit" = days/months.
_PERIOD = r"(?P<n>\d{1,3}|[a-z]+)\s*(?:\((?P<paren>\d{1,3})\)\s*)?(?P<unit>days?|months?)"


@dataclass(frozen=True)
class RedFlag:
    id: str
    title: str
    severity: str  # high | medium | low | info
    why: str
    rule_ids: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    # Optional extra check for things a regex alone can't decide, e.g.
    # "penalty is MORE than 2 months". Returns a detail string to flag, or None.
    check: Callable[[str], str | None] | None = None

    def matches(self, clause_text: str) -> str | None:
        """Return a short description of what matched, or None if nothing did."""
        if self.check is not None:
            return self.check(clause_text)
        for pattern in self.patterns:
            m = re.search(pattern, clause_text, flags=re.IGNORECASE)
            if m:
                return f'matched "{m.group(0)[:90]}"'
        return None


def _period_value(match: re.Match[str]) -> tuple[int | None, str]:
    raw = match.group("paren") or match.group("n")
    unit = "months" if match.group("unit").lower().startswith("month") else "days"
    return word_or_digit_to_int(raw), unit


def _early_termination_check(text: str) -> str | None:
    """Flag early-termination penalties of more than 2 months' rent."""
    if not re.search(r"terminat", text, re.IGNORECASE):
        return None
    for m in re.finditer(_PERIOD + r"'?s?\s+(?:of\s+)?(?:the\s+)?(?:annual\s+)?rent", text, re.IGNORECASE):
        months, unit = _period_value(m)
        if unit == "months" and months is not None and months > 2:
            return f"penalty of {months} months' rent"
    return None


def _short_landlord_termination_check(text: str) -> str | None:
    """Flag clauses letting the landlord end the tenancy / evict on < 12 months' notice."""
    pattern = (
        r"landlord[^.]{0,80}(?:terminate|evict|end the (?:tenancy|contract)|require the tenant to vacate|vacate)"
        r"[^.]{0,80}?" + _PERIOD
    )
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return None
    n, unit = _period_value(m)
    if n is None:
        return None
    if unit == "days" or (unit == "months" and n < 12):
        return f"landlord can end the tenancy on {n} {unit}' notice"
    return None


RED_FLAGS: list[RedFlag] = [
    RedFlag(
        id="non_refundable_deposit",
        title="Security deposit described as non-refundable",
        severity="high",
        why="The security deposit is to be returned at the end of the tenancy. A clause letting the landlord keep it regardless of the property's condition conflicts with that.",
        rule_ids=["security_deposit_return"],
        patterns=[
            r"deposit[^.]{0,120}non[-\s]?refundable",
            r"non[-\s]?refundable[^.]{0,60}deposit",
            r"deposit[^.]{0,80}(?:forfeit|shall not be (?:returned|refunded)|retained by the landlord)",
        ],
    ),
    RedFlag(
        id="rent_increase_any_time",
        title="Landlord may raise the rent at any time",
        severity="high",
        why="Rent increases are capped by the RERA Rental Index rules and need written notice at least 90 days before the contract expires. A mid-term increase on short notice does not fit those rules.",
        rule_ids=["rent_increase_caps", "notice_change_terms_90_days"],
        patterns=[
            r"(?:increase|raise|revise|review|amend)[^.]{0,30}\brent\b[^.]{0,60}(?:at any time|any time during|during the (?:tenancy|term|contract)|without (?:prior )?notice)",
            r"\brent\b[^.]{0,40}(?:may|can|shall) be (?:increased|revised|raised)[^.]{0,60}(?:at any time|during the (?:tenancy|term|contract))",
        ],
    ),
    RedFlag(
        id="early_termination_penalty_high",
        title="Early-termination penalty above 2 months' rent",
        severity="medium",
        why="This tool treats a penalty above 2 months' rent as high and worth negotiating. Our knowledge base has no specific article capping early-termination penalties, so check with RERA/RDSC.",
        rule_ids=[],
        check=_early_termination_check,
    ),
    RedFlag(
        id="tenant_bears_all_maintenance",
        title="Tenant responsible for all maintenance, including structural",
        severity="medium",
        why="Putting all maintenance, including structural or major repairs, on the tenant is unusual and one-sided. Our knowledge base has no specific article on maintenance, so verify with RERA/RDSC.",
        rule_ids=[],
        patterns=[
            r"tenant[^.]{0,80}(?:responsib|liable|bear|cost|expense)[^.]{0,80}structural",
            r"structural[^.]{0,80}(?:responsibility|cost|expense|account) of the tenant",
            r"tenant[^.]{0,60}\b(?:all|any and all)\s+(?:maintenance|repairs)",
            r"\b(?:all|any and all)\s+(?:maintenance|repairs)[^.]{0,80}(?:borne by|responsibility of|at the cost of) the tenant",
        ],
    ),
    RedFlag(
        id="waiver_of_rdsc",
        title="Tenant waives the right to go to the RDSC",
        severity="high",
        why="Rental disputes in Dubai are handled by the Rental Disputes Settlement Centre. A clause asking you to give up that route is a serious red flag; ask the RDSC whether it can be enforced against you.",
        rule_ids=["rdsc_disputes"],
        patterns=[
            r"waive[sd]?[^.]{0,100}(?:right|rights)[^.]{0,100}(?:rental dispute|rdsc|dispute|court|complain|legal action)",
            r"(?:shall|may|can)\s*not\s+(?:file|raise|bring|lodge)[^.]{0,60}(?:rental dispute|rdsc|complaint|case|claim)",
        ],
    ),
    RedFlag(
        id="auto_renewal_fixed_increase",
        title="Automatic renewal with a fixed rent increase",
        severity="medium",
        why="A pre-agreed increase applied automatically may exceed what the RERA Rental Index rules allow at renewal, and skips the 90-day written notice step.",
        rule_ids=["rent_increase_caps", "notice_change_terms_90_days"],
        patterns=[
            r"(?:automatic(?:ally)?\s+renew|auto[-\s]?renew)[^.]{0,200}(?:increase|increased|raise)[^.]{0,40}\d+(?:\.\d+)?\s*(?:%|per\s?cent)",
            r"(?:increase|increased|raise)[^.]{0,40}\d+(?:\.\d+)?\s*(?:%|per\s?cent)[^.]{0,120}(?:automatic(?:ally)?\s+renew|auto[-\s]?renew)",
        ],
    ),
    RedFlag(
        id="short_landlord_termination",
        title="Landlord can end the tenancy on short notice",
        severity="high",
        why="For eviction at expiry because the owner wants the property for own use, sale, demolition or major renovation, the rule is 12 months' notice via notary public or registered mail. Other eviction grounds exist that our knowledge base does not cover, so verify with RDSC.",
        rule_ids=["eviction_12_months_notice"],
        check=_short_landlord_termination_check,
    ),
]


def _excerpt(text: str, limit: int = 260) -> str:
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def clause_risk_scan(contract_text: str) -> dict[str, Any]:
    """Run every red-flag rule against every numbered clause."""
    clauses = split_clauses(contract_text)
    if not clauses:
        # No numbered clauses found: treat each paragraph as a "clause".
        paragraphs = [p.strip() for p in contract_text.split("\n\n") if p.strip()]
        clauses = [Clause(number="-", text=" ".join(p.split()), section=None) for p in paragraphs]

    flags: list[dict[str, Any]] = []
    for clause in clauses:
        for red_flag in RED_FLAGS:
            detail = red_flag.matches(clause.text)
            if detail:
                flags.append(
                    {
                        "flag_id": red_flag.id,
                        "title": red_flag.title,
                        "severity": red_flag.severity,
                        "clause_number": clause.number,
                        "clause_excerpt": _excerpt(clause.text),
                        "detail": detail,
                        "why_it_matters": red_flag.why,
                        "rule_ids": red_flag.rule_ids,
                        "citations": [citation_for(r) for r in red_flag.rule_ids],
                    }
                )

    if not re.search(r"(?i)\bejari\b", contract_text):
        flags.append(
            {
                "flag_id": "ejari_not_mentioned",
                "title": "Contract does not mention Ejari registration",
                "severity": "info",
                "clause_number": None,
                "clause_excerpt": None,
                "detail": "no mention of Ejari",
                "why_it_matters": "Tenancy contracts in Dubai should be registered with Ejari. Ask the landlord for the Ejari certificate.",
                "rule_ids": ["ejari_registration"],
                "citations": [citation_for("ejari_registration")],
            }
        )

    flags.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    by_severity: dict[str, int] = {}
    for f in flags:
        by_severity[f["severity"]] = by_severity.get(f["severity"], 0) + 1

    return {
        "flag_count": len(flags),
        "by_severity": by_severity,
        "flags": flags,
        "clauses_scanned": len(clauses),
        "red_flag_rules_checked": [rf.id for rf in RED_FLAGS],
        "method": "keyword/regex scan (deterministic)",
        "note": "A pattern scan can miss risky wording or flag harmless text. Read each flagged clause yourself.",
    }
