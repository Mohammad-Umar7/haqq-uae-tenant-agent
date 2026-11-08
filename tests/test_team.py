"""The multi-agent drafting team (Drafter -> Critic -> Drafter) with a fake Claude.

No network, no key: FakeClient returns scripted replies, so these tests pin
down the team's bookkeeping: who is called when, what they share (the
cached prefix), what is skipped to save money, and how the team degrades.
"""

import json
from datetime import date

import pytest
from fakes import FakeClient, json_reply, reply, text, tool, usage

from agent.config import Settings
from agent.cost import UsageMeter
from agent.inputs import TenantInputs
from agent.loop import TenantAgent, run_to_completion
from agent.registry import ToolContext, execute_tool
from agent.roster import CRITIC, DRAFTER, LAW_RESEARCHER
from agent.team.case_file import build_case_file, case_file_text
from agent.team.checks import audit_citations, check_letter, known_facts
from agent.team.drafting import run_drafting_team
from agent.trace import latest_letter, render_trace_markdown

TODAY = date(2026, 11, 8)
INPUTS = TenantInputs(rera_average_rent=100_000, proposed_new_rent=93_500, notice_date="2026-11-01",
                      notice_type="rent_increase", delivery_method="email")
RULES = ["rent_increase_caps", "rental_index_source", "notice_change_terms_90_days"]
GOOD_FACTS = [
    "Our tenancy contract runs until 14 January 2027 at an annual rent of AED 85,000.",
    "You have proposed a new annual rent of AED 93,500.",
    "According to the RERA Rental Index the average rent for similar units is AED 100,000, so my rent is "
    "15.0% below average and the maximum increase is 5% (AED 89,250 per year).",
    "I received your notice on 1 November 2026, 74 days before the contract expires; the rules require "
    "at least 90 days' written notice.",
]
PLANTED = [f.replace("AED 89,250", "AED 91,250") for f in GOOD_FACTS]   # the classic LLM slip


def settings(**overrides):
    base = dict(api_key="test", model="claude-haiku-5-5", effort="medium", max_tool_rounds=6)
    return Settings(**{**base, **overrides})


@pytest.fixture
def ctx(sample_text):
    """A session in which the Orchestrator already ran the analysis tools."""
    c = ToolContext(contract_text=sample_text, today=TODAY, inputs=INPUTS)
    for name, args in [
        ("extract_contract_terms", {}),
        ("clause_risk_scan", {}),
        ("rera_rent_increase_check", {"current_annual_rent": 85_000, "rera_average_rent": 100_000,
                                      "proposed_new_rent": 93_500}),
        ("notice_deadline_check", {"contract_end_date": "2027-01-14", "notice_type": "rent_increase",
                                   "notice_date": "2026-11-01", "delivery_method": "email"}),
        ("search_tenancy_law", {"query": "rent_increase_caps"}),
        ("search_tenancy_law", {"query": "notice_change_terms_90_days"}),
    ]:
        c.record(name, execute_tool(name, args, c).output)
    return c


def letter_args(facts):
    return {"letter_type": "rent_increase_objection", "facts": facts, "rule_ids": RULES,
            "tenant_name": "Ms. Priya Raman", "landlord_name": "Mr. Hamad Saif Al Marri",
            "property_address": "Apartment 1504, Marina Crest Tower, Dubai Marina, Dubai"}


def team_reply(objections=(), letter=None, changes=()):
    return json_reply({"objections": list(objections), "letter_english": letter, "letter_arabic": None,
                       "changes_made": list(changes)}, u=usage(600, 400, w=2_500))


def objection(kind="tone", quote="Dear", problem="Too generic.", fix="Be specific."):
    return {"kind": kind, "severity": "medium", "quote": quote, "problem": problem, "fix": fix}


def drive(gen):
    """Run a generator that yields events and returns a value."""
    events = []
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            return events, stop.value


def run_team(ctx, replies, facts=GOOD_FACTS, **overrides):
    client = FakeClient(replies)
    meter = UsageMeter("claude-haiku-5-5")
    events, outcome = drive(run_drafting_team(letter_args(facts), ctx, client, settings(**overrides), meter))
    return events, outcome, client, meter


def draft1_body(ctx, facts=GOOD_FACTS):
    return execute_tool("draft_letter", letter_args(facts), ctx).output["body"]


# ---- the automatic check (code) ----------------------------------------------

def facts_for(ctx):
    case = build_case_file(ctx, extra_rule_ids=RULES)
    return known_facts(case_file_text(case), case["allowed_citations"])


def test_a_correct_template_letter_passes_the_automatic_check(ctx):
    assert check_letter(draft1_body(ctx), facts_for(ctx)) == []


def test_a_planted_wrong_amount_is_caught(ctx):
    problems = check_letter(draft1_body(ctx, PLANTED), facts_for(ctx))
    assert [(p.kind, p.quote) for p in problems] == [("wrong_number", "AED 91,250")]
    assert problems[0].source == "automatic check"


