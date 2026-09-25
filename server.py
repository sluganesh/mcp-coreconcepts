"""A minimal local MCP server with math tools, employee read/write tools,
read-only resources (HR handbook, employee profiles, department rosters),
HR prompts with argument completion, and a long-running task that
demonstrates timeouts.

Runs over stdio by default (the client starts the server), or with --http as a
streamable HTTP service at http://127.0.0.1:8000/mcp that many clients share.

Every tool call is logged to mcp_calls.log (and stderr). Nothing is logged to
stdout, because the stdio transport uses stdout for MCP protocol messages.
"""

import argparse
import asyncio
import base64
import csv
import functools
import io
import inspect
import json
import logging
import os
import sys
import time
import warnings
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

import anyio.from_thread
import psycopg
from psycopg.rows import class_row, dict_row
from pydantic import BaseModel, Field, ValidationError
from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.server.mcpserver.prompts.base import Message, UserMessage
from mcp.shared.exceptions import MCPDeprecationWarning, MCPError
from mcp.types import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    Completion,
    EmbeddedResource,
    TextResourceContents,
    ToolAnnotations,
)

LOG_FILE = Path(__file__).with_name("mcp_calls.log")
HANDBOOK_FILE = Path(__file__).parent / "resources" / "hr_handbook.md"

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
    """A page of employees."""

    employees: list[Employee]
    count: int = Field(description="Number of employees in this page.")
    total: int | None = Field(default=None, description="Number of employees in all pages.")
    next_cursor: str | None = Field(
        default=None,
        description="Pass this as 'cursor' to get the next page. Null when this is the last page.",
    )


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


# Anticipated, user-facing failures: logged as warnings without a stack trace.
EXPECTED_ERRORS = (ToolError, ResourceError, MCPError)

# Long arguments and results (e.g. a CSV, the whole handbook) are cut short in the log.
MAX_LOGGED_VALUE = 500


def log_call(func):
    """Log each tool, resource or prompt call's name, arguments, result, and duration.

    Works for both sync and async functions. Apply it below @mcp.tool(): wraps()
    keeps the original signature, which the SDK uses to build the tool's
    input schema.
    """
    name = func.__name__

    def short(value):
        text = repr(value)
        if len(text) > MAX_LOGGED_VALUE:
            text = f"{text[:MAX_LOGGED_VALUE]}... ({len(text)} chars)"
        return text

    def log_start(args, kwargs):
        # The SDK-injected Context object is noise in the log, so leave it out.
        shown = {k: v for k, v in kwargs.items() if not isinstance(v, Context)}
        logger.info("CALL %s args=%s kwargs=%s", name, args, short(shown))
        return time.perf_counter()

    def log_result(result, start):
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info("RESULT %s -> %s (%.2f ms)", name, short(result), elapsed_ms)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = log_start(args, kwargs)
            try:
                result = await func(*args, **kwargs)
            except EXPECTED_ERRORS as exc:
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
        except EXPECTED_ERRORS as exc:
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


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

EmployeeId = Annotated[int, Field(ge=1, description="Id of the employee.")]
Salary = Annotated[float, Field(gt=0, lt=100_000_000, description="Annual salary in USD.")]
Name = Annotated[str, Field(min_length=1, max_length=50)]


# --- Change notifications ---------------------------------------------------
# When an employee changes, tell clients subscribed to the affected resources:
# the employee's profile and their department's roster. Called only after the
# database transaction has committed, so a subscriber that re-reads the
# resource sees the new data.
#
# These reach clients on the 2026-07-28 protocol, which subscribe with
# subscriptions/listen. MCPServer doesn't implement the older
# resources/subscribe, so clients on earlier protocols can't subscribe.


async def notify_employee_changed(ctx: Context, employee: Employee) -> None:
    for uri in (f"employees://{employee.id}", f"departments://{employee.department}/employees"):
        await ctx.notify_resource_updated(uri)
    logger.info("NOTIFY resources updated for employee %s (%s)", employee.id, employee.department)


def notify_from_thread(ctx: Context, employee: Employee) -> None:
    """notify_employee_changed for sync tools, which the SDK runs on a worker thread."""
    anyio.from_thread.run(notify_employee_changed, ctx, employee)


