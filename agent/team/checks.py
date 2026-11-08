"""Deterministic checks the team runs on its own work (plain code, free).

1. check_letter(): every AED amount, percentage, date, law and article in a
   letter must appear in the case file (the session's tool results). A
   letter with a number no tool produced is the classic LLM slip, and the
   landlord's lawyer would pounce on it. This is the Critic's automatic
   half; the Claude half judges tone, weak arguments and missing points.
2. audit_citations(): every law citation in the Orchestrator's final answer
   must have been returned by a tool this session (the Law Researcher's
   last job). A citation from memory is exactly what RAG is meant to stop.

Both are simple regex checks. They can't understand an argument, but they
never get tired, never miss a digit, and cost nothing. check_letter() reads
Arabic letters too: Arabic-Indic digits are normalised first, and there are
patterns for درهم amounts, Arabic month names, "المادة (14)" and
"رقم (26) لسنة 2007".
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from agent.dates import parse_date
from agent.team.arabic import ARABIC_MONTHS, normalize

# The only laws in the corpus. A letter citing any other law number cites
# something the Law Researcher could not have found.
KNOWN_LAWS = {("26", "2007"), ("33", "2008"), ("43", "2013")}

OBJECTION_KINDS = (
    "wrong_number", "wrong_date", "weak_citation", "unsupported_claim", "missing_point", "tone", "other",
)
SEVERITIES = ("high", "medium", "low")


@dataclass(frozen=True)
class Objection:
    """One problem with a draft letter, from the Critic or the automatic check."""

    kind: str          # one of OBJECTION_KINDS
    severity: str      # high | medium | low
    quote: str         # the words in the letter
    problem: str
    fix: str
    source: str = "Critic"   # "Critic" (Claude) or "automatic check" (code)

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


# ---- patterns ---------------------------------------------------------------

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
          r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)")
_AMOUNTS = [
    re.compile(rf"(?:AED|Dhs?\.?|dirhams?)\s*({_NUM})", re.IGNORECASE),
    re.compile(rf"({_NUM})\s*(?:AED|dirhams?)\b", re.IGNORECASE),
    re.compile(rf"(?:درهم|دراهم|د\.إ\.?)\s*({_NUM})"),
    re.compile(rf"({_NUM})\s*(?:درهماً|درهما|درهم|دراهم|د\.إ)"),
]
_PERCENT = re.compile(rf"({_NUM})\s*(?:%|per\s?cent\b|بالمئة|بالمائة|في المئة|في المائة)", re.IGNORECASE)
_ARABIC_DATE = re.compile(rf"(\d{{1,2}})\s+({'|'.join(ARABIC_MONTHS)})\s+(\d{{4}})")
_DATES = [
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}\.?,?\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(rf"\b{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"),
]
_LAW_REF = re.compile(r"\b(?:Law|Decree)\s+No\.?\s*\(?(\d+)\)?\s+of\s+(\d{4})", re.IGNORECASE)
_ARTICLE = re.compile(r"\bArt(?:icle)?s?\.?\s*\(?(\d+)\)?", re.IGNORECASE)
_LAW_REF_AR = re.compile(r"رقم\s*\(?\s*(\d+)\s*\)?\s*لسنة\s*(\d{4})")
_ARTICLE_AR = re.compile(r"المادة\s*\(?\s*(\d+)")


def _to_float(text: str) -> float:
    return float(text.replace(",", ""))


def _amounts(text: str) -> list[tuple[str, float]]:
    found: dict[int, tuple[str, float]] = {}
    for pattern in _AMOUNTS:
        for m in pattern.finditer(text):
            found.setdefault(m.start(1), (m.group(0).strip(), _to_float(m.group(1))))
    return [found[k] for k in sorted(found)]


def _dates(text: str) -> list[tuple[str, date]]:
    found: dict[int, tuple[str, date]] = {}
    for pattern in _DATES:
        for m in pattern.finditer(text):
            day = parse_date(m.group(0).replace(",", ", ").replace("  ", " "))
            if day is not None:
                found.setdefault(m.start(), (m.group(0), day))
    for m in _ARABIC_DATE.finditer(text):
        try:
            found.setdefault(m.start(), (m.group(0), date(int(m.group(3)), ARABIC_MONTHS[m.group(2)], int(m.group(1)))))
        except ValueError:
            continue
    return [found[k] for k in sorted(found)]


# ---- what the case file knows ---------------------------------------------------

@dataclass
class KnownFacts:
    numbers: set[float]
    dates: set[date]
    articles: set[str]

    def has_number(self, value: float, percent: bool = False) -> bool:
        tolerance = 0.05 if percent else 1.0     # rounding: 10.38% -> 10.4%, AED 89,250.4 -> 89,250
        return any(abs(value - known) <= tolerance for known in self.numbers)


def known_facts(case_text: str, allowed_citations: list[str]) -> KnownFacts:
    """Everything a letter may state: the numbers and dates in the case file,
    and the article numbers of the citations the team may use."""
    case_text = normalize(case_text)
    numbers = {_to_float(m.group(0)) for m in re.finditer(_NUM, case_text)}
    dates = {d for _, d in _dates(case_text)}
    articles = {m.group(1) for c in allowed_citations for m in _ARTICLE.finditer(c)}
    return KnownFacts(numbers, dates, articles)


# ---- 1. the letter check --------------------------------------------------------

def check_letter(letter: str, facts: KnownFacts) -> list[Objection]:
    """Objections for every amount, percentage, date, law or article in the
    letter that the case file doesn't contain. Works for English and Arabic letters."""
    letter = normalize(letter)
    problems: list[Objection] = []
    for quote, value in _amounts(letter):
        if not facts.has_number(value):
            problems.append(Objection(
                "wrong_number", "high", quote,
                f"{quote} does not match any amount in the case file (contract terms and calculator results).",
                "Use the exact amount from the calculator or the contract.", "automatic check"))
    for m in _PERCENT.finditer(letter):
        if not facts.has_number(_to_float(m.group(1)), percent=True):
            problems.append(Objection(
                "wrong_number", "high", m.group(0),
                f"{m.group(0)} does not match any percentage in the case file.",
                "Use the percentage the rent calculator returned.", "automatic check"))
    for quote, day in _dates(letter):
        if day not in facts.dates:
            problems.append(Objection(
                "wrong_date", "high", quote,
                f"{quote} is not a date in the case file (contract dates, notice date, deadlines, today).",
                "Use the date from the contract or the notice check.", "automatic check"))
    for m in [*_LAW_REF.finditer(letter), *_LAW_REF_AR.finditer(letter)]:
        if (m.group(1), m.group(2)) not in KNOWN_LAWS:
            problems.append(Objection(
                "weak_citation", "high", m.group(0),
                f"{m.group(0)} is not one of the laws in Haqq's corpus, so nobody on the team found it.",
                "Cite only the laws in the allowed citations.", "automatic check"))
    articles = [m.group(1) for pattern in (_ARTICLE, _ARTICLE_AR) for m in pattern.finditer(letter)]
    for article in dict.fromkeys(articles):
        if article not in facts.articles:
            problems.append(Objection(
                "weak_citation", "high", f"Art. {article}",
                f"Article {article} is not among the articles the Law Researcher found for this case.",
                "Cite only the articles in the allowed citations.", "automatic check"))
    return problems