@pytest.mark.parametrize("sentence, kind, quote", [
    ("I received your notice on 1 October 2026.", "wrong_date", "1 October 2026"),
    ("The increase is 12% above my rent.", "wrong_number", "12%"),
    ("See Dubai Law No. 26 of 2007, Art. 25.", "weak_citation", "Art. 25"),
    ("Federal Law No. 5 of 1985 also protects me.", "weak_citation", "Law No. 5 of 1985"),
])
def test_other_planted_errors_are_caught(ctx, sentence, kind, quote):
    problems = check_letter(draft1_body(ctx) + "\n" + sentence, facts_for(ctx))
    assert (kind, quote) in [(p.kind, p.quote) for p in problems]


def test_rounded_numbers_and_other_date_formats_are_accepted(ctx):
    letter = "Rent AED 85,000.00 (85000 AED), 15% below average, notice on 2026-11-01 and November 1, 2026."
    assert check_letter(letter, facts_for(ctx)) == []


def test_answer_citations_are_audited_against_the_session(ctx):
    answer = ("The increase is above the cap [Dubai Decree No. 43 of 2013, Art. 1]. Notice was late "
              "[Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 14]. Eviction rules "
              "[Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 25]. Sign as [Your name].")
    audit = audit_citations(answer, ctx.evidence)
    assert audit["checked"] == 3
    assert audit["not_found"] == ["Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 25"]


# ---- the team flow ---------------------------------------------------------------

def test_critic_objects_and_drafter_revises_once(ctx):
    revised = draft1_body(ctx).replace("Dear Mr. Hamad Saif Al Marri,", "Dear Mr. Al Marri,")
    events, outcome, client, meter = run_team(ctx, [
        team_reply([objection(quote="I believe it cannot be applied", problem="Hedged.", fix="State it.")]),
        team_reply(letter=revised, changes=["Firmer opening."]),
    ])
    assert not outcome.is_error
    out = outcome.output
    assert out["body"] == revised and out["version"].startswith("draft 2")
    assert out["changes_made"] == ["Firmer opening."]
    assert [o["source"] for o in out["review"]["objections"]] == ["Critic"]
    assert len(client.calls) == 2
    # Who did what is visible in the trace.
    agents = [(e.agent, e.kind, e.title) for e in events]
    assert (CRITIC, "tool_call", "critic_review") in agents and (DRAFTER, "tool_call", "revise_letter") in agents
    assert set(meter.by_agent) == {CRITIC, DRAFTER}


def test_critic_and_drafter_share_the_cached_prefix(ctx):
    _, _, client, _ = run_team(ctx, [
        team_reply([objection()]),
        team_reply(letter=draft1_body(ctx), changes=["none needed"]),
    ])
    critic, drafter = client.calls
    for key in ("model", "system", "thinking", "output_config"):
        assert critic[key] == drafter[key], key
    first_c, task_c = critic["messages"][0]["content"]
    first_d, task_d = drafter["messages"][0]["content"]
    assert first_c == first_d and first_c["cache_control"] == {"type": "ephemeral"}
    assert "<case_file>" in first_c["text"] and "<draft_letter>" in first_c["text"]
    assert "the Critic" in task_c["text"] and "the Drafter" in task_d["text"]
    assert "tools" not in critic and critic["output_config"]["format"]["type"] == "json_schema"


def test_no_objections_means_no_revision_call(ctx):
    _, outcome, client, _ = run_team(ctx, [team_reply([])])
    assert len(client.calls) == 1
    assert outcome.output["version"] == "draft 1 (template)"


def test_planted_number_is_fixed_even_if_the_critic_misses_it(ctx):
    fixed = draft1_body(ctx)                       # the correct letter
    _, outcome, client, _ = run_team(ctx, [team_reply([]), team_reply(letter=fixed, changes=["Fixed the cap."])],
                                     facts=PLANTED)
    assert "AED 91,250" in client.calls[1]["messages"][0]["content"][1]["text"]   # the Drafter was told
    assert outcome.output["body"] == fixed and outcome.output["review"]["unverified_in_final_letter"] == []


def test_a_revision_that_adds_an_unverified_number_is_rejected(ctx):
    bad = draft1_body(ctx) + "\nI can offer AED 77,777 instead."
    events, outcome, _, _ = run_team(ctx, [team_reply([objection()]), team_reply(letter=bad)])
    assert outcome.output["version"] == "draft 1 (template)"
    assert any(e.title == "Kept draft 1: draft 2 failed the automatic check" for e in events)


def test_spend_limit_skips_the_claude_specialists(ctx):
    client = FakeClient([])
    meter = UsageMeter("claude-haiku-5-5")
    meter.add(usage(500_000, 0))                    # already spent $0.05
    events, outcome = drive(run_drafting_team(letter_args(PLANTED), ctx, client, settings(max_cost_per_run_usd=0.01), meter))
    assert client.calls == []
    assert any(e.title == "Critic skipped: spend limit reached" for e in events)
    assert outcome.output["review"]["unverified_in_final_letter"][0]["quote"] == "AED 91,250"
    assert "could not verify" in outcome.output["note"]


