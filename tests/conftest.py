"""Shared fixtures: an isolated test database and an in-process MCP client.

Tests never touch the real `company` database. A `company_test` database is
created on the same Postgres server, and every test starts from the seed data
in db/init.sql. Postgres must be running (docker compose up -d --wait).
"""

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402  (needs the path above)
from mcp import Client, types  # noqa: E402

TEST_DB = "company_test"
SEED_SQL = (ROOT / "db" / "init.sql").read_text(encoding="utf-8")

# Every test that uses `harness` runs twice, once per protocol version:
# 2026-07-28 (the SDK's default negotiation) and 2025-11-25 (the older
# initialize handshake). Some features behave differently between them.
PROTOCOLS = {"2026-07-28": "auto", "2025-11-25": "legacy"}


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Create company_test and point the server at it for the whole run."""
    admin_url = make_conninfo(server.DATABASE_URL, dbname="postgres")
    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {TEST_DB}")
    except psycopg.OperationalError as exc:
        pytest.exit(f"Postgres isn't reachable: {exc}\nStart it with: docker compose up -d --wait", returncode=2)

    # Keep test calls out of the real call log.
    root = logging.getLogger()
    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    for handler in file_handlers:
        root.removeHandler(handler)

    original_url = server.DATABASE_URL
    server.DATABASE_URL = make_conninfo(original_url, dbname=TEST_DB)
    yield server.DATABASE_URL

    server.DATABASE_URL = original_url
    for handler in file_handlers:
        root.addHandler(handler)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


@pytest.fixture(autouse=True)
def seed_data(test_database):
    """Start every test from the same 10 employees, with ids 1-10."""
    with psycopg.connect(test_database, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS employees")
        conn.execute(SEED_SQL)


def db_query(sql: str, params: tuple = ()) -> list[tuple]:
    """Read the test database directly, to check what a tool really changed."""
    with psycopg.connect(server.DATABASE_URL) as conn:
        return conn.execute(sql, params).fetchall()


@dataclass
class Harness:
    """An MCP client plus scripted user answers and captured log messages."""

    client: Client | None = None
    protocol: str = ""
    answers: list[types.ElicitResult] = field(default_factory=list)
    prompts_shown: list[str] = field(default_factory=list)
    logs: list[tuple[str, str]] = field(default_factory=list)

    async def on_elicit(self, context, params):
        self.prompts_shown.append(params.message)
        return self.answers.pop(0)

    async def on_log(self, params):
        self.logs.append((params.level, params.data))


@pytest.fixture(params=list(PROTOCOLS), ids=list(PROTOCOLS))
async def harness(request):
    """In-process client connected to server.mcp (no subprocess, no port).

    Declares elicitation support and opts in to log messages at debug level.
    """
    harness = Harness(protocol=request.param)
    async with Client(
        server.mcp,
        mode=PROTOCOLS[request.param],
        elicitation_callback=harness.on_elicit,
        logging_callback=harness.on_log,
        log_level="debug",
    ) as client:
        assert client.protocol_version == request.param
        harness.client = client
        yield harness


@pytest.fixture(params=list(PROTOCOLS), ids=list(PROTOCOLS))
async def client_without_elicitation(request):
    """A client that can't show confirmation prompts."""
    async with Client(server.mcp, mode=PROTOCOLS[request.param]) as client:
        yield client
