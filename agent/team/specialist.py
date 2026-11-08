"""One focused Claude call for a specialist (the Critic or the Drafter).

A specialist is not a second agent loop: it is a single request with a
small role card, no tools, and a JSON reply format. That keeps it cheap and
predictable. What makes it part of a team is what it shares:

- the same client, model and settings as the Orchestrator;
- the same UsageMeter, so the per-run spend limit covers every agent;
- the same prompt-cache prefix as the other specialist (see prompts.py).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from agent.config import Settings
from agent.cost import UsageMeter, format_usd, usage_dict
from agent.team.checks import OBJECTION_KINDS, SEVERITIES, Objection
from agent.team.prompts import TEAM_REPLY_SCHEMA, TEAM_SYSTEM, case_block

TEAM_MAX_TOKENS = 16_000     # thinking counts toward it
MAX_OBJECTIONS = 6
# Focused tasks need less thinking than the Orchestrator's open-ended job.
# Measured on the sample: at "medium" the Critic alone thought for ~17K
# tokens ($0.009); "low" keeps the review sharp for a fraction of that.
# Both specialists must use the same effort, or they stop sharing the cache.
TEAM_EFFORT = "low"


class SpendLimitReached(Exception):
    """The run already spent HAQQ_MAX_COST_PER_RUN_USD, so the specialist was not called."""


class SpecialistError(Exception):
    """The specialist's reply could not be used (refusal, cut off, invalid JSON)."""


class _Objection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    severity: str
    quote: str
    problem: str
    fix: str

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        return value if value in OBJECTION_KINDS else "other"

    @field_validator("severity")
    @classmethod
    def _severity(cls, value: str) -> str:
        return value if value in SEVERITIES else "medium"


class TeamReply(BaseModel):
    """The validated reply. Same shape for the Critic and the Drafter."""

    model_config = ConfigDict(extra="forbid")
    objections: list[_Objection]
    letter_english: str | None
    letter_arabic: str | None
    changes_made: list[str]

    def critic_objections(self) -> list[Objection]:
        return [Objection(o.kind, o.severity, o.quote, o.problem, o.fix, "Critic") for o in self.objections[:MAX_OBJECTIONS]]


@dataclass
class SpecialistReply:
    reply: TeamReply
    cost_usd: float
    duration_ms: int
    usage: dict[str, int]
    thinking: list[str] = field(default_factory=list)

    @property
    def cost_line(self) -> str:
        """'$0.0006; read 2,950 prompt tokens from the cache' for the trace."""
        read, wrote = self.usage.get("cache_read_input_tokens", 0), self.usage.get("cache_creation_input_tokens", 0)
        if read:
            cache = f"read {read:,} prompt tokens from the cache"
        elif wrote:
            cache = f"wrote {wrote:,} prompt tokens to the cache for the next specialist"
        else:
            cache = "no cache use"
        return f"{format_usd(self.cost_usd)}; {cache}"


def team_request(settings: Settings, case_text: str, letter: str, task: str) -> dict[str, Any]:
    """Keyword arguments for messages.create. Everything up to the cache
    marker is identical for the Critic and the Drafter; only `task` differs."""
    return {
        "model": settings.model,
        "max_tokens": TEAM_MAX_TOKENS,
        "system": TEAM_SYSTEM,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": case_block(case_text, letter), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": task},
            ],
        }],
        # Same thinking and effort for both roles: changing them would change the cached prefix.
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": TEAM_EFFORT, "format": {"type": "json_schema", "schema": TEAM_REPLY_SCHEMA}},
    }


def ask_specialist(client: Any, settings: Settings, meter: UsageMeter, agent: str,
                   case_text: str, letter: str, task: str) -> SpecialistReply:
    """Make the call, record its cost under `agent`, and validate the JSON."""
    if meter.guard_cost_usd >= settings.max_cost_per_run_usd:
        raise SpendLimitReached(
            f"Spent {format_usd(meter.guard_cost_usd)} of the {format_usd(settings.max_cost_per_run_usd)} limit.")
    started = time.perf_counter()
    response = client.messages.create(**team_request(settings, case_text, letter, task))
    cost = meter.add(getattr(response, "usage", None), agent=agent)   # billed even if unusable
    duration = int((time.perf_counter() - started) * 1000)

    if response.stop_reason == "refusal":
        raise SpecialistError(f"The {agent} declined this request.")
    if response.stop_reason == "max_tokens":
        raise SpecialistError(f"The {agent}'s reply was cut off (max_tokens).")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise SpecialistError(f"The {agent}'s reply had no JSON.")
    try:
        reply = TeamReply.model_validate_json(text)
    except ValidationError as exc:
        raise SpecialistError(f"The {agent}'s JSON failed validation ({exc.error_count()} problem(s)).") from exc
    thinking = [b.thinking for b in response.content if getattr(b, "type", None) == "thinking" and getattr(b, "thinking", "")]
    return SpecialistReply(reply, cost, duration, usage_dict(getattr(response, "usage", None)), thinking)
