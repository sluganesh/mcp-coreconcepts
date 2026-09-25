"""Exercise every tool, resource and prompt, including failure cases.

    python test_client.py                               # starts server.py over stdio
    python test_client.py --http http://127.0.0.1:8000/mcp   # connects to a running HTTP server
"""

import argparse
import asyncio
import sys
import uuid
from contextlib import AsyncExitStack

import anyio
from mcp import Client, ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError


SERVER = StdioServerParameters(command=sys.executable, args=["server.py"])
http_url: str | None = None  # set by --http


def connect():
    """Open a transport to the server. Both return the same (read, write) pair."""
    if http_url:
        return streamable_http_client(http_url)
    return stdio_client(SERVER)

# Scripted answers for confirmation prompts, used in order. A real client
# would show the prompt to the user instead.
elicitation_answers: list[types.ElicitResult] = []


async def answer_elicitation(context, params):
    answer = elicitation_answers.pop(0)
    print(f"    prompt: {params.message}")
    print(f"    answer: {answer.action} {answer.content or ''}")
    return answer


async def main():
    print("Transport:", f"streamable HTTP ({http_url})" if http_url else "stdio")
    async with connect() as (read, write):
        # Passing an elicitation_callback makes the client declare that it
        # supports elicitation, so the server may ask the user questions.
        async with ClientSession(
            read, write, elicitation_callback=answer_elicitation, logging_callback=show_log_message
        ) as session:
            init = await session.initialize()
            print("Protocol version:", init.protocol_version)
            tools = await session.list_tools()
            print("Tools:", [t.name for t in tools.tools])
            result = await session.call_tool("add_integer", {"a": 7, "b": 35})
            print("add_integer(7, 35) =", result.content[0].text)

            for args in ({"a": 10, "b": 4}, {"a": 10, "b": 0}, {"a": 10, "b": "abc"}):
                result = await session.call_tool("divide", args)
                status = "ERROR" if result.is_error else "OK"
                print(f"divide({args}) -> {status}: {result.content[0].text}")

            for args in ({}, {"employee_id": 3}, {"employee_id": 999}, {"employee_id": 0}):
                result = await session.call_tool("get_employees", args)
                if result.is_error:
                    print(f"get_employees({args}) -> ERROR: {result.content[0].text}")
                else:
                    data = result.structured_content
                    print(f"get_employees({args}) -> OK: {data['count']} row(s), first: {data['employees'][0]}")

            await test_pagination(session)
            await test_write_tools(session)
            await test_resources(session)
            await test_prompts(session)
            await test_bulk_import(session)
            await test_long_running_task(session)

    await test_change_notifications()


def show(name, args, result):
    if result.is_error:
        print(f"{name}({args}) -> ERROR: {result.content[0].text}")
    else:
        print(f"{name}({args}) -> OK: {result.structured_content}")
    return result


async def test_pagination(session):
    # Follow next_cursor until it's null, the way any MCP client pages through results.
    args = {"page_size": 4}
    page = 1
    while True:
        data = (await session.call_tool("get_employees", args)).structured_content
        ids = [e["id"] for e in data["employees"]]
        print(f"get_employees page {page}: ids {ids} (count {data['count']}, total {data['total']}), "
              f"next_cursor={data['next_cursor']}")
        if data["next_cursor"] is None:
            break
        args = {"page_size": 4, "cursor": data["next_cursor"]}
        page += 1

    for args in ({"cursor": "not-a-real-cursor"}, {"page_size": 0}, {"page_size": 500}):
        show("get_employees", args, await session.call_tool("get_employees", args))


async def test_write_tools(session):
    print("Annotations (read_only / destructive / idempotent / open_world):")
    for tool in (await session.list_tools()).tools:
        a = tool.annotations
        print(f"  {tool.name:24} {a.read_only_hint} / {a.destructive_hint} / {a.idempotent_hint} / {a.open_world_hint}")

    # A unique email per run, so the test can be repeated.
    email = f"test.{uuid.uuid4().hex[:8]}@example.com"
    new = {
        "first_name": "Test", "last_name": "User", "email": email,
        "department": "QA", "job_title": "Tester", "salary": 50000,
    }
    result = show("create_employee", new, await session.call_tool("create_employee", new))
    new_id = result.structured_content["id"]

    for name, args in (
        ("create_employee", {**new, "first_name": "Dup"}),              # duplicate email
        ("create_employee", {**new, "email": "not-an-email"}),          # bad email format
        ("create_employee", {**new, "email": "x@example.com", "salary": -5}),  # salary must be > 0
        ("update_employee_salary", {"employee_id": new_id, "new_salary": 55000}),
        ("update_employee_salary", {"employee_id": 999, "new_salary": 55000}),
    ):
        show(name, args, await session.call_tool(name, args))

    await test_delete_confirmation(session, new_id)