def test_an_unusable_critic_reply_degrades_gracefully(ctx):
    events, outcome, client, _ = run_team(ctx, [reply("end_turn", text("not json"))])
    assert any(e.title == "Critic reply unusable" and e.is_error for e in events)
    assert outcome.output["body"] == draft1_body(ctx)
    assert len(client.calls) == 1


def test_critic_can_be_switched_off(ctx):
    _, outcome, client, _ = run_team(ctx, [], critic_enabled=False)
    assert client.calls == [] and outcome.output["version"] == "draft 1 (template)"


def test_bad_letter_arguments_never_reach_the_team(ctx):
    client = FakeClient([])
    _, outcome = drive(run_drafting_team({"letter_type": "poem", "facts": ["x"], "rule_ids": []}, ctx, client,
                                         settings(), UsageMeter("claude-haiku-5-5")))
    assert outcome.is_error and client.calls == []


# ---- inside the Orchestrator's loop ------------------------------------------------

def test_the_orchestrator_delegates_draft_letter_to_the_team(ctx):
    client = FakeClient([
        reply("tool_use", tool("t1", "draft_letter", letter_args(PLANTED))),
        team_reply([objection()]),
        team_reply(letter=draft1_body(ctx), changes=["Corrected the maximum rent to AED 89,250."]),
        reply("end_turn", text("Your letter is ready. The Critic caught a wrong amount "
                               "[Dubai Decree No. 43 of 2013, Art. 1].")),
    ])
    agent = TenantAgent(settings(), client=client)
    messages = []
    final, events = run_to_completion(agent, messages, "Draft my letter", ctx)

    assert "Corrected the maximum rent" in messages[2]["content"][0]["content"]   # what the Orchestrator saw
    assert "tools" in client.calls[0] and "tools" not in client.calls[1]           # specialists have no tools
    assert latest_letter(events)["body"] == draft1_body(ctx)
    assert any(e.agent == LAW_RESEARCHER and e.title.startswith("Citation check: all 1") for e in events)
    cost_by_agent = next(e for e in events if e.title == "Cost by agent").detail
    assert "| Critic | 1 |" in cost_by_agent and "| Drafter | 1 |" in cost_by_agent
    assert set(events[-1].detail["cost"]["by_agent"]) == {"Orchestrator", CRITIC, DRAFTER}
    trace = render_trace_markdown(events)
    assert "[Critic]" in trace and "[Drafter]" in trace and "**Team at work:**" in trace


def test_a_citation_from_memory_is_flagged_in_the_answer():
    c = ToolContext(contract_text=None, today=TODAY)
    client = FakeClient([reply("end_turn", text("You have 30 days [Dubai Law No. 26 of 2007, Art. 33]."))])
    final, events = run_to_completion(TenantAgent(settings(), client=client), [], "hi", c)
    assert "_Citation check: [Dubai Law No. 26 of 2007, Art. 33] did not come from this session's law search" in final


def test_tool_results_become_evidence_but_letters_do_not(sample_text):
    c = ToolContext(contract_text=sample_text, today=TODAY)
    c.record("clause_risk_scan", {"flags": []})
    c.record("draft_letter", {"body": "Dear landlord"})
    c.record("search_tenancy_law", {"error": "query must be ..."})
    assert [e["tool"] for e in c.evidence] == ["clause_risk_scan"]


def test_case_file_is_compact_and_deterministic(ctx):
    case = build_case_file(ctx, extra_rule_ids=RULES)
    assert case_file_text(case) == case_file_text(build_case_file(ctx, extra_rule_ids=RULES))
    assert {c["tool"] for c in case["calculations"]} == {"rera_rent_increase_check", "notice_deadline_check"}
    assert "Dubai Decree No. 43 of 2013, Art. 1" in case["allowed_citations"]
    # Art. 13 was never searched, but the 90-day rule is linked to it in the knowledge base.
    art13 = "Dubai Law No. 26 of 2007 (as amended by Law No. 33 of 2008), Art. 13"
    assert art13 in case["allowed_citations"] and art13 in [a["citation"] for a in case["law_articles"]]
    assert len(case_file_text(case)) < 12_000       # a few thousand tokens, not the whole chat
    json.loads(case_file_text(case))


def test_the_drafter_is_shown_the_allowed_citations(ctx):
    _, _, client, _ = run_team(ctx, [team_reply([objection()]), team_reply(letter=draft1_body(ctx))])
    task = client.calls[1]["messages"][0]["content"][1]["text"]
    assert "- Dubai Decree No. 43 of 2013, Art. 1" in task and "rejects the revision" in task
