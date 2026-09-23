"""Start server.py over stdio and call add_integer once."""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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


if __name__ == "__main__":
    asyncio.run(main())