async def test_delete_confirmation(session, employee_id):
    args = {"employee_id": employee_id}
    # Each answer is one way the user can respond to the confirmation prompt.
    for answer in (
        types.ElicitResult(action="decline"),
        types.ElicitResult(action="cancel"),
        types.ElicitResult(action="accept", content={"confirm": False}),
    ):
        elicitation_answers.append(answer)
        show("delete_employee", args, await session.call_tool("delete_employee", args))

    # A client without elicitation support is refused, and nothing is deleted.
    async with connect() as (read, write):
        async with ClientSession(read, write) as no_prompt_session:
            await no_prompt_session.initialize()
            print("  (client without elicitation support)")
            show("delete_employee", args, await no_prompt_session.call_tool("delete_employee", args))

    # Confirmed: the employee is deleted. A second attempt finds nothing, so no prompt is shown.
    elicitation_answers.append(types.ElicitResult(action="accept", content={"confirm": True}))
    show("delete_employee", args, await session.call_tool("delete_employee", args))
    show("delete_employee", args, await session.call_tool("delete_employee", args))


async def test_resources(session):
    print("Resources:", [(str(r.uri), r.mime_type) for r in (await session.list_resources()).resources])
    templates = (await session.list_resource_templates()).resource_templates
    print("Resource templates:", [t.uri_template for t in templates])

    for uri in (
        "hr://handbook",
        "employees://3",
        "departments://engineering/employees",
        "employees://999",                    # no such employee
        "employees://abc",                    # not an integer
        "departments://Legal/employees",      # no such department
        "payroll://2026",                     # nothing registered for this URI
    ):
        # Unlike tools, resource failures are raised as protocol errors.
        try:
            content = (await session.read_resource(uri)).contents[0]
        except MCPError as exc:
            print(f"read {uri} -> MCPError {exc.code}: {exc}")
            continue
        preview = content.text if len(content.text) < 200 else content.text[:120].replace("\n", " ") + "..."
        print(f"read {uri} -> OK ({content.mime_type}): {preview}")


async def test_prompts(session):
    for p in (await session.list_prompts()).prompts:
        args = [f"{a.name}{'' if a.required else '?'}" for a in p.arguments or []]
        print(f"Prompt: {p.name}({', '.join(args)}) - {p.description}")

    for name, args in (
        ("department_headcount_report", {"department": "Engineering"}),
        ("welcome_email", {"employee_id": "2", "tone": "friendly"}),
        ("department_headcount_report", {"department": "Legal"}),   # no such department
        ("welcome_email", {"employee_id": "abc"}),                  # not a valid id
    ):
        try:
            result = await session.get_prompt(name, args)
        except MCPError as exc:
            print(f"get_prompt {name}({args}) -> MCPError {exc.code}: {exc}")
            continue
        print(f"get_prompt {name}({args}) -> {len(result.messages)} messages:")
        for m in result.messages:
            if m.content.type == "resource":
                print(f"    [{m.role}] attached {m.content.resource.uri} ({m.content.resource.mime_type})")
            else:
                print(f"    [{m.role}] {m.content.text[:110]}...")

    # Autocomplete works for prompt arguments and resource template placeholders.
    for ref, arg in (
        (types.PromptReference(type="ref/prompt", name="department_headcount_report"), {"name": "department", "value": "eng"}),
        (types.PromptReference(type="ref/prompt", name="welcome_email"), {"name": "employee_id", "value": "1"}),
        (types.ResourceTemplateReference(type="ref/resource", uri="departments://{department}/employees"), {"name": "department", "value": "m"}),
    ):
        completion = (await session.complete(ref, arg)).completion
        print(f"complete {arg} -> {completion.values}")


