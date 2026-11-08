import pytest

from agent.contract import ContractLoadError, load_contract_file, split_clauses, validate_contract_text
from agent.tools.extract import extract_contract_terms


def test_sample_terms(sample_text):
    r = extract_contract_terms(sample_text)
    t = r["terms"]
    assert t["landlord"] == "Mr. Hamad Saif Al Marri"
    assert t["tenant"] == "Ms. Priya Raman"
    assert "Dubai Marina" in t["property"]
    assert "1BR" in t["property_type"]
    assert t["annual_rent_aed"] == 85_000
    assert t["start_date"] == "2026-01-15"
    assert t["end_date"] == "2027-01-14"
    assert t["security_deposit_aed"] == 4_250
    assert t["number_of_cheques"] == 4
    assert t["mentions_ejari"] is True
    assert r["missing_fields"] == []
    assert r["derived"]["amount_per_cheque_aed"] == 21_250
    assert r["derived"]["contract_length_days"] == 365
    assert r["special_clause_numbers"][:3] == ["7", "8", "9"]
    assert len(r["clauses"]) == 13


def test_alternative_layout():
    text = (
        "Lessor: Example Holdings LLC\n"
        "Lessee: John Doe\n"
        "Premises: Villa 12, Arabian Ranches\n"
        "Start Date: 01/03/2026\n"
        "End Date: 28/02/2027\n"
        "Yearly Rent: 120,000 AED payable in two (2) post-dated cheques\n"
        "Security Deposit: Dhs 6,000\n"
    )
    t = extract_contract_terms(text)["terms"]
    assert t["landlord"] == "Example Holdings LLC"
    assert t["tenant"] == "John Doe"
    assert t["property"] == "Villa 12, Arabian Ranches"
    assert t["start_date"] == "2026-03-01"
    assert t["end_date"] == "2027-02-28"
    assert t["annual_rent_aed"] == 120_000
    assert t["number_of_cheques"] == 2
    assert t["security_deposit_aed"] == 6_000
    assert t["mentions_ejari"] is False


def test_missing_fields_are_reported():
    r = extract_contract_terms("Tenant: Someone\nSome text without the other terms.")
    assert "annual_rent_aed" in r["missing_fields"]
    assert "end_date" in r["missing_fields"]
    assert "tenant" not in r["missing_fields"]


def test_split_clauses_joins_wrapped_lines():
    text = "TERMS\n1. The tenant shall\npay on time.\n2. Second clause."
    clauses = split_clauses(text)
    assert [c.number for c in clauses] == ["1", "2"]
    assert clauses[0].text == "The tenant shall pay on time."
    assert clauses[0].section == "TERMS"


def test_validate_rejects_too_short_text():
    with pytest.raises(ContractLoadError):
        validate_contract_text("too short")


def _make_pdf(lines: list[str]) -> bytes:
    """Build a tiny one-page text PDF by hand (no extra libraries needed)."""
    ops = ["BT", "/F1 9 Tf", "11 TL", "40 800 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
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


def test_pdf_upload_round_trip(tmp_path, sample_text):
    lines = [line for line in sample_text.splitlines()][:70]
    pdf_path = tmp_path / "contract.pdf"
    pdf_path.write_bytes(_make_pdf(lines))

    text = load_contract_file(pdf_path)
    terms = extract_contract_terms(text)["terms"]
    assert terms["annual_rent_aed"] == 85_000
    assert terms["end_date"] == "2027-01-14"


def test_unsupported_file_type(tmp_path):
    p = tmp_path / "contract.docx"
    p.write_bytes(b"not really a docx")
    with pytest.raises(ContractLoadError):
        load_contract_file(p)
