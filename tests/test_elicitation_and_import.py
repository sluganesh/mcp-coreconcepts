"""Confirming deletes (elicitation) and bulk import (logging to the client)."""

import pytest

from conftest import db_query
from mcp import types

pytestmark = pytest.mark.anyio


def employee_exists(employee_id: int) -> bool:
    return db_query("SELECT 1 FROM employees WHERE id = %s", (employee_id,)) != []


# --- Delete confirmation ----------------------------------------------------------


async def test_delete_asks_first_and_deletes_when_confirmed(harness):
    harness.answers.append(types.ElicitResult(action="accept", content={"confirm": True}))
    result = await harness.client.call_tool("delete_employee", {"employee_id": 3})

    assert harness.prompts_shown == [
        "Permanently delete Rahul Verma (id 3, Engineering Manager, Engineering)? This cannot be undone."
    ]
    assert result.structured_content["first_name"] == "Rahul"
    assert not employee_exists(3)


@pytest.mark.parametrize("answer, message", [
    (types.ElicitResult(action="decline"), "The user declined"),
    (types.ElicitResult(action="cancel"), "The user cancelled"),
    (types.ElicitResult(action="accept", content={"confirm": False}), "Deletion was not confirmed"),
])
async def test_delete_keeps_the_employee_unless_confirmed(harness, answer, message):
    harness.answers.append(answer)
    result = await harness.client.call_tool("delete_employee", {"employee_id": 3})
    assert result.is_error
    assert message in result.content[0].text
    assert employee_exists(3)


async def test_delete_refuses_when_the_client_cannot_ask(client_without_elicitation):
    result = await client_without_elicitation.call_tool("delete_employee", {"employee_id": 3})
    assert result.is_error
    assert "needs a client that supports confirmation prompts" in result.content[0].text
    assert employee_exists(3)


async def test_delete_unknown_employee_asks_nothing(harness):
    result = await harness.client.call_tool("delete_employee", {"employee_id": 999})
    assert "No employee found with id 999" in result.content[0].text
    assert harness.prompts_shown == []


async def test_confirmation_answer_is_not_a_tool_argument(harness):
    tools = {t.name: t for t in (await harness.client.list_tools()).tools}
    assert list(tools["delete_employee"].input_schema["properties"]) == ["employee_id"]


# --- Bulk import ------------------------------------------------------------------

CSV = """first_name,last_name,email,department,job_title,salary,hire_date
Kavya,Menon,kavya.menon@example.com,Engineering,Frontend Engineer,92000,2026-10-01
Rohan,Das,rohan.das@example.com,Sales,Account Executive,64000,
Bad,Email,not-an-email,Sales,Intern,30000,
Aarav,Clone,aarav.sharma@example.com,Engineering,Engineer,80000,
Kavya,Again,kavya.menon@example.com,Engineering,Engineer,80000,
"""


async def test_bulk_import_keeps_good_rows_and_reports_bad_ones(harness):
    result = await harness.client.call_tool("bulk_import_employees", {"csv_text": CSV})
    summary = result.structured_content

    assert summary["imported"] == 2
    assert summary["created_ids"] == [11, 12]
    assert summary["skipped"] == 3
    assert [p.split(":")[0] for p in summary["problems"]] == ["line 4", "line 5", "line 6"]
    # The duplicate on line 6 only rolled back its own savepoint: Kavya from line 2 is still there.
    assert db_query("SELECT first_name FROM employees WHERE id > 10 ORDER BY id") == [("Kavya",), ("Rohan",)]


async def test_bulk_import_sends_log_messages_at_each_level(harness):
    await harness.client.call_tool("bulk_import_employees", {"csv_text": CSV})
    levels = [level for level, _ in harness.logs]
    assert levels[0] == "info" and levels[-1] == "info"
    assert levels.count("warning") == 3  # one per skipped row
    assert levels.count("debug") == 2    # one per imported row
    assert harness.logs[-1][1] == "Import finished: 2 imported, 3 skipped."


@pytest.mark.parametrize("csv_text, message", [
    ("name,email\nX,x@example.com", "missing these columns"),
    ("first_name,last_name,email,department,job_title,salary\n", "no rows"),
])
async def test_bulk_import_rejects_unusable_csv(harness, csv_text, message):
    result = await harness.client.call_tool("bulk_import_employees", {"csv_text": csv_text})
    assert result.is_error
    assert message in result.content[0].text
