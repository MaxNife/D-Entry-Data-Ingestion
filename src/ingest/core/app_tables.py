"""The app's own tables: accounts, sessions, tokens, the audit log, settings, stored
connections and bookkeeping. Nobody reads them through the workspace, admins included:
they are hidden from the SQL editor, the table lists and connection browsing, queries
that name them are refused, and on PostgreSQL the editor runs as ``ingest_reader``,
which has no grants on them (migration 0004), so the database refuses them too.

``batches`` and ``rejected_records`` are platform data, not app tables: users query
run history and rejections. A migration that adds an app table must list it here and
revoke ``ingest_reader``'s default SELECT on it.
"""

import re
from pathlib import Path

from sqlalchemy.engine import Engine

APP_TABLES = frozenset(
    {
        "alembic_version",
        "connections",
        "users",
        "user_sessions",
        "api_tokens",
        "workspace_settings",
        "audit_events",
        "batch_chunks",
    }
)
READER_ROLE = "ingest_reader"

_NAMED = re.compile(r"\b(" + "|".join(sorted(APP_TABLES)) + r")\b", re.IGNORECASE)
_LOCAL = {"localhost", "127.0.0.1", "::1", ""}


def named_in(sql: str) -> str | None:
    """The first app table a statement names, if any."""
    m = _NAMED.search(sql or "")
    return m.group(1).lower() if m else None


def refuse(sql: str) -> None:
    name = named_in(sql)
    if name:
        raise ValueError(
            f"“{name}” is one of the platform's own tables and cannot be queried. "
            "Query your loaded tables, batches or rejected_records instead."
        )


def same_database(a: Engine, b: Engine) -> bool:
    """Whether two engines reach the same database (a connection registered to the
    app's own database must not become a way round the rules above)."""
    ua, ub = a.url, b.url
    if ua.get_backend_name() != ub.get_backend_name():
        return False
    if ua.get_backend_name() == "sqlite":
        da, db = ua.database or "", ub.database or ""
        if not da or not db or ":memory:" in (da, db):
            return False
        return Path(da).resolve() == Path(db).resolve()

    def where(u):
        h = (u.host or "").lower()
        port = u.port or {"postgresql": 5432, "mssql": 1433}.get(u.get_backend_name(), 0)
        return ("localhost" if h in _LOCAL else h, port, (u.database or "").lower())

    return where(ua) == where(ub)
