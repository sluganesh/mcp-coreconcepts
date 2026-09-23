# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A learning project: a local MCP server in Python (`server.py`) that runs over the stdio transport and exposes a few tools (`add_integer`, `divide`, `get_employees`). `get_employees` reads from a Postgres database that runs in Docker.

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

- **The SDK is `mcp` 2.x.** `FastMCP` was renamed `MCPServer` (`from mcp.server.mcpserver import MCPServer`), and `ToolError` lives in `mcp.server.mcpserver.exceptions`. Most online examples use the 1.x names and won't import.
- **Never write to stdout from the server.** The stdio transport uses stdout for protocol messages. Logging goes to `mcp_calls.log` and to stderr.
- **Defining a tool:** stack `@mcp.tool()` on the outside and `@log_call` on the inside. `log_call` uses `functools.wraps`, which keeps the original signature visible, and the SDK builds the tool's input schema and argument validation from that signature and its type hints. Each tool's docstring becomes its description.
- **Error handling:**
  - For expected, user-facing failures, raise `ToolError("message")`. The client receives an `is_error` result carrying that message, and `log_call` logs it as a `WARNING` without a stack trace.
  - Any other exception is logged with a full stack trace.
  - Database errors are caught, logged in full, and re-raised as a short `ToolError`.
  - Calls with bad argument types are rejected by the SDK before the tool function runs, so they produce no `CALL` log line.
- **Database:**
  - The connection string comes from the `DATABASE_URL` environment variable. The default matches `docker-compose.yml`: `postgresql://mcp_user:mcp_password@localhost:5433/company`. Port 5433 is deliberate, because other local projects' Postgres containers already use 5432 and 55432.
  - The server uses `psycopg` 3 with `dict_row`.
  - Queries cast `NUMERIC` and `DATE` columns to `float` and `text` in SQL (see `EMPLOYEE_COLUMNS`) so that returned rows serialize to JSON.
  - Use `%s` placeholders for all query parameters.
- **Schema and seed data:** `db/init.sql` runs only when the `pgdata` volume is first created. After editing it, run `docker compose down -v` and then `docker compose up -d` to re-seed.
