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
- **Resources and resource templates:** read-only data a client can load into the AI's context: an HR handbook, `employees://{employee_id}` profiles and `departments://{department}/employees` rosters.
- **Elicitation (asking the user partway through a call):** `delete_employee` pauses to ask the user to confirm, and deletes only on an explicit "yes". It refuses to run on clients that can't show the prompt.
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
| `delete_employee` | `employee_id: int` | The deleted employee record, after the user confirms | Unknown id; the user declined, cancelled or didn't confirm; client can't show a confirmation prompt |
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

### Confirming deletes (elicitation)

Annotations let a client ask before running a tool, but nothing forces it to. For deletes, the server asks the user itself:

1. `delete_employee` looks up the employee, then sends the client a prompt: *"Permanently delete Rahul Verma (id 3, Engineering Manager, Engineering)? This cannot be undone."* The prompt includes a `confirm` checkbox.
2. The client shows the prompt, and the user replies in one of three ways: **accept** (with the box checked or not), **decline** or **cancel**.
3. Only **accept** with `confirm` checked deletes the record. Every other answer returns an error saying nothing was deleted.
4. If the client doesn't support elicitation, the tool refuses rather than deleting without asking.

### Timeouts

`long_running_task` simulates a slow job and shows two ways a timeout can happen:

- **Server-side timeout.** The tool wraps its work in `asyncio.wait_for`. When `timeout_seconds` runs out, the work is cancelled and the client gets a normal error result: `Task timed out after 3s (it needed 8s)`.
- **Client-side timeout.** The caller passes `read_timeout_seconds` to `call_tool` and handles the `MCPError` raised when time runs out. The SDK also sends the server a cancellation message. The server stops the task and logs `CANCELLED long_running_task after 2.00 s`, so no work is left running in the background.

## Resources

Resources are read-only data identified by a URI. A **tool** is an action the AI decides to call. A **resource** is content the user or the client application chooses to load into the AI's context, like attaching a document to a chat.

| URI | Kind | Type | Contents |
|---|---|---|---|
| `hr://handbook` | Static | `text/markdown` | Company policies: working hours, leave, salaries, expenses and onboarding. Read from [resources/hr_handbook.md](resources/hr_handbook.md). |
| `employees://{employee_id}` | Template | `application/json` | One employee's record, for example `employees://3` |
| `departments://{department}/employees` | Template | `application/json` | Everyone in a department, for example `departments://engineering/employees` (not case-sensitive) |

A **template** is a URI pattern with placeholders. Clients get templates from a separate listing (`resources/templates/list`) and fill in the values themselves.

Resource errors reach the client as protocol errors (`MCPError`), not as tool-style `is_error` results:
- Code `-32602` for anything that doesn't exist: an unknown employee id, an id that isn't a number, an unknown department (the message lists the real departments), or a URI that matches nothing.
- Code `-32603` for other failures, such as the database being down.

## Getting started

**Prerequisites:** Python 3.12 or later, Docker, and Node.js (only needed for the browser-based Inspector).

```bash
# 1. Clone the repository and install dependencies
git clone https://github.com/sluganesh/mcp-coreconcepts.git
cd mcp-coreconcepts
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
create_employee({... 'email': 'test.cd83c531@example.com', 'salary': 50000}) -> OK: {'id': 17, ..., 'hire_date': '2026-09-24'}
create_employee({'first_name': 'Dup', ...}) -> ERROR: Error executing tool create_employee: An employee with email test.cd83c531@example.com already exists.
update_employee_salary({'employee_id': 17, 'new_salary': 55000}) -> OK: {'id': 17, ..., 'salary': 55000.0, ...}
update_employee_salary({'employee_id': 999, 'new_salary': 55000}) -> ERROR: Error executing tool update_employee_salary: No employee found with id 999.
    prompt: Permanently delete Test User (id 17, Tester, QA)? This cannot be undone.
    answer: decline
delete_employee({'employee_id': 17}) -> ERROR: Error executing tool delete_employee: The user declined. Employee 17 was not deleted.
    ... (cancel, and accept without confirming, are refused the same way)
  (client without elicitation support)
delete_employee({'employee_id': 17}) -> ERROR: Error executing tool delete_employee: delete_employee needs a client that supports confirmation prompts (MCP elicitation). Nothing was deleted.
    prompt: Permanently delete Test User (id 17, Tester, QA)? This cannot be undone.
    answer: accept {'confirm': True}
delete_employee({'employee_id': 17}) -> OK: {'id': 17, 'first_name': 'Test', ...}
delete_employee({'employee_id': 17}) -> ERROR: Error executing tool delete_employee: No employee found with id 17.
Resources: [('hr://handbook', 'text/markdown')]
Resource templates: ['employees://{employee_id}', 'departments://{department}/employees']
read hr://handbook -> OK (text/markdown): # Example Corp Employee Handbook ...
read employees://3 -> OK (application/json): {"id": 3, "first_name": "Rahul", "last_name": "Verma", ...
read departments://engineering/employees -> OK (application/json): {"employees": [{"id": 1, "first_name": "Aarav", ...
read employees://999 -> MCPError -32602: No employee found with id 999.
read employees://abc -> MCPError -32602: 'abc' is not a valid employee id. Ids are whole numbers.
read departments://Legal/employees -> MCPError -32602: No department named 'Legal'. Departments: Engineering, Finance, HR, Marketing, Sales.
read payroll://2026 -> MCPError -32602: Unknown resource: payroll://2026
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
resources/           Static resource content (HR handbook)
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
