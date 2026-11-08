"""Does the Critic catch planted mistakes in a draft letter?

evals/critic_cases.json holds one correct letter and five letters with one
planted problem each: a wrong amount, a wrong date, the wrong rent band
(every number in it exists in the case file, just attached to the wrong
claim), a threat, and a rule the law doesn't contain.

Two scorecards:
- automatic check only (code, free): run by `python -m evals.run_eval`
- automatic check + Claude Critic: `python -m evals.run_eval --mode critic`
  (one Critic call per letter, about $0.002 each on Haiku 5.5)

A planted problem counts as caught only when an objection quotes the
planted words (so a lucky objection about something else doesn't count).
On the clean letter, any wrong_number or wrong_date objection is a false
alarm; objections about strategy or tone are allowed there (the Critic is
meant to push even a correct letter).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from agent.config import Settings
from agent.contract import LoadedContract
from agent.cost import UsageMeter
from agent.inputs import TenantInputs
from agent.registry import ToolContext, execute_tool
from agent.roster import CRITIC
from agent.team.case_file import build_case_file, case_file_text
from agent.team.checks import Objection, check_letter, known_facts
from agent.team.prompts import CRITIC_TASK
from agent.team.specialist import SpecialistError, ask_specialist

CRITIC_CASES_PATH = Path(__file__).resolve().parent / "critic_cases.json"


@dataclass(frozen=True)
class CriticCase:
    id: str
    description: str
    letter: str
    expect: str | None
    planted: str | None


def load_critic_cases(path: Path = CRITIC_CASES_PATH) -> tuple[list[CriticCase], ToolContext, dict[str, Any]]:
    """Replay the session with the real tools, then build each letter from the template."""
    data = json.loads(path.read_text(encoding="utf-8"))
    contract = LoadedContract.from_text((path.parent / data["contract"]).read_text(encoding="utf-8"), "sample")
    ctx = ToolContext(contract_text=contract.text, today=date.fromisoformat(data["today"]),
                      inputs=TenantInputs(**data["inputs"]))
    for name, args in data["session"]:
        ctx.record(name, execute_tool(name, args, ctx).output)

    cases = []
    for c in data["cases"]:
        facts = list(data["letter"]["facts"])
        if "replace" in c:
            old, new = c["replace"]
            facts = [f.replace(old, new) for f in facts]
        if "add_fact" in c:
            facts.append(c["add_fact"])
        letter = execute_tool("draft_letter", {**data["letter"], "facts": facts}, ctx).output["body"]
        cases.append(CriticCase(c["id"], c["description"], letter, c["expect"], c.get("planted")))
    case_file = build_case_file(ctx, extra_rule_ids=data["letter"]["rule_ids"])
    return cases, ctx, case_file


def _caught(case: CriticCase, objections: list[Objection]) -> bool:
    return any(case.planted.lower() in o.quote.lower() for o in objections)


def _false_alarms(objections: list[Objection]) -> list[Objection]:
    return [o for o in objections if o.kind in ("wrong_number", "wrong_date")]


def score_critic(cases: list[CriticCase], found: dict[str, list[Objection]]) -> dict[str, Any]:
    rows, caught, planted, alarms = [], 0, 0, 0
    for case in cases:
        objections = found.get(case.id, [])
        if case.expect is None:
            bad = _false_alarms(objections)
            alarms += len(bad)
            result = "no false alarm" if not bad else "false alarm: " + "; ".join(o.quote for o in bad)
        else:
            planted += 1
            hit = _caught(case, objections)
            caught += hit
            result = "caught" if hit else "missed"
        rows.append({"id": case.id, "description": case.description, "result": result,
                     "objections": len(objections)})
    return {"caught": caught, "planted": planted, "false_alarms": alarms, "rows": rows}


def run_critic_deterministic(path: Path = CRITIC_CASES_PATH) -> dict[str, Any]:
    cases, _, case_file = load_critic_cases(path)
    facts = known_facts(case_file_text(case_file), case_file["allowed_citations"])
    return score_critic(cases, {c.id: check_letter(c.letter, facts) for c in cases})


def run_critic_llm(client: Any, settings: Settings, path: Path = CRITIC_CASES_PATH) -> dict[str, Any]:
    """The full Critic: automatic check + one Claude call per letter."""
    cases, _, case_file = load_critic_cases(path)
    text = case_file_text(case_file)
    facts = known_facts(text, case_file["allowed_citations"])
    meter = UsageMeter(settings.model)
    found: dict[str, list[Objection]] = {}
    claude_only: dict[str, list[Objection]] = {}
    for case in cases:
        try:
            answer = ask_specialist(client, settings, meter, CRITIC, text, case.letter, CRITIC_TASK)
            claude_only[case.id] = answer.reply.critic_objections()
        except SpecialistError:
            claude_only[case.id] = []
        found[case.id] = check_letter(case.letter, facts) + claude_only[case.id]
    return {"team": score_critic(cases, found), "claude_only": score_critic(cases, claude_only),
            "meter": meter, "objections": {k: [o.as_dict() for o in v] for k, v in claude_only.items()}}


def render_critic_markdown(card: dict[str, Any], title: str) -> str:
    lines = [
        f"## {title}", "",
        f"Planted problems caught: **{card['caught']}/{card['planted']}**. "
        f"False alarms on the clean letter: **{card['false_alarms']}**.", "",
        "| Letter | Planted problem | Result |", "|---|---|---|",
    ]
    lines += [f"| `{r['id']}` | {r['description']} | {r['result']} |" for r in card["rows"]]
    return "\n".join(lines) + "\n"
