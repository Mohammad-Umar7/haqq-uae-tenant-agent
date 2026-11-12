"""Demo mode: the same tools, run in a fixed order, with no LLM.

This is a *workflow*, not an agent: the order of steps is hard-coded below.
Compare it with loop.py, where Claude chooses the steps. Demo mode exists so
the app is useful (and judge-able) without an API key, and it doubles as an
end-to-end test of the tools.

The code-only members of the team still work here: the Contract Analyst's
regex reader, the Calculators, the Law Researcher's BM25 search, the
Drafter's template and the Critic's automatic check. Only the Claude calls
(the Orchestrator, the Critic's review and the Drafter's revision) are off.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from agent.dates import long_date
from agent.inputs import TenantInputs
from agent.knowledge import DISCLAIMER, citation_for
from agent.registry import ToolContext, ToolOutcome, execute_tool
from agent.roster import CRITIC, agent_for_tool
from agent.team.case_file import build_case_file, case_file_text
from agent.team.checks import check_letter, known_facts
from agent.team.drafting import automatic_check_event
from agent.trace import TraceEvent

DEMO_BANNER = (
    "Demo mode: the LLM is off (no ANTHROPIC_API_KEY). The deterministic tools ran in a fixed "
    "order; nothing was decided by an AI model."
)


def _run(name: str, args: dict[str, Any], ctx: ToolContext, events: list[TraceEvent]) -> ToolOutcome:
    """Run one tool and record call + result events, exactly like the LLM loop does."""
    agent = agent_for_tool(name)
    events.append(TraceEvent("tool_call", name, detail=args, agent=agent))
    t0 = time.perf_counter()
    outcome = execute_tool(name, args, ctx)
    if not outcome.is_error:
        ctx.record(name, outcome.output)
    events.append(
        TraceEvent(
            "tool_result", name, detail=outcome.output, is_error=outcome.is_error,
            rule_ids=outcome.rule_ids, duration_ms=int((time.perf_counter() - t0) * 1000), agent=agent,
        )
    )
    return outcome


def run_demo_analysis(ctx: ToolContext, inputs: TenantInputs) -> Iterator[TraceEvent]:
    """Run the fixed pipeline and finish with a markdown report (kind="final")."""
    events: list[TraceEvent] = []
    cursor = 0

    def flush() -> Iterator[TraceEvent]:
        # Yield events added since the last flush, so the UI updates live.
        nonlocal cursor
        new = events[cursor:]
        cursor = len(events)
        yield from new

    events.append(TraceEvent("info", "Demo mode: running the tools in a fixed order", detail=DEMO_BANNER))
    if not ctx.contract_text:
        yield from flush()
        yield TraceEvent("final", "answer", detail={"text": "Load a contract first (upload a PDF/TXT, paste the text, or use the sample contract)."})
        return

    # 1. Read the contract.
    terms_out = _run("extract_contract_terms", {}, ctx, events).output
    yield from flush()
    terms = terms_out.get("terms", {})

    # 2. Scan for red flags.
    scan_out = _run("clause_risk_scan", {}, ctx, events).output
    yield from flush()

    # 3. Rent increase check (needs the RERA average from the tenant).
    rent_out = None
    if terms.get("annual_rent_aed") and inputs.rera_average_rent:
        args = {"current_annual_rent": terms["annual_rent_aed"], "rera_average_rent": inputs.rera_average_rent}
        if inputs.proposed_new_rent:
            args["proposed_new_rent"] = inputs.proposed_new_rent
        rent_out = _run("rera_rent_increase_check", args, ctx, events).output
    else:
        events.append(TraceEvent("info", "Skipped rera_rent_increase_check",
                                 detail="Needs the annual rent from the contract and the RERA average rent from the form."))
    yield from flush()

    # 4. Notice timing.
    notice_out = None
    if terms.get("end_date"):
        notice_type = inputs.notice_type or "rent_increase"
        args = {"contract_end_date": terms["end_date"], "notice_type": notice_type}
        if inputs.notice_date:
            args["notice_date"] = inputs.notice_date
        if inputs.delivery_method:
            args["delivery_method"] = inputs.delivery_method
        notice_out = _run("notice_deadline_check", args, ctx, events).output
    else:
        events.append(TraceEvent("info", "Skipped notice_deadline_check", detail="No contract end date found."))
    yield from flush()

    # 5. Fetch the official law text behind every rule the earlier tools
    # pointed to (search_tenancy_law accepts a rule id as its query).
    wanted: list[str] = []
    for ev in events:
        for r in ev.rule_ids:
            if r not in wanted:
                wanted.append(r)
    law_outputs = [_run("search_tenancy_law", {"query": rule_id}, ctx, events).output for rule_id in wanted[:6]]
    yield from flush()

    # 6. Draft a letter if something is wrong.
    letter_out = _maybe_draft_letter(ctx, terms, scan_out, rent_out, notice_out, inputs, events)
    yield from flush()

    report = _build_report(terms_out, scan_out, rent_out, notice_out, letter_out, events, law_outputs)
    yield TraceEvent("final", "answer", detail={"text": report})


def _maybe_draft_letter(ctx, terms, scan_out, rent_out, notice_out, inputs, events) -> dict[str, Any] | None:
    facts: list[str] = []
    rule_ids: list[str] = []
    letter_type = None

    rent_problem = rent_out is not None and rent_out.get("is_within_cap") is False
    notice_late = notice_out is not None and notice_out.get("notice_on_time") is False
    eviction = inputs.notice_type == "eviction"

    if terms.get("end_date") and terms.get("annual_rent_aed"):
        facts.append(f"Our tenancy contract runs until {long_date(terms['end_date'])} at an annual rent of AED {terms['annual_rent_aed']:,.0f}.")

    bad_delivery = (notice_out or {}).get("delivery_method_ok") is False
    if eviction and (notice_late or bad_delivery):
        letter_type = "eviction_notice_response"
        if inputs.notice_date and notice_out.get("notice_days_before_expiry") is not None:
            facts.append(
                f"I received your notice to vacate on {long_date(inputs.notice_date)}, "
                f"{notice_out['notice_days_before_expiry']} days before the contract expires; "
                "the rule requires 12 months' notice."
            )
        if bad_delivery:
            facts.append(
                f"The notice was sent by {inputs.delivery_method}, not through a notary public or by registered mail."
            )
        rule_ids.append("eviction_12_months_notice")
    elif rent_problem or notice_late:
        letter_type = "rent_increase_objection"
        if inputs.proposed_new_rent:
            facts.append(f"You have proposed a new annual rent of AED {inputs.proposed_new_rent:,.0f}.")
        if rent_problem:
            facts.append(
                f"According to the RERA Rental Index the average rent for similar units is AED {rent_out['rera_average_rent']:,.0f}, "
                f"so my rent is {rent_out['percent_below_average']}% below average and the maximum increase is "
                f"{rent_out['max_increase_percent']}% (AED {rent_out['max_allowed_new_rent']:,.0f} per year)."
            )
            rule_ids += ["rent_increase_caps", "rental_index_source"]
        if notice_late:
            facts.append(
                f"I received your notice on {long_date(inputs.notice_date)}, {notice_out['notice_days_before_expiry']} days before the contract "
                "expires; the rules require at least 90 days' written notice."
            )
        rule_ids.append("notice_change_terms_90_days")
    else:
        high_flags = [f for f in scan_out.get("flags", []) if f["severity"] in ("high", "medium") and f["clause_number"]]
        if high_flags:
            letter_type = "clause_concern"
            for f in high_flags[:4]:
                facts.append(f"Clause {f['clause_number']}: {f['title'].lower()}.")
                rule_ids += f["rule_ids"]

    if not letter_type:
        events.append(TraceEvent("info", "No letter drafted", detail="No rule problem detected that needs a letter."))
        return None

    args = {
        "letter_type": letter_type,
        "facts": facts,
        "rule_ids": list(dict.fromkeys(rule_ids)),
        "tenant_name": terms.get("tenant") or "",
        "landlord_name": terms.get("landlord") or "",
        "property_address": terms.get("property") or "",
    }
    letter = _run("draft_letter", args, ctx, events).output
    if "body" in letter:
        # The Critic's free half: every amount, date and citation must be in the case file.
        case = build_case_file(ctx, extra_rule_ids=letter["rule_ids"])
        problems = check_letter(letter["body"], known_facts(case_file_text(case), case["allowed_citations"]))
        events.append(automatic_check_event(problems, "the letter"))
        events.append(TraceEvent("info", "Critic's Claude review skipped (demo mode)", agent=CRITIC, detail=(
            "With an API key, the Critic also reads the letter as the landlord's lawyer and the Drafter "
            "revises it once.")))
    return letter


def _money(x: float | None) -> str:
    return f"AED {x:,.0f}" if x else "not found"


def _law_section(law_outputs: list[dict[str, Any]]) -> list[str]:
    """'Law relied on': each official article once, with a short quote and its link."""
    seen: set[str] = set()
    lines: list[str] = []
    for out in law_outputs:
        for article in out.get("articles", []):
            if article["article_id"] in seen:
                continue
            seen.add(article["article_id"])
            quote = " ".join(article["text"].split())
            quote = quote if len(quote) <= 280 else quote[:277].rstrip() + "..."
            lines.append(f"- **{article['citation']}** ([official text]({article['source_url']})): \"{quote}\"")
    if not lines:
        return []
    return ["", "### Law relied on (official text)", *lines,
            "_The English text is an official translation; the Arabic original prevails._"]


def _build_report(terms_out, scan_out, rent_out, notice_out, letter_out, events, law_outputs=()) -> str:
    t = terms_out.get("terms", {})
    d = terms_out.get("derived", {})
    parts = [f"> **{DEMO_BANNER}**", "", "### Key terms (read by regex)"]
    parts += [
        "| Term | Value |", "|---|---|",
        f"| Landlord | {t.get('landlord') or 'not found'} |",
        f"| Tenant | {t.get('tenant') or 'not found'} |",
        f"| Property | {t.get('property') or 'not found'} |",
        f"| Annual rent | {_money(t.get('annual_rent_aed'))} |",
        f"| Period | {t.get('start_date') or '?'} to {t.get('end_date') or '?'} |",
        f"| Security deposit | {_money(t.get('security_deposit_aed'))}"
        + (f" ({d['deposit_percent_of_annual_rent']}% of annual rent)" if "deposit_percent_of_annual_rent" in d else "") + " |",
        f"| Cheques | {t.get('number_of_cheques') or 'not found'} |",
        f"| Mentions Ejari | {'yes' if t.get('mentions_ejari') else 'no'} |",
    ]
    if terms_out.get("missing_fields"):
        parts.append(f"\nCould not find: {', '.join(terms_out['missing_fields'])}.")

    parts += ["", f"### Red flags ({scan_out.get('flag_count', 0)})"]
    for f in scan_out.get("flags", []):
        where = f"Clause {f['clause_number']}" if f["clause_number"] else "Whole contract"
        cite = f" _({'; '.join(f['citations'])})_" if f["citations"] else ""
        parts.append(f"- **[{f['severity'].upper()}] {f['title']}** ({where}): {f['why_it_matters']}{cite}")
    if not scan_out.get("flags"):
        parts.append("- None found by the keyword scan (that doesn't mean the contract is fine).")

    parts += ["", "### Rent increase check"]
    if rent_out and "error" not in rent_out:
        parts.append(
            f"Your rent is **{rent_out['percent_below_average']}%** below the RERA average you entered "
            f"({rent_out['band']}), so the maximum increase is **{rent_out['max_increase_percent']}%** "
            f"(up to {_money(rent_out['max_allowed_new_rent'])} per year)."
        )
        parts.append(f"**Verdict:** {rent_out['verdict']}")
    else:
        parts.append("Enter the RERA Rental Index average rent (and the landlord's proposed rent) in the form to run this check.")
    parts.append("_The official DLD/RERA Rental Index calculator is the source of truth; Dubai updated its rental index in 2025._")

    parts += ["", "### Notice deadline"]
    if notice_out and "error" not in notice_out:
        parts.append(f"Rule: {notice_out['requirement']}. Last valid notice date: **{notice_out['last_valid_notice_date']}**.")
        parts.append(f"**Verdict:** {notice_out['verdict']}")
        if notice_out.get("delivery_note"):
            parts.append(notice_out["delivery_note"])
    else:
        parts.append("Could not run (no contract end date found).")

    if letter_out and "body" in letter_out:
        parts += ["", "### Draft letter", "```text", letter_out["body"], "```", f"_{letter_out['note']}_"]

    used: list[str] = []
    for ev in events:
        for r in ev.rule_ids:
            if r not in used:
                used.append(r)
    if used:
        parts += ["", "**Rules used:** " + "; ".join(c for c in (citation_for(r) for r in used) if c)]
    parts += _law_section(list(law_outputs))
    parts += ["", f"_{DISCLAIMER}_"]
    return "\n".join(parts)


def answer_demo_question(question: str, ctx: ToolContext) -> Iterator[TraceEvent]:
    """Demo-mode chat: no LLM to understand the question, so we show what
    the local BM25 law search finds (retrieval without generation)."""
    events: list[TraceEvent] = [TraceEvent("info", "Demo mode: law search only (no LLM)", detail=DEMO_BANNER)]
    out = _run("search_tenancy_law", {"query": question, "top_k": 2}, ctx, events).output
    yield from events

    if out.get("found"):
        lines = ["Here are the articles of the law that best match your question (keyword search, no AI):", ""]
        for article in out["articles"]:
            lines.append(f"**{article['citation']}** ([official text]({article['source_url']}))")
            lines.append("> " + article["text"].replace("\n", "\n> "))
            lines.append("")
        for note in out.get("plain_english", []):
            lines.append(f"**In plain English ({note['title']}):** {note['plain_english']}")
            lines.append(f"Tip: {note['tenant_tip']}")
            lines.append("")
        lines.append("_The English text is an official translation; the Arabic original prevails._")
    else:
        lines = ["I couldn't find this in the law. Try words like *rent increase*, *notice*, *eviction*, "
                 "*deposit*, *Ejari* or *repairs*, or ask RERA or the RDSC.", ""]
    lines.append("For a full review, run the contract analysis. With an ANTHROPIC_API_KEY the AI team answers questions in its own words.")
    lines.append(f"\n_{DISCLAIMER}_")
    yield TraceEvent("final", "answer", detail={"text": "\n".join(lines)})
