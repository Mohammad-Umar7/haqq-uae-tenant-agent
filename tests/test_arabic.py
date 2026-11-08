"""Arabic letters: citation forms written by code, the Arabic automatic check,
and the Drafter's Arabic version inside the drafting team (fake Claude)."""

import pytest
from fakes import FakeClient, json_reply, usage
from test_team import (  # noqa: F401  (ctx is a fixture)
    GOOD_FACTS,
    INPUTS,
    RULES,
    ctx,
    draft1_body,
    drive,
    facts_for,
    letter_args,
    objection,
    settings,
)

from agent.cost import UsageMeter
from agent.inputs import TenantInputs
from agent.knowledge import all_rules
from agent.law_search import all_articles
from agent.team.arabic import arabic_citation, normalize
from agent.team.checks import check_letter
from agent.team.drafting import run_drafting_team

ARABIC_OK = """التاريخ: 8 نوفمبر 2026
إلى: السيد حمد سيف المري

ينتهي عقد الإيجار في 14 يناير 2027 ببدل إيجار سنوي قدره 85,000 درهم.
اقترحتم بدلاً جديداً قدره 93,500 درهم، أي زيادة بنسبة 10%.
الحد الأقصى للزيادة هو 5% (89,250 درهم سنوياً) وفق المرسوم رقم (43) لسنة 2013، المادة (1).
تسلمت الإشعار في 1 نوفمبر 2026، والقانون رقم (26) لسنة 2007 (المعدَّل بالقانون رقم (33) لسنة 2008)، المادة (14) يشترط إشعاراً قبل 90 يوماً من انتهاء العقد.
"""


@pytest.mark.parametrize("english, arabic", [
    ("Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14",
     "القانون رقم (26) لسنة 2007 (المعدَّل بالقانون رقم (33) لسنة 2008)، المادة (14)"),
    ("Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 25(2)",
     "القانون رقم (26) لسنة 2007 (المعدَّل بالقانون رقم (33) لسنة 2008)، المادة (25)، البند (2)"),
    ("Dubai Decree No. 43 of 2013, Art. 1", "المرسوم رقم (43) لسنة 2013، المادة (1)"),
    ("Dubai Decree No. 43 of 2013 (rent increase caps)", "المرسوم رقم (43) لسنة 2013 (نسب الزيادة في بدل الإيجار)"),
    ("Dubai Rental Disputes Settlement Centre (RDSC)", "مركز فض المنازعات الإيجارية في دبي"),
])
def test_citations_are_converted_by_code(english, arabic):
    assert arabic_citation(english) == arabic


def test_every_citation_haqq_can_produce_has_an_arabic_form():
    citations = [a["citation"] for a in all_articles() if a["in_force"]] + [r["citation"] for r in all_rules()]
    missing = [c for c in citations if arabic_citation(c) is None]
    assert missing == []
    assert arabic_citation("Federal Law No. 5 of 1985, Art. 1") is None     # unknown: not converted


def test_the_arabic_form_keeps_the_numbers_of_the_english_citation():
    for article in all_articles():
        if article["in_force"]:
            ar = arabic_citation(article["citation"])
            assert f"({article['article']})" in ar and article["citation"].split(" of ")[1][:4] in ar


def test_arabic_indic_digits_are_normalised():
    assert normalize("٨٥٬٠٠٠ درهم و ١٠٪") == "85,000 درهم و 10%"


def test_a_correct_arabic_letter_passes_the_automatic_check(ctx):
    assert check_letter(ARABIC_OK, facts_for(ctx)) == []
    assert check_letter(ARABIC_OK.replace("85,000", "٨٥٬٠٠٠"), facts_for(ctx)) == []


@pytest.mark.parametrize("wrong, kind", [
    ("89,250 درهم", "wrong_number"),
    ("1 نوفمبر 2026", "wrong_date"),
    ("المادة (14)", "weak_citation"),
    ("القانون رقم (26) لسنة 2007", "weak_citation"),
])
def test_planted_errors_in_an_arabic_letter_are_caught(ctx, wrong, kind):
    planted = {"89,250 درهم": "91,250 درهم", "1 نوفمبر 2026": "1 أكتوبر 2026",
               "المادة (14)": "المادة (25)", "القانون رقم (26) لسنة 2007": "القانون رقم (5) لسنة 1985"}[wrong]
    problems = check_letter(ARABIC_OK.replace(wrong, planted), facts_for(ctx))
    assert kind in [p.kind for p in problems]


