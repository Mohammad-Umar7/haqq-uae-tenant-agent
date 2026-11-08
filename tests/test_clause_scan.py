import pytest

from agent.tools.clause_scan import RED_FLAGS, clause_risk_scan

EJARI = "\n\n20. This contract shall be registered with Ejari."


def flag_ids(text: str) -> set[str]:
    return {f["flag_id"] for f in clause_risk_scan(text)["flags"]}


def test_sample_contract_flags_exactly_the_three_planted_clauses(sample_text):
    result = clause_risk_scan(sample_text)
    found = {(f["flag_id"], f["clause_number"]) for f in result["flags"]}
    assert found == {
        ("non_refundable_deposit", "7"),
        ("rent_increase_any_time", "8"),
        ("early_termination_penalty_high", "9"),
    }
    # Clause 5 puts structural maintenance on the LANDLORD: must not be flagged.
    assert "tenant_bears_all_maintenance" not in {f["flag_id"] for f in result["flags"]}
    assert result["clauses_scanned"] == 13


def test_flags_carry_rule_citations(sample_text):
    flags = {f["flag_id"]: f for f in clause_risk_scan(sample_text)["flags"]}
    assert flags["non_refundable_deposit"]["rule_ids"] == ["security_deposit_return"]
    assert "Art. 20" in flags["non_refundable_deposit"]["citations"][0]
    # No curated rule for termination penalties: no citation invented.
    assert flags["early_termination_penalty_high"]["rule_ids"] == []


def test_high_severity_sorted_first(sample_text):
    severities = [f["severity"] for f in clause_risk_scan(sample_text)["flags"]]
    assert severities == sorted(severities, key=["high", "medium", "low", "info"].index)


@pytest.mark.parametrize(
    "clause, expected_flag",
    [
        ("1. The deposit paid is non-refundable.", "non_refundable_deposit"),
        ("1. The security deposit shall be forfeited at the end of the term.", "non_refundable_deposit"),
        ("1. The Landlord may revise the rent at any time during the contract.", "rent_increase_any_time"),
        ("1. The rent may be increased during the tenancy at the Landlord's discretion.", "rent_increase_any_time"),
        ("1. Should the Tenant terminate early, a penalty of three (3) months' rent applies.", "early_termination_penalty_high"),
        ("1. The Tenant shall bear all maintenance costs, including structural repairs.", "tenant_bears_all_maintenance"),
        ("1. The Tenant waives the right to file any case at the Rental Dispute Settlement Centre.", "waiver_of_rdsc"),
        ("1. The Tenant shall not file any complaint with the RDSC.", "waiver_of_rdsc"),
        ("1. This contract will automatically renew each year with the rent increased by 8%.", "auto_renewal_fixed_increase"),
        ("1. The Landlord may terminate this contract by giving the Tenant thirty (30) days' notice.", "short_landlord_termination"),
        ("1. The Landlord may require the Tenant to vacate on 3 months notice.", "short_landlord_termination"),
    ],
)
def test_each_red_flag_detects_an_example(clause, expected_flag):
    assert expected_flag in flag_ids(clause + EJARI)


@pytest.mark.parametrize(
    "clause",
    [
        "1. The security deposit shall be refunded to the Tenant at the end of the tenancy.",
        "1. If the Tenant terminates early, a penalty of two (2) months' rent applies.",
        "1. Rent increases at renewal follow the RERA Rental Index.",
        "1. The Landlord may terminate for own use by giving twelve (12) months' notice by notary public.",
        "1. Major and structural maintenance shall be the responsibility of the Landlord.",
    ],
)
def test_fair_clauses_are_not_flagged(clause):
    assert flag_ids(clause + EJARI) == set()


def test_missing_ejari_gives_info_flag():
    result = clause_risk_scan("1. The Tenant shall pay rent on time.\n\n2. Pets are allowed.")
    flags = result["flags"]
    assert [f["flag_id"] for f in flags] == ["ejari_not_mentioned"]
    assert flags[0]["severity"] == "info"


def test_unnumbered_text_is_still_scanned():
    text = "The deposit is non-refundable under any circumstances.\n\nRegistered with Ejari."
    assert "non_refundable_deposit" in flag_ids(text)


def test_red_flag_ids_are_unique():
    ids = [rf.id for rf in RED_FLAGS]
    assert len(ids) == len(set(ids))
