"""Resources, resource templates, prompts and completions."""

import json

import pytest

from mcp import types
from mcp.shared.exceptions import MCPError

pytestmark = pytest.mark.anyio


# --- Resources --------------------------------------------------------------------


async def test_resources_and_templates_are_listed(harness):
    resources = (await harness.client.list_resources()).resources
    templates = (await harness.client.list_resource_templates()).resource_templates
    assert [str(r.uri) for r in resources] == ["hr://handbook"]
    assert {t.uri_template for t in templates} == {
        "employees://{employee_id}", "departments://{department}/employees",
    }


async def test_read_handbook(harness):
    content = (await harness.client.read_resource("hr://handbook")).contents[0]
    assert content.mime_type == "text/markdown"
    assert content.text.startswith("# Example Corp Employee Handbook")


async def test_read_employee_profile(harness):
    content = (await harness.client.read_resource("employees://3")).contents[0]
    assert content.mime_type == "application/json"
    assert json.loads(content.text)["first_name"] == "Rahul"


async def test_department_roster_is_not_case_sensitive(harness):
    content = (await harness.client.read_resource("departments://engineering/employees")).contents[0]
    roster = json.loads(content.text)
    assert roster["count"] == 3
    assert {e["department"] for e in roster["employees"]} == {"Engineering"}


@pytest.mark.parametrize("uri, message", [
    ("employees://999", "No employee found with id 999"),
    ("employees://abc", "not a valid employee id"),
    ("departments://Legal/employees", "Departments: Engineering, Finance, HR, Marketing, Sales"),
    ("payroll://2026", "Unknown resource"),
])
async def test_resource_errors_are_protocol_errors(harness, uri, message):
    # Unlike tools, a failed resource read raises instead of returning is_error.
    with pytest.raises(MCPError) as caught:
        await harness.client.read_resource(uri)
    assert caught.value.code == types.INVALID_PARAMS  # -32602
    assert message in str(caught.value)


# --- Prompts ----------------------------------------------------------------------


async def test_prompts_are_listed_with_their_arguments(harness):
    prompts = {p.name: p for p in (await harness.client.list_prompts()).prompts}
    assert [(a.name, a.required) for a in prompts["welcome_email"].arguments] == [
        ("employee_id", True), ("tone", False),
    ]
    assert [a.name for a in prompts["department_headcount_report"].arguments] == ["department"]


async def test_welcome_email_attaches_profile_and_handbook(harness):
    result = await harness.client.get_prompt("welcome_email", {"employee_id": "2", "tone": "friendly"})
    attached = [m.content.resource.uri for m in result.messages if m.content.type == "resource"]
    instructions = result.messages[-1].content.text

    assert [str(uri) for uri in attached] == ["employees://2", "hr://handbook"]
    assert instructions.startswith("Draft a friendly welcome email to Priya Iyer")
    assert "Don't mention salary" in instructions


async def test_headcount_report_attaches_the_roster(harness):
    result = await harness.client.get_prompt("department_headcount_report", {"department": "Engineering"})
    roster = json.loads(result.messages[0].content.resource.text)
    assert roster["count"] == 3
    assert "don't name individuals next to their salaries" in result.messages[-1].content.text


@pytest.mark.parametrize("name, args, message", [
    ("department_headcount_report", {"department": "Legal"}, "No department named 'Legal'"),
    ("welcome_email", {"employee_id": "abc"}, "not a valid employee id"),
])
async def test_prompt_errors_keep_their_message(harness, name, args, message):
    with pytest.raises(MCPError) as caught:
        await harness.client.get_prompt(name, args)
    assert caught.value.code == types.INVALID_PARAMS
    assert message in str(caught.value)


# --- Completions ------------------------------------------------------------------


@pytest.mark.parametrize("ref, argument, expected", [
    (types.PromptReference(type="ref/prompt", name="department_headcount_report"),
     {"name": "department", "value": "eng"}, ["Engineering"]),
    (types.PromptReference(type="ref/prompt", name="welcome_email"),
     {"name": "employee_id", "value": "1"}, ["1", "10"]),
    (types.ResourceTemplateReference(type="ref/resource", uri="departments://{department}/employees"),
     {"name": "department", "value": "m"}, ["Marketing"]),
])
async def test_completions(harness, ref, argument, expected):
    completion = (await harness.client.complete(ref, argument)).completion
    assert completion.values == expected