async def show_log_message(params):
    print(f"    log [{params.level}] {params.data}")


async def test_bulk_import(session):
    suffix = uuid.uuid4().hex[:6]
    csv_text = f"""first_name,last_name,email,department,job_title,salary,hire_date
Nisha,Rao,nisha.{suffix}@example.com,Engineering,Software Engineer,90000,
Omar,Khan,omar.{suffix}@example.com,Sales,Account Executive,65000,2026-10-01
Bad,Row,not-an-email,Sales,Intern,-100,
Dup,Email,aarav.sharma@example.com,Engineering,Engineer,80000,
"""
    print("bulk_import_employees (4 rows: 2 valid, 1 invalid, 1 duplicate):")
    result = await session.call_tool("bulk_import_employees", {"csv_text": csv_text})
    summary = result.structured_content
    print(f"  -> imported {summary['imported']}, skipped {summary['skipped']}, ids {summary['created_ids']}")
    for problem in summary["problems"]:
        print(f"     problem: {problem}")

    result = await session.call_tool("bulk_import_employees", {"csv_text": "name,email\nX,x@example.com"})
    print(f"bulk_import_employees (wrong header) -> ERROR: {result.content[0].text}")

    # Clean up: delete the imported employees, confirming each prompt.
    for employee_id in summary["created_ids"]:
        elicitation_answers.append(types.ElicitResult(action="accept", content={"confirm": True}))
        await session.call_tool("delete_employee", {"employee_id": employee_id})


async def test_change_notifications():
    # Subscriptions need the 2026-07-28 protocol. The high-level Client negotiates
    # it; the ClientSession used above does the older handshake (2025-11-25).
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(Client(http_url or SERVER))
        # Over HTTP, a second client makes the changes: a shared server notifies
        # every subscriber. Over stdio each client has its own server, so the
        # same client both watches and edits.
        editor = await stack.enter_async_context(Client(http_url)) if http_url else client
        who = "another client" if http_url else "the same client"
        print(f"Change notifications (protocol {client.protocol_version}, changes made by {who}):")
        watched = ["employees://3", "departments://Engineering/employees"]
        subscription = await stack.enter_async_context(client.listen(resource_subscriptions=watched))
        print(f"  subscribed to {subscription.honored.resource_subscriptions}")

        # Change Rahul's salary, then put it back, so the sample data is unchanged.
        for salary in (150000, 145000):
            await editor.call_tool("update_employee_salary", {"employee_id": 3, "new_salary": salary})
            with anyio.fail_after(5):
                for _ in watched:
                    event = await anext(subscription)
                    print(f"  salary -> {salary}: {type(event).__name__} {event.uri}")

        # A change to someone in another department doesn't notify these subscriptions.
        await editor.call_tool("update_employee_salary", {"employee_id": 4, "new_salary": 72000})
        with anyio.move_on_after(1) as waited:
            event = await anext(subscription)
            print(f"  unexpected event: {event}")
        if waited.cancelled_caught:
            print("  employee 4 (Marketing) changed -> no event for these subscriptions, as expected")


async def show_progress(progress, total, message):
    print(f"    progress: {progress:g}/{total:g} - {message}")


async def test_long_running_task(session):
    # 1. Finishes within the server-side timeout.
    # 2. Server-side timeout: the tool stops itself and returns an error result.
    for args in ({"duration_seconds": 2, "timeout_seconds": 5}, {"duration_seconds": 8, "timeout_seconds": 3}):
        print(f"long_running_task({args}):")
        result = await session.call_tool("long_running_task", args, progress_callback=show_progress)
        status = "ERROR" if result.is_error else "OK"
        print(f"  -> {status}: {result.content[0].text}")

    # 3. Client-side timeout: the client stops waiting before the task finishes.
    args = {"duration_seconds": 10, "timeout_seconds": 30}
    print(f"long_running_task({args}) with a 2s client timeout:")
    try:
        await session.call_tool("long_running_task", args, read_timeout_seconds=2)
    except MCPError as exc:
        if exc.code != types.REQUEST_TIMEOUT:
            raise
        # The SDK also sends a cancellation, so the server stops the task.
        print(f"  -> CLIENT TIMEOUT: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--http", metavar="URL", help="Connect to a server already running with --http.")
    http_url = parser.parse_args().http
    asyncio.run(main())
