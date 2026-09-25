"""Watch an employee's profile and department roster for changes.

Subscribes to resources on the shared HTTP server and prints each change,
re-reading the employee's profile when it changes. Leave it running, then
change the employee from another client (for example, run
update_employee_salary in the MCP Inspector). Press Ctrl+C to stop.

    python server.py --http                       # terminal 1
    python examples/watch_employee.py             # terminal 2: watches employee 3
    python examples/watch_employee.py 5 Marketing # watch another employee and department

Subscriptions need the 2026-07-28 protocol, which the SDK's Client negotiates.
"""

import argparse
import asyncio

from mcp import Client


async def watch(url: str, employee_id: int, department: str) -> None:
    profile_uri = f"employees://{employee_id}"
    watched = [profile_uri, f"departments://{department}/employees"]
    async with Client(url) as client:
        async with client.listen(resource_subscriptions=watched) as subscription:
            print(f"Watching {watched} (protocol {client.protocol_version}). Waiting for changes...")
            async for event in subscription:
                print(f"CHANGED: {event.uri}")
                if event.uri == profile_uri:
                    profile = await client.read_resource(profile_uri)
                    print(f"   new profile: {profile.contents[0].text}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Watch an employee for changes.")
    parser.add_argument("employee_id", nargs="?", type=int, default=3)
    parser.add_argument("department", nargs="?", default="Engineering",
                        help="Department name with its stored capitalization (default Engineering).")
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    args = parser.parse_args()
    try:
        asyncio.run(watch(args.url, args.employee_id, args.department))
    except KeyboardInterrupt:
        pass
