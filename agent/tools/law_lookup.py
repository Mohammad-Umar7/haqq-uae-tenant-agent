"""Tool: search_tenancy_law - agentic RAG over the official law text.

The model calls this with a question in plain words (or with a rule_id that
another tool returned). It gets back the official article text, a citation
and the source URL, plus our curated plain-English note when one exists.

This replaces the old lookup_tenant_rule tool: the curated notes still come
back, but now they are attached to the real article they explain, so every
claim can be checked against the official wording.
"""

from __future__ import annotations

from typing import Any

from agent.knowledge import all_rules, get_rule
from agent.law_search import get_article, search_law

NOT_FOUND_MESSAGE = (
    "I couldn't find this in the law. None of the articles in force in Law No. 26 of 2007 (as amended) "
    "or Decree No. 43 of 2013 matched. Do not answer from memory: tell the tenant you couldn't find it "
    "in the law and suggest asking RERA or the Rental Disputes Settlement Centre (RDSC)."
)

ANSWER_NOTE = (
    "Rely only on the article text above and cite the 'citation' field. The English text is an official "
    "translation; the Arabic original prevails. Articles replaced in 2008 are not searchable."
)


def _rules_for_article(article_id: str) -> list[dict[str, Any]]:
    return [r for r in all_rules() if article_id in r.get("law_article_ids", [])]


def _article_view(article: dict[str, Any], score: float | None = None) -> dict[str, Any]:
    view = {
        "article_id": article["id"],
        "citation": article["citation"],
        "title": article.get("title") or article.get("chapter"),
        "text": article["text"],
        "source_url": article["source_url"],
        "related_rule_ids": [r["id"] for r in _rules_for_article(article["id"])],
    }
    if score is not None:
        view["score"] = score
    return view


def _note(rule: dict[str, Any]) -> dict[str, str]:
    return {
        "rule_id": rule["id"],
        "title": rule["title"],
        "plain_english": rule["plain_english"],
        "tenant_tip": rule["tenant_tip"],
    }


def search_tenancy_law(query: str, top_k: int = 3) -> dict[str, Any]:
    """Search the official law text. Returns articles, notes and citations."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query must be a few words about the topic, e.g. 'security deposit refund'.")

    # A rule id from another tool (e.g. "security_deposit_return") goes
    # straight to the articles that rule is based on.
    rule = get_rule(query.replace(" ", "_").lower())
    if rule is not None:
        articles = [get_article(a) for a in rule.get("law_article_ids", [])]
        views = [_article_view(a) for a in articles if a and a["in_force"]]
        return {
            "query": query,
            "found": True,
            "articles": views,
            "plain_english": [_note(rule)],
            "rule_ids": [rule["id"]],
            "citations": [v["citation"] for v in views] or [rule["citation"]],
            "note": ANSWER_NOTE if views else (
                "This rule is not based on one article of the tenancy law; cite it as given and "
                "suggest the tenant verifies with the RDSC."
            ),
        }

    hits = search_law(query, top_k=top_k)
    if not hits:
        return {"query": query, "found": False, "articles": [], "rule_ids": [], "message": NOT_FOUND_MESSAGE}

    views = [_article_view(h.article, h.score) for h in hits]
    rule_ids = list(dict.fromkeys(r for v in views for r in v["related_rule_ids"]))
    return {
        "query": query,
        "found": True,
        "articles": views,
        "plain_english": [_note(get_rule(r)) for r in rule_ids],
        "rule_ids": rule_ids,
        "citations": [v["citation"] for v in views],
        "note": ANSWER_NOTE,
    }
