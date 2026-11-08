"""The agents' step-by-step record, shown in the "What the agents did" panel.

Both the LLM loop and demo mode emit the same TraceEvent objects, so the UI
renders them the same way. Every event names the agent that did the work
(see agent/roster.py), so the panel shows who did what.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from agent.roster import ORCHESTRATOR

EventKind = Literal["info", "thinking", "assistant_text", "tool_call", "tool_result", "error", "final"]


@dataclass
class TraceEvent:
    kind: EventKind
    title: str
    detail: Any = None            # tool input/output, text, etc.
    is_error: bool = False
    rule_ids: list[str] = field(default_factory=list)
    duration_ms: int | None = None
    agent: str = ORCHESTRATOR     # which member of the team did this step


def _json_block(value: Any, max_chars: int = 2500) -> str:
    text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated for display)"
    return f"```json\n{text}\n```"


def _quote(text: str, max_chars: int = 800) -> str:
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars] + " ..."
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _team_line(events: list[TraceEvent]) -> str:
    """'Team at work: Orchestrator (5 steps), Contract Analyst (3 steps), ...' in order of first appearance."""
    counts: dict[str, int] = {}
    for ev in events:
        if ev.kind != "final":
            counts[ev.agent] = counts.get(ev.agent, 0) + 1
    if len(counts) <= 1:
        return ""
    return "**Team at work:** " + ", ".join(f"{name} ({n} step{'s' if n > 1 else ''})" for name, n in counts.items())


def latest_letter(events: list[TraceEvent]) -> dict[str, Any] | None:
    """The newest letter the drafting team produced (the draft_letter tool result), if any."""
    for ev in reversed(events):
        if ev.kind == "tool_result" and ev.title == "draft_letter" and not ev.is_error and isinstance(ev.detail, dict):
            if ev.detail.get("body"):
                return ev.detail
    return None


def render_trace_markdown(events: list[TraceEvent]) -> str:
    """Turn events into numbered markdown steps, each labelled with its agent."""
    if not events:
        return "_Nothing yet. Load a contract and click **Analyze my contract**, or ask a question._"

    lines: list[str] = []
    team = _team_line(events)
    if team:
        lines += [team, ""]
    step = 0
    rules_seen: list[str] = []
    for ev in events:
        for r in ev.rule_ids:
            if r not in rules_seen:
                rules_seen.append(r)
        if ev.kind == "final":
            continue
        step += 1
        timing = f" ({ev.duration_ms} ms)" if ev.duration_ms is not None else ""
        who = f"[{ev.agent}]"
        if ev.kind == "tool_call":
            lines.append(f"**{step}. {who} Called tool `{ev.title}`**{timing}")
            if ev.detail:
                lines.append("Input:\n" + _json_block(ev.detail))
            else:
                lines.append("Input: none (the tool reads the loaded contract)")
        elif ev.kind == "tool_result":
            label = "Tool error" if ev.is_error else "Result"
            lines.append(f"**{step}. {who} {label} from `{ev.title}`**{timing}")
            lines.append(_json_block(ev.detail))
        elif ev.kind == "thinking":
            lines.append(f"**{step}. {who} Reasoning (summary)**")
            lines.append(_quote(str(ev.detail or "")))
        elif ev.kind == "assistant_text":
            lines.append(f"**{step}. {who} Note**")
            lines.append(_quote(str(ev.detail or "")))
        elif ev.kind == "error":
            lines.append(f"**{step}. {who} Problem:** {ev.title}")
            if ev.detail:
                lines.append(_quote(str(ev.detail)))
        else:  # info
            lines.append(f"**{step}. {who} {ev.title}**")
            if ev.detail:
                lines.append(str(ev.detail))
        lines.append("")

    if rules_seen:
        lines.append("**Rules consulted:** " + ", ".join(f"`{r}`" for r in rules_seen))
    return "\n".join(lines)
