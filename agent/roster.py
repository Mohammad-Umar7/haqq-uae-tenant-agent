"""Who is on the tenant-advocate team, and which agent owns which tool.

Haqq is a small multi-agent system. One agent, the Orchestrator, talks to
the tenant and decides what to do next (that is the Claude tool-use loop in
agent/loop.py). The others are specialists it hands work to. Some are
focused Claude calls, some are plain code: an agent only needs an LLM when
the job needs language or judgement.

This module has no imports on purpose: the trace, the cost meter and the
team code all use these names, and a dependency-free module can't cause an
import cycle.
"""

from __future__ import annotations

ORCHESTRATOR = "Orchestrator"
CONTRACT_ANALYST = "Contract Analyst"
LAW_RESEARCHER = "Law Researcher"
CALCULATORS = "Calculators"
DRAFTER = "Drafter"
CRITIC = "Critic"

# name -> (how it works, what it does). Shown in the UI and the README.
ROSTER: dict[str, tuple[str, str]] = {
    ORCHESTRATOR: ("Claude (tool-use loop)",
                   "Talks to the tenant, decides which specialist to ask next, writes the answer."),
    CONTRACT_ANALYST: ("Claude (read once) + code",
                       "Reads the contract once into JSON and scans the clauses for red flags."),
    LAW_RESEARCHER: ("code (BM25 search)",
                     "Finds the official law articles, and checks every citation in the answer was really found."),
    CALCULATORS: ("code",
                  "Rent caps (Decree No. 43 of 2013) and notice deadlines (Law No. 26 of 2007)."),
    DRAFTER: ("template + Claude",
              "Writes the first draft from a template, then revises it once after the Critic's review."),
    CRITIC: ("Claude + code",
             "Plays the landlord's lawyer: attacks the draft (numbers, dates, citations, tone) before the landlord can."),
}

# Which specialist does the work behind each tool the Orchestrator can call.
AGENT_FOR_TOOL: dict[str, str] = {
    "read_contract_once": CONTRACT_ANALYST,
    "extract_contract_terms": CONTRACT_ANALYST,
    "clause_risk_scan": CONTRACT_ANALYST,
    "search_tenancy_law": LAW_RESEARCHER,
    "rera_rent_increase_check": CALCULATORS,
    "notice_deadline_check": CALCULATORS,
    "draft_letter": DRAFTER,
}


def agent_for_tool(tool_name: str) -> str:
    return AGENT_FOR_TOOL.get(tool_name, ORCHESTRATOR)
