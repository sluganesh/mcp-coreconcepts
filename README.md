# MCP Learning Server

A local [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server written in Python. It gives AI assistants such as Claude three tools: two arithmetic tools and an employee lookup backed by PostgreSQL. Every call is logged, and every failure returns a clear error message instead of crashing the server.

**Built with:** Python 3.12 · MCP Python SDK 2.x · PostgreSQL 16 · psycopg 3 · Docker Compose

## What it shows

- **MCP server development:** tools are defined with type hints, and the SDK generates each tool's input schema and argument validation from them.
- **Error handling:** expected failures such as dividing by zero, an unknown employee id or an unreachable database return a short `ToolError` message to the client. The full technical detail goes only to the server log.
- **Logging:** a decorator records every tool call with its arguments, result and duration. Logs never go to stdout, because the stdio transport uses stdout for protocol messages.
- **Database access:** parameterized SQL queries, connection timeouts, and results converted to JSON-friendly types.
- **Reproducible setup:** Docker Compose starts Postgres with a health check and loads sample data automatically.
- **End-to-end testing:** a test client starts the server as a real MCP client would and calls every tool, covering both success and failure cases.

## How it works

```mermaid
flowchart LR
    Client["MCP client<br/>(Claude, MCP Inspector, test_client.py)"]
    Server["server.py<br/>MCPServer + @log_call"]
    Log[("mcp_calls.log")]
    DB[("PostgreSQL 16<br/>Docker, port 5433")]

    Client <-- "JSON-RPC over stdio" --> Server
    Server -- "every call" --> Log
    Server -- "get_employees" --> DB
```

## Tools

| Tool | Arguments | Returns | Errors |
|---|---|---|---|
| `add_integer` | `a: int`, `b: int` | The sum | Arguments that aren't integers |
| `divide` | `a: float`, `b: float` | `a / b` | Division by zero; arguments that aren't numbers |
| `get_employees` | `employee_id: int` (optional) | All employees, or the one with that id | Unknown id; id less than 1; database unavailable |

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
Tools: ['add_integer', 'divide', 'get_employees']
add_integer(7, 35) = 42
divide({'a': 10, 'b': 4}) -> OK: 2.5
divide({'a': 10, 'b': 0}) -> ERROR: Error executing tool divide: Cannot divide by zero: 'b' must be a non-zero number.
get_employees({}) -> OK: 10 row(s), first: {'id': 1, 'first_name': 'Aarav', ...}
get_employees({'employee_id': 3}) -> OK: 1 row(s), first: {'id': 3, 'first_name': 'Rahul', ...}
get_employees({'employee_id': 999}) -> ERROR: Error executing tool get_employees: No employee found with id 999.
get_employees({'employee_id': 0}) -> ERROR: Error executing tool get_employees: 'employee_id' must be a positive integer.
```

(The output above is shortened. The full run also prints a validation error for `divide(10, "abc")`.)

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
docker-compose.yml   PostgreSQL 16 container with health check
requirements.txt     Python dependencies
```

## Adding a tool

```python
@mcp.tool()      # registers the tool; the schema comes from the type hints
@log_call        # logs arguments, result, duration and errors
def multiply(a: float, b: float) -> float:
    """Multiply two numbers."""   # shown to the AI as the tool's description
    return a * b
```

For a failure the caller should know about, raise `ToolError("clear message")`.
