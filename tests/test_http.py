"""Run server.py --http as a real process and test it over the network."""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from conftest import ROOT
from mcp import Client

pytestmark = pytest.mark.anyio


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def http_url(test_database):
    """Start the server over HTTP, using the test database."""
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--http", "--port", str(port)],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": test_database},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("server.py --http didn't start")
                time.sleep(0.2)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        process.wait(timeout=10)


async def test_client_can_use_the_server_over_http(http_url):
    async with Client(http_url) as client:
        tools = [t.name for t in (await client.list_tools()).tools]
        assert len(tools) == 8
        result = await client.call_tool("get_employees", {"employee_id": 3})
        assert result.structured_content["employees"][0]["first_name"] == "Rahul"


def post_ping(url: str, **headers) -> int:
    request = urllib.request.Request(
        url,
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.mark.parametrize("headers, status", [
    ({"Host": "evil.example.com"}, 421),               # DNS rebinding: wrong Host
    ({"Origin": "http://evil.example.com"}, 403),      # a web page on another site
])
def test_dns_rebinding_protection(http_url, headers, status):
    assert post_ping(http_url, **headers) == status
