"""Prompt caching only works if the start of every request is byte-identical.

Render order is tools -> system -> messages. These tests pin the cached
prefix (tools + system) so a change that would silently break caching
(a date in the system prompt, an unordered set, a per-user tool list)
fails here instead of quietly raising the bill.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from fakes import FakeClient, reply, text

from agent.config import Settings
from agent.inputs import TenantInputs
from agent.loop import TenantAgent, run_to_completion
from agent.prompts import SYSTEM_BLOCKS, build_user_message
from agent.registry import TOOL_SCHEMAS, ToolContext

PROJECT = Path(__file__).resolve().parent.parent
SETTINGS = Settings(api_key="test", model="claude-haiku-5-5", effort="medium", max_tool_rounds=5)


def prefix_bytes(call: dict) -> bytes:
    """The cacheable prefix of one request, serialized exactly once."""
    return json.dumps({"tools": call["tools"], "system": call["system"]}, ensure_ascii=False).encode("utf-8")


def run_once(question: str, today: date, inputs: TenantInputs, contract: str | None) -> list[dict]:
    client = FakeClient([reply("end_turn", text("ok"))])
    agent = TenantAgent(SETTINGS, client=client)
    content = build_user_message(question, inputs, contract is not None, len(contract or ""), today)
    run_to_completion(agent, [], content, ToolContext(contract_text=contract, today=today))
    return client.calls


def test_tools_and_system_are_byte_identical_across_two_runs(sample_text):
    first = run_once("Is my rent increase legal?", date(2026, 11, 8),
                     TenantInputs(rera_average_rent=100_000, proposed_new_rent=93_500), sample_text)
    second = run_once("Can my landlord keep my deposit?", date(2027, 3, 2), TenantInputs(), None)
    assert prefix_bytes(first[0]) == prefix_bytes(second[0])
    # The varying parts went into the last user message instead.
    assert first[0]["messages"][-1] != second[0]["messages"][-1]


def test_prefix_is_identical_in_fresh_interpreters():
    # A new Python process with a different hash seed catches anything that
    # depends on set/dict iteration order or on import-time randomness.
    code = (
        "import hashlib, json; from agent.registry import TOOL_SCHEMAS; from agent.prompts import SYSTEM_BLOCKS; "
        "print(hashlib.sha256(json.dumps({'tools': TOOL_SCHEMAS, 'system': SYSTEM_BLOCKS}, "
        "ensure_ascii=False).encode('utf-8')).hexdigest())"
    )
    digests = set()
    for seed in ("1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", code], cwd=PROJECT, env=env, capture_output=True, text=True, check=True)
        digests.add(out.stdout.strip())
    here = hashlib.sha256(json.dumps({"tools": TOOL_SCHEMAS, "system": SYSTEM_BLOCKS},
                                     ensure_ascii=False).encode("utf-8")).hexdigest()
    assert digests == {here}


def test_no_dates_or_ids_in_the_cached_prefix(sample_text):
    call = run_once("hi", date(2026, 11, 8), TenantInputs(), sample_text)[0]
    prefix = prefix_bytes(call).decode("utf-8")
    assert not re.search(r"\b20\d\d-\d\d-\d\d\b", prefix)       # no ISO dates
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", prefix)   # no UUIDs
    assert "Today's date: 2026-11-08" in call["messages"][-1]["content"]


def test_cache_breakpoints_and_fixed_tool_order():
    call = run_once("hi", date(2026, 11, 8), TenantInputs(), None)[0]
    assert call["system"] == SYSTEM_BLOCKS
    assert call["system"][-1]["cache_control"] == {"type": "ephemeral"}   # caches tools + system
    assert call["cache_control"] == {"type": "ephemeral"}                 # moving breakpoint for the chat
    assert [t["name"] for t in call["tools"]] == [
        "extract_contract_terms", "clause_risk_scan", "rera_rent_increase_check",
        "notice_deadline_check", "search_tenancy_law", "draft_letter",
    ]


def test_every_round_of_one_run_sends_the_same_prefix(sample_text):
    from fakes import tool
    client = FakeClient([
        reply("tool_use", tool("t1", "extract_contract_terms", {})),
        reply("tool_use", tool("t2", "search_tenancy_law", {"query": "deposit"})),
        reply("end_turn", text("done")),
    ])
    agent = TenantAgent(SETTINGS, client=client)
    run_to_completion(agent, [], "Check my deposit", ToolContext(contract_text=sample_text, today=date(2026, 11, 8)))
    assert len({prefix_bytes(c) for c in client.calls}) == 1
    # Append-only history: each request starts with the previous one's messages.
    for earlier, later in zip(client.calls, client.calls[1:]):
        assert later["messages"][: len(earlier["messages"])] == earlier["messages"]
