"""Read-only ad-hoc SQL over the app database (and, later, registered connections).

Defence in depth: the statement must be a single read statement (checked with a
quote- and comment-aware scan), and it runs inside a transaction the database
itself treats as read-only (Postgres ``SET TRANSACTION READ ONLY``, SQLite
``PRAGMA query_only``) that is always rolled back. SQL Server has no read-only
transaction, so connect it with a read-only login.

A read-only transaction does not stop a *superuser* calling server functions such
as ``pg_read_file``. On the app database, queries run as a role with only ``SELECT``
grants (``SET LOCAL ROLE``): ``INGEST_QUERY_ROLE`` if set, otherwise
``ingest_reader`` (created by migration 0004, without access to the app's own tables).
"""

import os
import re
import time
from contextlib import contextmanager

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
READ_START = re.compile(r"^\s*(select|with|values|table|explain|show)\b", re.IGNORECASE)
MAX_ROWS = 1000
TIMEOUT_S = 15


def single_statement(sql: str) -> str:
    """Return the statement without trailing semicolons; reject a second statement."""
    out, i, n = [], 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:  # escaped '' or ""
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
            continue
        elif ch == ";":
            if sql[i + 1 :].strip(" \t\r\n;"):
                raise ValueError("Run one statement at a time.")
            break
        else:
            out.append(ch)
        i += 1
    if quote:
        raise ValueError("The statement has an unclosed quote.")
    stmt = "".join(out).strip()
    if not stmt:
        raise ValueError("Write a query to run.")
    if not READ_START.match(stmt):
        raise ValueError(
            "Only read queries are allowed (SELECT, WITH, VALUES, TABLE, EXPLAIN, SHOW)."
        )
    return stmt


def reader_role(conn) -> str:
    """The role app-database queries run as, or "" when none is available."""
    if conn.dialect.name != "postgresql":
        return ""
    role = os.environ.get("INGEST_QUERY_ROLE", "")
    if role:
        if not IDENT.match(role):
            raise ValueError("INGEST_QUERY_ROLE must be a plain role name.")
        return role
    member = conn.exec_driver_sql(
        "SELECT 1 FROM pg_roles WHERE rolname = 'ingest_reader'"
        " AND pg_has_role(current_user, oid, 'MEMBER')"
    ).scalar()
    return "ingest_reader" if member else ""


@contextmanager
def read_only(conn, *, timeout_s: int = TIMEOUT_S, app_db: bool = False):
    """A transaction the database itself treats as read-only; always rolled back.

    ``app_db``: the query is against the app's own database, so it runs as the reader
    role, which cannot see the app's tables.
    """
    dialect = conn.dialect.name
    trans = conn.begin()
    try:
        if dialect == "postgresql":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_s * 1000)}")
            role = reader_role(conn) if app_db else ""
            if role:
                conn.exec_driver_sql(f'SET LOCAL ROLE "{role}"')
        elif dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = ON")
        yield
    finally:
        trans.rollback()
        if dialect == "sqlite":
            conn.exec_driver_sql("PRAGMA query_only = OFF")


def run_query(
    engine,
    sql: str,
    *,
    limit: int = MAX_ROWS,
    timeout_s: int = TIMEOUT_S,
    app_db: bool = False,
) -> dict:
    """Execute a read query. Returns columns, rows (≤ limit), truncated flag and timing."""
    stmt = single_statement(sql)
    started = time.perf_counter()
    with engine.connect() as conn, read_only(conn, timeout_s=timeout_s, app_db=app_db):
        # The raw cursor with no parameters: the driver does no placeholder processing,
        # so a literal % (LIKE 'a%') reaches the database untouched.
        cur = conn.connection.dbapi_connection.cursor()
        try:
            cur.execute(stmt)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchmany(limit + 1)] if cur.description else []
        finally:
            cur.close()
    return {
        "columns": columns,
        "rows": rows[:limit],
        "truncated": len(rows) > limit,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "sql": stmt,
    }
