"""Token accounting and the cost meter.

Every Claude response has a `usage` block with four counters:

    input_tokens                  prompt tokens billed at the full input price
    cache_read_input_tokens       prompt tokens served from the prompt cache (0.1x)
    cache_creation_input_tokens   prompt tokens written to the cache (1.25x, 5-minute TTL)
    output_tokens                 everything the model wrote, thinking included

The total prompt size is the sum of the first three. We add the counters up
over every API call in one analysis and turn them into US dollars with the
small price table below.

Prices are per million tokens (MTok) and come from Anthropic's published
pricing (checked 2026-11-08). Check https://platform.claude.com/docs/en/about-claude/pricing
before relying on them for budgeting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Multipliers on the base input price (same for every current model,
# except where a model overrides cache_read below).
CACHE_READ_MULTIPLIER = 0.10       # cache hits cost 10% of the input price
CACHE_WRITE_5M_MULTIPLIER = 1.25   # writing a 5-minute cache entry costs 125%
CACHE_WRITE_1H_MULTIPLIER = 2.00   # 1-hour entries cost 200% (not used by this app)
BATCH_DISCOUNT = 0.50              # the Message Batches API halves every price


@dataclass(frozen=True)
class Price:
    input: float                         # USD per MTok of uncached input
    output: float                        # USD per MTok of output
    cache_read_multiplier: float = CACHE_READ_MULTIPLIER
    # Claude Haiku 5.5 has a second rate card for prompts over 100K tokens.
    long_prompt_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None


PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, long_prompt_threshold=100_000, long_input=0.50, long_output=2.50),
    "claude-sonnet-5-5": Price(2.00, 10.00),
    "claude-opus-5-5": Price(4.00, 20.00, cache_read_multiplier=0.05),
    "claude-haiku-4-5": Price(1.00, 5.00),
    "claude-sonnet-5": Price(2.00, 10.00),
    "claude-opus-5": Price(5.00, 25.00),
}

# Used by the spend guard when the model isn't in the table: deliberately
# expensive, so an unknown model stops early rather than late.
CONSERVATIVE_PRICE = Price(10.00, 50.00)

_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def usage_dict(usage: Any) -> dict[str, int]:
    """Read the four counters from an SDK Usage object (or a dict)."""
    if usage is None:
        return {f: 0 for f in _FIELDS}
    if isinstance(usage, dict):
        return {f: int(usage.get(f) or 0) for f in _FIELDS}
    return {f: int(getattr(usage, f, 0) or 0) for f in _FIELDS}


def request_cost_usd(usage: Any, price: Price, batch: bool = False) -> float:
    """Cost of ONE API response. The long-prompt rate card is chosen per request."""
    u = usage_dict(usage)
    prompt_tokens = u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
    input_price, output_price = price.input, price.output
    if price.long_prompt_threshold is not None and prompt_tokens > price.long_prompt_threshold:
        input_price, output_price = price.long_input, price.long_output
    dollars = (
        u["input_tokens"] * input_price
        + u["cache_read_input_tokens"] * input_price * price.cache_read_multiplier
        + u["cache_creation_input_tokens"] * input_price * CACHE_WRITE_5M_MULTIPLIER
        + u["output_tokens"] * output_price
    ) / 1_000_000
    return dollars * (BATCH_DISCOUNT if batch else 1.0)


def format_usd(amount: float) -> str:
    """$0.0031 for small amounts, $0.12 for larger ones."""
    if amount == 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.4f}"
    if amount < 1:
        return f"${amount:.3f}"
    return f"${amount:,.2f}"


@dataclass
class UsageMeter:
    """Adds up usage and cost over every API call in one analysis."""

    model: str
    batch: bool = False
    totals: dict[str, int] = field(default_factory=lambda: {f: 0 for f in _FIELDS})
    requests: int = 0
    cost_usd: float = 0.0           # exact, using the price table
    guard_cost_usd: float = 0.0     # what the spend guard compares against

    @property
    def price(self) -> Price | None:
        return PRICES.get(self.model)

    def add(self, usage: Any) -> None:
        """Record one API response's usage."""
        u = usage_dict(usage)
        for f in _FIELDS:
            self.totals[f] += u[f]
        self.requests += 1
        price = self.price
        if price is not None:
            cost = request_cost_usd(u, price, self.batch)
            self.cost_usd += cost
            self.guard_cost_usd += cost
        else:
            self.guard_cost_usd += request_cost_usd(u, CONSERVATIVE_PRICE, self.batch)

    def add_meter(self, other: "UsageMeter") -> None:
        for f in _FIELDS:
            self.totals[f] += other.totals[f]
        self.requests += other.requests
        self.cost_usd += other.cost_usd
        self.guard_cost_usd += other.guard_cost_usd

    @property
    def prompt_tokens(self) -> int:
        t = self.totals
        return t["input_tokens"] + t["cache_read_input_tokens"] + t["cache_creation_input_tokens"]

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.totals["output_tokens"]

    @property
    def cache_share(self) -> float:
        """Fraction of prompt tokens that were read from the cache (0..1)."""
        return self.totals["cache_read_input_tokens"] / self.prompt_tokens if self.prompt_tokens else 0.0

    def summary(self) -> str:
        if self.requests == 0:
            return "This analysis cost $0.00 (no API calls)."
        cost = format_usd(self.cost_usd) if self.price is not None else "an unknown amount (model not in the price table)"
        return (
            f"This analysis cost about {cost} ({self.total_tokens:,} tokens, "
            f"{round(self.cache_share * 100)}% from cache)."
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.totals,
            "requests": self.requests,
            "cost_usd": round(self.cost_usd, 6) if self.price is not None else None,
            "cache_share": round(self.cache_share, 3),
        }
