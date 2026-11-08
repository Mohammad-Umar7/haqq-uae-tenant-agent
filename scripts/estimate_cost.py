"""Estimate what one "Analyze my contract" run costs, without an API key.

How it works: we build the REAL payloads the agent would send for the sample
contract (tool schemas, system prompt, the user message, the extraction
request, and the actual tool outputs for the sample) and measure them.
Then we replay a typical 4-round run through the same price table the app's
cost meter uses, once with prompt caching and once without.

Assumptions (edit them below):
- CHARS_PER_TOKEN: Claude Haiku 5.5's tokenizer gives roughly 3 characters
  per token for this kind of English + JSON text. Rough! With an API key,
  run with --count-tokens to get exact prompt sizes from the free
  messages.count_tokens endpoint instead.
- OUTPUT_TOKENS: what Claude writes each round (thinking included).

Usage:  python scripts/estimate_cost.py [--count-tokens]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from agent.contract import LoadedContract  # noqa: E402
from agent.cost import PRICES, format_usd, request_cost_usd  # noqa: E402
from agent.extraction import EXTRACTION_SCHEMA, EXTRACTION_SYSTEM, build_extraction_params  # noqa: E402
from agent.inputs import TenantInputs  # noqa: E402
from agent.prompts import ANALYZE_REQUEST, SYSTEM_PROMPT, build_user_message  # noqa: E402
from agent.registry import TOOL_SCHEMAS, ToolContext, execute_tool  # noqa: E402

MODEL = "claude-haiku-5-5"
CHARS_PER_TOKEN = 3.0
TODAY = date(2026, 11, 8)
INPUTS = TenantInputs(rera_average_rent=100_000, proposed_new_rent=93_500, notice_date="2026-11-01",
                      notice_type="rent_increase", delivery_method="email")
# Output tokens per API call, thinking included (medium effort; extraction at low effort).
OUTPUT_TOKENS = {"extraction": 1_200, "round1": 250, "round2": 300, "round3": 500, "round4": 1_200}
TOOL_CALL_OVERHEAD = 60   # tokens for each tool_use / tool_result wrapper


def tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def payloads() -> dict[str, int]:
    """Token sizes of everything the run sends, measured from the real objects."""
    sample = (PROJECT / "samples" / "sample_contract.txt").read_text(encoding="utf-8")
    contract = LoadedContract.from_text(sample, "sample")
    ctx = ToolContext(contract_text=contract.text, today=TODAY)

    def tool_out(name: str, args: dict) -> int:
        return tokens(execute_tool(name, args, ctx).as_json()) + TOOL_CALL_OVERHEAD

    extraction = build_extraction_params(contract, MODEL)
    sizes = {
        "tools": tokens(json.dumps(TOOL_SCHEMAS)),
        "system": tokens(SYSTEM_PROMPT),
        "user": tokens(build_user_message(ANALYZE_REQUEST, INPUTS, True, len(contract.text), TODAY)),
        "extraction_input": tokens(EXTRACTION_SYSTEM) + tokens(json.dumps(EXTRACTION_SCHEMA))
        + tokens(json.dumps(extraction["messages"])),
        # Round 1 asks for these two; their results go into round 2's prompt.
        "results1": tool_out("extract_contract_terms", {}) + tool_out("clause_risk_scan", {}),
        "results2": tool_out("rera_rent_increase_check", {"current_annual_rent": 85_000, "rera_average_rent": 100_000,
                                                          "proposed_new_rent": 93_500})
        + tool_out("notice_deadline_check", {"contract_end_date": "2027-01-14", "notice_type": "rent_increase",
                                             "notice_date": "2026-11-01", "delivery_method": "email"}),
        "results3": tool_out("search_tenancy_law", {"query": "90 days notice to change the rent"})
        + tool_out("search_tenancy_law", {"query": "security deposit refund"})
        + tool_out("draft_letter", {"letter_type": "rent_increase_objection",
                                    "facts": ["My rent is AED 85,000.", "You proposed AED 93,500."],
                                    "rule_ids": ["rent_increase_caps", "notice_change_terms_90_days"]}),
    }
    return sizes


def simulate(sizes: dict[str, int], cached: bool, warm_prefix: bool = False) -> tuple[float, int, float]:
    """Replay extraction + 4 agent rounds. Returns (USD, total tokens, share of prompt tokens from cache).

    warm_prefix: tools + system were already cached by someone else's run in
    the last 5 minutes, so round 1 reads them instead of writing them.
    """
    price = PRICES[MODEL]
    total_cost, total_tokens, prompt_total, read_total = 0.0, 0, 0, 0

    # The extraction call: a one-off prompt, deliberately not cached.
    ext = {"input_tokens": sizes["extraction_input"], "output_tokens": OUTPUT_TOKENS["extraction"],
           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    total_cost += request_cost_usd(ext, price)
    total_tokens += sizes["extraction_input"] + OUTPUT_TOKENS["extraction"]
    prompt_total += sizes["extraction_input"]

    # Agent loop. Each round re-sends everything before it.
    prefix = sizes["tools"] + sizes["system"]
    new_input = [prefix + sizes["user"],
                 OUTPUT_TOKENS["round1"] + sizes["results1"],
                 OUTPUT_TOKENS["round2"] + sizes["results2"],
                 OUTPUT_TOKENS["round3"] + sizes["results3"]]
    already_cached = 0
    for i, added in enumerate(new_input, start=1):
        if cached:
            # Everything sent before is read from the cache; only the new
            # part is written (the automatic breakpoint sits at the end).
            read, write = already_cached, added
            if i == 1 and warm_prefix:
                read, write = prefix, added - prefix
            u = {"input_tokens": 0, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": write, "output_tokens": OUTPUT_TOKENS[f"round{i}"]}
            already_cached += added
        else:
            already_cached += added
            u = {"input_tokens": already_cached, "cache_read_input_tokens": 0,
                 "cache_creation_input_tokens": 0, "output_tokens": OUTPUT_TOKENS[f"round{i}"]}
        read_total += u["cache_read_input_tokens"]
        prompt_total += already_cached
        total_cost += request_cost_usd(u, price)
        total_tokens += already_cached + u["output_tokens"]
    return total_cost, total_tokens, read_total / prompt_total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count-tokens", action="store_true", help="use the API's count_tokens for the agent prefix")
    args = parser.parse_args()
    sizes = payloads()
    if args.count_tokens:
        import anthropic

        client = anthropic.Anthropic()
        counted = client.messages.count_tokens(model=MODEL, system=SYSTEM_PROMPT, tools=TOOL_SCHEMAS,
                                               messages=[{"role": "user", "content": "x"}])
        sizes["tools"], sizes["system"] = counted.input_tokens, 0
        print(f"count_tokens: tools + system = {counted.input_tokens:,} tokens")

    with_cache, tokens_c, share_c = simulate(sizes, cached=True)
    warm, _, share_w = simulate(sizes, cached=True, warm_prefix=True)
    without_cache, tokens_n, _ = simulate(sizes, cached=False)
    print("Estimated cost of one 'Analyze my contract' run on the sample (claude-haiku-5-5):\n")
    print("| Part | Tokens (approx.) |\n|---|---|")
    for key, value in sizes.items():
        print(f"| {key} | {value:,} |")
    print()
    print("| Scenario | Cost | Tokens | Prompt from cache |\n|---|---|---|---|")
    print(f"| Prompt caching on, first run (as shipped) | {format_usd(with_cache)} | {tokens_c:,} | {share_c:.0%} |")
    print(f"| Prompt caching on, tools + system already warm | {format_usd(warm)} | {tokens_c:,} | {share_w:.0%} |")
    print(f"| Prompt caching off (for comparison) | {format_usd(without_cache)} | {tokens_n:,} | 0% |")
    print("| Same request again (local result cache) | $0.00 | 0 | - |")
    print(f"\nRuns per US dollar with caching: about {int(1 / with_cache):,}.")


if __name__ == "__main__":
    main()
