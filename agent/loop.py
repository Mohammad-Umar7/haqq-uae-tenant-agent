"""The agent loop: Claude decides which tools to call, we run them, repeat.

One "round" looks like this:

    1. Send the conversation (+ tool definitions) to Claude.
    2. Claude replies. If stop_reason == "tool_use", the reply contains one
       or more tool_use blocks: "please run tool X with arguments Y".
    3. We run every requested tool and send the results back in ONE user
       message made of tool_result blocks (matched by tool_use_id).
    4. Go to 1. When Claude answers without asking for a tool
       (stop_reason == "end_turn"), that text is the final answer.

The loop is written by hand (instead of the SDK's beta tool runner) so you
can see every step, and so we can yield a trace event after each one for
the UI to display live.

Cost control lives here too: a UsageMeter adds up `usage` from every
response, and the loop stops gracefully once a run has spent
Settings.max_cost_per_run_usd (checked before each new API call).

This loop is the Orchestrator of a small team (agent/roster.py). Most tools
are plain code, but draft_letter is delegated to the drafting team
(agent/team/drafting.py: Drafter -> Critic -> Drafter), which makes its own
focused Claude calls on the same meter, so the spend limit covers them too.
Before the answer goes out, the Law Researcher checks its citations.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import anthropic

from agent.config import Settings
from agent.cost import UsageMeter, format_usd, usage_dict
from agent.prompts import SYSTEM_BLOCKS
from agent.registry import TOOL_SCHEMAS, ToolContext, execute_tool
from agent.roster import LAW_RESEARCHER, agent_for_tool
from agent.team.checks import audit_citations
from agent.team.drafting import run_drafting_team
from agent.trace import TraceEvent

MAX_TOKENS = 16_000  # per response; thinking counts toward it
log = logging.getLogger("haqq.cost")


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _close_dangling_tool_calls(messages: list[dict[str, Any]]) -> None:
    """If a previous run was interrupted right after Claude asked for tools,
    the history ends with tool_use blocks that never got results. The API
    rejects that, so append error results to close them (append-only: we
    never edit earlier messages).
    """
    if not messages or messages[-1].get("role") != "assistant":
        return
    pending = [b for b in messages[-1]["content"] if getattr(b, "type", None) == "tool_use"]
    if pending:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": b.id, "content": "Interrupted before it ran.", "is_error": True}
                    for b in pending
                ],
            }
        )


class TenantAgent:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        # `client` can be injected (tests pass a fake). Otherwise build the
        # real SDK client; it retries 429/5xx errors twice by itself.
        self.client = client or anthropic.Anthropic(api_key=settings.api_key, max_retries=2, timeout=120.0)

    def _call_claude(self, messages: list[dict[str, Any]]) -> Any:
        return self.client.messages.create(
            model=self.settings.model,
            max_tokens=MAX_TOKENS,
            # Prompt caching, two breakpoints:
            # 1. SYSTEM_BLOCKS carries an explicit marker: tools + system are
            #    the same for every user, so they are cached once and read
            #    back at 0.1x the price on every later request.
            # 2. Top-level cache_control moves an automatic marker to the end
            #    of the conversation, so each round re-reads earlier rounds
            #    from the cache instead of paying for them again.
            system=SYSTEM_BLOCKS,
            tools=TOOL_SCHEMAS,
            messages=messages,
            # Adaptive thinking: the model decides how much to think.
            # "summarized" returns a readable summary we show in the trace.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.settings.effort},
            cache_control={"type": "ephemeral"},
        )

    def _finish(self, text: str, meter: UsageMeter) -> Iterator[TraceEvent]:
        """The last events of every run: cost by agent, the cost line, then the answer."""
        summary = meter.summary()
        log.info("%s model=%s usage=%s", summary, self.settings.model, meter.as_dict())
        if len(meter.by_agent) > 1:
            yield TraceEvent("info", "Cost by agent", detail=meter.by_agent_markdown())
        yield TraceEvent("info", "Cost", detail=summary)
        yield TraceEvent(
            "final", "answer",
            detail={"text": text, "usage": dict(meter.totals), "cost": meter.as_dict(), "cost_summary": summary},
        )

    def run(
        self,
        messages: list[dict[str, Any]],
        user_content: str,
        ctx: ToolContext,
        meter: UsageMeter | None = None,
    ) -> Iterator[TraceEvent]:
        """Add the tenant's new message to `messages` and run the loop.

        `messages` is the whole conversation so far and is extended in place,
        so the next chat turn continues where this one ended. `meter` may
        already hold the cost of the contract extraction for this run. Yields
        TraceEvents as it goes; the last event is always kind="final" with
        detail={"text": ..., "usage": ..., "cost": ..., "cost_summary": ...}.
        """
        meter = meter or UsageMeter(self.settings.model)
        _close_dangling_tool_calls(messages)
        messages.append({"role": "user", "content": user_content})
        notes: list[str] = []   # text Claude wrote between tool calls (shown if we stop early)

        for round_number in range(1, self.settings.max_tool_rounds + 1):
            limit = self.settings.max_cost_per_run_usd
            if meter.guard_cost_usd >= limit:
                text = (f"I stopped because this analysis reached its spend limit of {format_usd(limit)} "
                        "(HAQQ_MAX_COST_PER_RUN_USD). Try a narrower question.")
                if notes:
                    text += "\n\nWhat I had so far:\n\n" + "\n\n".join(notes)
                yield TraceEvent("error", "Spend limit reached", detail=f"Spent {format_usd(meter.guard_cost_usd)} of {format_usd(limit)}.", is_error=True)
                yield from self._finish(text, meter)
                return

            started = time.perf_counter()
            try:
                response = self._call_claude(messages)
            except anthropic.APIError as exc:
                message = _describe_api_error(exc, self.settings.model)
                yield TraceEvent("error", "Claude API call failed", detail=message, is_error=True)
                yield from self._finish(message, meter)
                return

            round_cost = meter.add(getattr(response, "usage", None))
            u = usage_dict(getattr(response, "usage", None))
            prompt = u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
            yield TraceEvent(
                "info",
                f"Round {round_number}: Claude replied (stop_reason={response.stop_reason})",
                detail=(f"This round cost {format_usd(round_cost)}: {prompt:,} prompt tokens "
                        f"({u['cache_read_input_tokens']:,} from the cache), {u['output_tokens']:,} output tokens."),
                duration_ms=_ms(started),
            )

            # Show reasoning summaries and any text written before tool calls.
            for block in response.content:
                if block.type == "thinking" and getattr(block, "thinking", ""):
                    yield TraceEvent("thinking", "reasoning", detail=block.thinking)
                elif block.type == "text" and response.stop_reason == "tool_use" and block.text.strip():
                    notes.append(block.text.strip())
                    yield TraceEvent("assistant_text", "note", detail=block.text)

            if response.stop_reason == "refusal":
                text = ("Claude declined to answer this request. Try rephrasing it, "
                        "or contact RERA/RDSC directly.")
                yield TraceEvent("error", "Request declined (stop_reason=refusal)", is_error=True)
                yield from self._finish(text, meter)
                return

            if response.stop_reason == "max_tokens":
                text = "The answer was cut off (it hit the length limit). Please ask a narrower question."
                yield TraceEvent("error", "Response hit max_tokens", is_error=True)
                yield from self._finish(text, meter)
                return

            # Keep Claude's whole reply, including thinking blocks, unchanged.
            # The API needs them back exactly as they were sent.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "pause_turn":
                continue  # only happens with server-side tools; just resend

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                final_text = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
                final_text = yield from self._check_citations(final_text, ctx)
                yield from self._finish(final_text or "(no answer)", meter)
                return

            # Run every requested tool. Claude may ask for several at once
            # (parallel tool use); all results go back in ONE user message.
            # Each tool belongs to a specialist on the team (agent/roster.py).
            tool_results = []
            for block in tool_uses:
                agent = agent_for_tool(block.name)
                yield TraceEvent("tool_call", block.name, detail=block.input, agent=agent)
                t0 = time.perf_counter()
                if block.name == "draft_letter":
                    # Delegated: the drafting team yields its own trace events
                    # and returns the reviewed letter as the tool's outcome.
                    outcome = yield from run_drafting_team(block.input, ctx, self.client, self.settings, meter)
                else:
                    outcome = execute_tool(block.name, block.input, ctx)
                if not outcome.is_error:
                    ctx.record(block.name, outcome.output)    # evidence for the Critic
                yield TraceEvent(
                    "tool_result",
                    block.name,
                    detail=outcome.output,
                    is_error=outcome.is_error,
                    rule_ids=outcome.rule_ids,
                    duration_ms=_ms(t0),
                    agent=agent,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome.as_json(),
                        "is_error": outcome.is_error,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        text = (f"I stopped after {self.settings.max_tool_rounds} rounds of tool calls without a final answer. "
                "Try asking a more specific question.")
        yield TraceEvent("error", "Reached the tool-round limit", is_error=True)
        yield from self._finish(text, meter)


    def _check_citations(self, text: str, ctx: ToolContext) -> Iterator[TraceEvent]:
        """The Law Researcher's last job: every law citation in the answer must
        have come back from a tool this session. Returns the (annotated) text."""
        audit = audit_citations(text, ctx.evidence)
        if not audit["checked"]:
            return text
        if audit["not_found"]:
            yield TraceEvent("info", f"Citation check: {len(audit['not_found'])} of {audit['checked']} citation(s) "
                             "were not returned by the law search", detail=audit, agent=LAW_RESEARCHER)
            missing = "; ".join(f"[{c}]" for c in audit["not_found"])
            return (text + f"\n\n_Citation check: {missing} did not come from this session's law search. "
                    "Verify it against the official text before relying on it._")
        yield TraceEvent("info", f"Citation check: all {audit['checked']} citation(s) in the answer came from "
                         "this session's law search", detail=audit, agent=LAW_RESEARCHER)
        return text


def _describe_api_error(exc: anthropic.APIError, model: str) -> str:
    """A friendly message for each kind of failure (most specific first)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY, or remove it to use demo mode."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key isn't allowed to use this model or feature."
    if isinstance(exc, anthropic.NotFoundError):
        return f"Model '{model}' was not found. Check the CLAUDE_MODEL setting."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limit reached. Wait a minute and try again."
    if isinstance(exc, anthropic.BadRequestError):
        return f"The API rejected the request: {exc.message}"
    if isinstance(exc, anthropic.APIStatusError):
        return f"The Claude API returned an error ({exc.status_code}). Try again shortly."
    if isinstance(exc, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API. Check your internet connection."
    return f"Unexpected API error: {exc}"


def run_to_completion(
    agent: TenantAgent, messages: list[dict[str, Any]], user_content: str, ctx: ToolContext
) -> tuple[str, list[TraceEvent]]:
    """Convenience wrapper: run the loop and return (final_text, events)."""
    events = list(agent.run(messages, user_content, ctx))
    final = events[-1]
    return final.detail["text"], events
