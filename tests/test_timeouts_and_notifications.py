"""Progress, server- and client-side timeouts, and change notifications."""

import anyio
import pytest

from mcp import types
from mcp.shared.exceptions import MCPError

pytestmark = pytest.mark.anyio


# --- Timeouts ---------------------------------------------------------------------


async def test_long_task_reports_progress_and_finishes(harness):
    progress = []

    async def on_progress(value, total, message):
        progress.append((value, total))

    result = await harness.client.call_tool(
        "long_running_task", {"duration_seconds": 2, "timeout_seconds": 5}, progress_callback=on_progress
    )
    assert result.structured_content == {"result": "Task finished in 2s."}
    assert progress == [(1.0, 2.0), (2.0, 2.0)]


async def test_server_side_timeout_returns_a_tool_error(harness):
    result = await harness.client.call_tool("long_running_task", {"duration_seconds": 3, "timeout_seconds": 1})
    assert result.is_error
    assert "Task timed out after 1s (it needed 3s)" in result.content[0].text


async def test_client_side_timeout_raises_request_timeout(harness):
    with pytest.raises(MCPError) as caught:
        await harness.client.call_tool(
            "long_running_task", {"duration_seconds": 10, "timeout_seconds": 30}, read_timeout_seconds=1
        )
    assert caught.value.code == types.REQUEST_TIMEOUT


@pytest.mark.parametrize("args", [
    {"duration_seconds": 0, "timeout_seconds": 5},
    {"duration_seconds": 5, "timeout_seconds": 121},
])
async def test_task_limits(harness, args):
    result = await harness.client.call_tool("long_running_task", args)
    assert result.is_error
    assert "must be greater than 0 and at most 120" in result.content[0].text


# --- Change notifications --------------------------------------------------------


async def test_subscribers_are_notified_of_changes(harness):
    if harness.protocol != "2026-07-28":
        pytest.skip("subscriptions/listen needs the 2026-07-28 protocol")
    watched = ["employees://3", "departments://Engineering/employees"]
    async with harness.client.listen(resource_subscriptions=watched) as subscription:
        assert subscription.honored.resource_subscriptions == watched

        await harness.client.call_tool("update_employee_salary", {"employee_id": 3, "new_salary": 150000})
        with anyio.fail_after(5):
            received = {(await anext(subscription)).uri for _ in watched}
        assert received == set(watched)

        # A change in another department doesn't reach these subscriptions.
        await harness.client.call_tool("update_employee_salary", {"employee_id": 4, "new_salary": 73000})
        with anyio.move_on_after(0.5) as waited:
            await anext(subscription)
        assert waited.cancelled_caught, "got an event for an unrelated department"


async def test_older_protocol_clients_cannot_subscribe(harness):
    if harness.protocol != "2025-11-25":
        pytest.skip("checks the older protocol's capabilities")
    assert harness.client.server_capabilities.resources.subscribe is False