@mcp.tool(annotations=READ_ONLY)
@log_call
def get_employees(
    employee_id: int | None = None,
    page_size: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Employees per page.")] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[
        str | None, Field(description="The next_cursor from a previous page. Omit for the first page.")
    ] = None,
) -> EmployeeList:
    """List employees one page at a time, ordered by id, or get one employee by id.

    If next_cursor in the result isn't null, call again with cursor=next_cursor for more.
    """
    if employee_id is not None:
        if employee_id < 1:
            raise ToolError("'employee_id' must be a positive integer.")
        return EmployeeList(employees=[find_employee(employee_id)], count=1, total=1)

    after_id = decode_cursor(cursor) if cursor else 0
    with employees_db() as conn:
        # Keyset pagination: continue after the last id seen, rather than
        # skipping rows with OFFSET. Fetch one extra row to learn if there's more.
        rows = conn.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id > %s ORDER BY id LIMIT %s",
            (after_id, page_size + 1),
        ).fetchall()
        total = conn.cursor(row_factory=dict_row).execute("SELECT count(*) AS n FROM employees").fetchone()["n"]

    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = encode_cursor(page[-1].id) if has_more else None
    return EmployeeList(employees=page, count=len(page), total=total, next_cursor=next_cursor)


# Cursors are opaque to clients: they pass next_cursor back unchanged and must
# not build or parse one. Encoding the position means the format can change later.
def encode_cursor(after_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"after_id": after_id}).encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        after_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))["after_id"]
    except (ValueError, KeyError, TypeError):
        raise ToolError("Invalid cursor. Pass the next_cursor value from a previous page unchanged.") from None
    if not isinstance(after_id, int) or after_id < 0:
        raise ToolError("Invalid cursor. Pass the next_cursor value from a previous page unchanged.")
    return after_id


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
    ctx: Context,
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
            employee = conn.execute(
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
    notify_from_thread(ctx, employee)
    return employee


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
def update_employee_salary(ctx: Context, employee_id: EmployeeId, new_salary: Salary) -> Employee:
    """Change an employee's annual salary and return the updated record."""
    with employees_db() as conn:
        employee = conn.execute(
            f"UPDATE employees SET salary = %s WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}",
            (new_salary, employee_id),
        ).fetchone()
    if employee is None:
        raise ToolError(f"No employee found with id {employee_id}.")
    notify_from_thread(ctx, employee)
    return employee


class DeleteConfirmation(BaseModel):
    """What the user fills in when asked to confirm a deletion."""

    confirm: bool = Field(description="Check to permanently delete this employee.")


def find_employee(employee_id: int) -> Employee:
    with employees_db() as conn:
        employee = conn.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id = %s", (employee_id,)
        ).fetchone()
    if employee is None:
        raise ToolError(f"No employee found with id {employee_id}.")
    return employee


def remove_employee(employee_id: int) -> Employee:
    with employees_db() as conn:
        employee = conn.execute(
            f"DELETE FROM employees WHERE id = %s RETURNING {EMPLOYEE_COLUMNS}",
            (employee_id,),
        ).fetchone()
    # Someone else may have deleted it while the user was deciding.
    if employee is None:
        raise ToolError(f"No employee found with id {employee_id}.")
    return employee


