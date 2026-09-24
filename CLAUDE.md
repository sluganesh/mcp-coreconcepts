# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A learning project: a local MCP server in Python (`server.py`) that runs over the stdio transport and exposes a few tools (`add_integer`, `divide`, `get_employees`, `create_employee`, `update_employee_salary`, `delete_employee`, `long_running_task`). `get_employees` reads from a Postgres database that runs in Docker.

## Commands

All Python commands use the project virtualenv at `.venv` (Windows paths).

```powershell
python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt   # setup
docker compose up -d --wait                  # start Postgres (container mcp-learning-db, host port 5433)
.\.venv\Scripts\python.exe test_client.py    # end-to-end test: spawns server.py over stdio, calls every tool
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe server.py   # browser UI for calling tools (http://127.0.0.1:6274)
docker exec -it mcp-learning-db psql -U mcp_user -d company                # SQL prompt
```

There is no pytest suite, linter, or build step. `test_client.py` is the only test: it's a plain script that prints each call's result or error. To exercise a single tool, edit the calls in that script or use the Inspector.

## Architecture and gotchas

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the full design, with diagrams. When you add or change a tool, keep it in sync with README.md.

- **The SDK is `mcp` 2.x.** `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`), and `ToolError` lives in `mcp.server.mcpserver.exceptions`. Most online examples use the 1.x names and won't import.
- **Never write to stdout from the server.** The stdio transport uses stdout for protocol messages. Logging goes to `mcp_calls.log` and to stderr.
- **Defining a tool:** stack `@mcp.tool()` on the outside and `@log_call` on the inside. `log_call` uses `functools.wraps`, which keeps the original signature visible, and the SDK builds the tool's input schema and argument validation from that signature and its type hints. Each tool's docstring becomes its description.
- **Tool annotations:** every tool passes `annotations=` to `@mcp.tool()`. Read-only tools use the shared `READ_ONLY` preset. Write tools set `destructive_hint` (true when existing data is overwritten or removed) and `idempotent_hint` explicitly, with a comment explaining the choice. Keep the annotations table in README.md in sync.
- **Elicitation:** `delete_employee` is async so it can `await ctx.elicit(...)`. It checks `ctx.client_capabilities.elicitation` first and refuses if the client can't show prompts. It deletes only on `accept` with `confirm=True`. Its blocking database work runs through `asyncio.to_thread`, so the event loop stays free. In `test_client.py`, confirmation answers are scripted: queue an `ElicitResult` in `elicitation_answers` before each call.
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
  - `test_client.py` creates an employee with a random email and deletes it at the end, so the table stays at the 10 seeded rows.
  - Pydantic converts `NUMERIC` to `float` and `DATE` to an ISO string. Tools return models (`EmployeeList`), which gives clients a detailed output schema. Model docstrings and field descriptions appear in that schema, so write them for the client; put developer notes in comments.
  - Use `%s` placeholders for all query parameters.
- **Schema and seed data:** `db/init.sql` runs only when the `pgdata` volume is first created. After editing it, run `docker compose down -v` and then `docker compose up -d` to re-seed.