# ---- 2. the answer's citations ---------------------------------------------------

_BRACKET = re.compile(r"\[([^\[\]]{5,300})\]")


def _citation_key(text: str) -> tuple[str, str | None] | None:
    law = _LAW_REF.search(text)
    if not law:
        return None
    article = _ARTICLE.search(text[law.end():]) or _ARTICLE.search(text)
    return f"{law.group(1)}/{law.group(2)}", article.group(1) if article else None


def citations_in(node: Any) -> list[str]:
    """Every citation string anywhere in a tool output."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("citation", "citations", "cited_rules"):
                found += [value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)]
            else:
                found += citations_in(value)
    elif isinstance(node, list):
        for item in node:
            found += citations_in(item)
    return found


def _rule_article_citations(output: Any) -> list[str]:
    """A rule id a tool returned also vouches for the official articles it is based on."""
    from agent.knowledge import get_rule
    from agent.law_search import get_article
    from agent.registry import rule_ids_in

    citations = []
    for rule_id in rule_ids_in(output):
        for article_id in (get_rule(rule_id) or {}).get("law_article_ids", []):
            article = get_article(article_id)
            if article and article["in_force"]:
                citations.append(article["citation"])
    return citations


def audit_citations(answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Which law citations in the answer were (or weren't) returned by a tool."""
    returned = [c for ev in evidence for c in citations_in(ev["output"]) + _rule_article_citations(ev["output"])]
    known = {key for c in returned if (key := _citation_key(c))}
    known_laws = {law for law, _ in known}
    grounded, not_found = [], []
    for bracket in _BRACKET.findall(answer):
        for part in bracket.split(";"):
            part = " ".join(part.split())
            key = _citation_key(part)
            if key is None:
                continue          # [Your name], [Landlord's name] and other placeholders
            law, article = key
            ok = key in known or (article is None and law in known_laws)
            (grounded if ok else not_found).append(part)
    return {
        "checked": len(grounded) + len(not_found),
        "grounded": list(dict.fromkeys(grounded)),
        "not_found": list(dict.fromkeys(not_found)),
    }