async def ask_to_confirm_delete(ctx: Context, employee_id: int) -> Elicit[DeleteConfirmation]:
    """Resolver: build the confirmation question for delete_employee.

    The SDK asks it in the way the connection's protocol expects:
    - 2025-11-25 and earlier: a server-to-client elicitation/create request, mid-call.
    - 2026-07-28 and later: the call returns an InputRequiredResult; the client
      asks the user and calls the tool again with the answer. This resolver may
      run on each round, so it must not change anything.
    """
    # Refuse rather than delete without asking: this client can't show a prompt.
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        raise ToolError(
            "delete_employee needs a client that supports confirmation prompts "
            "(MCP elicitation). Nothing was deleted."
        )
    # Database calls are blocking, so run them on a worker thread.
    employee = await asyncio.to_thread(find_employee, employee_id)
    return Elicit(
        f"Permanently delete {employee.first_name} {employee.last_name} "
        f"(id {employee.id}, {employee.job_title}, {employee.department})? This cannot be undone.",
        DeleteConfirmation,
    )


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
async def delete_employee(
    ctx: Context,
    employee_id: EmployeeId,
    # Filled in by the SDK from the user's answer, not by the caller, so it isn't
    # part of the input schema.
    answer: Annotated[ElicitationResult[DeleteConfirmation], Resolve(ask_to_confirm_delete)],
) -> Employee:
    """Permanently delete an employee and return the record that was removed.

    The user is asked to confirm before anything is deleted.
    """
    logger.info(
        "ELICIT delete_employee id=%s -> %s %s",
        employee_id, answer.action, getattr(answer, "data", None) or "",
    )

    if answer.action == "decline":
        raise ToolError(f"The user declined. Employee {employee_id} was not deleted.")
    if answer.action == "cancel":
        raise ToolError(f"The user cancelled. Employee {employee_id} was not deleted.")
    if not answer.data.confirm:
        raise ToolError(f"Deletion was not confirmed. Employee {employee_id} was not deleted.")

    employee = await asyncio.to_thread(remove_employee, employee_id)
    await notify_employee_changed(ctx, employee)
    return employee


# --- Bulk import (logging to the client) ------------------------------------
# ctx.debug/info/warning send log messages to the client while the tool runs,
# separate from the server's own log file. The client chooses which levels it
# wants, so per-row detail goes out at debug and problems at warning.
#
# The MCP logging capability is deprecated as of the 2026-07-28 spec
# (SEP-2577). It still works on earlier protocol versions, which is what most
# clients use today. On 2026-07-28+ connections, messages are only sent when the
# client opts in per request. So the tool's result, ImportSummary, carries every
# problem too, and never depends on log messages arriving. The SDK warns on each
# ctx.info() call; that warning is silenced here because the deprecation is
# deliberate and documented (README and docs/ARCHITECTURE.md).
warnings.filterwarnings(
    "ignore", message="The logging capability is deprecated", category=MCPDeprecationWarning
)

MAX_IMPORT_ROWS = 1000
IMPORT_COLUMNS = ["first_name", "last_name", "email", "department", "job_title", "salary"]


class NewEmployee(BaseModel):
    """One CSV row, checked with the same rules as create_employee."""

    first_name: Name
    last_name: Name
    email: Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=100)]
    department: Name
    job_title: Annotated[str, Field(min_length=1, max_length=100)]
    salary: Salary
    hire_date: date | None = None


class ImportSummary(BaseModel):
    """Result of bulk_import_employees."""

    imported: int = Field(description="Number of employees added.")
    skipped: int = Field(description="Number of rows that were not imported.")
    created_ids: list[int] = Field(description="Ids of the employees added.")
    problems: list[str] = Field(description="One line per skipped row, e.g. 'line 3: duplicate email'.")


def insert_new_employees(rows: list[tuple[int, NewEmployee]]) -> list[tuple[int, Employee | str]]:
    """Insert rows one by one; return each line number with the new record or a problem."""
    outcomes = []
    with employees_db() as conn:
        for line, row in rows:
            try:
                # A nested transaction is a savepoint: a duplicate only undoes its own row.
                with conn.transaction():
                    employee = conn.execute(
                        f"""
                        INSERT INTO employees (first_name, last_name, email, department, job_title, salary, hire_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING {EMPLOYEE_COLUMNS}
                        """,
                        (row.first_name, row.last_name, row.email, row.department, row.job_title,
                         row.salary, row.hire_date or date.today()),
                    ).fetchone()
                outcomes.append((line, employee))
            except psycopg.errors.UniqueViolation:
                outcomes.append((line, f"an employee with email {row.email} already exists"))
    return outcomes


