"""External databases: register, test, and use as sources or destinations.

A *source* is read by snapshotting a table (or a read-only query) to CSV; the snapshot
then goes through the normal file pipeline, so validation, idempotency (same data →
same SHA-256 → skipped) and rejections work exactly as for an upload.

A *destination* is named by a contract's ``target.connection``: accepted rows load
into that database, while batches, chunks and rejections stay in the app database.
"""

import csv
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from ingest.core import query, secrets, tables
from ingest.db.models import Connection

KINDS = {
    "postgresql": {"label": "PostgreSQL", "driver": "postgresql+psycopg", "port": 5432},
    "mssql": {"label": "SQL Server", "driver": "mssql+pymssql", "port": 1433},
    "sqlite": {"label": "SQLite", "driver": "sqlite", "port": None},
}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
CONNECT_TIMEOUT_S = 8
_engines: dict[str, tuple[datetime, Engine]] = {}


def driver_available(kind: str) -> bool:
    module = {"postgresql": "psycopg", "mssql": "pymssql", "sqlite": "sqlite3"}.get(kind)
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def url_for(c: Connection, password: str | None = None) -> URL:
    if c.kind == "sqlite":
        return URL.create("sqlite", database=c.database)
    return URL.create(
        KINDS[c.kind]["driver"],
        username=c.username or None,
        password=password,
        host=c.host,
        port=c.port,
        database=c.database or None,
    )


def make_engine(c: Connection) -> Engine:
    if c.kind not in KINDS:
        raise ValueError(f"unknown connection kind {c.kind!r}")
    if not driver_available(c.kind):
        extra = " Install it with: uv sync --extra sqlserver" if c.kind == "mssql" else ""
        raise ValueError(f"The {KINDS[c.kind]['label']} driver is not installed.{extra}")
    password = secrets.decrypt(c.password_enc) if c.password_enc else None
    args = {
        "postgresql": {"connect_timeout": CONNECT_TIMEOUT_S},
        "mssql": {"login_timeout": CONNECT_TIMEOUT_S, "timeout": 120},
        "sqlite": {},
    }[c.kind]
    return create_engine(url_for(c, password), pool_pre_ping=True, connect_args=args)


# ---- registry -------------------------------------------------------------------------


def list_all(app_engine) -> list[Connection]:
    with Session(app_engine) as s:
        return list(s.execute(select(Connection).order_by(Connection.name)).scalars())


def get(app_engine, name: str) -> Connection | None:
    with Session(app_engine) as s:
        return s.get(Connection, name)


def engine_for(app_engine, name: str | None) -> Engine:
    """The engine for connection ``name``; the app engine itself when ``name`` is empty."""
    if not name:
        return app_engine
    c = get(app_engine, name)
    if c is None:
        raise ValueError(f"unknown connection {name!r}")
    cached = _engines.get(name)
    if cached and cached[0] == c.updated_at:
        return cached[1]
    if cached:
        cached[1].dispose()
    engine = make_engine(c)
    _engines[name] = (c.updated_at, engine)
    return engine


def target_engine(app_engine, contract) -> Engine:
    """Where a contract's accepted rows (and its exists_in reference tables) live."""
    return engine_for(app_engine, getattr(contract, "connection", None))


def check(data: dict) -> list[str]:
    problems = []
    if not NAME_RE.match(str(data.get("name") or "")):
        problems.append(
            "Name must start with a letter and use lowercase letters, digits and underscores."
        )
    kind = data.get("kind")
    if kind not in KINDS:
        problems.append("Choose PostgreSQL, SQL Server or SQLite.")
    elif kind == "sqlite":
        if not data.get("database"):
            problems.append("Give the path of the SQLite file.")
    else:
        if not data.get("host"):
            problems.append("Give the server's host name.")
        if not data.get("database"):
            problems.append("Give the database name.")
    if data.get("port") not in (None, ""):
        try:
            if not 0 < int(data["port"]) < 65536:
                raise ValueError
        except ValueError:
            problems.append("Port must be a number between 1 and 65535.")
    return problems


