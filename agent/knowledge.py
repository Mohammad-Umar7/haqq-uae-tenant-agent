"""Loads the curated rule notes in knowledge_base/tenant_rules.json.

Two layers of legal knowledge:
- law_articles.json: the official law text, searched by agent/law_search.py.
- tenant_rules.json (this module): short plain-English notes with stable
  rule ids. The calculators and letter templates cite rules by id, and each
  rule links to the official articles it explains (law_article_ids).
Tools copy citation text from here, so the agent can't cite a rule that
isn't in the file.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

KB_PATH = Path(__file__).parent / "knowledge_base" / "tenant_rules.json"

DISCLAIMER = (
    "Not legal advice. This tool quotes the official English translation of Dubai's tenancy law "
    "(the Arabic text prevails) and can be wrong or out of date. Verify with RERA or the Rental "
    "Disputes Settlement Centre (RDSC) before you act."
)


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with KB_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def all_rules() -> list[dict[str, Any]]:
    return list(_load()["rules"])


def rule_ids() -> list[str]:
    return [rule["id"] for rule in all_rules()]


def get_rule(rule_id: str) -> dict[str, Any] | None:
    for rule in all_rules():
        if rule["id"] == rule_id:
            return rule
    return None


def citation_for(rule_id: str) -> str | None:
    rule = get_rule(rule_id)
    return rule["citation"] if rule else None


def search_rules(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """Very small keyword search: score each rule by how many of its keywords
    (and title words) appear in the query. Good enough for 7 rules; swap in
    embeddings if the knowledge base grows to hundreds of entries.
    """
    q = query.lower().strip()
    if not q:
        return []

    # An exact rule id always wins (the LLM often passes ids it saw earlier).
    exact = get_rule(q.replace(" ", "_"))
    if exact:
        return [exact]

    query_words = set(re.findall(r"[a-z0-9]+", q))
    scored: list[tuple[int, dict[str, Any]]] = []
    for rule in all_rules():
        score = 0
        for keyword in rule["keywords"]:
            kw = keyword.lower()
            if " " in kw:
                # Multi-word keyword: must appear as a phrase.
                score += 3 if kw in q else 0
            elif kw in query_words or (len(kw) > 4 and any(w.startswith(kw) for w in query_words)):
                score += 2
        title_words = set(re.findall(r"[a-z0-9]+", rule["title"].lower())) - _STOPWORDS
        score += len(title_words & query_words)
        if score > 0:
            scored.append((score, rule))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [rule for _, rule in scored[:limit]]


_STOPWORDS = {"the", "a", "an", "to", "of", "for", "and", "or", "at", "is", "in", "with", "go", "goes"}