@mcp.tool(
    annotations=ToolAnnotations(
        title="Bulk import employees",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
)
@log_call
async def bulk_import_employees(
    ctx: Context,
    csv_text: Annotated[
        str,
        Field(description=(
            "CSV text with a header row. Columns: first_name, last_name, email, department, "
            "job_title, salary, and optionally hire_date (YYYY-MM-DD, defaults to today)."
        )),
    ],
) -> ImportSummary:
    """Add many employees from CSV. Valid rows are imported; invalid or duplicate rows are skipped and reported."""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    missing = [c for c in IMPORT_COLUMNS if c not in (reader.fieldnames or [])]
    if missing:
        raise ToolError(f"The CSV header is missing these columns: {', '.join(missing)}.")
    records = [(reader.line_num, record) for record in reader]
    if not records:
        raise ToolError("The CSV has a header but no rows.")
    if len(records) > MAX_IMPORT_ROWS:
        raise ToolError(f"Too many rows ({len(records)}). Import at most {MAX_IMPORT_ROWS} at a time.")

    await ctx.info(f"Importing {len(records)} rows.")
    problems: list[str] = []

    # 1. Check every row, and report the invalid ones as they're found.
    valid: list[tuple[int, NewEmployee]] = []
    for line, record in records:
        try:
            valid.append((line, NewEmployee.model_validate({k: v or None for k, v in record.items()})))
        except ValidationError as exc:
            reason = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            problems.append(f"line {line}: {reason}")
            await ctx.warning(f"Skipped line {line}: {reason}")

    # 2. Insert the valid rows. Database work is blocking, so it runs on a worker thread.
    created_ids: list[int] = []
    for line, outcome in await asyncio.to_thread(insert_new_employees, valid):
        if isinstance(outcome, str):
            problems.append(f"line {line}: {outcome}")
            await ctx.warning(f"Skipped line {line}: {outcome}")
        else:
            created_ids.append(outcome.id)
            await notify_employee_changed(ctx, outcome)
            await ctx.debug(f"Imported line {line}: {outcome.first_name} {outcome.last_name} (id {outcome.id})")

    await ctx.info(f"Import finished: {len(created_ids)} imported, {len(problems)} skipped.")
    return ImportSummary(
        imported=len(created_ids), skipped=len(problems), created_ids=created_ids, problems=problems
    )


# --- Resources --------------------------------------------------------------
# Resources are read-only data the client or user chooses to load into the
# AI's context. Tools are actions the AI decides to call. Resource failures
# reach the client as protocol errors (MCPError), not as is_error results.


@contextmanager
def resource_errors():
    """Report database failures as ResourceErrors instead of ToolErrors."""
    try:
        yield
    except ToolError as exc:
        raise ResourceError(str(exc)) from exc


@mcp.resource(
    "hr://handbook",
    title="Employee handbook",
    description="Company policies: working hours, leave, salaries, expenses and onboarding.",
    mime_type="text/markdown",
)
@log_call
def hr_handbook() -> str:
    return HANDBOOK_FILE.read_text(encoding="utf-8")


@mcp.resource(
    "employees://{employee_id}",
    title="Employee profile",
    description="One employee's record, by id.",
    mime_type="application/json",
)
@log_call
def employee_profile(employee_id: str) -> Employee:
    # Template values arrive as text. Checking here gives a clear "not found"
    # for a URI like employees://abc, instead of a generic conversion error.
    if not employee_id.isdigit():
        raise ResourceNotFoundError(f"'{employee_id}' is not a valid employee id. Ids are whole numbers.")
    with resource_errors(), employees_db() as conn:
        employee = conn.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE id = %s", (employee_id,)
        ).fetchone()
    if employee is None:
        raise ResourceNotFoundError(f"No employee found with id {employee_id}.")
    return employee


@mcp.resource(
    "departments://{department}/employees",
    title="Department roster",
    description="Everyone in a department, e.g. departments://Engineering/employees. Not case-sensitive.",
    mime_type="application/json",
)
@log_call
def department_roster(department: str) -> EmployeeList:
    with resource_errors(), employees_db() as conn:
        rows = conn.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM employees WHERE lower(department) = lower(%s) ORDER BY id",
            (department,),
        ).fetchall()
    if not rows:
        # Tell the reader which departments do exist.
        with resource_errors():
            known = ", ".join(list_departments())
        raise ResourceNotFoundError(f"No department named '{department}'. Departments: {known}.")
    return EmployeeList(employees=rows, count=len(rows))


def list_departments() -> list[str]:
    with employees_db() as conn:
        rows = conn.cursor(row_factory=dict_row).execute(
            "SELECT DISTINCT department FROM employees ORDER BY department"
        ).fetchall()
    return [row["department"] for row in rows]


# --- Prompts ----------------------------------------------------------------
# Prompts are reusable, user-picked templates: the client shows them (e.g. as
# slash commands), asks the user for the arguments, and sends the returned
# messages to the AI. Prompt arguments always arrive as strings.


