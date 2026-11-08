"""The bridge between the LLM and the Python tools.

Two things live here:
1. TOOL_SCHEMAS - the JSON descriptions Claude reads to decide which tool to
   call and with what arguments. Good descriptions matter more than clever
   code: they are the tool's "user manual" for the model.
2. execute_tool() - takes the tool name + arguments Claude produced, checks
   them, runs the matching Python function, and returns JSON-ready output.

The model never runs code itself. It only *asks* for a tool call; our code
decides whether and how to run it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from agent.knowledge import rule_ids as known_rule_ids
from agent.tools import (
    clause_risk_scan,
    draft_letter,
    extract_contract_terms,
    notice_deadline_check,
    rera_rent_increase_check,
    search_tenancy_law,
)
from agent.tools.letters import LETTER_TYPES
from agent.tools.notice import DELIVERY_METHODS, NOTICE_TYPES


@dataclass
class ToolContext:
    """Per-session data tools can read but the model doesn't have to send.

    The contract stays here instead of being pasted into every tool call,
    which saves tokens and stops the model from "editing" the contract.
    `contract_terms` holds the read-once extraction (LLM mode); when it is
    None, extract_contract_terms falls back to regex on `contract_text`.
    """

    contract_text: str | None
    today: date
    contract_terms: dict[str, Any] | None = None


class ToolInputError(ValueError):
    """The model sent arguments we can't use. The message goes back to it."""


