"""The case file: the facts the drafting team may use, built from tool results.

Every tool result of the session is kept in ToolContext.evidence. Before the
Critic reviews a letter we condense that evidence into one compact JSON
"case file": the contract terms, the calculator outputs, the red flags, the
official law articles the Law Researcher found, and what the tenant typed.

Why a case file instead of handing the Critic the whole conversation?
- Cost: a few thousand tokens instead of the full chat history.
- Focus: the Critic sees facts, not the Orchestrator's reasoning, so it
  judges the letter on the evidence alone (a fresh pair of eyes).
- Checkable: the automatic check (checks.py) reads the same file, so
  "is this number in the case file?" has a yes/no answer.

To keep it small, article text is included only for the articles the
letter relies on (the official articles its rules are linked to) and the
best match of each search. Every other article found still appears in
allowed_citations, so it may be cited.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agent.knowledge import get_rule
from agent.law_search import get_article
from agent.registry import ToolContext, rule_ids_in

_CALCULATORS = ("rera_rent_increase_check", "notice_deadline_check")
_DROP = ("caveats", "citations", "rule_ids", "note")     # repeated elsewhere in the file
_ARTICLE_CHARS = 600                                     # enough for the sentence that matters


def _latest(evidence: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    outputs = [ev["output"] for ev in evidence if ev["tool"] == tool]
    return outputs[-1] if outputs else None


def _red_flags(evidence: list[dict[str, Any]], terms_out: dict[str, Any]) -> list[dict[str, Any]]:
    flags = [{"flag_id": f["flag_id"], "clause_number": f["clause_number"], "title": f["title"],
              "excerpt": f.get("clause_excerpt")}
             for f in (_latest(evidence, "clause_risk_scan") or {}).get("flags", [])]
    flags += [{"flag_id": f["flag_id"], "clause_number": f["clause_number"], "quote": f["quote"]}
              for f in terms_out.get("llm_flagged_clauses", [])]
    return flags


def build_case_file(ctx: ToolContext, extra_rule_ids: list[str] = ()) -> dict[str, Any]:
    """Condense the session's evidence. `extra_rule_ids`: the rules the letter cites."""
    evidence = ctx.evidence
    terms_out = ctx.contract_terms or _latest(evidence, "extract_contract_terms") or {}
    letter_rules = [rule for r in dict.fromkeys(extra_rule_ids) if (rule := get_rule(r))]
    letter_articles = {a for rule in letter_rules for a in rule.get("law_article_ids", [])}

    # Law: full text for the letter's articles and each search's top hit; citations for all.
    law_text: dict[str, dict[str, str]] = {}
    found: list[str] = []

    def add_text(article: dict[str, Any]) -> None:
        law_text.setdefault(article["citation"], {
            "citation": article["citation"],
            "text": " ".join(article["text"].split())[:_ARTICLE_CHARS],
            "source_url": article["source_url"],
        })

    for article_id in sorted(letter_articles):      # curated links: grounded even if not searched
        if (article := get_article(article_id)) and article["in_force"]:
            found.append(article["citation"])
            add_text(article)
    for ev in evidence:
        if ev["tool"] != "search_tenancy_law":
            continue
        for rank, article in enumerate(ev["output"].get("articles", [])):
            found.append(article["citation"])
            if rank == 0:
                add_text(article)

    seen_rules = [rule for r in dict.fromkeys(r for ev in evidence for r in rule_ids_in(ev["output"]))
                  if (rule := get_rule(r))]
    form = {k: v for k, v in asdict(ctx.inputs).items() if v not in (None, "")}
    return {
        "today": ctx.today.isoformat(),
        "contract_terms": terms_out.get("terms", {}),
        "contract_derived": terms_out.get("derived", {}),
        "tenant_form": form,
        "calculations": [
            {"tool": ev["tool"], **{k: v for k, v in ev["output"].items() if k not in _DROP}}
            for ev in evidence if ev["tool"] in _CALCULATORS
        ],
        "red_flags": _red_flags(evidence, terms_out),
        "law_articles": list(law_text.values()),
        # The plain-English notes the letter's "rules relied on" section quotes.
        "letter_rules": [{"rule_id": r["id"], "citation": r["citation"], "summary": r["summary"]} for r in letter_rules],
        "allowed_citations": list(dict.fromkeys(
            [*found, *(r["citation"] for r in letter_rules), *(r["citation"] for r in seen_rules)])),
    }


def case_file_text(case: dict[str, Any]) -> str:
    """The case file as the exact text the Critic and the Drafter both read.

    Built once per letter and reused byte for byte, so the second specialist
    reads it from the prompt cache.
    """
    return json.dumps(case, ensure_ascii=False, indent=1, default=str)