def build(data: dict, existing: Connection | None = None) -> Connection:
    """A Connection from form data. A blank password keeps the stored one."""
    problems = check(data)
    if problems:
        raise ValueError(" ".join(problems))
    kind = data["kind"]
    c = Connection(
        name=data["name"],
        kind=kind,
        host=(data.get("host") or None) if kind != "sqlite" else None,
        port=int(data["port"])
        if data.get("port") not in (None, "") and kind != "sqlite"
        else KINDS[kind]["port"],
        database=data.get("database") or None,
        username=(data.get("username") or None) if kind != "sqlite" else None,
        options={},
    )
    password = data.get("password") or ""
    if password:
        c.password_enc = secrets.encrypt(password)
    elif existing is not None and kind != "sqlite":
        c.password_enc = existing.password_enc
    return c


def save(app_engine, c: Connection, *, replace: str | None = None) -> Connection:
    now, name = datetime.now(UTC), c.name  # c expires (and detaches) when the session commits
    with Session(app_engine) as s, s.begin():
        old = s.get(Connection, replace) if replace else None
        if old is None and s.get(Connection, c.name) is not None:
            raise ValueError(f"A connection named “{c.name}” already exists.")
        created = old.created_at if old else now
        if old is not None:
            s.delete(old)
            s.flush()
        c.created_at, c.updated_at = created, now
        s.add(c)
    return get(app_engine, name)


def delete(app_engine, name: str) -> None:
    with Session(app_engine) as s, s.begin():
        c = s.get(Connection, name)
        if c is not None:
            s.delete(c)
    cached = _engines.pop(name, None)
    if cached:
        cached[1].dispose()


def test(c: Connection) -> tuple[bool, str]:
    """Connect and run a trivial query. Returns (ok, human message)."""
    try:
        engine = make_engine(c)
    except ValueError as e:
        return False, str(e)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            version = ".".join(str(p) for p in (engine.dialect.server_version_info or ())[:3])
            n_tables = len(inspect(conn).get_table_names())
        label = KINDS[c.kind]["label"]
        return True, f"Connected to {label}{(' ' + version) if version else ''} · {n_tables} tables"
    except Exception as e:
        # psycopg repeats the same failure once per resolved address; the first says it all.
        message = " ".join(str(getattr(e, "orig", None) or e).split())
        return False, message.split(" Multiple connection attempts failed")[0][:400]
    finally:
        engine.dispose()


def record_test(app_engine, name: str, ok: bool, message: str) -> None:
    with Session(app_engine) as s, s.begin():
        c = s.get(Connection, name)
        if c is not None:
            c.last_tested_at, c.last_test_ok, c.last_test_message = datetime.now(UTC), ok, message


def browse(engine: Engine) -> list[dict]:
    insp = inspect(engine)
    return [
        {
            "name": t,
            "columns": [(c["name"], tables.type_name(c["type"])) for c in insp.get_columns(t)],
        }
        for t in sorted(insp.get_table_names())
    ]


# ---- snapshots (sources) ---------------------------------------------------------------


def _text(v) -> str:
    """The canonical text a contract check sees, matching what a CSV export would hold."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, bytes | bytearray | memoryview):
        return bytes(v).hex()
    return str(v)


def snapshot(
    engine: Engine,
    dest: Path,
    *,
    table: str | None = None,
    sql: str | None = None,
    chunk: int = 10_000,
) -> int:
    """Write a source table (or a read-only query) to ``dest`` as CSV. Returns the row count."""
    if bool(table) == bool(sql):
        raise ValueError("Choose a table or write a query.")
    if table:
        if table not in inspect(engine).get_table_names():
            raise ValueError(f"There is no table {table!r} in this database.")
        stmt = select(Table(table, MetaData(), autoload_with=engine))
    else:
        stmt = text(query.single_statement(sql))
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with (
        engine.connect() as conn,
        query.read_only(conn, timeout_s=600),
        dest.open("w", newline="", encoding="utf-8") as fp,
    ):
        result = conn.execution_options(stream_results=True).execute(stmt)
        w = csv.writer(fp)
        w.writerow(list(result.keys()))
        while rows := result.fetchmany(chunk):
            w.writerows([_text(v) for v in r] for r in rows)
            n += len(rows)
    return n
