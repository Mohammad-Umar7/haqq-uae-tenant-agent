"""The agent's tools: plain, deterministic Python functions.

None of these call an LLM. Each one takes simple inputs and returns a dict
that is turned into JSON for the model. That keeps them easy to unit-test
and means demo mode (no API key) can run exactly the same code.
"""

from agent.tools.clause_scan import clause_risk_scan
from agent.tools.extract import extract_contract_terms
from agent.tools.law_lookup import search_tenancy_law
from agent.tools.letters import draft_letter
from agent.tools.notice import notice_deadline_check
from agent.tools.rent_increase import rera_rent_increase_check

__all__ = [
    "clause_risk_scan",
    "draft_letter",
    "extract_contract_terms",
    "notice_deadline_check",
    "rera_rent_increase_check",
    "search_tenancy_law",
]
