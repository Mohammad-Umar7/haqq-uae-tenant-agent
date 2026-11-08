"""Tests for the official law corpus and the BM25 search over it (no network)."""

from datetime import date

import pytest

from agent.law_search import all_articles, get_article, search_law, sources, tokenize
from agent.registry import ToolContext, execute_tool
from agent.tools.law_lookup import search_tenancy_law

SUPERSEDED_IN_2008 = {"2", "3", "4", "9", "13", "14", "15", "25", "26", "29", "36"}
LAW33_URL = "https://dlp.dubai.gov.ae/Legislation%20Reference/2009/Law%20No.%20(33)%20of%202008%20Amending%20Law%20No.%20(26)%20of%202007.pdf"


# ---- the corpus -------------------------------------------------------------

def test_sources_are_the_official_legislation_portal():
    urls = {s["url"] for s in sources()}
    assert len(urls) == 3
    for s in sources():
        assert s["url"].startswith("https://dlp.dubai.gov.ae/Legislation%20Reference/")
        assert len(s["sha256"]) == 64 and s["pages"] > 0


def test_every_article_has_text_citation_and_source():
    articles = all_articles()
    assert len({a["id"] for a in articles}) == len(articles)
    urls = {s["url"] for s in sources()}
    for a in articles:
        assert a["text"].strip(), a["id"]
        assert a["source_url"] in urls, a["id"]
        assert f"Art. {a['article']}" in a["citation"], a["id"]


def test_law_26_is_complete_and_2008_replacements_are_marked():
    law26 = [a for a in all_articles() if a["id"].startswith("law26_")]
    assert {a["article"] for a in law26} == {str(n) for n in range(1, 38)}
    for number in SUPERSEDED_IN_2008:
        old, new = get_article(f"law26_art{number}_2007"), get_article(f"law26_art{number}")
        assert old["in_force"] is False and old["superseded_by"]
        assert new["in_force"] is True and new["source_url"] == LAW33_URL
        assert "as amended by Law No. 33 of 2008" in new["citation"]


def test_decree_43_bands_are_verbatim():
    text = get_article("decree43_art1")["text"]
    assert "up to ten percent (10%) less than the average rental value" in text
    assert "twenty percent (20%) of the Rent of the Real Property Unit, where the Rent is more than forty percent (40%)" in text


# ---- retrieval --------------------------------------------------------------

@pytest.mark.parametrize(
    "query, expected_first",
    [
        ("90 days notice rent change", "law26_art14"),
        ("eviction sale of property", "law26_art25"),
        ("how much notice to evict me so he can sell the flat", "law26_art25"),
        ("security deposit refund", "law26_art20"),
        ("Can my landlord keep my deposit?", "law26_art20"),
        ("is a 10% rent increase allowed", "decree43_art1"),
        ("landlord cut off electricity", "law26_art34"),
        ("do I need Ejari", "law26_art4"),
        ("who pays for repairs", "law26_art16"),
        ("break the lease early", "law26_art7"),
        ("can I sublet my apartment", "law26_art24"),
    ],
)
def test_retrieval_puts_the_right_article_first(query, expected_first):
    hits = search_law(query)
    assert hits, query
    assert hits[0].article["id"] == expected_first


def test_art_14_hit_is_the_2008_text_with_its_official_source():
    hit = search_law("90 days notice rent change")[0]
    assert "ninety (90) days before the date on which the Lease Contract expires" in hit.article["text"]
    assert hit.article["source_url"] == LAW33_URL


def test_superseded_2007_wording_is_never_returned():
    # The 2007 Art. 25(2) said 90 days' eviction notice; since 2008 it is 12 months.
    for query in ["eviction notice ninety days prior to the expiry", "rent may not be increased before two years",
                  "evict at expiry demolish own use"]:
        for hit in search_law(query, top_k=5):
            assert hit.article["in_force"], (query, hit.article["id"])
    texts = [h.article["text"] for h in search_law("eviction notice", top_k=5)]
    assert not any("at least ninety (90) days prior to the expiry" in t for t in texts)


@pytest.mark.parametrize("query", ["Can my landlord ban guests?", "visa sponsorship for my nanny", "landlord tenant"])
def test_off_topic_or_generic_questions_find_nothing(query):
    assert search_law(query) == []


def test_tokenizer_folds_word_forms_together():
    assert tokenize("Eviction evicted evict") == ["evict", "evict", "evict"]
    assert tokenize("notified notice notification") == ["notice", "notice", "notice"]
    assert tokenize("sub-let subletting") == ["sublease", "sublease"]
    assert tokenize("the tenant shall") == ["tenant"]


# ---- the tool -------------------------------------------------------------

def test_tool_returns_text_citation_url_and_plain_english():
    out = search_tenancy_law("security deposit refund")
    top = out["articles"][0]
    assert out["found"] is True
    assert top["citation"] == "Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 20"
    assert top["source_url"].startswith("https://dlp.dubai.gov.ae/")
    assert "refund such deposit" in top["text"]
    assert "security_deposit_return" in out["rule_ids"]
    assert out["plain_english"][0]["rule_id"] == "security_deposit_return"


def test_tool_says_it_could_not_find_it_in_the_law():
    out = search_tenancy_law("Can my landlord ban guests?")
    assert out["found"] is False and out["articles"] == []
    assert "I couldn't find this in the law" in out["message"]


def test_tool_accepts_a_rule_id_from_another_tool():
    out = search_tenancy_law("eviction_12_months_notice")
    assert [a["article_id"] for a in out["articles"]] == ["law26_art25", "law26_art26"]
    assert out["rule_ids"] == ["eviction_12_months_notice"]


def test_rule_without_an_article_still_answers():
    out = search_tenancy_law("rdsc_disputes")
    assert out["found"] is True and out["articles"] == []
    assert out["citations"] == ["Dubai Rental Disputes Settlement Centre (RDSC)"]


def test_registry_clamps_top_k_and_rejects_bad_input():
    ctx = ToolContext(contract_text=None, today=date(2026, 11, 8))
    out = execute_tool("search_tenancy_law", {"query": "notice", "top_k": 50}, ctx)
    assert not out.is_error and 1 <= len(out.output["articles"]) <= 5
    assert execute_tool("search_tenancy_law", {"query": "notice", "top_k": "lots"}, ctx).is_error
    assert execute_tool("search_tenancy_law", {"query": "  "}, ctx).is_error