# The list order is fixed and nothing in it depends on the user, the date or
# a set's iteration order: tools are the very start of every request, so any
# change here would invalidate the prompt cache for everything after it.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "extract_contract_terms",
        "description": (
            "Return the key terms of the tenancy contract the tenant uploaded (landlord, tenant, "
            "property, annual rent in AED, start and end dates, security deposit, number of cheques, "
            "whether Ejari is mentioned), derived numbers, and the numbered clauses. The contract was "
            "read once when it was loaded, so this is cheap. Call it first whenever a contract is "
            "loaded; it is the only way to see the contract. Check missing_fields, method and any "
            "cross_check_with_regex disagreements, and read the clauses."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "clause_risk_scan",
        "description": (
            "Scan every clause of the uploaded contract for known red flags (non-refundable deposit, "
            "rent increase at any time, early-termination penalty above 2 months' rent, tenant pays "
            "all maintenance including structural, waiver of the right to go to the RDSC, automatic "
            "renewal with a fixed increase, landlord can end the tenancy on short notice, no Ejari). "
            "Returns each flag with the clause number, an excerpt, why it matters and rule_ids. "
            "It is a keyword scan: it can miss risky wording, so also read the clauses yourself."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "rera_rent_increase_check",
        "description": (
            "Calculate the maximum legal rent increase under Dubai Decree No. 43 of 2013 from the "
            "current annual rent and the RERA Rental Index average for similar units, and judge a "
            "proposed new rent if one is given. Use this for every rent-increase question instead of "
            "doing the math yourself. The RERA average must come from the tenant (official DLD/RERA "
            "Rental Index calculator); if you don't have it, ask the tenant rather than guessing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "current_annual_rent": {"type": "number", "description": "Current rent in AED per year."},
                "rera_average_rent": {
                    "type": "number",
                    "description": "RERA Rental Index average annual rent for similar units, in AED, as given by the tenant.",
                },
                "proposed_new_rent": {
                    "type": "number",
                    "description": "The landlord's proposed new annual rent in AED, if known.",
                },
            },
            "required": ["current_annual_rent", "rera_average_rent"],
            "additionalProperties": False,
        },
    },
    {
        "name": "notice_deadline_check",
        "description": (
            "Date math for Dubai notice rules. notice_type 'rent_increase' or 'change_terms': written "
            "notice at least 90 days before expiry (Law No. 26 of 2007, Art. 14). notice_type "
            "'eviction': eviction at expiry for the owner's own use, sale, demolition or major "
            "renovation needs 12 months' notice via notary public or registered mail (Art. 25(2)). "
            "Returns the last valid notice date and, if notice_date is given, whether the notice was "
            "on time (and for eviction whether the delivery method is valid). Dates as YYYY-MM-DD."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contract_end_date": {"type": "string", "description": "Contract expiry date, YYYY-MM-DD."},
                "notice_type": {"type": "string", "enum": list(NOTICE_TYPES)},
                "notice_date": {
                    "type": "string",
                    "description": "Date the tenant received the landlord's notice, YYYY-MM-DD. Omit if no notice yet.",
                },
                "delivery_method": {
                    "type": "string",
                    "enum": list(DELIVERY_METHODS),
                    "description": "How the notice was delivered, if known.",
                },
            },
            "required": ["contract_end_date", "notice_type"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_tenancy_law",
        "description": (
            "Search the official English text of Dubai's tenancy laws: Law No. 26 of 2007 as amended by "
            "Law No. 33 of 2008, and Decree No. 43 of 2013 on rent increases. Returns the most relevant "
            "articles in force, each with its exact text, a citation and the official source URL, plus a "
            "plain-English note when one exists. Call it before you state what the law says, and cite the "
            "'citation' field of every article you rely on. Query with plain words (e.g. '90 days notice "
            "to change the rent', 'eviction to sell the property', 'security deposit refund') or with a "
            "rule_id another tool returned. If found is false, tell the tenant you couldn't find this in "
            "the law; don't answer from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A short topic in plain words, or a rule_id."},
                "top_k": {"type": "integer", "description": "How many articles to return, 1 to 5. Default 3."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_letter",
        "description": (
            "Draft a polite, firm letter to the landlord, or an RDSC complaint summary, from a template. "
            "You supply the facts (short factual statements with dates and AED amounts from tool "
            "results), the rule_ids you rely on (from tool results, e.g. search_tenancy_law), and "
            "optionally the request. Citations are filled in from the knowledge base; unknown rule ids "
            "are dropped and reported in ignored_rule_ids. "
            "Show the returned body to the tenant and remind them to check it before sending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "letter_type": {"type": "string", "enum": list(LETTER_TYPES)},
                "facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short factual statements, one per item (max 12).",
                },
                "rule_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": known_rule_ids()},
                    "description": "Knowledge-base rule ids the letter relies on.",
                },
                "request": {"type": "string", "description": "What the tenant is asking the landlord (or RDSC) to do."},
                "tenant_name": {"type": "string"},
                "landlord_name": {"type": "string"},
                "property_address": {"type": "string"},
            },
            "required": ["letter_type", "facts", "rule_ids"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = [schema["name"] for schema in TOOL_SCHEMAS]


# ---- input checking helpers -------------------------------------------------
# The model usually sends well-formed arguments, but "usually" isn't enough.
# When something is off we raise ToolInputError; the loop sends that message
# back as an error tool_result so the model can correct itself and retry.

def _number(args: dict[str, Any], key: str, required: bool = True) -> float | None:
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise ToolInputError(f"'{key}' is required.")
        return None
    if isinstance(value, bool):
        raise ToolInputError(f"'{key}' must be a number.")
    if isinstance(value, (int, float)):
        return float(value)
    # Accept "85,000" or "AED 85,000" - models sometimes format numbers.
    cleaned = re.sub(r"(?i)aed|dhs?|[,\s]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        raise ToolInputError(f"'{key}' must be a number, got {value!r}.") from None


def _string(args: dict[str, Any], key: str, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ToolInputError(f"'{key}' is required.")
        return None
    return str(value).strip()


def _string_list(args: dict[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ToolInputError(f"'{key}' must be a list of strings.")
    return [str(v) for v in value]


def _integer(args: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ToolInputError(f"'{key}' must be a whole number from {low} to {high}.") from None
    return max(low, min(high, number))  # clamp: the schema can't say min/max


def _require_contract(ctx: ToolContext) -> str:
    if not ctx.contract_text:
        raise ToolInputError(
            "No contract is loaded. Ask the tenant to upload a PDF or paste the contract text."
        )
    return ctx.contract_text


def _contract_terms(ctx: ToolContext) -> dict[str, Any]:
    if ctx.contract_terms is not None:
        return ctx.contract_terms          # read once, at the start of the session
    return extract_contract_terms(_require_contract(ctx))


# ---- dispatch table: tool name -> function that unpacks args and runs it ----

_HANDLERS: dict[str, Callable[[dict[str, Any], ToolContext], dict[str, Any]]] = {
    "extract_contract_terms": lambda a, ctx: _contract_terms(ctx),
    "clause_risk_scan": lambda a, ctx: clause_risk_scan(_require_contract(ctx)),
    "rera_rent_increase_check": lambda a, ctx: rera_rent_increase_check(
        current_annual_rent=_number(a, "current_annual_rent"),
        rera_average_rent=_number(a, "rera_average_rent"),
        proposed_new_rent=_number(a, "proposed_new_rent", required=False),
    ),
    "notice_deadline_check": lambda a, ctx: notice_deadline_check(
        contract_end_date=_string(a, "contract_end_date"),
        notice_type=_string(a, "notice_type"),
        notice_date=_string(a, "notice_date", required=False),
        delivery_method=_string(a, "delivery_method", required=False),
        today=ctx.today,
    ),
    "search_tenancy_law": lambda a, ctx: search_tenancy_law(
        query=_string(a, "query"),
        top_k=_integer(a, "top_k", default=3, low=1, high=5),
    ),
    "draft_letter": lambda a, ctx: draft_letter(
        letter_type=_string(a, "letter_type"),
        facts=_string_list(a, "facts"),
        rule_ids=_string_list(a, "rule_ids"),
        request=_string(a, "request", required=False),
        tenant_name=_string(a, "tenant_name", required=False),
        landlord_name=_string(a, "landlord_name", required=False),
        property_address=_string(a, "property_address", required=False),
        today=ctx.today,
    ),
}


def rule_ids_in(output: Any) -> list[str]:
    """Collect every knowledge-base rule id mentioned anywhere in a tool output.

    Used to show "Rules consulted" in the trace.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "rule_ids" and isinstance(value, list):
                    found.extend(v for v in value if isinstance(v, str))
                elif key == "rule_id" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(output)
    return list(dict.fromkeys(found))  # de-duplicate, keep order


@dataclass
class ToolOutcome:
    output: dict[str, Any]
    is_error: bool

    def as_json(self) -> str:
        # ensure_ascii=False keeps Arabic names readable; default=str
        # handles any stray date objects.
        return json.dumps(self.output, ensure_ascii=False, default=str)

    @property
    def rule_ids(self) -> list[str]:
        return rule_ids_in(self.output)


def execute_tool(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolOutcome:
    """Run one tool call. Never raises: errors become an error outcome.

    Never raising matters: every tool_use the model sends must get a
    tool_result back, or the next API request is rejected.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolOutcome({"error": f"Unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}."}, True)
    if args is not None and not isinstance(args, dict):
        return ToolOutcome({"error": "Tool input must be a JSON object."}, True)
    try:
        return ToolOutcome(handler(args or {}, ctx), False)
    except (ToolInputError, ValueError) as exc:
        # Bad arguments: tell the model what was wrong so it can retry.
        return ToolOutcome({"error": str(exc)}, True)
    except Exception as exc:  # noqa: BLE001 - a bug in a tool must not crash the loop
        return ToolOutcome({"error": f"Internal error in {name}: {type(exc).__name__}: {exc}"}, True)
