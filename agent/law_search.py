"""Local BM25 search over the official law text (knowledge_base/law_articles.json).

This is the "retrieval" in retrieval-augmented generation (RAG): before the
model says what the law requires, it searches the real article text and
quotes what it finds. Everything here runs on your machine with no API call.

Why BM25 and not embeddings?
- The corpus is tiny (about 40 articles in force), so keyword search is
  fast, free and easy to inspect.
- Legal questions usually share exact words with the law ("notice", "90
  days", "security deposit", "evict"). BM25 rewards rare shared words.
- No embeddings service = no extra cost, no extra API key, deterministic
  results that unit tests can pin down.

BM25 in one sentence: a document scores higher when it contains the query's
words, especially rare ones (IDF), with diminishing returns for repeating a
word (k1) and a penalty for very long documents (b).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from rank_bm25 import BM25Okapi

LAW_PATH = Path(__file__).parent / "knowledge_base" / "law_articles.json"

# Words that carry no meaning for search.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "by", "for", "with", "from", "as", "is",
    "are", "was", "be", "been", "it", "its", "this", "that", "these", "those", "he", "his", "him", "her",
    "they", "them", "their", "i", "me", "my", "we", "our", "you", "your", "can", "could", "do", "does",
    "did", "how", "what", "when", "which", "who", "whom", "why", "if", "not", "no", "so", "than", "then",
    "there", "into", "any", "all", "such", "other", "will", "shall", "may", "must", "would", "should",
    "has", "have", "had", "thereof", "therein", "hereby", "where", "whether", "unless", "otherwise",
    "provided", "accordance", "pursuant", "article", "law", "decree", "emirate", "dubai", "much", "many",
    "want", "wants", "need", "needs", "get", "give", "say", "says", "about", "under", "per", "same",
    "out", "up", "happen", "happens", "happened", "allowed", "legal", "okay", "ok",
}

# Words that appear in nearly every article. A match on only these does not
# count as "found" (otherwise "Can my landlord ban guests?" would match).
_GENERIC = {"landlord", "tenant", "real", "property", "leas", "contract", "rent"}

# Morphological variants the tiny stemmer below misses, mapped to one form.
_LEMMAS = {
    "notify": "notice", "notified": "notice", "notifying": "notice", "notification": "notice",
    "notifications": "notice", "notices": "notice",
    "sale": "sell", "sold": "sell", "selling": "sell", "sells": "sell",
    "refundable": "refund", "refunded": "refund", "refunds": "refund",
    "repairs": "repair", "repairing": "repair",
    "sublet": "sublease", "sublets": "sublease", "subletting": "sublease",
    # Everyday words for the home all mean "Real Property" (a generic term).
    "apartment": "property", "apartments": "property", "flat": "property", "villa": "property",
    "home": "property", "house": "property", "unit": "property", "building": "property",
}

# Tenant words -> extra law words added to the QUERY (never to the law).
_RELATIVES = ["relatives"]
_QUERY_SYNONYMS = {
    "raise": ["increase"], "hike": ["increase"], "kick": ["evict"], "vacate": ["evict"],
    "change": ["amend"], "cut": ["disconnect"], "court": ["tribunal"], "rdsc": ["tribunal", "dispute"],
    "fix": ["repair", "maintenance"], "break": ["terminate"], "ejari": ["register", "rera"],
    "sell": ["ownership"], "sells": ["ownership"], "sold": ["ownership"], "sale": ["ownership"],
    "son": _RELATIVES, "daughter": _RELATIVES, "family": _RELATIVES, "father": _RELATIVES,
    "mother": _RELATIVES, "brother": _RELATIVES, "sister": _RELATIVES, "wife": _RELATIVES,
    "husband": _RELATIVES, "relative": _RELATIVES,
}

# (suffix, replacement, minimum stem length). First match wins.
_SUFFIXES = [
    ("ies", "y", 3), ("ing", "", 3), ("ions", "", 3), ("ion", "", 3), ("ments", "", 3),
    ("ment", "", 3), ("ed", "", 3), ("al", "", 4), ("es", "e", 3), ("s", "", 3),
]


def _stem(word: str) -> str:
    """A deliberately tiny stemmer: eviction/evicted -> evict, renewal -> renew."""
    word = _LEMMAS.get(word, word)
    if word in _LEMMAS.values() or word.isdigit():
        return word
    for suffix, replacement, min_len in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= min_len and not word.endswith("ss"):
            word = word[: -len(suffix)] + replacement
            break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lowercase, split into words, drop stopwords, stem."""
    text = re.sub(r"\bsub-(\w)", r"sub\1", text.lower())     # sub-let -> sublet
    words = re.findall(r"[a-z0-9]+", text)
    return [_stem(w) for w in words if w not in _STOPWORDS]


# Added synonyms count half as much as the words the tenant actually typed.
SYNONYM_WEIGHT = 0.5


def _query_tokens(query: str) -> dict[str, float]:
    """Query words -> weight (1.0 for typed words, SYNONYM_WEIGHT for added ones)."""
    weights = {token: 1.0 for token in tokenize(query)}
    for word in re.findall(r"[a-z0-9]+", query.lower()):
        for synonym in _QUERY_SYNONYMS.get(word, []):
            weights.setdefault(_stem(synonym), SYNONYM_WEIGHT)
    return weights


@dataclass(frozen=True)
class LawHit:
    article: dict[str, Any]
    score: float
    matched_terms: list[str]


@lru_cache(maxsize=1)
def _corpus() -> dict[str, Any]:
    with LAW_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def all_articles() -> list[dict[str, Any]]:
    return list(_corpus()["articles"])


def sources() -> list[dict[str, Any]]:
    return list(_corpus()["sources"])


def get_article(article_id: str) -> dict[str, Any] | None:
    for article in all_articles():
        if article["id"] == article_id:
            return article
    return None


@lru_cache(maxsize=1)
def _index() -> tuple[list[dict[str, Any]], list[list[str]], BM25Okapi]:
    """Build the BM25 index once, over articles that are in force only.

    Superseded 2007 wording (e.g. the old 90-day eviction notice) is never
    searchable, so the agent can't quote a rule that no longer applies.
    """
    docs = [a for a in all_articles() if a["in_force"]]
    tokenized = []
    for a in docs:
        searchable = " ".join(filter(None, [a.get("title"), a.get("chapter"), a["text"], *a["search_keywords"]]))
        tokenized.append(tokenize(searchable))
    return docs, tokenized, BM25Okapi(tokenized)


def search_law(query: str, top_k: int = 3) -> list[LawHit]:
    """Return up to top_k in-force articles relevant to the query, best first.

    An article only counts if it shares at least one specific (non-generic)
    word with the query; an empty list means "not found in the law".
    """
    top_k = max(1, min(int(top_k), 5))
    weights = _query_tokens(query)
    specific = [t for t in weights if t not in _GENERIC]
    if not specific:
        return []
    docs, tokenized, bm25 = _index()
    # BM25 is a sum over query words, so we can score each word on its own
    # and weight it before adding up.
    scores = [0.0] * len(docs)
    for token, weight in weights.items():
        for i, s in enumerate(bm25.get_scores([token])):
            scores[i] += weight * s
    hits: list[LawHit] = []
    for i, (doc, score) in enumerate(zip(docs, scores)):
        doc_terms = set(tokenized[i])
        matched = [t for t in specific if t in doc_terms]
        if matched and score > 0:
            hits.append(LawHit(article=doc, score=round(float(score), 3), matched_terms=matched))
    hits.sort(key=lambda h: -h.score)  # stable sort keeps corpus order on ties
    return hits[:top_k]
