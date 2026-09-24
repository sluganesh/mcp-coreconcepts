"""Start server.py over stdio and exercise every tool, including failure cases."""

import asyncio
import sys
import uuid

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError


async def main():
    params = StdioServerParameters(command=sys.executable, args=["server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
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

            await test_write_tools(session)
            await test_long_running_task(session)


def show(name, args, result):
    if result.is_error:
        print(f"{name}({args}) -> ERROR: {result.content[0].text}")
    else:
        print(f"{name}({args}) -> OK: {result.structured_content}")
    return result


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
        ("delete_employee", {"employee_id": new_id}),
        ("delete_employee", {"employee_id": new_id}),                   # already deleted
    ):
        show(name, args, await session.call_tool(name, args))


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
    asyncio.run(main())
