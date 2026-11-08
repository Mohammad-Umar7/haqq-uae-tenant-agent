from datetime import date

import pytest

from agent.registry import _HANDLERS, TOOL_NAMES, TOOL_SCHEMAS, ToolContext, execute_tool, rule_ids_in


@pytest.fixture
def ctx(sample_text):
    return ToolContext(contract_text=sample_text, today=date(2026, 11, 8))


def test_every_schema_has_a_handler_and_vice_versa():
    assert set(TOOL_NAMES) == set(_HANDLERS)
    for schema in TOOL_SCHEMAS:
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"


def test_the_six_tools_exist():
    # lookup_tenant_rule was merged into search_tenancy_law (official law text + plain-English notes).
    assert set(TOOL_NAMES) == {
        "extract_contract_terms", "rera_rent_increase_check", "notice_deadline_check",
        "clause_risk_scan", "search_tenancy_law", "draft_letter",
    }


def test_unknown_tool_is_an_error(ctx):
    out = execute_tool("delete_everything", {}, ctx)
    assert out.is_error and "Unknown tool" in out.output["error"]


def test_contract_tools_need_a_contract():
    out = execute_tool("extract_contract_terms", {}, ToolContext(contract_text=None, today=date(2026, 11, 8)))
    assert out.is_error and "No contract" in out.output["error"]


def test_numbers_formatted_as_text_are_accepted(ctx):
    out = execute_tool(
        "rera_rent_increase_check",
        {"current_annual_rent": "AED 85,000", "rera_average_rent": "100,000"},
        ctx,
    )
    assert not out.is_error
    assert out.output["max_increase_percent"] == 5


def test_missing_required_argument_comes_back_as_error(ctx):
    out = execute_tool("rera_rent_increase_check", {"current_annual_rent": 85000}, ctx)
    assert out.is_error and "rera_average_rent" in out.output["error"]


def test_tool_validation_errors_come_back_as_error(ctx):
    out = execute_tool("notice_deadline_check", {"contract_end_date": "2027-01-14", "notice_type": "party"}, ctx)
    assert out.is_error


def test_notice_tool_uses_context_today(ctx):
    out = execute_tool("notice_deadline_check", {"contract_end_date": "2027-01-14", "notice_type": "rent_increase"}, ctx)
    assert out.output["today"].startswith("2026-11-08")


def test_non_dict_input_is_rejected(ctx):
    assert execute_tool("search_tenancy_law", ["deposit"], ctx).is_error


def test_output_is_json_and_rule_ids_are_collected(ctx):
    out = execute_tool("clause_risk_scan", {}, ctx)
    assert out.as_json().startswith("{")
    assert "security_deposit_return" in out.rule_ids


def test_rule_ids_in_walks_nested_structures():
    data = {"rule_ids": ["a"], "rules": [{"rule_id": "b"}, {"x": {"rule_ids": ["a", "c"]}}]}
    assert rule_ids_in(data) == ["a", "b", "c"]
