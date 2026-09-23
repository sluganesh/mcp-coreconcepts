"""A minimal local MCP server with math tools and an employees lookup tool.

Every tool call is logged to mcp_calls.log (and stderr). Nothing is logged to
stdout, because the stdio transport uses stdout for MCP protocol messages.
"""

import functools
import logging
import os
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

LOG_FILE = Path(__file__).with_name("mcp_calls.log")

# Matches docker-compose.yml; override with the DATABASE_URL environment variable.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://mcp_user:mcp_password@localhost:5433/company",
)

# Cast salary and hire_date so rows are plain JSON-friendly values.
EMPLOYEE_COLUMNS = """
    id, first_name, last_name, email, department, job_title,
    salary::float AS salary, hire_date::text AS hire_date
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("mcp-learning")

mcp = MCPServer("learning-server")


def log_call(func):
    """Log each tool call's name, arguments, result, and duration."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        logger.info("CALL %s args=%s kwargs=%s", func.__name__, args, kwargs)
        try:
            result = func(*args, **kwargs)
        except ToolError as exc:
            # Expected, user-facing error: log it without a stack trace.
            logger.warning("FAILED %s: %s", func.__name__, exc)
            raise
        except Exception:
            logger.exception("ERROR in %s", func.__name__)
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info("RESULT %s -> %r (%.2f ms)", func.__name__, result, elapsed_ms)
        return result

    return wrapper


@mcp.tool()
@log_call
def add_integer(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@mcp.tool()
@log_call
def divide(a: float, b: float) -> float:
    """Divide a by b and return the quotient."""
    if b == 0:
        raise ToolError("Cannot divide by zero: 'b' must be a non-zero number.")
    return a / b


@mcp.tool()
@log_call
def get_employees(employee_id: int | None = None) -> list[dict]:
    """List all employees, or only the employee with the given id."""
    if employee_id is not None and employee_id < 1:
        raise ToolError("'employee_id' must be a positive integer.")

    query = f"SELECT {EMPLOYEE_COLUMNS} FROM employees"
    params: tuple = ()
    if employee_id is not None:
        query += " WHERE id = %s"
        params = (employee_id,)
    query += " ORDER BY id"

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5, row_factory=dict_row) as conn:
            rows = conn.execute(query, params).fetchall()
    except psycopg.OperationalError as exc:
        logger.error("Database connection failed: %s", exc)
        raise ToolError(
            "Could not connect to the employees database. Is the Postgres container running?"
        ) from exc
    except psycopg.Error as exc:
        logger.error("Database query failed: %s", exc)
        raise ToolError("The employees query failed. Check the server log for details.") from exc

    if employee_id is not None and not rows:
        raise ToolError(f"No employee found with id {employee_id}.")
    return rows


if __name__ == "__main__":
    logger.info("Starting learning-server (stdio transport)")
    mcp.run()
