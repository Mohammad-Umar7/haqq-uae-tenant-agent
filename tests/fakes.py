"""Test doubles shaped like the Anthropic SDK's objects (no network, no key).

They only have the attributes our code reads, so a test fails loudly if
the code starts relying on something new.
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS
from typing import Any


def text(t: str) -> NS:
    return NS(type="text", text=t)


def thinking(t: str = "") -> NS:
    return NS(type="thinking", thinking=t, signature="sig")


def tool(id_: str, name: str, input_: dict[str, Any]) -> NS:
    return NS(type="tool_use", id=id_, name=name, input=input_)


def usage(i: int = 100, o: int = 20, r: int = 0, w: int = 0) -> NS:
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=r, cache_creation_input_tokens=w)


def reply(stop_reason: str, *blocks: NS, u: NS | None = None) -> NS:
    return NS(stop_reason=stop_reason, content=list(blocks), usage=u or usage())


def json_reply(data: dict[str, Any], u: NS | None = None) -> NS:
    """A structured-outputs response: thinking first (as Haiku 5.5 may send), then the JSON text."""
    return reply("end_turn", thinking(), text(json.dumps(data)), u=u or usage(1500, 900))


class FakeClient:
    """Stands in for anthropic.Anthropic(); messages.create pops the next scripted reply."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = self   # so client.messages.create works

    def create(self, **kwargs: Any) -> Any:
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = list(snapshot["messages"])
        self.calls.append(snapshot)
        if not self._replies:
            raise AssertionError("FakeClient ran out of scripted replies")
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_pdf(pages: list[list[str]]) -> bytes:
    """A small text PDF built by hand: one list of lines per page ([] = a blank, 'scanned' page)."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]  # pages object filled in below
    font_id = 3
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for lines in pages:
        ops = ["BT", "/F1 9 Tf", "11 TL", "40 800 Td"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_id, content_id)
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_at)
    return bytes(pdf)


# A valid extraction of samples/sample_contract.txt, as Claude would return it.
SAMPLE_EXTRACTION: dict[str, Any] = {
    "is_tenancy_contract": True,
    "language": "english",
    "landlord": "Mr. Hamad Saif Al Marri",
    "tenant": "Ms. Priya Raman",
    "property": "Apartment 1504, Marina Crest Tower, Dubai Marina, Dubai",
    "property_type": "Residential apartment, 1 Bedroom (1BR), approx. 780 sq ft",
    "annual_rent_aed": 85000,
    "start_date": "2026-01-15",
    "end_date": "2027-01-14",
    "security_deposit_aed": 4250,
    "number_of_cheques": 4,
    "mentions_ejari": True,
    "clauses": [
        {"number": "6", "section": "TERMS AND CONDITIONS", "translated_from_arabic": False,
         "text": "This contract shall be registered with Ejari within fourteen (14) days of signing."},
        {"number": "7", "section": "ADDITIONAL TERMS", "translated_from_arabic": False,
         "text": "The security deposit is non-refundable and shall be retained by the Landlord upon expiry "
                 "of this contract regardless of the condition of the property."},
        {"number": "8", "section": "ADDITIONAL TERMS", "translated_from_arabic": False,
         "text": "The Landlord reserves the right to increase the rent at any time during the tenancy period "
                 "by giving the Tenant thirty (30) days' notice."},
    ],
    "red_flags": [
        {"flag_id": "non_refundable_deposit", "clause_number": "7", "quote": "The security deposit is non-refundable"},
        {"flag_id": "rent_increase_any_time", "clause_number": "8", "quote": "increase the rent at any time"},
    ],
    "embedded_instructions": [],
    "notes": None,
}
