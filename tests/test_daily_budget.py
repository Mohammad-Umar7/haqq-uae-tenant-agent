"""The app-wide daily spend limit: it adds up every Claude call, and once it is
used up the app answers in free demo mode until midnight (Dubai time)."""

from datetime import date

import pytest
from fakes import SAMPLE_EXTRACTION, FakeClient, json_reply, reply, text, tool, usage
from fastapi.testclient import TestClient

from agent.config import Settings
from agent.contract import LoadedContract
from agent.daily_budget import DailyBudget, shared_budget
from agent.inputs import TenantInputs
from agent.loop import TenantAgent
from agent.runner import Session, read_notice, run_turn
from agent.team.notice_reader import SAMPLE_NOTICE, SAMPLE_READING
from webapi import SESSION_HEADER, create_web_app

TODAY = date(2026, 11, 13)
INPUTS = TenantInputs(rera_average_rent=100_000, proposed_new_rent=93_500, notice_date="2026-11-01",
                      notice_type="rent_increase", delivery_method="email")
FORM = {"rera_average_rent": 100_000, "proposed_new_rent": 93_500, "notice_type": "rent_increase",
        "notice_date": "2026-11-01", "delivery_method": "email", "letter_language": "en"}


def settings(**overrides):
    base = dict(api_key="test", model="claude-haiku-5-5", effort="medium", max_tool_rounds=6)
    return Settings(**{**base, **overrides})


def analysis_replies():
    return [
        json_reply(SAMPLE_EXTRACTION, u=usage(1_500, 900)),
        reply("tool_use", tool("t1", "extract_contract_terms", {}), u=usage(200, 80, w=3_000)),
        reply("end_turn", text("The increase is above the cap [Dubai Decree No. 43 of 2013, Art. 1]."),
              u=usage(50, 400, r=3_000, w=2_000)),
    ]


def turn(session, s, client, budget):
    return list(run_turn(session, "Analyze my contract.", analyze=True, inputs=INPUTS, today=TODAY, settings=s,
                         agent=TenantAgent(s, client=client), budget=budget))


# ---- the counter -------------------------------------------------------------------

def test_budget_adds_up_and_resets_at_midnight():
    day = [date(2026, 11, 13)]
    budget = DailyBudget(0.05, clock=lambda: day[0])
    budget.add(0.03)
    assert not budget.exhausted() and budget.spent() == pytest.approx(0.03)
    budget.add(0.02)
    budget.add(-1)                       # a negative cost never refunds the budget
    assert budget.exhausted()
    day[0] = date(2026, 11, 14)          # a new day in Dubai
    assert not budget.exhausted() and budget.spent() == 0.0


def test_one_shared_budget_per_process():
    assert shared_budget(settings()) is shared_budget(settings(max_daily_spend_usd=9.0))


def test_daily_limit_setting_is_read_from_the_environment(monkeypatch):
    from agent.config import load_settings

    monkeypatch.setenv("HAQQ_MAX_DAILY_SPEND_USD", "2.5")
    assert load_settings().max_daily_spend_usd == 2.5
    monkeypatch.setenv("HAQQ_MAX_DAILY_SPEND_USD", "nonsense")
    assert load_settings().max_daily_spend_usd == 1.0


# ---- the runner --------------------------------------------------------------------

def test_every_claude_call_of_a_turn_is_added_to_the_budget(sample_text):
    s = settings()
    budget = DailyBudget(1.0, clock=lambda: TODAY)
    events = turn(Session(contract=LoadedContract.from_text(sample_text, "sample")), s,
                  FakeClient(analysis_replies()), budget)
    assert "above the cap" in events[-1].detail["text"]
    assert budget.spent() == pytest.approx(events[-1].detail["cost"]["cost_usd"]) and budget.spent() > 0


def test_used_up_budget_answers_in_demo_mode_without_calling_claude(sample_text):
    s = settings()
    budget = DailyBudget(0.01, clock=lambda: TODAY)
    budget.add(0.01)
    client = FakeClient([])   # any call would fail the test
    session = Session(contract=LoadedContract.from_text(sample_text, "sample"))
    events = turn(session, s, client, budget)
    assert events[0].title == "Daily AI budget used up: free demo mode"
    assert "midnight Dubai time" in events[0].detail
    assert any(e.title == "Cost" and "Demo mode" in e.detail for e in events)
    assert client.calls == [] and session.llm_requests == 0


def test_used_up_budget_reads_only_the_sample_notice():
    s = settings()
    budget = DailyBudget(0.01, clock=lambda: TODAY)
    budget.add(0.5)
    client = FakeClient([])
    events = list(read_notice(Session(), str(SAMPLE_NOTICE), settings=s, agent=TenantAgent(s, client=client),
                              budget=budget))
    assert events[0].title == "Daily AI budget used up: free demo mode"
    assert events[-1].detail["reading"] is not None and client.calls == []


def test_a_notice_reading_is_added_to_the_budget():
    import json

    s = settings()
    budget = DailyBudget(1.0, clock=lambda: TODAY)
    reading = json.loads(SAMPLE_READING.read_text(encoding="utf-8"))["reading"]
    client = FakeClient([json_reply(reading, u=usage(1_000, 300))])
    events = list(read_notice(Session(), str(SAMPLE_NOTICE), settings=s, agent=TenantAgent(s, client=client),
                              budget=budget))
    assert events[-1].detail["reading"] is not None and len(client.calls) == 1
    assert budget.spent() > 0


# ---- the website -------------------------------------------------------------------

def test_website_switches_to_demo_mode_when_the_budget_is_used_up(sample_text):
    s = settings()
    budget = DailyBudget(0.01, clock=lambda: TODAY)
    client = FakeClient(analysis_replies())
    app = create_web_app(settings=s, agent=TenantAgent(s, client=client), cache=None, today=lambda: TODAY,
                         budget=budget)
    with TestClient(app) as c:
        token = c.get("/api/meta").json()["session"]
        h = {SESSION_HEADER: token}
        meta = c.get("/api/meta", headers=h).json()
        assert meta["mode"] == "llm" and meta["daily_budget_used_up"] is False
        assert meta["limits"]["max_daily_spend_usd"] == s.max_daily_spend_usd

        budget.add(0.01)   # someone else used today's budget
        meta = c.get("/api/meta", headers=h).json()
        assert meta["mode"] == "demo" and meta["daily_budget_used_up"] is True
        c.post("/api/contract", headers=h, data={"text": sample_text})
        body = c.post("/api/analyze", headers=h, json=FORM).text
    assert '"mode": "demo"' in body or '"mode":"demo"' in body
    assert client.calls == []
