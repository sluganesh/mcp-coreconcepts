# MCP Learning Server

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server written in Python. It gives AI assistants such as Claude seven tools: two arithmetic tools, tools to read, create, update and delete employees in PostgreSQL, and a long-running job that demonstrates timeouts. Every call is logged, and every failure returns a clear error message instead of crashing the server.

**Built with:** Python 3.12 · MCP Python SDK 2.x · PostgreSQL 16 · psycopg 3 · Docker Compose

## What it shows

- **MCP server development:** tools are defined with type hints, and the SDK generates each tool's input schema and argument validation from them.
- **Error handling:** expected failures such as dividing by zero, an unknown employee id or an unreachable database return a short `ToolError` message to the client. The full technical detail goes only to the server log.
- **Async and timeouts:** an async tool reports progress while it runs and enforces its own time limit. The client can also set a timeout, and the server logs the resulting cancellation.
- **Logging:** a decorator records every tool call with its arguments, result and duration. Logs never go to stdout, because the stdio transport uses stdout for protocol messages.
- **Typed results:** `get_employees` returns Pydantic models, so clients get a detailed output schema (field names, types and descriptions), and every row is checked before it's returned.
- **Write tools with tool annotations:** create, update and delete tools run in database transactions and check their inputs (email format, salary above 0). Each tool is marked as read-only, destructive or idempotent, so clients can decide which calls need the user's approval.
- **Database access:** parameterized SQL queries, transactions that roll back on failure, and connection timeouts.
- **Reproducible setup:** Docker Compose starts Postgres with a health check and loads sample data automatically.
- **End-to-end testing:** a test client starts the server as a real MCP client would and calls every tool, covering both success and failure cases.

## How it works

For the full design, including request flow, error handling, timeouts and trade-offs, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```mermaid
flowchart LR
    Client["MCP client<br/>(Claude, MCP Inspector, test_client.py)"]
    Server["server.py<br/>MCPServer + @log_call"]
    Log[("mcp_calls.log")]
    DB[("PostgreSQL 16<br/>Docker, port 5433")]

    Client <-- "JSON-RPC over stdio" --> Server
    Server -- "every call" --> Log
    Server -- "employee tools" --> DB
```

## Tools

| Tool | Arguments | Returns | Errors |
|---|---|---|---|
| `add_integer` | `a: int`, `b: int` | The sum | Arguments that aren't integers |
| `divide` | `a: float`, `b: float` | `a / b` | Division by zero; arguments that aren't numbers |
| `get_employees` | `employee_id: int` (optional) | `{"employees": [...], "count": n}`: all employees, or the one with that id | Unknown id; id less than 1; database unavailable |
| `create_employee` | `first_name`, `last_name`, `email`, `department`, `job_title`, `salary`, `hire_date` (optional, defaults to today) | The new employee record | Duplicate email; invalid email; salary not above 0; empty or too-long text |
| `update_employee_salary` | `employee_id: int`, `new_salary: float` | The updated employee record | Unknown id; salary not above 0 |
| `delete_employee` | `employee_id: int` | The deleted employee record | Unknown id |
| `long_running_task` | `duration_seconds: float`, `timeout_seconds: float` (default 5) | A completion message, with a progress update every second | Task ran past its timeout; a value that isn't between 0 and 120 |

### Tool annotations

Each tool tells clients how it behaves. Clients can use these hints, for example to run read-only tools without asking and to ask the user before running destructive ones.

| Tool | Read-only | Destructive | Idempotent | Why |
|---|---|---|---|---|
| `add_integer`, `divide`, `get_employees`, `long_running_task` | ✅ | — | ✅ | They don't change anything |
| `create_employee` | ❌ | ❌ | ❌ | Adds a row without changing existing data; calling it twice adds two rows |
| `update_employee_salary` | ❌ | ✅ | ✅ | Overwrites the old salary; setting the same value twice gives the same result |
| `delete_employee` | ❌ | ✅ | ✅ | Removes data; deleting the same id again changes nothing more |

All tools set `open_world_hint` to false, because they only touch this server's own data. Annotations are hints: the server doesn't enforce them, and clients shouldn't treat them as a security boundary.

### Timeouts

`long_running_task` simulates a slow job and shows two ways a timeout can happen:

- **Server-side timeout.** The tool wraps its work in `asyncio.wait_for`. When `timeout_seconds` runs out, the work is cancelled and the client gets a normal error result: `Task timed out after 3s (it needed 8s)`.
- **Client-side timeout.** The caller passes `read_timeout_seconds` to `call_tool` and handles the `MCPError` raised when time runs out. The SDK also sends the server a cancellation message. The server stops the task and logs `CANCELLED long_running_task after 2.00 s`, so no work is left running in the background.

## Getting started

**Prerequisites:** Python 3.12 or later, Docker, and Node.js (only needed for the browser-based Inspector).

