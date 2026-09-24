"""A minimal local MCP server with math tools, employee read/write tools, and a
long-running task that demonstrates timeouts.

Every tool call is logged to mcp_calls.log (and stderr). Nothing is logged to
stdout, because the stdio transport uses stdout for MCP protocol messages.
"""

import asyncio
import functools
import inspect
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

import psycopg
from psycopg.rows import class_row
from pydantic import BaseModel, Field
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

LOG_FILE = Path(__file__).with_name("mcp_calls.log")

# Matches docker-compose.yml; override with the DATABASE_URL environment variable.
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://mcp_user:mcp_password@localhost:5433/company",
)


# The SDK turns these models' fields, descriptions and docstrings into the
# tool's output schema, which clients see. Pydantic also converts database
# types: NUMERIC (Decimal) to float, and DATE to an ISO date string in JSON.
class Employee(BaseModel):
    """An employee record."""

    id: int = Field(description="Unique employee id.")
    first_name: str
    last_name: str
    email: str = Field(description="Work email address.")
    department: str = Field(description="Department name, e.g. Engineering or Sales.")
    job_title: str
    salary: float = Field(description="Annual salary in USD.")
    hire_date: date = Field(description="Date the employee joined (YYYY-MM-DD).")


class EmployeeList(BaseModel):
    """Result of get_employees."""

    employees: list[Employee]
    count: int = Field(description="Number of employees returned.")


# Tool annotations are hints for clients, e.g. to auto-approve read-only tools
# and ask the user before running destructive ones. They are not enforced.
# open_world_hint=False: the tools only touch this server's own data.
READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

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
    """Log each tool call's name, arguments, result, and duration.

    Works for both sync and async tools. Apply it below @mcp.tool(): wraps()
    keeps the original signature, which the SDK uses to build the tool's
    input schema.
    """
    name = func.__name__

    def log_start(args, kwargs):
        # The SDK-injected Context object is noise in the log, so leave it out.
        shown = {k: v for k, v in kwargs.items() if not isinstance(v, Context)}
        logger.info("CALL %s args=%s kwargs=%s", name, args, shown)
        return time.perf_counter()

    def log_result(result, start):
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info("RESULT %s -> %r (%.2f ms)", name, result, elapsed_ms)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = log_start(args, kwargs)
            try:
                result = await func(*args, **kwargs)
            except ToolError as exc:
                logger.warning("FAILED %s: %s", name, exc)
                raise
            except asyncio.CancelledError:
                # The client cancelled the request or disconnected. A client-side
                # read_timeout_seconds expiring ends up here, because the SDK
                # sends a cancellation when it stops waiting.
                logger.warning("CANCELLED %s after %.2f s", name, time.perf_counter() - start)
                raise
            except Exception:
                logger.exception("ERROR in %s", name)
                raise
            log_result(result, start)
            return result

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = log_start(args, kwargs)
        try:
            result = func(*args, **kwargs)
        except ToolError as exc:
            # Expected, user-facing error: log it without a stack trace.
            logger.warning("FAILED %s: %s", name, exc)
            raise
        except Exception:
            logger.exception("ERROR in %s", name)
            raise
        log_result(result, start)
        return result

    return wrapper


