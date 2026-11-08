"""One chat turn from start to finish: the glue between the UI and the agent.

app.py calls run_turn() and renders the TraceEvents it yields. Keeping this
out of app.py means it can be tested without Gradio. A turn in LLM mode:

    1. Spend guard: has this browser session used up its request allowance?
    2. Free repeat: was this exact request answered before? Then replay it.
    3. Read the contract once (first turn only): one structured-extraction
       call turns the contract (text or PDF) into compact JSON.
    4. The agent loop (agent/loop.py) answers, using that JSON.
    5. Store the answer in the local cache.

Demo mode (no key) skips all of that and runs the fixed pipeline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import anthropic

from agent.config import Settings
from agent.contract import LoadedContract
from agent.cost import UsageMeter, format_usd
from agent.demo import answer_demo_question, run_demo_analysis
from agent.extraction import (
    EXTRACTION_SCHEMA,
    EXTRACTION_SYSTEM,
    EXTRACTION_VERSION,
    ExtractionError,
    extract_with_claude,
    extraction_from_json,
    render_contract_text,
    to_tool_output,
)
from agent.inputs import TenantInputs
from agent.knowledge import KB_PATH
from agent.law_search import LAW_PATH
from agent.prompts import SYSTEM_PROMPT, build_user_message
from agent.registry import TOOL_SCHEMAS, ToolContext
from agent.result_cache import ResultCache, event_from_dict, event_to_dict, make_key
from agent.roster import CONTRACT_ANALYST, NOTICE_READER
from agent.team.notice_reader import (
    NoticeImageError,
    describe_reading,
    load_notice_image,
    run_notice_reader,
    sample_reading,
)
from agent.team.prompts import CRITIC_TASK, TEAM_REPLY_SCHEMA, TEAM_SYSTEM, drafter_task
from agent.trace import TraceEvent

# Anything that changes how an answer is produced is part of the cache key,
# so editing a prompt, a tool description or the law corpus never serves a
# stale cached answer.
EXTRACTION_FINGERPRINT = make_key(EXTRACTION_VERSION, EXTRACTION_SYSTEM, json.dumps(EXTRACTION_SCHEMA, sort_keys=True))
PROMPT_FINGERPRINT = make_key(
    SYSTEM_PROMPT,
    json.dumps(TOOL_SCHEMAS, sort_keys=True),
    EXTRACTION_FINGERPRINT,
    LAW_PATH.read_bytes(),
    KB_PATH.read_bytes(),
    TEAM_SYSTEM, CRITIC_TASK, drafter_task([], [], []), json.dumps(TEAM_REPLY_SCHEMA, sort_keys=True),
)


@dataclass
class Session:
    """Everything one browser tab remembers between clicks."""

    contract: LoadedContract | None = None
    extraction_output: dict[str, Any] | None = None   # extract_contract_terms output (read once)
    rendered_text: str | None = None                  # text rebuilt from a scanned/Arabic PDF
    messages: list[dict[str, Any]] = field(default_factory=list)   # Claude conversation history
    llm_requests: int = 0                             # for the per-session request limit
    evidence: list[dict[str, Any]] = field(default_factory=list)   # tool results (the Critic's case file)

    @property
    def contract_text(self) -> str | None:
        if self.contract is not None and self.contract.text:
            return self.contract.text
        return self.rendered_text

    def fresh_conversation(self) -> "Session":
        """Same contract (and its extraction), empty chat. The request count carries over."""
        return Session(self.contract, self.extraction_output, self.rendered_text, [], self.llm_requests)


def _final(text: str, cost_line: str, **extra: Any) -> Iterator[TraceEvent]:
    yield TraceEvent("info", "Cost", detail=cost_line)
    yield TraceEvent("final", "answer", detail={"text": text, "cost_summary": cost_line, **extra})


def _summary_of(output: dict[str, Any]) -> dict[str, Any]:
    """A short view of the extraction for the trace panel (clauses omitted)."""
    keys = ("method", "terms", "missing_fields", "llm_flagged_clauses", "cross_check_with_regex",
            "embedded_instructions", "language")
    view = {k: output[k] for k in keys if k in output}
    view["clauses"] = f"{len(output.get('clauses', []))} clauses (sent to the agent, hidden here)"
    return view


def _read_contract_once(session: Session, client: Any, settings: Settings, cache: ResultCache | None,
                        meter: UsageMeter) -> Iterator[TraceEvent]:
    """Step 3. Sets session.extraction_output (or leaves it None on failure)."""
    contract = session.contract
    assert contract is not None
    key = make_key("extract", EXTRACTION_FINGERPRINT, settings.model, contract.raw_bytes)
    cached = cache.get(key) if cache else None
    source = "the PDF (document block)" if contract.needs_document_block else "the contract text"
    yield TraceEvent("tool_call", "read_contract_once", agent=CONTRACT_ANALYST, detail={
        "model": settings.model, "reads": source, "output": "JSON schema (structured outputs)",
        "cached": bool(cached),
    })
    t0 = time.perf_counter()
    if cached:
        extraction = extraction_from_json(cached["extraction"])
    else:
        try:
            extraction = extract_with_claude(client, contract, settings.model, meter=meter)
        except (ExtractionError, anthropic.APIError) as exc:
            detail = str(exc) if isinstance(exc, ExtractionError) else f"{type(exc).__name__}: {exc}"
            fallback = ("Using the regex reader instead." if contract.text
                        else "This PDF has no text layer, so there is nothing to fall back on.")
            yield TraceEvent("error", "Couldn't read the contract with Claude", detail=f"{detail} {fallback}",
                             is_error=True, agent=CONTRACT_ANALYST)
            return
        if cache:
            cache.put(key, {"extraction": extraction.model_dump(mode="json")})
    output = to_tool_output(extraction, contract, settings.model)
    session.extraction_output = output
    if contract.text is None:
        session.rendered_text = render_contract_text(extraction)
    yield TraceEvent("tool_result", "read_contract_once", detail=_summary_of(output),
                     duration_ms=int((time.perf_counter() - t0) * 1000), agent=CONTRACT_ANALYST)
    if not extraction.is_tenancy_contract:
        yield TraceEvent("error", "This may not be a tenancy contract", detail="Claude's extraction flagged it; check the upload.",
                         is_error=True, agent=CONTRACT_ANALYST)


def _replay(hit: dict[str, Any]) -> Iterator[TraceEvent]:
    yield TraceEvent("info", "Answered from the local cache", detail=(
        "This exact request (same contract, question, form details, date and model) was answered before, "
        "so no API call was made."
    ))
    for data in hit["events"]:
        event = event_from_dict(data)
        if event.title != "Cost":       # the old cost line is replaced below
            yield event
    first_run = hit.get("cost_usd")
    was = f"; the first run cost about {format_usd(first_run)}" if first_run is not None else ""
    yield from _final(hit["text"], f"This analysis cost $0.00 (answered from the local cache{was}).",
                      usage={}, cached=True)


def read_notice(
    session: Session,
    image_path: str,
    *,
    settings: Settings,
    agent: Any = None,
    cache: ResultCache | None = None,
) -> Iterator[TraceEvent]:
    """The Notice Reader on its own (before an analysis): screenshot -> form values.

    The last event is kind="final" with detail={"reading": NoticeReading | None, "text": markdown}.
    The tenant confirms the values in the form; nothing is analysed yet.
    """
    try:
        image = load_notice_image(image_path, max_bytes=int(settings.max_upload_mb * 1_048_576))
    except (NoticeImageError, OSError) as exc:
        yield TraceEvent("error", "Couldn't use that image", detail=str(exc), is_error=True, agent=NOTICE_READER)
        yield TraceEvent("final", "notice", detail={"reading": None, "text": f"**Couldn't use that image:** {exc}"})
        return

    if agent is None:   # demo mode: no vision without a key, except the bundled sample
        reading = sample_reading(image)
        if reading is None:
            text = ("**Reading a screenshot needs Claude vision**, so it only works with an ANTHROPIC_API_KEY. "
                    "Type the details into the form instead, or click **Load sample notice** to see how it works.")
            yield TraceEvent("final", "notice", detail={"reading": None, "text": text})
            return
        yield TraceEvent("info", "Demo mode: showing the stored reading of the sample notice", agent=NOTICE_READER,
                         detail="No API key, so Claude vision is off. samples/sample_notice.json holds the correct reading.")
        yield TraceEvent("tool_result", "read_notice_image", detail=reading.model_dump(mode="json"), agent=NOTICE_READER)
        text = describe_reading(reading, "Demo mode: $0.00 (no API calls).")
        yield TraceEvent("final", "notice", detail={"reading": reading, "text": text})
        return

    if session.llm_requests >= settings.max_requests_per_session:
        text = (f"This session has used its {settings.max_requests_per_session} questions "
                "(HAQQ_MAX_REQUESTS_PER_SESSION). Reload the page to start a new session.")
        yield TraceEvent("error", "Session request limit reached", is_error=True, agent=NOTICE_READER)
        yield TraceEvent("final", "notice", detail={"reading": None, "text": text})
        return
    session.llm_requests += 1

    reader = run_notice_reader(image, agent.client, settings, cache)
    events: list[TraceEvent] = []
    while True:
        try:
            event = next(reader)
        except StopIteration as stop:
            reading = stop.value
            break
        events.append(event)
        yield event
    if reading is None:
        text = "**I couldn't read that screenshot.** Try a sharper image, or type the details into the form."
    else:
        cost = next((e.detail for e in reversed(events) if e.title == "Cost"), "")
        text = describe_reading(reading, cost)
    yield TraceEvent("final", "notice", detail={"reading": reading, "text": text})


def run_turn(
    session: Session,
    user_text: str,
    *,
    analyze: bool,
    inputs: TenantInputs,
    today: date,
    settings: Settings,
    agent: Any = None,
    cache: ResultCache | None = None,
) -> Iterator[TraceEvent]:
    """Run one turn and yield trace events; the last one is kind="final"."""
    if agent is None:   # demo mode: free, deterministic, no limits needed
        ctx = ToolContext(contract_text=session.contract_text, today=today, inputs=inputs)
        stream = run_demo_analysis(ctx, inputs) if analyze else answer_demo_question(user_text, ctx)
        for event in stream:
            if event.kind == "final":
                yield TraceEvent("info", "Cost", detail="Demo mode: $0.00 (no API calls).")
            yield event
        return

    # 1. Per-session request limit (protects a public deploy's API budget).
    if session.llm_requests >= settings.max_requests_per_session:
        text = (f"This session has used its {settings.max_requests_per_session} questions "
                "(HAQQ_MAX_REQUESTS_PER_SESSION). Reload the page to start a new session.")
        yield TraceEvent("error", "Session request limit reached", is_error=True)
        yield from _final(text, "This analysis cost $0.00 (not sent).", usage={})
        return

    content = build_user_message(user_text, inputs, session.contract is not None,
                                 len(session.contract_text or ""), today)

    # 2. Free repeats: only for the first turn, which depends on nothing but
    # the inputs below (later turns also depend on the chat history).
    key = None
    if cache is not None and cache.enabled and not session.messages:
        raw = session.contract.raw_bytes if session.contract else b""
        key = make_key("answer", PROMPT_FINGERPRINT, settings.model, settings.effort, raw, content)
        if (hit := cache.get(key)) is not None:
            session.extraction_output = hit.get("extraction_output") or session.extraction_output
            session.rendered_text = hit.get("rendered_text") or session.rendered_text
            session.evidence += hit.get("evidence") or []
            # Plain-text history, so a follow-up question still has context.
            session.messages += [{"role": "user", "content": content}, {"role": "assistant", "content": hit["text"]}]
            yield from _replay(hit)
            return

    session.llm_requests += 1
    meter = UsageMeter(settings.model)
    events: list[TraceEvent] = []

    # 3. Read the contract once.
    if session.contract is not None and session.extraction_output is None:
        for event in _read_contract_once(session, agent.client, settings, cache, meter):
            events.append(event)
            yield event
        if session.contract_text is None:   # scanned PDF and the extraction failed
            text = "I couldn't read this PDF. Try a clearer scan, or paste the contract text instead."
            yield from _final(text, meter.summary(), usage=dict(meter.totals))
            return

    # 4. The agent loop.
    ctx = ToolContext(contract_text=session.contract_text, today=today, contract_terms=session.extraction_output,
                      inputs=inputs, evidence=session.evidence)   # the same list: evidence carries over turns
    for event in agent.run(session.messages, content, ctx, meter=meter):
        events.append(event)
        yield event

    # 5. Remember a clean answer for free repeats.
    final = events[-1]
    if key is not None and not any(e.is_error for e in events):
        cache.put(key, {
            "text": final.detail["text"],
            "cost_usd": final.detail.get("cost", {}).get("cost_usd"),
            "events": [event_to_dict(e) for e in events if e.kind != "final"],
            "extraction_output": session.extraction_output,
            "rendered_text": session.rendered_text,
            "evidence": session.evidence,
            "model": settings.model,
        })
