from datetime import date

from agent.demo import answer_demo_question, run_demo_analysis
from agent.inputs import TenantInputs
from agent.prompts import SYSTEM_PROMPT, build_user_message
from agent.registry import ToolContext
from agent.trace import render_trace_markdown

DEMO_INPUTS = TenantInputs(
    rera_average_rent=100_000, proposed_new_rent=93_500,
    notice_date="2026-11-01", notice_type="rent_increase", delivery_method="email",
)


def run(ctx, inputs):
    events = list(run_demo_analysis(ctx, inputs))
    return events[-1].detail["text"], events


def test_demo_runs_the_fixed_pipeline_on_the_sample(sample_text):
    ctx = ToolContext(contract_text=sample_text, today=date(2026, 11, 8))
    report, events = run(ctx, DEMO_INPUTS)

    calls = [e.title for e in events if e.kind == "tool_call"]
    assert calls[:4] == ["extract_contract_terms", "clause_risk_scan", "rera_rent_increase_check", "notice_deadline_check"]
    assert "search_tenancy_law" in calls
    assert calls[-1] == "draft_letter"

    assert "Demo mode" in report
    assert "exceeds the 5% cap" in report
    assert "LATE by 16 days" in report
    assert "Draft letter" in report and "Dear Mr. Hamad Saif Al Marri" in report
    assert "Non-refundable" in report or "non-refundable" in report
    assert "Not legal advice" in report
    assert "Art. 14" in report

    trace = render_trace_markdown(events)
    assert "Called tool `clause_risk_scan`" in trace
    assert "Rules consulted" in trace


def test_demo_without_rera_figure_skips_the_calculator(sample_text):
    ctx = ToolContext(contract_text=sample_text, today=date(2026, 11, 8))
    report, events = run(ctx, TenantInputs())
    calls = [e.title for e in events if e.kind == "tool_call"]
    assert "rera_rent_increase_check" not in calls
    assert "Enter the RERA Rental Index average" in report
    # Red flags alone still produce a clause-concern letter.
    assert calls[-1] == "draft_letter"


def test_demo_without_contract_asks_for_one():
    ctx = ToolContext(contract_text=None, today=date(2026, 11, 8))
    report, _ = run(ctx, DEMO_INPUTS)
    assert "Load a contract first" in report


def test_demo_chat_does_keyword_lookup():
    ctx = ToolContext(contract_text=None, today=date(2026, 11, 8))
    events = list(answer_demo_question("Can my landlord keep my deposit?", ctx))
    assert "Art. 20" in events[-1].detail["text"]


def test_user_message_carries_form_details():
    msg = build_user_message("Is this legal?", DEMO_INPUTS, True, 4000, date(2026, 11, 8))
    assert "Today's date: 2026-11-08" in msg
    assert "AED 100,000" in msg and "AED 93,500" in msg
    assert "<tenant_message>\nIs this legal?\n</tenant_message>" in msg


def test_system_prompt_is_static_and_mentions_every_tool():
    # No dates or per-user data in the system prompt (keeps the prompt cache warm).
    assert "2026" not in SYSTEM_PROMPT
    for name in ("extract_contract_terms", "clause_risk_scan", "rera_rent_increase_check",
                 "notice_deadline_check", "search_tenancy_law", "draft_letter"):
        assert name in SYSTEM_PROMPT


def test_form_cleaning():
    inputs = TenantInputs.from_form(0, None, "01/11/2026", "none", "unknown")
    assert inputs == TenantInputs(notice_date="2026-11-01")