@contextmanager
def prompt_errors():
    """Report failures as MCPErrors, the only kind a prompt passes through unchanged.

    Any other exception reaches the client as a generic "Error rendering prompt".
    """
    try:
        yield
    except ResourceNotFoundError as exc:
        raise MCPError(code=INVALID_PARAMS, message=str(exc)) from exc
    except ResourceError as exc:
        raise MCPError(code=INTERNAL_ERROR, message=str(exc)) from exc


def embedded_resource(uri: str, mime_type: str, text: str) -> EmbeddedResource:
    """Attach a resource's content to a prompt message, like attaching a file."""
    return EmbeddedResource(
        type="resource",
        resource=TextResourceContents(uri=uri, mime_type=mime_type, text=text),
    )


@mcp.prompt(
    title="Department headcount report",
    description="Summarize a department's team: size, roles, tenure and salary range.",
)
@log_call
def department_headcount_report(department: str) -> list[Message]:
    with prompt_errors():
        roster = department_roster(department)
    uri = f"departments://{department}/employees"
    return [
        UserMessage(embedded_resource(uri, "application/json", roster.model_dump_json(indent=2))),
        UserMessage(
            f"Using the attached roster, write a short headcount report for the {department} department. "
            "Include: the number of people, a breakdown by job title, average tenure in years "
            f"(today is {date.today().isoformat()}), and the salary range and median. "
            "Finish with one or two observations, such as a missing role or a single point of failure. "
            "Salary information is confidential, so don't name individuals next to their salaries."
        ),
    ]


@mcp.prompt(
    title="Welcome email for a new hire",
    description="Draft a welcome email for an employee, using their profile and the handbook.",
)
@log_call
def welcome_email(employee_id: str, tone: str = "warm and professional") -> list[Message]:
    with prompt_errors():
        employee = employee_profile(employee_id)
    return [
        UserMessage(embedded_resource(f"employees://{employee_id}", "application/json", employee.model_dump_json(indent=2))),
        UserMessage(embedded_resource("hr://handbook", "text/markdown", hr_handbook())),
        UserMessage(
            f"Draft a {tone} welcome email to {employee.first_name} {employee.last_name}, "
            f"who is joining {employee.department} as {employee.job_title} on {employee.hire_date.isoformat()}. "
            "Use the attached profile and handbook. Mention their onboarding buddy, the probation period "
            "and core working hours. Don't mention salary. Keep it under 200 words and end with a subject line suggestion."
        ),
    ]


# --- Completions ------------------------------------------------------------
# Suggest values while the user fills in a prompt argument or a resource
# template placeholder, e.g. typing "eng" suggests "Engineering".
MAX_COMPLETIONS = 100


@mcp.completion()
async def complete_argument(ref, argument, context) -> Completion | None:
    typed = argument.value.lower()
    if argument.name == "department":
        options = await asyncio.to_thread(list_departments)
    elif argument.name == "employee_id":
        options = [str(i) for i in await asyncio.to_thread(list_employee_ids)]
    else:
        return None
    matches = [o for o in options if o.lower().startswith(typed)]
    return Completion(values=matches[:MAX_COMPLETIONS], total=len(matches), has_more=len(matches) > MAX_COMPLETIONS)


def list_employee_ids() -> list[int]:
    with employees_db() as conn:
        rows = conn.cursor(row_factory=dict_row).execute("SELECT id FROM employees ORDER BY id").fetchall()
    return [row["id"] for row in rows]


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
    parser = argparse.ArgumentParser(description="Run the learning MCP server.")
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over streamable HTTP at http://127.0.0.1:<port>/mcp instead of stdio.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for --http (default 8000).")
    cli = parser.parse_args()

    if cli.http:
        # Bound to localhost on purpose: the SDK then turns on DNS-rebinding
        # protection (it checks the Host and Origin headers). This server has
        # no authentication, so it must not listen on other interfaces.
        logger.info("Starting learning-server (streamable HTTP) at http://127.0.0.1:%d/mcp", cli.port)
        mcp.run("streamable-http", host="127.0.0.1", port=cli.port)
    else:
        logger.info("Starting learning-server (stdio transport)")
        mcp.run()
