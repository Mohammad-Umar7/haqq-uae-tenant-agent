"""One full turn (read once -> agent loop -> cost -> cache) with a fake Claude."""

import json
from datetime import date

import pytest
from fakes import SAMPLE_EXTRACTION, FakeClient, json_reply, make_pdf, reply, text, tool, usage

from agent.config import Settings
from agent.contract import LoadedContract
from agent.inputs import TenantInputs
from agent.loop import TenantAgent
from agent.result_cache import ResultCache, make_key
from agent.runner import Session, run_turn

TODAY = date(2026, 11, 8)
INPUTS = TenantInputs(rera_average_rent=100_000, proposed_new_rent=93_500, notice_date="2026-11-01",
                      notice_type="rent_increase", delivery_method="email")


def settings(**overrides):
    base = dict(api_key="test", model="claude-haiku-5-5", effort="medium", max_tool_rounds=6)
    return Settings(**{**base, **overrides})


def make(replies, **overrides):
    s = settings(**overrides)
    client = FakeClient(replies)
    return TenantAgent(s, client=client), client, s


def turn(session, agent, s, cache=None, question="Analyze my contract."):
    events = list(run_turn(session, question, analyze=True, inputs=INPUTS, today=TODAY,
                           settings=s, agent=agent, cache=cache))
    return events[-1].detail["text"], events


def analysis_replies():
    """Extraction, then a two-round agent loop."""
    return [
        json_reply(SAMPLE_EXTRACTION, u=usage(1_500, 900)),
        reply("tool_use", tool("t1", "extract_contract_terms", {}), u=usage(200, 80, w=3_000)),
        reply("end_turn", text("The increase is above the cap [Dubai Decree No. 43 of 2013, Art. 1]."),
              u=usage(50, 400, r=3_000, w=2_000)),
    ]


@pytest.fixture
def sample_session(sample_text):
    return Session(contract=LoadedContract.from_text(sample_text, "sample"))


# ---- read once + cost --------------------------------------------------------

