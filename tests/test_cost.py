"""The cost meter's math, checked by hand against the price table."""

from types import SimpleNamespace as NS

import pytest

from agent.cost import PRICES, UsageMeter, format_usd, request_cost_usd

HAIKU = PRICES["claude-haiku-5-5"]


def u(i=0, o=0, r=0, w=0):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": r, "cache_creation_input_tokens": w}


def test_haiku_base_prices_per_million_tokens():
    assert request_cost_usd(u(i=100_000), HAIKU) == pytest.approx(0.010)      # $0.10 / MTok
    assert request_cost_usd(u(o=1_000_000), HAIKU) == pytest.approx(0.50)     # $0.50 / MTok


def test_cache_reads_cost_a_tenth_and_writes_125_percent():
    assert request_cost_usd(u(r=80_000), HAIKU) == pytest.approx(80_000 * 0.10 * 0.10 / 1e6)
    assert request_cost_usd(u(w=80_000), HAIKU) == pytest.approx(80_000 * 0.10 * 1.25 / 1e6)


def test_a_mixed_request_by_hand():
    # 2,000 uncached + 10,000 cache-read + 3,000 cache-write input, 1,500 output:
    # (2000*0.10 + 10000*0.01 + 3000*0.125 + 1500*0.50) / 1e6 = 0.001425
    assert request_cost_usd(u(i=2_000, r=10_000, w=3_000, o=1_500), HAIKU) == pytest.approx(0.001425)


def test_prompts_over_100k_tokens_use_the_higher_rate_card():
    # 150,000-token prompt: $0.50 / MTok in, $2.50 / MTok out
    assert request_cost_usd(u(i=150_000, o=1_000), HAIKU) == pytest.approx(0.075 + 0.0025)


def test_the_rate_card_is_chosen_per_request_not_per_run():
    meter = UsageMeter("claude-haiku-5-5")
    meter.add(u(i=60_000))
    meter.add(u(i=60_000))      # 120K in total, but each prompt is under 100K
    assert meter.cost_usd == pytest.approx(0.012)


def test_batches_halve_the_price():
    assert request_cost_usd(u(i=2_000, o=1_500), HAIKU, batch=True) == pytest.approx(0.5 * (0.0002 + 0.00075))


def test_opus_cache_reads_are_cheaper_still():
    assert request_cost_usd(u(r=10_000), PRICES["claude-opus-5-5"]) == pytest.approx(10_000 * 4.00 * 0.05 / 1e6)


def test_meter_adds_rounds_and_writes_the_summary_line():
    meter = UsageMeter("claude-haiku-5-5")
    meter.add(NS(input_tokens=2_000, output_tokens=500, cache_read_input_tokens=None, cache_creation_input_tokens=3_000))
    meter.add(NS(input_tokens=0, output_tokens=1_000, cache_read_input_tokens=10_000, cache_creation_input_tokens=0))
    assert meter.requests == 2
    assert meter.totals == u(i=2_000, o=1_500, r=10_000, w=3_000)
    assert meter.cost_usd == pytest.approx(0.001425)
    # 16,500 tokens in total; 10,000 of 15,000 prompt tokens came from the cache.
    assert meter.summary() == "This analysis cost about $0.0014 (16,500 tokens, 67% from cache)."


def test_unknown_model_shows_unknown_price_but_still_guards():
    meter = UsageMeter("claude-mystery-1")
    meter.add(u(i=1_000, o=1_000))
    assert meter.cost_usd == 0 and meter.guard_cost_usd > 0
    assert "unknown amount" in meter.summary()


def test_no_calls_costs_nothing():
    assert UsageMeter("claude-haiku-5-5").summary() == "This analysis cost $0.00 (no API calls)."


@pytest.mark.parametrize("amount, text", [(0, "$0.00"), (0.00142, "$0.0014"), (0.1234, "$0.123"), (2.5, "$2.50")])
def test_format_usd(amount, text):
    assert format_usd(amount) == text
