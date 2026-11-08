"""The agent's step-by-step record, shown in the "What the agent did" panel.

Both the LLM loop and demo mode emit the same TraceEvent objects, so the UI
renders them the same way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal["info", "thinking", "assistant_text", "tool_call", "tool_result", "error", "final"]


@dataclass
class TraceEvent:
    kind: EventKind
    title: str
    detail: Any = None            # tool input/output, text, etc.
    is_error: bool = False
    rule_ids: list[str] = field(default_factory=list)
    duration_ms: int | None = None


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


def render_trace_markdown(events: list[TraceEvent]) -> str:
    """Turn events into numbered markdown steps."""
    if not events:
        return "_Nothing yet. Load a contract and click **Analyze my contract**, or ask a question._"

    lines: list[str] = []
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
        if ev.kind == "tool_call":
            lines.append(f"**{step}. Called tool `{ev.title}`**{timing}")
            if ev.detail:
                lines.append("Input:\n" + _json_block(ev.detail))
            else:
                lines.append("Input: none (the tool reads the loaded contract)")
        elif ev.kind == "tool_result":
            label = "Tool error" if ev.is_error else "Result"
            lines.append(f"**{step}. {label} from `{ev.title}`**{timing}")
            lines.append(_json_block(ev.detail))
        elif ev.kind == "thinking":
            lines.append(f"**{step}. Reasoning (summary)**")
            lines.append(_quote(str(ev.detail or "")))
        elif ev.kind == "assistant_text":
            lines.append(f"**{step}. Agent note**")
            lines.append(_quote(str(ev.detail or "")))
        elif ev.kind == "error":
            lines.append(f"**{step}. Problem:** {ev.title}")
            if ev.detail:
                lines.append(_quote(str(ev.detail)))
        else:  # info
            lines.append(f"**{step}. {ev.title}**")
            if ev.detail:
                lines.append(str(ev.detail))
        lines.append("")

    if rules_seen:
        lines.append("**Rules consulted:** " + ", ".join(f"`{r}`" for r in rules_seen))
    return "\n".join(lines)
