"""Math tools, reading and paging employees, and the write tools."""

from datetime import date

import pytest

from conftest import db_query

pytestmark = pytest.mark.anyio


async def call(harness, name, args=None):
    return await harness.client.call_tool(name, args or {})


def error_text(result) -> str:
    assert result.is_error, f"expected an error, got {result.structured_content}"
    return result.content[0].text


# --- Math ---------------------------------------------------------------------


async def test_add_integer(harness):
    result = await call(harness, "add_integer", {"a": 7, "b": 35})
    assert result.structured_content == {"result": 42}


async def test_divide(harness):
    result = await call(harness, "divide", {"a": 10, "b": 4})
    assert result.structured_content == {"result": 2.5}


async def test_divide_by_zero_is_a_tool_error(harness):
    result = await call(harness, "divide", {"a": 10, "b": 0})
    assert "Cannot divide by zero" in error_text(result)


async def test_arguments_are_validated_before_the_tool_runs(harness):
    result = await call(harness, "divide", {"a": 10, "b": "abc"})
    assert "validation error" in error_text(result)


# --- Reading employees ----------------------------------------------------------


async def test_get_one_employee(harness):
    result = await call(harness, "get_employees", {"employee_id": 3})
    employee = result.structured_content["employees"][0]
    assert employee["first_name"] == "Rahul"
    assert employee["salary"] == 145000.0
    assert employee["hire_date"] == "2017-11-20"


@pytest.mark.parametrize("employee_id, message", [
    (999, "No employee found with id 999"),
    (0, "must be a positive integer"),
])
async def test_get_employee_errors(harness, employee_id, message):
    result = await call(harness, "get_employees", {"employee_id": employee_id})
    assert message in error_text(result)


async def test_output_schema_describes_every_field(harness):
    tools = {t.name: t for t in (await harness.client.list_tools()).tools}
    employee = tools["get_employees"].output_schema["$defs"]["Employee"]
    assert employee["properties"]["salary"]["type"] == "number"
    assert employee["properties"]["hire_date"]["format"] == "date"
    assert set(employee["required"]) == {
        "id", "first_name", "last_name", "email", "department", "job_title", "salary", "hire_date",
    }


async def test_pagination_follows_next_cursor_to_the_end(harness):
    pages, args = [], {"page_size": 4}
    while True:
        page = (await call(harness, "get_employees", args)).structured_content
        pages.append([e["id"] for e in page["employees"]])
        assert page["total"] == 10
        if page["next_cursor"] is None:
            break
        args = {"page_size": 4, "cursor": page["next_cursor"]}
    assert pages == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]


async def test_pagination_is_stable_when_rows_are_deleted_between_pages(harness):
    first = (await call(harness, "get_employees", {"page_size": 4})).structured_content
    db_query("DELETE FROM employees WHERE id = 2 RETURNING id")  # someone deletes a row on page 1
    second = (await call(harness, "get_employees", {"page_size": 4, "cursor": first["next_cursor"]})).structured_content
    # Keyset pagination continues after id 4, so nobody is skipped (OFFSET 4 would skip id 5).
    assert [e["id"] for e in second["employees"]] == [5, 6, 7, 8]


@pytest.mark.parametrize("args, message", [
    ({"cursor": "not-a-real-cursor"}, "Invalid cursor"),
    ({"page_size": 0}, "validation error"),
    ({"page_size": 101}, "validation error"),
])
async def test_pagination_errors(harness, args, message):
    assert message in error_text(await call(harness, "get_employees", args))


# --- Writing employees ----------------------------------------------------------

NEW_EMPLOYEE = {
    "first_name": "Nisha", "last_name": "Rao", "email": "nisha.rao@example.com",
    "department": "Engineering", "job_title": "Software Engineer", "salary": 90000,
}


async def test_create_employee(harness):
    created = (await call(harness, "create_employee", NEW_EMPLOYEE)).structured_content
    assert created["id"] == 11
    assert created["hire_date"] == date.today().isoformat()  # defaults to today
    assert db_query("SELECT first_name FROM employees WHERE id = 11") == [("Nisha",)]


@pytest.mark.parametrize("changes, message", [
    ({"email": "aarav.sharma@example.com"}, "already exists"),
    ({"email": "not-an-email"}, "validation error"),
    ({"salary": 0}, "validation error"),
    ({"first_name": ""}, "validation error"),
])
async def test_create_employee_rejects_bad_input(harness, changes, message):
    result = await call(harness, "create_employee", {**NEW_EMPLOYEE, **changes})
    assert message in error_text(result)
    assert db_query("SELECT count(*) FROM employees") == [(10,)]  # nothing was added


async def test_update_employee_salary(harness):
    updated = (await call(harness, "update_employee_salary", {"employee_id": 3, "new_salary": 150000})).structured_content
    assert updated["salary"] == 150000.0
    assert db_query("SELECT salary FROM employees WHERE id = 3") == [(150000,)]


async def test_update_unknown_employee(harness):
    result = await call(harness, "update_employee_salary", {"employee_id": 999, "new_salary": 1})
    assert "No employee found with id 999" in error_text(result)


async def test_tool_annotations(harness):
    tools = {t.name: t.annotations for t in (await harness.client.list_tools()).tools}
    assert tools["get_employees"].read_only_hint is True
    assert tools["create_employee"].destructive_hint is False
    assert tools["create_employee"].idempotent_hint is False
    assert tools["update_employee_salary"].destructive_hint is True
    assert tools["delete_employee"].destructive_hint is True
    assert all(a.open_world_hint is False for a in tools.values())
