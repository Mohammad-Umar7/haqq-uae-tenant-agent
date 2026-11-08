"""The drafting team: Drafter -> Critic -> Drafter, behind the draft_letter tool.

When the Orchestrator calls draft_letter, it doesn't get a letter straight
from a template any more. The tool hands the job to a small team:

    1. Drafter, draft 1   the template fills in the Orchestrator's facts (code, free)
    2. Critic, part 1     automatic check: every amount, date and citation in
                          draft 1 must be in the case file (code, free)
    3. Critic, part 2     Claude plays the landlord's lawyer and attacks draft 1
    4. Drafter, draft 2   Claude revises ONCE, answering the objections, and
                          writes the Arabic version if the tenant asked for one
    5. Gate               draft 2 (and the Arabic letter) must pass the automatic
                          check, or we keep draft 1 (and drop the Arabic)

Step 4 is skipped when nobody objected and no Arabic was requested, so a
clean English letter costs one Claude call, not two. If the spend limit is
reached or a call fails, the team falls back to the best draft it has and
says so: the tenant always gets a letter.

This is a generator: it yields TraceEvents (so the UI can show each agent
at work) and *returns* the final ToolOutcome, which the Orchestrator's loop
receives with `outcome = yield from run_drafting_team(...)`.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import anthropic

from agent.config import Settings
from agent.cost import UsageMeter
from agent.registry import ToolContext, ToolOutcome, execute_tool
from agent.roster import CRITIC, DRAFTER
from agent.team.arabic import citation_pairs, untranslated_words
from agent.team.case_file import build_case_file, case_file_text
from agent.team.checks import KnownFacts, Objection, check_letter, known_facts
from agent.team.prompts import CRITIC_TASK, drafter_task
from agent.team.specialist import SpecialistError, SpendLimitReached, ask_specialist
from agent.trace import TraceEvent

Events = Generator[TraceEvent, None, Any]


def _letter_block(text: str) -> str:
    return f"```text\n{text}\n```"


def automatic_check_event(problems: list[Objection], label: str) -> TraceEvent:
    if problems:
        title = f"Automatic check of {label}: {len(problems)} problem(s) found"
        detail = "\n".join(f"- **{p.kind}** `{p.quote}`: {p.problem}" for p in problems)
    else:
        title = f"Automatic check of {label}: every amount, date and citation is in the case file"
        detail = None
    return TraceEvent("info", title, detail=detail, agent=CRITIC)


def _ask(agent: str, title: str, client: Any, settings: Settings, meter: UsageMeter,
         case_text: str, letter: str, task: str, call_detail: dict[str, Any]) -> Events:
    """Run one specialist call with trace events. Returns the reply or None."""
    yield TraceEvent("tool_call", title, detail=call_detail, agent=agent)
    try:
        answer = ask_specialist(client, settings, meter, agent, case_text, letter, task)
    except SpendLimitReached as exc:
        yield TraceEvent("error", f"{agent} skipped: spend limit reached", detail=str(exc), is_error=True, agent=agent)
        return None
    except SpecialistError as exc:
        yield TraceEvent("error", f"{agent} reply unusable", detail=str(exc), is_error=True, agent=agent)
        return None
    except anthropic.APIError as exc:
        yield TraceEvent("error", f"{agent} call failed", detail=f"{type(exc).__name__}: {exc}", is_error=True, agent=agent)
        return None
    for summary in answer.thinking:
        yield TraceEvent("thinking", "reasoning", detail=summary, agent=agent)
    return answer


def run_drafting_team(args: dict[str, Any] | None, ctx: ToolContext, client: Any, settings: Settings,
                      meter: UsageMeter) -> Events:
    """Draft, review and revise one letter. Returns the ToolOutcome for the Orchestrator."""
    # 1. Drafter, draft 1: the template (validates the arguments too).
    first = execute_tool("draft_letter", args, ctx)
    if first.is_error:
        return first
    draft1 = first.output
    yield TraceEvent("info", "Draft 1 written from the template (code, $0.00)", detail=_letter_block(draft1["body"]),
                     agent=DRAFTER)

    case = build_case_file(ctx, extra_rule_ids=draft1["rule_ids"])
    case_text = case_file_text(case)
    facts: KnownFacts = known_facts(case_text, case["allowed_citations"])

    # 2. Critic, part 1: the free automatic check.
    automatic = check_letter(draft1["body"], facts)
    yield automatic_check_event(automatic, "draft 1")

    # 3. Critic, part 2: Claude as the landlord's lawyer.
    critic: list[Objection] = []
    if settings.critic_enabled:
        answer = yield from _ask(
            CRITIC, "critic_review", client, settings, meter, case_text, draft1["body"], CRITIC_TASK,
            {"role": "the landlord's lawyer", "reads": f"case file ({len(case_text):,} characters) + draft 1",
             "model": settings.model},
        )
        if answer is not None:
            critic = answer.reply.critic_objections()
            yield TraceEvent("tool_result", "critic_review", agent=CRITIC, duration_ms=answer.duration_ms, detail={
                "objections": [o.as_dict() for o in critic] or "none: the Critic found the letter solid",
                "cost": answer.cost_line,
            })
    objections = automatic + critic

    # 4. Drafter, draft 2: revise once if someone objected, and/or write the Arabic version.
    language = ctx.inputs.letter_language
    arabic_pairs = citation_pairs(case["allowed_citations"]) if language != "en" else None
    revise = bool(objections) and settings.critic_enabled
    body, version, changes, rejected = draft1["body"], "draft 1 (template)", [], []
    arabic, arabic_note = None, None
    if revise or language != "en":
        answer = yield from _ask(
            DRAFTER, "revise_letter", client, settings, meter, case_text, draft1["body"],
            drafter_task(objections if revise else [], case["allowed_citations"], arabic_pairs),
            {"objections_to_answer": len(objections) if revise else 0, "arabic_version": language != "en",
             "model": settings.model},
        )
        if answer is not None:
            yield TraceEvent("tool_result", "revise_letter", agent=DRAFTER, duration_ms=answer.duration_ms, detail={
                "changes_made": answer.reply.changes_made, "cost": answer.cost_line,
            })
            # 5. The gate: a revision may not introduce an unverified number, date or citation.
            revised = (answer.reply.letter_english or "").strip()
            if revise and revised:
                gate = check_letter(revised, facts)
                yield automatic_check_event(gate, "draft 2")
                if not gate:
                    body, version, changes = revised, "draft 2 (revised by the Drafter after the Critic's review)", answer.reply.changes_made
                else:
                    rejected = [f"{o.quote}: {o.problem}" for o in gate]
                    yield TraceEvent("info", "Kept draft 1: draft 2 failed the automatic check", agent=DRAFTER,
                                     detail="A revision may not add amounts, dates or citations the case file doesn't have.")
            candidate = (answer.reply.letter_arabic or "").strip()
            if language != "en" and candidate and not rejected:
                gate = check_letter(candidate, facts)
                yield automatic_check_event(gate, "the Arabic letter")
                if not gate:
                    arabic = candidate
                    if leftovers := untranslated_words(arabic):
                        arabic_note = ("The Arabic letter still contains English words (" + ", ".join(leftovers)
                                       + "): the tenant should fix them before sending.")
                        yield TraceEvent("info", "Arabic letter: untranslated English words found", agent=CRITIC,
                                         detail=arabic_note)
                else:
                    arabic_note = "The Arabic version failed the automatic check, so it is not shown."
            elif language != "en":
                arabic_note = "The Arabic version could not be written this time; the English letter is shown."
        elif language != "en":
            arabic_note = "The Arabic version could not be written this time; the English letter is shown."

    unresolved = check_letter(body, facts)
    note = ("This is the drafting team's final letter for this request: don't call draft_letter again unless "
            "the tenant asks for changes. It is shown to the tenant in the Letter panel, so don't paste it into "
            "your answer: summarise the Critic's main objections and what changed, and remind the tenant to "
            "check every fact and fill any [placeholders] before sending. Not legal advice.")
    if rejected:
        note += (" The Drafter's revision was rejected by the automatic check, so the letter is draft 1: tell the "
                 "tenant which of the Critic's objections they may want to fix by hand.")
    if unresolved:
        note += (" The automatic check could not verify: " + "; ".join(o.quote for o in unresolved)
                 + ". Tell the tenant to check these before sending.")
    if arabic:
        note += (" An Arabic version is shown next to it. Remind the tenant that the Arabic text of the law is the "
                 "original and prevails, and to check the Arabic letter too.")
        if arabic_note:
            note += " " + arabic_note
    elif arabic_note:
        note += " " + arabic_note + " Tell the tenant."
    output = {
        "letter_type": draft1["letter_type"],
        "subject": draft1["subject"],
        "body": body,
        "body_arabic": arabic,
        "letter_language": language,
        "version": version,
        "review": {
            "objections": [o.as_dict() for o in objections],
            "unverified_in_final_letter": [o.as_dict() for o in unresolved],
        },
        "changes_made": changes,
        "revision_rejected_because": rejected,
        "cited_rules": draft1["cited_rules"],
        "rule_ids": draft1["rule_ids"],
        "ignored_rule_ids": draft1["ignored_rule_ids"],
        "note": note,
    }
    return ToolOutcome(output, False)
