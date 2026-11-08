"""Tool: draft_letter - build a polite, firm letter (or RDSC summary) from a template.

Why a template instead of letting the LLM write freely? The legal citations
are copied from the knowledge base by rule id, so the letter can only cite
rules that exist. Unknown rule ids are dropped and reported back. The LLM
chooses the facts and the request; the template guarantees the structure.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from agent.knowledge import get_rule

LETTER_TYPES = (
    "rent_increase_objection",
    "eviction_notice_response",
    "deposit_return_request",
    "clause_concern",
    "rdsc_complaint_summary",
)

_SUBJECTS = {
    "rent_increase_objection": "Proposed rent increase for {property}",
    "eviction_notice_response": "Your notice to vacate {property}",
    "deposit_return_request": "Return of the security deposit for {property}",
    "clause_concern": "Clauses in our tenancy contract for {property}",
    "rdsc_complaint_summary": "RDSC complaint summary: {property}",
}

_OPENINGS = {
    "rent_increase_objection": (
        "Thank you for your notice about the rent for the renewal of our tenancy contract. "
        "I would like to continue renting the property, but I have checked the proposed increase "
        "against the Dubai rules and I believe it cannot be applied as proposed."
    ),
    "eviction_notice_response": (
        "I have received your notice asking me to vacate the property. I have checked it against "
        "the Dubai tenancy rules and I have concerns about whether it is valid."
    ),
    "deposit_return_request": (
        "I am writing about the return of my security deposit at the end of our tenancy."
    ),
    "clause_concern": (
        "While reviewing our tenancy contract I noticed some clauses that I believe do not match "
        "the Dubai tenancy rules, and I would like to discuss them with you."
    ),
}

_DEFAULT_REQUESTS = {
    "rent_increase_objection": "I ask that the rent for the renewal stays within the legal limit set out above.",
    "eviction_notice_response": "I ask you to confirm in writing that the tenancy continues until valid notice is given in line with the rules above.",
    "deposit_return_request": "I ask you to return the full security deposit, or to send me an itemised list with evidence of any deduction you believe is justified.",
    "clause_concern": "I ask that we agree in writing that these clauses will be applied in line with the rules above.",
    "rdsc_complaint_summary": "The tenant asks the RDSC to resolve the dispute in line with the rules listed above.",
}


def _legal_basis(rule_ids: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """Split requested rule ids into (known rules, unknown ids)."""
    known, unknown = [], []
    for rule_id in dict.fromkeys(rule_ids):  # de-duplicate, keep order
        rule = get_rule(rule_id)
        if rule:
            known.append({"rule_id": rule_id, "citation": rule["citation"], "summary": rule["summary"]})
        else:
            unknown.append(rule_id)
    return known, unknown


def draft_letter(
    letter_type: str,
    facts: list[str],
    rule_ids: list[str],
    request: str | None = None,
    tenant_name: str | None = None,
    landlord_name: str | None = None,
    property_address: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    if letter_type not in LETTER_TYPES:
        raise ValueError(f"letter_type must be one of {', '.join(LETTER_TYPES)}.")
    facts = [f.strip() for f in facts if f and f.strip()][:12]
    if not facts:
        raise ValueError("facts must contain at least one short statement of fact.")

    tenant = (tenant_name or "").strip() or "[Your name]"
    landlord = (landlord_name or "").strip() or "[Landlord's name]"
    prop = (property_address or "").strip() or "[Property address]"
    request_text = (request or "").strip() or _DEFAULT_REQUESTS[letter_type]
    day = (today or date.today()).strftime("%d %B %Y").lstrip("0")

    rules, unknown = _legal_basis(rule_ids)
    subject = _SUBJECTS[letter_type].format(property=prop)
    fact_lines = "\n".join(f"- {f}" for f in facts)
    rule_lines = "\n".join(f"- {r['citation']}: {r['summary']}" for r in rules)

    if letter_type == "rdsc_complaint_summary":
        body = (
            "RDSC COMPLAINT SUMMARY (draft for the tenant's own preparation)\n\n"
            f"Complainant (tenant): {tenant}\n"
            f"Respondent (landlord): {landlord}\n"
            f"Property: {prop}\n"
            f"Prepared on: {day}\n\n"
            "1. Facts\n"
            f"{fact_lines}\n\n"
            "2. Rules relied on\n"
            f"{rule_lines or '- (none cited; ask the RDSC which rules apply)'}\n\n"
            "3. What the tenant is asking for\n"
            f"{request_text}\n\n"
            "4. Documents to bring\n"
            "- Tenancy contract and Ejari certificate\n"
            "- The landlord's notice and any written messages\n"
            "- Proof of rent payments (cheque copies, bank statements)\n"
            "- RERA Rental Index calculator result, if the dispute is about rent\n"
            "- Move-in / move-out photos, if the dispute is about the deposit\n\n"
            "Check the RDSC's current filing steps and fees on its official channels before filing."
        )
    else:
        legal_section = (
            f"The rules I am relying on:\n{rule_lines}\n\n" if rules else ""
        )
        body = (
            f"Date: {day}\n"
            f"To: {landlord}\n"
            f"From: {tenant}\n"
            f"Property: {prop}\n\n"
            f"Subject: {subject}\n\n"
            f"Dear {landlord},\n\n"
            f"{_OPENINGS[letter_type]}\n\n"
            f"The facts as I understand them:\n{fact_lines}\n\n"
            f"{legal_section}"
            f"{request_text}\n\n"
            "I would prefer to settle this amicably and I am happy to discuss it. Please reply in writing "
            "within 14 days. If we cannot agree, I may refer the matter to the Rental Disputes Settlement "
            "Centre (RDSC).\n\n"
            "Kind regards,\n"
            f"{tenant}"
        )

    return {
        "letter_type": letter_type,
        "subject": subject,
        "body": body,
        "cited_rules": [r["citation"] for r in rules],
        "rule_ids": [r["rule_id"] for r in rules],
        "ignored_rule_ids": unknown,
        "note": "Draft only. Check every fact and fill any [placeholders] before sending. Not legal advice.",
    }