@mcp.tool(annotations=READ_ONLY)
@log_call
def add_integer(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@mcp.tool(annotations=READ_ONLY)
@log_call
def divide(a: float, b: float) -> float:
    """Divide a by b and return the quotient."""
    if b == 0:
        raise ToolError("Cannot divide by zero: 'b' must be a non-zero number.")
    return a / b


EMPLOYEE_COLUMNS = ", ".join(Employee.model_fields)


@contextmanager
def employees_db():
    """Open a database transaction and turn database errors into ToolErrors.

    psycopg commits when the block finishes normally and rolls back if it
    raises, so a failed write never leaves partial changes behind.
    """
    try:
        # class_row builds an Employee from each row, so Pydantic validates it.
        with psycopg.connect(DATABASE_URL, connect_timeout=5, row_factory=class_row(Employee)) as conn:
            yield conn
    except psycopg.OperationalError as exc:
        logger.error("Database connection failed: %s", exc)
        raise ToolError(
            "Could not connect to the employees database. Is the Postgres container running?"
        ) from exc
    except psycopg.Error as exc:
        logger.error("Database operation failed: %s", exc)
        raise ToolError("The database operation failed. Check the server log for details.") from exc


EmployeeId = Annotated[int, Field(ge=1, description="Id of the employee.")]
Salary = Annotated[float, Field(gt=0, lt=100_000_000, description="Annual salary in USD.")]
Name = Annotated[str, Field(min_length=1, max_length=50)]


@mcp.tool(annotations=READ_ONLY)
@log_call
def get_employees(employee_id: int | None = None) -> EmployeeList:
    """List all employees, or only the employee with the given id."""
    if employee_id is not None and employee_id < 1:
        raise ToolError("'employee_id' must be a positive integer.")

    query = f"SELECT {EMPLOYEE_COLUMNS} FROM employees"
    params: tuple = ()
    if employee_id is not None:
        query += " WHERE id = %s"
        params = (employee_id,)
    query += " ORDER BY id"

    with employees_db() as conn:
        rows = conn.execute(query, params).fetchall()

    if employee_id is not None and not rows:
        raise ToolError(f"No employee found with id {employee_id}.")
    return EmployeeList(employees=rows, count=len(rows))


# Adds a new row and changes nothing that already exists, so it isn't
# destructive. Not idempotent: calling it twice tries to add two employees.
@mcp.tool(
    annotations=ToolAnnotations(
        title="Create employee",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
@log_call
def create_employee(
    first_name: Name,
    last_name: Name,
    email: Annotated[
        str,
        Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=100, description="Work email address. Must be unique."),
    ],
    department: Name,
    job_title: Annotated[str, Field(min_length=1, max_length=100)],
    salary: Salary,
    hire_date: Annotated[date | None, Field(description="Start date (YYYY-MM-DD). Defaults to today.")] = None,
) -> Employee:
    """Add a new employee and return the created record."""
    try:
        with employees_db() as conn:
            return conn.execute(
                f"""
                INSERT INTO employees (first_name, last_name, email, department, job_title, salary, hire_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {EMPLOYEE_COLUMNS}
                """,
                (first_name, last_name, email, department, job_title, salary, hire_date or date.today()),
            ).fetchone()
    except ToolError as exc:
        # Report a duplicate email clearly instead of as a generic failure.
        if isinstance(exc.__cause__, psycopg.errors.UniqueViolation):
            raise ToolError(f"An employee with email {email} already exists.") from None
        raise


# Overwrites the old salary, so it's destructive. Idempotent: setting the same
# salary twice leaves the same result.
@mcp.tool(
    annotations=ToolAnnotations(
        title="Update employee salary",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
@log_call
def update_employee_salary(employee_id: EmployeeId, new_salary: Salary) -> Employee:
    """Change an employee's annual salary and return the updated record."""
    with employees_db() as conn:
        employee = conn.execute(
            f"UPDATE employees SET salary = %s WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}",
            (new_salary, employee_id),
        ).fetchone()
    if employee is None:
        raise ToolError(f"No employee found with id {employee_id}.")
    return employee


# Removes data, so it's destructive. Idempotent: deleting the same id again
# leaves the database in the same state (the second call reports "not found").
@mcp.tool(
    annotations=ToolAnnotations(
        title="Delete employee",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=True,
        open_world_hint=False,
    )
)
@log_call
def delete_employee(employee_id: EmployeeId) -> Employee:
    """Permanently delete an employee and return the record that was removed."""
    with employees_db() as conn:
        employee = conn.execute(
            f"DELETE FROM employees WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}",
            (employee_id,),
        ).fetchone()
    if employee is None:
        raise ToolError(f"No employee found with id {employee_id}.")
    return employee


# Upper bound for both arguments, so a caller can't tie the server up indefinitely.
MAX_TASK_SECONDS = 120


# ctx is injected by the SDK and is not part of the tool's input schema.
@mcp.tool(annotations=READ_ONLY)
@log_call
async def long_running_task(
    ctx: Context, duration_seconds: float, timeout_seconds: float = 5.0
) -> str:
    """Simulate a job that takes duration_seconds to finish.

    The job is stopped with an error if it runs longer than timeout_seconds.
    Sends a progress update every second while it runs.
    """
    for arg, value in (("duration_seconds", duration_seconds), ("timeout_seconds", timeout_seconds)):
        if not 0 < value <= MAX_TASK_SECONDS:
            raise ToolError(f"'{arg}' must be greater than 0 and at most {MAX_TASK_SECONDS}.")

    async def work():
        elapsed = 0.0
        while elapsed < duration_seconds:
            step = min(1.0, duration_seconds - elapsed)
            await asyncio.sleep(step)
            elapsed += step
            await ctx.report_progress(elapsed, duration_seconds, f"{elapsed:.0f}s of {duration_seconds:.0f}s done")

    # Server-side timeout: wait_for cancels work() when the time is up and
    # raises TimeoutError, which becomes a normal error result for the client.
    # `from None` hides the internal traceback, which isn't useful to the caller.
    try:
        await asyncio.wait_for(work(), timeout=timeout_seconds)
    except TimeoutError:
        raise ToolError(
            f"Task timed out after {timeout_seconds:g}s "
            f"(it needed {duration_seconds:g}s). Try a larger timeout_seconds."
        ) from None
    return f"Task finished in {duration_seconds:g}s."


if __name__ == "__main__":
    logger.info("Starting learning-server (stdio transport)")
    mcp.run()
