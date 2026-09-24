# Architecture

This document explains how the MCP Learning Server is put together: its components, how a tool call travels through the system, and how errors, timeouts and logging work. For setup and usage, see the [README](../README.md).

## Contents

1. [Overview](#overview)
2. [Components](#components)
3. [Life of a tool call](#life-of-a-tool-call)
4. [Error handling](#error-handling)
5. [Tool annotations](#tool-annotations)
6. [Elicitation: confirming deletes](#elicitation-confirming-deletes)
7. [Resources](#resources)
8. [Prompts and completions](#prompts-and-completions)
9. [Bulk import and logging to the client](#bulk-import-and-logging-to-the-client)
10. [Timeouts and cancellation](#timeouts-and-cancellation)
11. [Logging](#logging)
12. [Data layer](#data-layer)
13. [Design decisions](#design-decisions)
14. [Known limitations](#known-limitations)

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
        T3["get_employees<br/>(read)"]
        T5["create / update / delete<br/>employee (write)"]
        T4["long_running_task"]
        R["Resources<br/>hr://handbook · employees://{id}<br/>departments://{dept}/employees"]
        P["Prompts<br/>headcount report · welcome email"]
        H["employees_db()<br/>transaction + error translation"]
        SDK --> LC
        LC --> T1 & T2 & T3 & T5 & T4 & R & P
        P -- "embeds" --> R
        T3 & T5 & R --> H
    end

    subgraph Docker
        DB[("PostgreSQL 16<br/>company.employees<br/>host port 5433")]
    end

    LOG[("mcp_calls.log<br/>+ stderr")]

    C <-- "JSON-RPC over stdio" --> SDK
    LC -- "every call" --> LOG
    H -- "psycopg 3 / SQL" --> DB
```

## Components

| Component | File | Responsibility |
|---|---|---|
| MCP server | `server.py` | Registers the tools, runs the stdio transport, and holds all tool logic |
| `log_call` decorator | `server.py` | Logs each call's arguments, result, duration and outcome, for both sync and async tools |
| Tools | `server.py` | `add_integer`, `divide`, `get_employees`, `create_employee`, `update_employee_salary`, `delete_employee`, `bulk_import_employees` and `long_running_task` |
| Resources | `server.py`, `resources/hr_handbook.md` | `hr://handbook`, `employees://{employee_id}` and `departments://{department}/employees` |
| Prompts and completions | `server.py` | `department_headcount_report` and `welcome_email`, plus autocomplete for `department` and `employee_id` |
| `employees_db()` | `server.py` | Opens a database transaction for the employee tools and turns database errors into `ToolError`s |
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
        opt employee tools
            T->>DB: BEGIN; parameterized SELECT / INSERT / UPDATE / DELETE … RETURNING
            DB-->>T: rows
            T->>DB: COMMIT (or ROLLBACK if anything raised)
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
- **Registration.** Each tool is registered as `@mcp.tool(annotations=...)` stacked on `@log_call`. `log_call` uses `functools.wraps`, which keeps the original function's signature visible, so the SDK still builds the correct schema from it.
- **Input constraints.** Rules such as `salary > 0`, an email pattern and text length limits are written into the signature as `Annotated[type, Field(...)]`. They appear in the input schema (for example `"exclusiveMinimum": 0`, `"pattern": ...`), so clients know them in advance, and the SDK rejects bad input before the tool runs.
- **Sync vs async tools.** The SDK runs synchronous tools (the math tools and most employee tools) on a worker thread through `anyio.to_thread.run_sync`, so a blocking database call doesn't stall the event loop. Async tools (`long_running_task` and `delete_employee`) run directly on the event loop, so `delete_employee` moves its own database calls onto a worker thread with `asyncio.to_thread`.
- **Return values.** The SDK builds each tool's output schema from its return type.
  - `get_employees` returns a Pydantic model, `EmployeeList` (`{"employees": [...], "count": n}`). The write tools return a single `Employee`: the record as it is after the change, or, for `delete_employee`, the record that was removed. The schema therefore lists every field with its type and description, for example `salary` as a number described as "Annual salary in USD" and `hire_date` as a string with `"format": "date"`.
  - Simple return types such as `int` or `str` are wrapped by the SDK as `{"result": ...}` in `structured_content`.

## Error handling

Errors fall into three categories, and each one is handled differently:

| Category | Example | Where it's caught | What the client sees | What gets logged |
|---|---|---|---|---|
| **Invalid input** (wrong type or broken constraint) | `divide(10, "abc")`, `create_employee` with a negative salary | The SDK, before the tool runs | `is_error` result with the validation message | An SDK log line; no `CALL` line |
| **Expected failure** (`ToolError`) | Dividing by zero, unknown employee id, duplicate email, database down, timeout | Raised by the tool | `is_error` result with a short, readable message | `WARNING FAILED …`, no stack trace |
| **Unexpected failure** (any other exception) | A bug | `log_call` | `is_error` result | `ERROR` with a full stack trace |

Database errors are translated in one place, the `employees_db()` context manager. It catches `psycopg.OperationalError` (the database can't be reached) and `psycopg.Error` (the query failed), writes the full detail to the log, and raises a short `ToolError`. This keeps connection strings and driver internals out of the client's view.

When a tool needs a more specific message, it catches that `ToolError` and checks the original database error in `exc.__cause__`. For example, `create_employee` turns a `UniqueViolation` on the email column into "An employee with email … already exists."

## Tool annotations

Every tool declares how it behaves, using the MCP tool annotations:

| Tool | `read_only_hint` | `destructive_hint` | `idempotent_hint` | `open_world_hint` |
|---|---|---|---|---|
| `add_integer`, `divide`, `get_employees`, `long_running_task` | true | — | true | false |
| `create_employee`, `bulk_import_employees` | false | false | false | false |
| `update_employee_salary` | false | true | true | false |
| `delete_employee` | false | true | true | false |

How the values were chosen:
- **Destructive** means the tool can overwrite or remove existing data, as opposed to only adding new data. Creating is additive. Updating a salary overwrites the old value, so it counts as destructive even though it isn't a delete.
- **Idempotent** means repeating the same call with the same arguments has no further effect. Setting a salary to 55,000 twice leaves 55,000. Deleting id 13 twice leaves id 13 deleted; the second call just reports "not found". Creating twice would add two employees, so `create_employee` is not idempotent.
- **Open world** is false for every tool, because they only touch this server's own database, not outside systems such as the web or third-party APIs.
- `destructive_hint` and `idempotent_hint` only matter when `read_only_hint` is false, so the read-only tools leave `destructive_hint` unset.

Clients use these hints to decide how to treat each call, for example running read-only tools without asking and asking the user before destructive ones. They are only hints: the server doesn't enforce them, and they are no substitute for real authorization.

## Elicitation: confirming deletes

Annotations leave it to the client to ask before a destructive call. For `delete_employee`, the server asks the user itself, using MCP elicitation, which lets a tool pause and request input from the user.

```mermaid
sequenceDiagram
    participant U as User
    participant C as MCP client
    participant S as delete_employee
    participant DB as PostgreSQL

    C->>S: tools/call delete_employee {employee_id: 3}
    S->>S: Client declared elicitation support?
    alt no
        S-->>C: is_error: "needs a client that supports confirmation prompts… Nothing was deleted."
    else yes
        S->>DB: SELECT employee 3
        DB-->>S: Rahul Verma, Engineering Manager
        S->>C: elicitation/create "Permanently delete Rahul Verma (id 3, …)?" + schema {confirm: bool}
        C->>U: Show prompt
        U-->>C: accept / decline / cancel
        C-->>S: ElicitResult
        alt accept with confirm = true
            S->>DB: DELETE … RETURNING
            S-->>C: deleted Employee record
        else decline, cancel, or confirm = false
            S-->>C: is_error: "…was not deleted."
        end
    end
```

- **Capability check.** A client declares elicitation support when it connects. In the Python SDK, it does so by passing an `elicitation_callback`. The tool checks `ctx.client_capabilities.elicitation` first, and refuses instead of deleting without asking. For a destructive action, that's the safe default.
- **Three responses.** `accept` means the user submitted the form. `decline` means they explicitly said no. `cancel` means they dismissed the prompt. The form also has a `confirm` checkbox, so an `accept` with the box unchecked doesn't delete anything either.
- **Looking up the record first.** The prompt shows the employee's name and role, so the user can confirm the right record. Unknown ids fail before any prompt is shown. If the record disappears while the user is deciding, the `DELETE … RETURNING` finds nothing and the tool reports "not found".
- **Async tool, blocking database.** `delete_employee` is async so that it can `await ctx.elicit(...)`. Its database calls are blocking, so it runs them through `asyncio.to_thread` to keep the event loop free for other requests while it waits for the user.
- **Audit log.** Every answer is logged, for example `ELICIT delete_employee id=17 -> accept confirm=True`.

## Resources

Resources are read-only data identified by a URI. They complement tools: the AI decides when to call a tool, while the user or client application decides which resources to load into the AI's context.

| URI | Kind | Handler | Source |
|---|---|---|---|
| `hr://handbook` | Static | `hr_handbook()` | `resources/hr_handbook.md` (Markdown) |
| `employees://{employee_id}` | Template | `employee_profile(employee_id)` | One row from `employees`, returned as an `Employee` in JSON |
| `departments://{department}/employees` | Template | `department_roster(department)` | Matching rows, returned as an `EmployeeList` in JSON |

```mermaid
sequenceDiagram
    participant C as MCP client
    participant S as MCPServer (SDK)
    participant H as Resource handler
    participant DB as PostgreSQL

    C->>S: resources/list, resources/templates/list
    S-->>C: hr://handbook · employees://{employee_id} · departments://{department}/employees
    C->>S: resources/read employees://3
    S->>S: Match URI to template, extract employee_id = "3"
    S->>H: employee_profile("3")
    H->>DB: SELECT … WHERE id = 3
    DB-->>H: row
    H-->>S: Employee
    S-->>C: contents [{uri, mimeType: application/json, text: {...}}]
```

- **Static vs template.** The SDK treats any URI containing `{placeholders}` as a template and matches incoming URIs against it. Static resources and templates are listed separately.
- **Template values arrive as text.** `employee_profile` accepts `employee_id` as a string and checks that it's a number itself. If the parameter were typed `int`, a URI like `employees://abc` would fail inside the SDK with a generic internal error and a stack trace. Checking it in the handler gives a clear "not found" instead.
- **Errors are protocol errors.** Unlike tools, a failed read doesn't return an `is_error` result. Handlers raise `ResourceNotFoundError`, which the client receives as `MCPError` code `-32602`, or `ResourceError`, which it receives as code `-32603`. The `resource_errors()` context manager turns the `ToolError`s raised by `employees_db()` into `ResourceError`s, so the database helper is shared between tools and resources.
- **Helpful not-found messages.** An unknown department lists the departments that do exist, so the client or AI can correct itself.
- **Same logging.** Resource handlers use `log_call` too. `ResourceError` is treated as an expected failure (a warning without a stack trace), like `ToolError`.
### Why expose the same data as both a tool and a resource

`employees://{employee_id}` and `get_employees(employee_id)` return the same record. The overlap is deliberate: the two primitives differ in **who controls access**, not in what they return.

| | Tool (`get_employees`) | Resource (`employees://{employee_id}`) |
|---|---|---|
| Controlled by | The model: the AI decides when to call it and with which arguments | The application or user: they choose what to attach to the context |
| Typical use | Open-ended lookups and actions the AI works out itself | Context the user already has in mind, such as a specific profile or a policy document |
| Side effects | Tools may write (see the write tools) | Always read-only |
| Approval | Clients may ask the user before each call | No approval: reading isn't a tool call |
| Discovery | Visible only when the AI uses it | Listed by the client, for example in a picker or with `@` mentions |
| Change tracking | None | Clients can subscribe and be notified when the resource changes |

A production server decides per piece of data. It uses a tool when the model should decide, a resource when the user should choose, and both only when both uses are real. The HR handbook is exposed only as a resource, because it's reference content, not something to act on.

## Prompts and completions

Prompts are reusable templates that the **user** invokes. Clients typically show them as a menu or as slash commands. The client collects the arguments, calls `prompts/get`, and sends the returned messages to the AI.

| Prompt | Arguments | Messages returned |
|---|---|---|
| `department_headcount_report` | `department` | The department roster as an embedded resource, then instructions for the report |
| `welcome_email` | `employee_id`, `tone` (optional) | The employee profile and the HR handbook as embedded resources, then instructions for the email |

```mermaid
sequenceDiagram
    participant U as User
    participant C as MCP client
    participant S as Server
    participant DB as PostgreSQL

    U->>C: Pick "Welcome email for a new hire"
    C->>S: completion/complete {employee_id: "1"}
    S-->>C: ["1", "10"]
    U->>C: employee_id = 2, tone = friendly
    C->>S: prompts/get welcome_email {employee_id: "2", tone: "friendly"}
    S->>DB: SELECT employee 2
    S-->>C: [profile (embedded), handbook (embedded), instructions]
    C->>C: Send the messages to the AI
```

- **Prompts reuse resources.** The prompts call the resource handlers (`department_roster`, `employee_profile`, `hr_handbook`) and attach the results as `EmbeddedResource` content, with the same URIs and MIME types. The AI receives the data together with the instructions in a single turn, without making any tool calls.
- **Guardrails live in the template.** The instructions tell the AI not to put names next to salaries and not to mention salary in the welcome email. Writing these rules once in the server is more reliable than depending on every user to remember them.
- **Arguments are strings.** The MCP spec defines prompt arguments as strings, so the handlers reuse the same validation as the resource templates (for example, "'abc' is not a valid employee id").
- **Error handling.** The SDK passes an `MCPError` raised by a prompt straight to the client, and turns any other exception into a generic "Error rendering prompt X". `prompt_errors()` therefore converts `ResourceNotFoundError` into `MCPError(-32602)` and `ResourceError` into `MCPError(-32603)`, so the user sees the real reason, such as "No department named 'Legal'. Departments: …".
- **Completions.** One `@mcp.completion()` handler suggests values by argument name: `department` returns matching department names, and `employee_id` returns matching ids (at most 100, with `has_more` set when there are more). The same handler serves the prompt arguments and the resource template placeholders, because they share argument names.

### How each primitive reports errors

| Primitive | Controlled by | How a failure reaches the client | Raise in the handler |
|---|---|---|---|
| Tool | The AI | A normal result with `is_error: true`, which the AI can read and react to | `ToolError` |
| Resource | The user or client app | A JSON-RPC error (`MCPError`) | `ResourceNotFoundError` (-32602), `ResourceError` (-32603) |
| Prompt | The user | A JSON-RPC error (`MCPError`) | `MCPError` directly (see `prompt_errors()`) |

Tool errors are results rather than protocol errors so that the AI can see what went wrong and try again. Resources and prompts are driven by the user or the app, so their failures are reported to the client as ordinary request errors.

## Bulk import and logging to the client

`bulk_import_employees(csv_text)` imports many employees at once and reports what it's doing while it runs.

```mermaid
sequenceDiagram
    participant C as MCP client
    participant S as bulk_import_employees
    participant DB as PostgreSQL

    C->>S: tools/call {csv_text}
    S->>S: Check header (missing columns → ToolError), row count (max 1,000)
    S-->>C: log info "Importing 4 rows."
    loop each row
        S->>S: Validate with NewEmployee (same rules as create_employee)
        S-->>C: log warning "Skipped line 4: …" (invalid rows)
    end
    S->>DB: BEGIN
    loop each valid row
        S->>DB: SAVEPOINT; INSERT … RETURNING
        alt duplicate email
            DB-->>S: UniqueViolation → ROLLBACK TO SAVEPOINT
            S-->>C: log warning "Skipped line 5: … already exists"
        else ok
            S-->>C: log debug "Imported line 2: Nisha Rao (id 32)"
        end
    end
    S->>DB: COMMIT
    S-->>C: log info "Import finished: 2 imported, 2 skipped."
    S-->>C: ImportSummary {imported, skipped, created_ids, problems}
```

- **Best effort, not all-or-nothing.** A problem with one row doesn't stop the others. Validation errors are caught before anything is written. A duplicate email is caught by the database, and a nested transaction (a savepoint) rolls back only that row. Header, row-count and connection problems are still `ToolError`s, because they mean no row can be imported at all.
- **Choosing log levels.** Per-row successes are `debug`, skipped rows are `warning`, and the start and finish messages are `info`. A client that only wants problems can ask for `warning` and above.
- **Client log vs server log.** `ctx.info()` and similar calls send `notifications/message` to the client, which may show them to the user. `mcp_calls.log` is the operator's own log. The two are independent.
- **Async tool, blocking database.** Validation and logging run on the event loop. The inserts run in one `asyncio.to_thread` call, and their results are then reported to the client.
- **Protocol deprecation.** The MCP 2026-07-28 revision (SEP-2577) deprecates the logging capability. On earlier protocol versions, which this project's client negotiates (2025-11-25), every level is delivered. On 2026-07-28+ connections, a server only sends log messages for requests where the client opts in through `_meta`. `ImportSummary` therefore repeats every problem, so callers never depend on log delivery. The SDK's deprecation warning for `ctx.log` is filtered in `server.py`, with a comment explaining why.

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
- **Connection.** The server reads `DATABASE_URL`, which defaults to `postgresql://mcp_user:mcp_password@localhost:5433/company`. It opens a new connection per call with a 5-second connect timeout.
- **Rows as models.** The query uses `class_row(Employee)`, so each row is built as a Pydantic `Employee` and validated. Pydantic converts `NUMERIC` (`Decimal`) to `float`, and `DATE` to an ISO string in the JSON result. If a row doesn't match the model, the call fails with a logged error instead of returning wrong data.
- **Queries.** Values are always passed as `%s` parameters, never formatted into the SQL string. The column list (`EMPLOYEE_COLUMNS`) is taken from the model's fields and used in both `SELECT` and `RETURNING`, so the queries and the model can't drift apart.
- **Transactions.** Every tool call runs in one transaction through `employees_db()`. psycopg commits when the block finishes normally and rolls back if anything raises, including a `ToolError`. Write tools use `RETURNING`, so each change and the record it returns come from a single statement.
- **Id gaps are expected.** A failed insert, such as one with a duplicate email, still uses up the next value of the `id` sequence, because Postgres sequences are never rolled back. Ids are therefore unique but not guaranteed to be consecutive.
- **Port.** The database uses host port 5433 so that it can run alongside another Postgres server on the default port, 5432.

## Design decisions

| Decision | Reason |
|---|---|
| stdio transport | This is the standard way to run a local MCP server. The client manages the server process, and no network port or authentication is needed. |
| Type hints as the contract | One source of truth: the SDK builds both the schema the AI sees and the input validation from the function signature. |
| Server-side confirmation (elicitation) for deletes, refusing if the client can't prompt | Annotations are only hints. For an action that can't be undone, the server enforces the "ask first" rule itself and fails safe. |
| Resources for read-only reference data, next to the read tools | The client or user can attach data (a policy document, a profile) to the conversation directly, without the AI having to decide to call a tool |
| `ToolError` for expected failures | The caller gets a clear, useful message, and the server keeps running. |
| A logging decorator instead of logging in each tool | Every tool is logged the same way, and adding a tool doesn't need any logging code. |
| Pydantic models as tool results | Clients get a detailed output schema, rows are validated, and type conversion (`Decimal`, `date`) is handled in one place. |
| Postgres in Docker Compose | One command starts the database and loads sample data, the same way on any machine. |

## Known limitations

These are deliberate simplifications for a learning project, with what a production version would change:

- **A new database connection per call.** A connection pool (`psycopg_pool`) would avoid the setup cost of each call.
- **Database queries can't be interrupted.** `get_employees` runs on a worker thread, so a client cancellation doesn't stop a query that's already running. An async tool with `psycopg.AsyncConnection` and a Postgres `statement_timeout` would make queries cancellable and time-limited.
- **No automated test suite.** `test_client.py` is an end-to-end script that prints results rather than asserting them. The next step would be pytest tests that check `is_error` and returned values.
- **Only deletes are confirmed.** `update_employee_salary` is also destructive, but it relies on the client respecting its annotations. The same elicitation pattern could confirm large salary changes, for example.
- **No authentication.** That's appropriate for stdio, where only the parent process can talk to the server. The streamable HTTP transport would need an auth layer.
- **Development credentials in `docker-compose.yml`.** Fine for a local sample database. A real deployment would load them from secrets.