```bash
# 1. Clone the repository and install dependencies
git clone https://github.com/sluganesh/mcp-learning.git
cd mcp-learning
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2. Start PostgreSQL (the sample employee data loads on first start)
docker compose up -d --wait

# 3. Run the end-to-end test
python test_client.py
```

Expected output:

```text
Tools: ['add_integer', 'divide', 'get_employees', 'create_employee', 'update_employee_salary', 'delete_employee', 'long_running_task']
add_integer(7, 35) = 42
divide({'a': 10, 'b': 4}) -> OK: 2.5
divide({'a': 10, 'b': 0}) -> ERROR: Error executing tool divide: Cannot divide by zero: 'b' must be a non-zero number.
get_employees({}) -> OK: 10 row(s), first: {'id': 1, 'first_name': 'Aarav', ...}
get_employees({'employee_id': 3}) -> OK: 1 row(s), first: {'id': 3, 'first_name': 'Rahul', ...}
get_employees({'employee_id': 999}) -> ERROR: Error executing tool get_employees: No employee found with id 999.
get_employees({'employee_id': 0}) -> ERROR: Error executing tool get_employees: 'employee_id' must be a positive integer.
create_employee({... 'email': 'test.554db078@example.com', 'salary': 50000}) -> OK: {'id': 13, ..., 'hire_date': '2026-09-24'}
create_employee({'first_name': 'Dup', ...}) -> ERROR: Error executing tool create_employee: An employee with email test.554db078@example.com already exists.
update_employee_salary({'employee_id': 13, 'new_salary': 55000}) -> OK: {'id': 13, ..., 'salary': 55000.0, ...}
update_employee_salary({'employee_id': 999, 'new_salary': 55000}) -> ERROR: Error executing tool update_employee_salary: No employee found with id 999.
delete_employee({'employee_id': 13}) -> OK: {'id': 13, 'first_name': 'Test', ...}
delete_employee({'employee_id': 13}) -> ERROR: Error executing tool delete_employee: No employee found with id 13.
long_running_task({'duration_seconds': 2, 'timeout_seconds': 5}):
    progress: 1/2 - 1s of 2s done
    progress: 2/2 - 2s of 2s done
  -> OK: Task finished in 2s.
long_running_task({'duration_seconds': 8, 'timeout_seconds': 3}):
    progress: 1/8 - 1s of 8s done
    progress: 2/8 - 2s of 8s done
  -> ERROR: Error executing tool long_running_task: Task timed out after 3s (it needed 8s). Try a larger timeout_seconds.
long_running_task({'duration_seconds': 10, 'timeout_seconds': 30}) with a 2s client timeout:
  -> CLIENT TIMEOUT: Request 'tools/call' timed out
```

(The output above is shortened. The full run also prints the tool annotations and the validation errors for `divide(10, "abc")`, an invalid email and a negative salary. The test creates its own employee with a random email and deletes it at the end, so it can be run repeatedly.)

## Other ways to try it

**In a browser, with the MCP Inspector:**

```bash
npx @modelcontextprotocol/inspector python server.py
```

Open the URL it prints, click **Connect**, then go to **Tools** → **List Tools** and run any tool.

**From Claude Code:**

```bash
claude mcp add learning-server -- <path-to>/.venv/Scripts/python.exe <path-to>/server.py
```

Then ask Claude something like *"Show me employee 3"* or *"What is 10 divided by 0?"*

## Logging

Each call adds lines like these to `mcp_calls.log`:

```text
2026-09-23 16:35:51,569 | INFO | CALL divide args=() kwargs={'a': 10.0, 'b': 4.0}
2026-09-23 16:35:51,569 | INFO | RESULT divide -> 2.5 (0.16 ms)
2026-09-23 16:35:51,572 | INFO | CALL divide args=() kwargs={'a': 10.0, 'b': 0.0}
2026-09-23 16:35:51,572 | WARNING | FAILED divide: Cannot divide by zero: 'b' must be a non-zero number.
```

Expected failures are logged as warnings without a stack trace. Unexpected exceptions are logged with a full stack trace.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql://mcp_user:mcp_password@localhost:5433/company` | Postgres connection string |

The database uses port **5433** so it doesn't clash with a Postgres server that may already be running on the default port, 5432. The credentials are for local development only.

## Project structure

```text
server.py            MCP server: tools, logging decorator, database access
test_client.py       End-to-end test that talks to the server over stdio
db/init.sql          Employees table and sample data
docs/ARCHITECTURE.md Design, request flow, error handling, timeouts
docker-compose.yml   PostgreSQL 16 container with health check
requirements.txt     Python dependencies
```

## Adding a tool

```python
@mcp.tool(annotations=READ_ONLY)   # registers the tool; the schema comes from the type hints
@log_call        # logs arguments, result, duration and errors
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""   # shown to the AI as the tool's description
    return a * b
```

For a failure the caller should know about, raise `ToolError("clear message")`.
