# Architecture

This document explains how the MCP Learning Server is put together: its components, how a tool call travels through the system, and how errors, timeouts and logging work. For setup and usage, see the [README](../README.md).

## Contents

1. [Overview](#overview)
2. [Components](#components)
3. [Life of a tool call](#life-of-a-tool-call)
4. [Error handling](#error-handling)
5. [Timeouts and cancellation](#timeouts-and-cancellation)
6. [Logging](#logging)
7. [Data layer](#data-layer)
8. [Design decisions](#design-decisions)
9. [Known limitations](#known-limitations)

## Overview

The system is a single Python process that speaks the [Model Context Protocol](https://modelcontextprotocol.io), plus a PostgreSQL database running in Docker. An MCP client, such as Claude, the MCP Inspector or `test_client.py`, starts the server as a child process and sends it JSON-RPC messages over stdin/stdout.

```mermaid
flowchart LR
    subgraph Client process
        C["MCP client<br/>Claude · MCP Inspector · test_client.py"]
    end

    subgraph "Server process (server.py)"
        SDK["MCP SDK (MCPServer)<br/>protocol, schema, validation"]
        LC["log_call decorator"]
        T1["add_integer"]
        T2["divide"]
        T3["get_employees"]
        T4["long_running_task"]
        SDK --> LC
        LC --> T1 & T2 & T3 & T4
    end

    subgraph Docker
        DB[("PostgreSQL 16<br/>company.employees<br/>host port 5433")]
    end

    LOG[("mcp_calls.log<br/>+ stderr")]

    C <-- "JSON-RPC over stdio" --> SDK
    LC -- "every call" --> LOG
    T3 -- "psycopg 3 / SQL" --> DB
```

## Components

| Component | File | Responsibility |
|---|---|---|
| MCP server | `server.py` | Registers the tools, runs the stdio transport, and holds all tool logic |
| `log_call` decorator | `server.py` | Logs each call's arguments, result, duration and outcome, for both sync and async tools |
| Tools | `server.py` | `add_integer`, `divide`, `get_employees` and `long_running_task` |
| Database | `docker-compose.yml`, `db/init.sql` | PostgreSQL 16 with an `employees` table and 10 sample rows |
| Test client | `test_client.py` | Starts the server over stdio and calls every tool, covering success, failure and timeout cases |

The MCP Python SDK (version 2.x, class `MCPServer`) does most of the protocol work:
- It turns each tool's type hints and docstring into a JSON schema and a description.
- It validates incoming arguments against that schema, using Pydantic.
- It converts return values and exceptions into MCP results.

## Life of a tool call

```mermaid
sequenceDiagram
    autonumber
    participant C as MCP client
    participant S as MCPServer (SDK)
    participant L as log_call
    participant T as Tool function
    participant DB as PostgreSQL

    C->>S: tools/call {name, arguments}
    S->>S: Validate arguments against the schema from the type hints
    alt invalid arguments
        S-->>C: result, is_error = true (validation message)
    else valid
        S->>L: call wrapped tool
        L->>L: log CALL name + arguments
        L->>T: invoke
        opt get_employees
            T->>DB: parameterized SELECT
            DB-->>T: rows
        end
        alt success
            T-->>L: return value
            L->>L: log RESULT + duration
            L-->>S: return value
            S-->>C: result (text + structured content)
        else ToolError raised
            T-->>L: ToolError("clear message")
            L->>L: log FAILED (WARNING, no stack trace)
            L-->>S: re-raise
            S-->>C: result, is_error = true, "Error executing tool …: clear message"
        end
    end
```

Notes:
- **Registration.** Each tool is registered as `@mcp.tool()` stacked on `@log_call`. `log_call` uses `functools.wraps`, which keeps the original function's signature visible, so the SDK still builds the correct schema from it.
- **Sync vs async tools.** The SDK runs synchronous tools (`add_integer`, `divide`, `get_employees`) on a worker thread through `anyio.to_thread.run_sync`, so a blocking database call doesn't stall the event loop. Async tools (`long_running_task`) run directly on the event loop.
- **Return values.** A tool that returns something other than an object, such as `get_employees` returning a list, has its value wrapped by the SDK as `{"result": ...}` in `structured_content`.

## Error handling

Errors fall into three categories, and each one is handled differently:

| Category | Example | Where it's caught | What the client sees | What gets logged |
|---|---|---|---|---|
| **Invalid input** (wrong type) | `divide(10, "abc")` | The SDK, before the tool runs | `is_error` result with the validation message | An SDK log line; no `CALL` line |
| **Expected failure** (`ToolError`) | Dividing by zero, unknown employee id, database down, timeout | Raised by the tool | `is_error` result with a short, readable message | `WARNING FAILED …`, no stack trace |
| **Unexpected failure** (any other exception) | A bug | `log_call` | `is_error` result | `ERROR` with a full stack trace |

Database errors are translated at the tool boundary. The tool catches `psycopg.OperationalError` (the database can't be reached) and `psycopg.Error` (the query failed), writes the full detail to the log, and raises a short `ToolError`. This keeps connection strings and driver internals out of the client's view.

## Timeouts and cancellation

`long_running_task(duration_seconds, timeout_seconds)` simulates a slow job and sends a progress notification every second. It shows the two ways a call can run out of time.

```mermaid
sequenceDiagram
    participant C as MCP client
    participant S as Server
    participant W as work() coroutine

    Note over C,W: Server-side timeout (duration 8s, timeout_seconds 3)
    C->>S: tools/call long_running_task
    S->>W: asyncio.wait_for(work(), 3)
    W-->>C: progress 1/8, 2/8
    S->>W: cancel after 3s
    S-->>C: is_error: "Task timed out after 3s (it needed 8s)…"

    Note over C,W: Client-side timeout (duration 10s, read_timeout_seconds 2)
    C->>S: tools/call long_running_task
    S->>W: asyncio.wait_for(work(), 30)
    C->>C: 2s pass, raise MCPError(REQUEST_TIMEOUT)
    C->>S: notifications/cancelled
    S->>W: cancel
    S->>S: log "CANCELLED long_running_task after 2.00 s"
```

- **Server-side.** The tool owns its time limit. `asyncio.wait_for` cancels the work and raises `TimeoutError`, which the tool turns into a `ToolError`. The client gets an ordinary error result.
- **Client-side.** The caller owns the time limit. `call_tool(..., read_timeout_seconds=N)` raises `MCPError` with code `REQUEST_TIMEOUT`, and the SDK sends a cancellation notification to the server. On the server, `log_call` catches `asyncio.CancelledError`, logs it and re-raises it, so no work is left running.
- **Input bounds.** Both arguments must be greater than 0 and at most 120 seconds (`MAX_TASK_SECONDS`), so a caller can't keep the server busy indefinitely.

## Logging

- **Destinations.** Logs go to `mcp_calls.log` and to stderr, **never stdout**. The stdio transport sends protocol messages over stdout, so any stray output there would corrupt the connection.
- **Format.** `timestamp | LEVEL | message`, with one `CALL` line per call followed by `RESULT`, `FAILED`, `CANCELLED` or `ERROR`.
- **Arguments.** The SDK passes async tools a `Context` object, which `log_call` leaves out of the logged arguments because it's internal and not useful to read.

## Data layer

- **Schema.** One table, `employees`, with columns `id`, `first_name`, `last_name`, `email`, `department`, `job_title`, `salary` and `hire_date`. It's created and filled with sample data by `db/init.sql`, which Postgres runs only when the `pgdata` volume is first created.
- **Connection.** The server reads `DATABASE_URL`, which defaults to `postgresql://mcp_user:mcp_password@localhost:5433/company`. It opens a new connection per call with a 5-second connect timeout and uses `dict_row`, so each row comes back as a dictionary.
- **Queries.** Values are always passed as `%s` parameters, never formatted into the SQL string. `NUMERIC` and `DATE` columns are cast to `float` and `text` in SQL, so the rows can be serialized to JSON without extra conversion code.
- **Port.** The database uses host port 5433 so that it can run alongside another Postgres server on the default port, 5432.

## Design decisions

| Decision | Reason |
|---|---|
| stdio transport | This is the standard way to run a local MCP server. The client manages the server process, and no network port or authentication is needed. |
| Type hints as the contract | One source of truth: the SDK builds both the schema the AI sees and the input validation from the function signature. |
| `ToolError` for expected failures | The caller gets a clear, useful message, and the server keeps running. |
| A logging decorator instead of logging in each tool | Every tool is logged the same way, and adding a tool doesn't need any logging code. |
| Casting column types in SQL | Keeps the Python code free of conversion logic for `Decimal` and `date` values. |
| Postgres in Docker Compose | One command starts the database and loads sample data, the same way on any machine. |

## Known limitations

These are deliberate simplifications for a learning project, with what a production version would change:

- **A new database connection per call.** A connection pool (`psycopg_pool`) would avoid the setup cost of each call.
- **Database queries can't be interrupted.** `get_employees` runs on a worker thread, so a client cancellation doesn't stop a query that's already running. An async tool with `psycopg.AsyncConnection` and a Postgres `statement_timeout` would make queries cancellable and time-limited.
- **No automated test suite.** `test_client.py` is an end-to-end script that prints results rather than asserting them. The next step would be pytest tests that check `is_error` and returned values.
- **No authentication.** That's appropriate for stdio, where only the parent process can talk to the server. The streamable HTTP transport would need an auth layer.
- **Development credentials in `docker-compose.yml`.** Fine for a local sample database. A real deployment would load them from secrets.
