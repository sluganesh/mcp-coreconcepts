# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A learning project: a local MCP server in Python (`server.py`) that runs over stdio (default) or streamable HTTP (`--http`) and exposes a few tools (`add_integer`, `divide`, `get_employees`, `create_employee`, `update_employee_salary`, `delete_employee`, `long_running_task`). `get_employees` reads from a Postgres database that runs in Docker.

## Commands

All Python commands use the project virtualenv at `.venv` (Windows paths).

```powershell
python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt   # setup
docker compose up -d --wait                  # start Postgres (container mcp-learning-db, host port 5433)
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # adds pytest
.\.venv\Scripts\python.exe -m pytest                                 # full automated suite (~20 s)
.\.venv\Scripts\python.exe -m pytest tests/test_tools.py -k pagination    # one file / matching tests
.\.venv\Scripts\python.exe -m pytest "tests/test_tools.py::test_divide[2026-07-28]"   # one test, one protocol
.\.venv\Scripts\python.exe test_client.py    # printed walkthrough: spawns server.py over stdio, calls every tool
.\.venv\Scripts\python.exe server.py --http   # streamable HTTP at http://127.0.0.1:8000/mcp (--port to change)
.\.venv\Scripts\python.exe test_client.py --http http://127.0.0.1:8000/mcp   # same tests over HTTP
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe server.py   # browser UI for calling tools (http://127.0.0.1:6274)
docker exec -it mcp-learning-db psql -U mcp_user -d company                # SQL prompt
```

The server is registered with Claude Code for this project (local scope) as `learning-server`. Its prompts only run as `/mcp__learning-server__<prompt>` in a Claude Code session started after the registration; the terminal CLI supports this.

There is no linter or build step. GitHub Actions (`.github/workflows/tests.yml`) runs `pytest` on every push, with a Postgres service on port 5433.

**Tests (`tests/`):**
- **Test database:** `conftest.py` creates a separate `company_test` database on the same Postgres server, points `server.DATABASE_URL` at it, and rebuilds the `employees` table from `db/init.sql` before **every** test. Tests can therefore rely on the 10 seed rows and ids 1–10, and never touch the real `company` data.
- **In-process client:** the `harness` fixture connects `mcp.Client` to `server.mcp` in-process (no subprocess or port), and runs each test **twice**: on protocol 2026-07-28 (`mode="auto"`) and on 2025-11-25 (`mode="legacy"`). Features differ between them, so new features need tests on both. Queue scripted confirmation answers in `harness.answers`; captured client log messages are in `harness.logs`.
- **Checking the database:** use `db_query()` to see what a tool actually changed.
- **HTTP:** `tests/test_http.py` starts `server.py --http` as a subprocess against the test database.
- **`test_client.py`** is kept as a printed, end-to-end walkthrough, not the test suite.

## Architecture and gotchas

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the full design, with diagrams. When you add or change a tool, keep it in sync with README.md.