def test_contract_is_read_once_then_the_loop_uses_the_json(sample_session):
    agent, client, s = make(analysis_replies())
    final, events = turn(sample_session, agent, s)

    assert "above the cap" in final
    extraction_call, *loop_calls = client.calls
    assert extraction_call["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in extraction_call
    assert all(c["tools"] for c in loop_calls)
    # The tool result is the compact JSON from the extraction, not raw text.
    result = json.loads(loop_calls[1]["messages"][2]["content"][0]["content"])
    assert result["method"].startswith("structured extraction by claude-haiku-5-5")
    assert [e.title for e in events if e.kind == "tool_call"][0] == "read_contract_once"


def test_cost_line_covers_extraction_and_loop(sample_session):
    agent, client, s = make(analysis_replies())
    _, events = turn(sample_session, agent, s)
    final = events[-1].detail
    assert final["usage"]["input_tokens"] == 1_500 + 200 + 50
    assert final["cost"]["requests"] == 3
    # (1750*0.10 + 3000*0.01 + 5000*0.125 + 1380*0.50) / 1e6
    assert final["cost"]["cost_usd"] == pytest.approx((175 + 30 + 625 + 690) / 1e6)
    cost_events = [e for e in events if e.title == "Cost"]
    assert cost_events and cost_events[-1].detail.startswith("This analysis cost about $0.0015 (")
    assert "% from cache)" in cost_events[-1].detail


def test_follow_up_turn_does_not_reread_the_contract(sample_session):
    agent, client, s = make(analysis_replies() + [reply("end_turn", text("Yes."))])
    turn(sample_session, agent, s)
    turn(sample_session, agent, s, question="Is the deposit refundable?")
    assert len(client.calls) == 4                       # 1 extraction + 2 loop + 1 loop
    assert "output_config" in client.calls[3] and "format" not in client.calls[3]["output_config"]


def test_failed_extraction_falls_back_to_regex(sample_session):
    agent, client, s = make([
        reply("end_turn", text("not json")),
        reply("tool_use", tool("t1", "extract_contract_terms", {})),
        reply("end_turn", text("ok")),
    ])
    _, events = turn(sample_session, agent, s)
    assert any(e.title == "Couldn't read the contract with Claude" for e in events)
    result = json.loads(client.calls[2]["messages"][2]["content"][0]["content"])
    assert result["method"] == "regex extraction (deterministic)"


def test_scanned_pdf_is_read_by_claude_and_scanned_for_red_flags():
    pdf = make_pdf([[]])
    session = Session(contract=LoadedContract("scan.pdf", pdf, None, pdf, 1))
    data = {**SAMPLE_EXTRACTION, "clauses": SAMPLE_EXTRACTION["clauses"][1:]}
    agent, client, s = make([
        json_reply(data),
        reply("tool_use", tool("t1", "clause_risk_scan", {})),
        reply("end_turn", text("Two red flags.")),
    ])
    turn(session, agent, s)
    assert client.calls[0]["messages"][0]["content"][0]["type"] == "document"
    scan = json.loads(client.calls[2]["messages"][2]["content"][0]["content"])
    assert {"non_refundable_deposit", "rent_increase_any_time"} <= {f["flag_id"] for f in scan["flags"]}


def test_unreadable_scanned_pdf_stops_before_the_loop():
    pdf = make_pdf([[]])
    session = Session(contract=LoadedContract("scan.pdf", pdf, None, pdf, 1))
    agent, client, s = make([reply("refusal")])
    final, _ = turn(session, agent, s)
    assert "couldn't read this PDF" in final
    assert len(client.calls) == 1


# ---- free repeats ------------------------------------------------------------

def test_identical_request_is_free_the_second_time(tmp_path, sample_text):
    cache = ResultCache(tmp_path / "cache")
    agent, client, s = make(analysis_replies())
    first_text, _ = turn(Session(contract=LoadedContract.from_text(sample_text, "a")), agent, s, cache)

    agent2, client2, _ = make([])                      # no replies: any API call would fail
    session = Session(contract=LoadedContract.from_text(sample_text, "b"))
    second_text, events = turn(session, agent2, s, cache)

    assert second_text == first_text and client2.calls == []
    assert events[0].title == "Answered from the local cache"
    assert events[-2].detail.startswith("This analysis cost $0.00 (answered from the local cache; the first run cost about $0.0015")
    assert session.llm_requests == 0                     # free repeats don't use the allowance
    assert [m["role"] for m in session.messages] == ["user", "assistant"]   # follow-ups keep context


def test_different_question_or_form_misses_the_cache(tmp_path, sample_text):
    cache = ResultCache(tmp_path / "cache")
    agent, client, s = make(analysis_replies() + analysis_replies()[1:])
    turn(Session(contract=LoadedContract.from_text(sample_text, "a")), agent, s, cache)
    turn(Session(contract=LoadedContract.from_text(sample_text, "a")), agent, s, cache, question="Something else?")
    # Second turn: extraction came from the cache, but the answer did not.
    assert len(client.calls) == 5


def test_errors_are_not_cached(tmp_path, sample_text):
    cache = ResultCache(tmp_path / "cache")
    agent, client, s = make([json_reply(SAMPLE_EXTRACTION), reply("refusal")])
    turn(Session(contract=LoadedContract.from_text(sample_text, "a")), agent, s, cache)
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1     # only the extraction


def test_cache_unit_behaviour(tmp_path):
    assert make_key("ab", "c") != make_key("a", "bc")
    cache = ResultCache(tmp_path)
    cache.put("k", {"text": "hi"})
    assert cache.get("k") == {"text": "hi"} and cache.get("missing") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert cache.get("bad") is None
    off = ResultCache(tmp_path / "off", enabled=False)
    off.put("k", {"x": 1})
    assert off.get("k") is None and not (tmp_path / "off").exists()


# ---- spend guards -----------------------------------------------------------

def test_spend_limit_stops_the_loop_gracefully():
    agent, client, s = make([
        reply("tool_use", text("Let me look that up."), tool("t1", "search_tenancy_law", {"query": "deposit"}),
              u=usage(5_000, 2_000)),
        reply("end_turn", text("never reached")),
    ], max_cost_per_run_usd=0.0001)
    session = Session()
    final, events = turn(session, agent, s)
    assert "spend limit of $0.0001" in final and "Let me look that up." in final
    assert len(client.calls) == 1
    assert any(e.title == "Spend limit reached" for e in events)
    # The conversation is still valid: the tool_use got its tool_result.
    assert session.messages[-1]["content"][0]["type"] == "tool_result"


def test_per_session_request_limit():
    agent, client, s = make([reply("end_turn", text("one"))], max_requests_per_session=1)
    session = Session()
    turn(session, agent, s, question="first")
    final, events = turn(session, agent, s, question="second")
    assert "used its 1 questions" in final
    assert len(client.calls) == 1


def test_new_conversation_keeps_the_request_count(sample_session):
    sample_session.llm_requests = 7
    fresh = sample_session.fresh_conversation()
    assert fresh.llm_requests == 7 and fresh.messages == [] and fresh.contract is sample_session.contract


def test_demo_mode_reports_zero_cost(sample_session):
    events = list(run_turn(sample_session, "Analyze", analyze=True, inputs=INPUTS, today=TODAY,
                           settings=settings(api_key=None), agent=None))
    assert events[-2].title == "Cost" and events[-2].detail == "Demo mode: $0.00 (no API calls)."
    assert "exceeds the 5% cap" in events[-1].detail["text"]
