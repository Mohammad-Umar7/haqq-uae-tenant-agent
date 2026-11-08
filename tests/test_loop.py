"""Tests for the agent loop, using a fake Claude client (no network, no key).

The fake returns scripted responses shaped like the SDK's objects, so we can
check the loop's bookkeeping: tool results matched by id, sent back in one
message, errors flagged, and the right stop conditions.
"""

import json
from datetime import date
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest

from agent.config import Settings
from agent.loop import TenantAgent, run_to_completion
from agent.registry import TOOL_SCHEMAS, ToolContext


def text(t):
    return NS(type="text", text=t)


def tool(id_, name, input_):
    return NS(type="tool_use", id=id_, name=name, input=input_)


def thinking(t):
    return NS(type="thinking", thinking=t, signature="sig")


def reply(stop_reason, *blocks):
    usage = NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=50, cache_creation_input_tokens=0)
    return NS(stop_reason=stop_reason, content=list(blocks), usage=usage)


class FakeClient:
    """Stands in for anthropic.Anthropic(); `client.messages.create(...)` pops the next scripted reply."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.messages = self  # so client.messages.create works

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_agent(replies, max_rounds=10):
    settings = Settings(api_key="test", model="claude-haiku-5-5", effort="medium", max_tool_rounds=max_rounds)
    client = FakeClient(replies)
    return TenantAgent(settings, client=client), client


@pytest.fixture
def ctx(sample_text):
    return ToolContext(contract_text=sample_text, today=date(2026, 11, 8))


def test_parallel_tool_calls_then_final_answer(ctx):
    agent, client = make_agent([
        reply("tool_use", thinking("Read the contract first."),
              tool("t1", "extract_contract_terms", {}), tool("t2", "clause_risk_scan", {})),
        reply("end_turn", text("Your deposit clause is a red flag.")),
    ])
    messages = []
    final, events = run_to_completion(agent, messages, "Check my contract", ctx)

    assert final == "Your deposit clause is a red flag."
    # user -> assistant(tool_use x2) -> user(tool_result x2) -> assistant(final)
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    results = messages[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert all(r["type"] == "tool_result" and r["is_error"] is False for r in results)
    assert json.loads(results[0]["content"])["terms"]["annual_rent_aed"] == 85000

    kinds = [e.kind for e in events]
    assert kinds.count("tool_call") == 2 and kinds.count("tool_result") == 2
    assert "thinking" in kinds and kinds[-1] == "final"
    assert events[-1].detail["usage"]["input_tokens"] == 200


def test_request_shape(ctx):
    agent, client = make_agent([reply("end_turn", text("Hi"))])
    run_to_completion(agent, [], "Hello", ctx)
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["tools"] == TOOL_SCHEMAS
    assert call["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert call["output_config"] == {"effort": "medium"}
    assert "temperature" not in call
    assert call["messages"][-1] == {"role": "user", "content": "Hello"}


def test_bad_tool_input_is_returned_as_error_and_loop_continues(ctx):
    agent, client = make_agent([
        reply("tool_use", tool("t1", "rera_rent_increase_check", {"current_annual_rent": 85000})),
        reply("end_turn", text("What is the RERA average rent for your unit?")),
    ])
    messages = []
    final, events = run_to_completion(agent, messages, "Is my increase legal?", ctx)
    result = messages[2]["content"][0]
    assert result["is_error"] is True
    assert "rera_average_rent" in result["content"]
    assert final.startswith("What is the RERA average")
    assert any(e.kind == "tool_result" and e.is_error for e in events)


def test_refusal_stops_without_appending(ctx):
    agent, _ = make_agent([reply("refusal")])
    messages = []
    final, _ = run_to_completion(agent, messages, "something", ctx)
    assert "declined" in final
    assert [m["role"] for m in messages] == ["user"]


def test_api_error_gives_friendly_message(ctx):
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    agent, _ = make_agent([error])
    final, events = run_to_completion(agent, [], "hi", ctx)
    assert "internet" in final
    assert any(e.kind == "error" for e in events)


def test_round_limit(ctx):
    agent, client = make_agent(
        [reply("tool_use", tool("a", "search_tenancy_law", {"query": "deposit"})),
         reply("tool_use", tool("b", "search_tenancy_law", {"query": "ejari"}))],
        max_rounds=2,
    )
    final, _ = run_to_completion(agent, [], "loop forever", ctx)
    assert "stopped after 2 rounds" in final
    assert len(client.calls) == 2


def test_interrupted_tool_call_is_closed_before_next_turn(ctx):
    # History ends with a tool_use that never got a result (e.g. the user
    # closed the tab mid-run). The loop must add a result before continuing.
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [tool("old", "clause_risk_scan", {})]},
    ]
    agent, client = make_agent([reply("end_turn", text("ok"))])
    run_to_completion(agent, messages, "second", ctx)
    sent = client.calls[0]["messages"]
    assert sent[2]["content"][0]["tool_use_id"] == "old"
    assert sent[2]["content"][0]["is_error"] is True
    assert sent[3] == {"role": "user", "content": "second"}


def test_conversation_continues_across_turns(ctx):
    agent, client = make_agent([reply("end_turn", text("one")), reply("end_turn", text("two"))])
    messages = []
    run_to_completion(agent, messages, "q1", ctx)
    final, _ = run_to_completion(agent, messages, "q2", ctx)
    assert final == "two"
    assert len(client.calls[1]["messages"]) == 3  # q1, answer one, q2