- **The SDK is `mcp` 2.x.** `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`), and `ToolError` lives in `mcp.server.mcpserver.exceptions`. Most online examples use the 1.x names and won't import.
- **HTTP mode must stay on 127.0.0.1.** Binding to localhost is what makes the SDK turn on DNS-rebinding protection (a forged Host gets 421, a forged Origin gets 403), and the server has no authentication. Any new feature must work over both transports; `test_client.py` runs with or without `--http`.
- **Never write to stdout from the server.** The stdio transport uses stdout for protocol messages. Logging goes to `mcp_calls.log` and to stderr.
- **Defining a tool:** stack `@mcp.tool()` on the outside and `@log_call` on the inside. `log_call` uses `functools.wraps`, which keeps the original signature visible, and the SDK builds the tool's input schema and argument validation from that signature and its type hints. Each tool's docstring becomes its description.
- **Tool annotations:** every tool passes `annotations=` to `@mcp.tool()`. Read-only tools use the shared `READ_ONLY` preset. Write tools set `destructive_hint` (true when existing data is overwritten or removed) and `idempotent_hint` explicitly, with a comment explaining the choice. Keep the annotations table in README.md in sync.
- **Elicitation: use a resolver, never `ctx.elicit()` directly.** On the 2026-07-28 protocol, a server can't send requests mid-call, so `ctx.elicit()` raises `NoBackChannelError`. Instead, `delete_employee` takes `answer: Annotated[ElicitationResult[DeleteConfirmation], Resolve(ask_to_confirm_delete)]`. The resolver returns `Elicit(message, schema)`, and the SDK asks the question the right way for each protocol: a mid-call `elicitation/create` on 2025-11-25, or an `InputRequiredResult` plus a client retry on 2026-07-28. Resolvers may run on every retry round, so they must not change anything. `ask_to_confirm_delete` also refuses clients without elicitation support, and looks up the employee so unknown ids fail before any question is asked.
- **Resources:** registered with `@mcp.resource(uri, ...)` stacked on `@log_call`. A URI with `{placeholders}` becomes a template, and its values arrive as strings, so validate them in the handler (see `employee_profile`) rather than typing them as `int`. Resource failures raise `ResourceNotFoundError` or `ResourceError`, which clients receive as `MCPError` codes -32602 and -32603. Wrap database access in `resource_errors()` so `employees_db()`'s `ToolError`s become `ResourceError`s. Static content lives in `resources/`.
- **Prompts:** registered with `@mcp.prompt(title=..., description=...)` stacked on `@log_call`. They return a list of `UserMessage`s, and live data is attached with `embedded_resource()`. Prompt arguments are always strings. Only `MCPError` reaches the client with its message intact; the SDK turns any other exception into "Error rendering prompt X". Wrap calls to resource helpers in `prompt_errors()`, which converts `ResourceNotFoundError` and `ResourceError` into `MCPError` codes -32602 and -32603.
- **Completions:** a single `@mcp.completion()` handler, `complete_argument`, serves both prompt arguments and resource template placeholders. It matches on the argument's name (`department`, `employee_id`), so reusing those names elsewhere gets autocomplete for free.
- **Deprecated in the 2026-07-28 spec (SEP-2577):** logging to the client (`ctx.info()` and similar), sampling, and roots. `bulk_import_employees` still uses logging deliberately, and filters the SDK's `MCPDeprecationWarning` for it. Its `ImportSummary` result must always carry every problem, so callers never depend on log delivery. Don't add sampling or roots; the learning plan replaced them.
- **Change notifications:** any tool that changes an employee must, after the transaction commits, call `notify_employee_changed(ctx, employee)` from async tools or `notify_from_thread(ctx, employee)` from sync tools. The sync version uses `anyio.from_thread.run`, because the SDK runs sync tools on a worker thread. It publishes `ResourceUpdated` for `employees://{id}` and `departments://{department}/employees`, using the department's stored capitalization. These only reach 2026-07-28 clients (`subscriptions/listen`). `test_client.py` uses the high-level `Client` for them, because `ClientSession` negotiates 2025-11-25.
- **Pagination:** `get_employees` uses keyset pagination (`WHERE id > after_id ORDER BY id LIMIT page_size + 1`) with opaque cursors from `encode_cursor()` and `decode_cursor()`. Any new list tool should follow the same pattern: return `next_cursor` (null on the last page) and never use `OFFSET`. `EmployeeList.count` is the size of this page, and `total` covers all pages.
- **Bulk import:** rows are validated with `NewEmployee` (same rules as `create_employee`), then inserted in one `asyncio.to_thread` call. Each insert runs in a nested `conn.transaction()` (a savepoint), so a duplicate rolls back only its own row.
- **Input constraints** go in the signature as `Annotated[type, Field(...)]` (see `EmployeeId`, `Salary`, `Name`), so they appear in the input schema and the SDK enforces them before the tool runs.
- **Error handling:**
  - For expected, user-facing failures, raise `ToolError("message")`. The client receives an `is_error` result carrying that message, and `log_call` logs it as a `WARNING` without a stack trace.
  - Any other exception is logged with a full stack trace.
  - Database errors are caught, logged in full, and re-raised as a short `ToolError`.
  - Calls with bad argument types are rejected by the SDK before the tool function runs, so they produce no `CALL` log line.
- **Async tools:** `log_call` detects coroutine functions and wraps them with an async wrapper. It logs `asyncio.CancelledError` as `CANCELLED`, which is what happens when a client's `read_timeout_seconds` expires, because the SDK then sends the server a cancellation. It also leaves the SDK-injected `Context` argument out of the logged arguments. For a server-side time limit, use `asyncio.wait_for` and turn the resulting `TimeoutError` into a `ToolError` (see `long_running_task`).
- **Database:**
  - The connection string comes from the `DATABASE_URL` environment variable. The default matches `docker-compose.yml`: `postgresql://mcp_user:mcp_password@localhost:5433/company`. Port 5433 is deliberate, because other local projects' Postgres containers already use 5432 and 55432.
  - All database access goes through the `employees_db()` context manager. It opens a transaction (committed on success, rolled back on any exception) and turns `psycopg` errors into `ToolError`s. To give a specific message for one database error, catch the `ToolError` and check `exc.__cause__` (see the duplicate-email handling in `create_employee`).
  - The server uses `psycopg` 3 with `class_row(Employee)`, so each row is built as a Pydantic model and validated. `EMPLOYEE_COLUMNS` is taken from `Employee.model_fields` and used in `SELECT` and `RETURNING`, so a new column needs a new model field.
  - `test_client.py` runs against the real `company` database. It creates an employee with a random email and deletes it at the end, so the table stays at the 10 seeded rows.
  - Pydantic converts `NUMERIC` to `float` and `DATE` to an ISO string. Tools return models (`EmployeeList`), which gives clients a detailed output schema. Model docstrings and field descriptions appear in that schema, so write them for the client; put developer notes in comments.
  - Use `%s` placeholders for all query parameters.
- **Schema and seed data:** `db/init.sql` runs only when the `pgdata` volume is first created. After editing it, run `docker compose down -v` and then `docker compose up -d` to re-seed.