def test_untranslated_english_words_are_found():
    from agent.team.arabic import untranslated_words
    assert untranslated_words("أرغب في مواصلة الاستئجار، but أعتقد أن الزيادة") == ["but"]
    assert untranslated_words(ARABIC_OK) == []
    assert untranslated_words("برج Marina Crest Tower، [Your name]") == []


def test_letter_language_comes_from_the_form():
    inputs = TenantInputs.from_form(None, None, None, "none", "unknown", "both")
    assert inputs.letter_language == "both"
    assert "Write any letter to the landlord in: Arabic and English" in inputs.as_lines()
    assert TenantInputs.from_form(None, None, None, "none", "unknown", "klingon").letter_language == "en"
    assert TenantInputs().as_lines() == []          # English adds nothing to the prompt


# ---- the drafting team writes Arabic on request --------------------------------------

def arabic_ctx(ctx, language):
    ctx.inputs = TenantInputs(**{**INPUTS.__dict__, "letter_language": language})
    return ctx


def run(ctx, replies):
    client = FakeClient(replies)
    events, outcome = drive(run_drafting_team(letter_args(GOOD_FACTS), ctx, client, settings(), UsageMeter("claude-haiku-5-5")))
    return events, outcome, client


def reply_with(english, arabic):
    return json_reply({"objections": [], "letter_english": english, "letter_arabic": arabic,
                       "changes_made": ["Arabic version written."]}, u=usage(600, 900, r=2_500))


def test_arabic_is_written_even_when_nobody_objects(ctx):
    arabic_ctx(ctx, "ar")
    events, outcome, client = run(ctx, [json_reply({"objections": [], "letter_english": None, "letter_arabic": None,
                                                    "changes_made": []}), reply_with(draft1_body(ctx), ARABIC_OK)])
    assert len(client.calls) == 2
    task = client.calls[1]["messages"][0]["content"][1]["text"]
    assert "Nobody objected" in task and "letter_arabic" in task
    assert "Dubai Decree No. 43 of 2013, Art. 1 => المرسوم رقم (43) لسنة 2013، المادة (1)" in task
    out = outcome.output
    assert out["body_arabic"] == ARABIC_OK.strip() and out["letter_language"] == "ar"
    assert out["body"] == draft1_body(ctx) and "Arabic text of the law is the original" in out["note"]
    assert any(e.title.startswith("Automatic check of the Arabic letter: every") for e in events)


def test_an_arabic_letter_with_an_english_word_is_shown_with_a_warning(ctx):
    arabic_ctx(ctx, "ar")
    slip = ARABIC_OK.replace("ينتهي عقد", "but ينتهي عقد")
    events, outcome, _ = run(ctx, [json_reply({"objections": [], "letter_english": None, "letter_arabic": None,
                                               "changes_made": []}), reply_with(draft1_body(ctx), slip)])
    assert outcome.output["body_arabic"] == slip.strip()
    assert "still contains English words (but)" in outcome.output["note"]


def test_an_arabic_letter_with_a_wrong_number_is_not_shown(ctx):
    arabic_ctx(ctx, "both")
    bad = ARABIC_OK.replace("89,250 درهم", "91,250 درهم")
    _, outcome, _ = run(ctx, [json_reply({"objections": [objection()], "letter_english": None, "letter_arabic": None,
                                          "changes_made": []}), reply_with(draft1_body(ctx), bad)])
    assert outcome.output["body_arabic"] is None
    assert "Arabic version failed the automatic check" in outcome.output["note"]


def test_english_only_sends_no_arabic_instructions(ctx):
    _, _, client = run(ctx, [json_reply({"objections": [objection()], "letter_english": None, "letter_arabic": None,
                                         "changes_made": []}), reply_with(draft1_body(ctx), None)])
    task = client.calls[1]["messages"][0]["content"][1]["text"]
    assert "letter_arabic is null" in task and "يناير" not in task


def test_rules_quoted_in_letters_are_all_convertible():
    for rule_id in RULES:
        rule = next(r for r in all_rules() if r["id"] == rule_id)
        assert arabic_citation(rule["citation"])
