from datetime import date

import pytest

from agent.knowledge import all_rules, get_rule, search_rules
from agent.law_search import get_article
from agent.tools.letters import LETTER_TYPES, draft_letter

REQUIRED_KEYS = {"id", "title", "source", "article", "citation", "law_article_ids", "summary", "plain_english",
                 "tenant_tip", "keywords", "verify_with"}


def test_every_rule_is_complete_and_ids_unique():
    rules = all_rules()
    assert len({r["id"] for r in rules}) == len(rules)
    for r in rules:
        assert REQUIRED_KEYS <= set(r), r["id"]
        assert r["keywords"], r["id"]


def test_only_the_curated_articles_are_cited():
    # Guard rail: adding a new article number should be a deliberate,
    # reviewed change, so this test fails until it's added here too.
    articles = {r["article"] for r in all_rules()}
    assert articles <= {None, "14", "20", "25(2)"}


def test_citations_match_the_brief():
    assert get_rule("notice_change_terms_90_days")["citation"].endswith("Art. 14")
    assert get_rule("eviction_12_months_notice")["citation"].endswith("Art. 25(2)")
    assert get_rule("security_deposit_return")["citation"].endswith("Art. 20")
    assert "Decree No. 43 of 2013" in get_rule("rent_increase_caps")["citation"]


@pytest.mark.parametrize(
    "query, expected_first",
    [
        ("security_deposit_return", "security_deposit_return"),
        ("Can my landlord keep my deposit?", "security_deposit_return"),
        ("how much notice to evict me so he can sell the flat", "eviction_12_months_notice"),
        ("My landlord wants me out to sell the flat. How much notice?", "eviction_12_months_notice"),
        ("how much notice does the landlord need to change the contract terms", "notice_change_terms_90_days"),
        ("is a 10% rent increase allowed", "rent_increase_caps"),
        ("do I need Ejari", "ejari_registration"),
        ("where do I file a dispute", "rdsc_disputes"),
    ],
)
def test_search(query, expected_first):
    assert search_rules(query)[0]["id"] == expected_first


def test_every_rule_links_to_official_articles_in_force():
    for rule in all_rules():
        for article_id in rule["law_article_ids"]:
            article = get_article(article_id)
            assert article is not None, (rule["id"], article_id)
            assert article["in_force"], (rule["id"], article_id)


def test_curated_articles_match_the_official_text():
    # The plain-English notes must agree with the law text they link to.
    assert "ninety (90) days" in get_article("law26_art14")["text"]
    assert "twelve (12) months" in get_article("law26_art25")["text"]
    assert "registered mail" in get_article("law26_art25")["text"]
    assert "refund such deposit" in get_article("law26_art20")["text"]
    assert "registered with RERA" in get_article("law26_art4")["text"]
    assert "Rent Index of the Emirate of Dubai" in get_article("decree43_art3")["text"]


def test_letter_cites_only_known_rules():
    out = draft_letter(
        "rent_increase_objection",
        facts=["My rent is AED 85,000.", "You proposed AED 93,500."],
        rule_ids=["rent_increase_caps", "notice_change_terms_90_days", "made_up_rule"],
        tenant_name="Priya Raman",
        landlord_name="Mr. Hamad Saif Al Marri",
        property_address="Apartment 1504, Marina Crest Tower",
        today=date(2026, 11, 8),
    )
    assert out["ignored_rule_ids"] == ["made_up_rule"]
    assert out["rule_ids"] == ["rent_increase_caps", "notice_change_terms_90_days"]
    body = out["body"]
    assert "Date: 8 November 2026" in body
    assert "Dear Mr. Hamad Saif Al Marri" in body
    assert "Art. 14" in body
    assert "made_up_rule" not in body
    assert "- You proposed AED 93,500." in body


def test_letter_uses_placeholders_when_names_missing():
    out = draft_letter("deposit_return_request", facts=["The tenancy ended on 14 January 2027."], rule_ids=["security_deposit_return"])
    assert "[Your name]" in out["body"] and "[Landlord's name]" in out["body"]


def test_rdsc_summary_has_sections():
    out = draft_letter("rdsc_complaint_summary", facts=["Late notice."], rule_ids=["notice_change_terms_90_days"])
    for heading in ("1. Facts", "2. Rules relied on", "3. What the tenant is asking for", "4. Documents to bring"):
        assert heading in out["body"]


def test_every_letter_type_renders():
    for letter_type in LETTER_TYPES:
        out = draft_letter(letter_type, facts=["A fact."], rule_ids=[])
        assert out["body"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"letter_type": "love_letter", "facts": ["x"], "rule_ids": []},
        {"letter_type": "clause_concern", "facts": ["  "], "rule_ids": []},
    ],
)
def test_letter_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        draft_letter(**kwargs)
