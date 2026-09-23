"""Start server.py over stdio and exercise every tool, including failure cases."""

import asyncio
import sys

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
                    rows = result.structured_content["result"]
                    print(f"get_employees({args}) -> OK: {len(rows)} row(s), first: {rows[0]}")

            await test_long_running_task(session)


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
