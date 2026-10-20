"""Read side of the target tables: catalogue, preview, export.

Tables are only ever addressed by reflecting a name that is a known target table,
so no table or column name is ever interpolated into SQL.
"""

import re
from collections.abc import Iterator

from sqlalchemy import MetaData, String, Table, UniqueConstraint, cast, func, inspect, or_, select

from ingest.core.contracts import list_contracts, load_contract

SYSTEM_COLUMNS = ("batch_id", "row_key", "loaded_at")
_COLLATE = re.compile(r"\s+COLLATE\s+\S+", re.IGNORECASE)


def type_name(sql_type) -> str:
    """A column type for display: ``VARCHAR(36)``, not ``VARCHAR(36) COLLATE "SQL_Latin1…"``."""
    return _COLLATE.sub("", str(sql_type))


def table_key(table: str, connection: str | None) -> str:
    """Catalogue key: ``table`` in the app database, ``table@connection`` elsewhere."""
    return f"{table}@{connection}" if connection else table


def target_tables() -> dict[str, dict]:
    """key → {key, table, connection, contracts, versions}, from every contract version."""
    out: dict[str, dict] = {}
    for name, version in list_contracts():
        try:
            c = load_contract(f"{name}@v{version}")
        except (FileNotFoundError, ValueError):
            continue
        key = table_key(c.target_table, c.connection)
        entry = out.setdefault(
            key,
            {
                "key": key,
                "table": c.target_table,
                "connection": c.connection,
                "contracts": [],
                "versions": [],
            },
        )
        if name not in entry["contracts"]:
            entry["contracts"].append(name)
        entry["versions"].append(f"{name}@v{version}")
    return dict(sorted(out.items()))


def exists(engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def reflect(engine, table: str) -> Table:
    return Table(table, MetaData(), autoload_with=engine)


def stats(engine, table: str) -> dict:
    t = reflect(engine, table)
    cols = [c.name for c in t.columns]
    q = [func.count()]
    if "loaded_at" in cols:
        q.append(func.max(t.c.loaded_at))
    if "batch_id" in cols:
        q.append(func.count(func.distinct(t.c.batch_id)))
    with engine.connect() as conn:
        row = conn.execute(select(*q).select_from(t)).one()
    return {
        "rows": int(row[0]),
        "last_loaded": row[1] if "loaded_at" in cols else None,
        "batches": int(row[-1]) if "batch_id" in cols else None,
        "columns": len(cols),
    }


def _filtered(t: Table, *, batch_ids=None, q: str | None = None):
    stmt = select(t)
    if batch_ids and "batch_id" in t.c:
        stmt = stmt.where(t.c.batch_id.in_(list(batch_ids)))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(*(cast(c, String).ilike(like) for c in t.columns)))
    return stmt


def page(
    engine,
    table: str,
    *,
    page: int = 1,
    per_page: int = 50,
    batch_id: str | None = None,
    q: str | None = None,
    sort: str | None = None,
    desc: bool = False,
) -> dict:
    t = reflect(engine, table)
    stmt = _filtered(t, batch_ids=[batch_id] if batch_id else None, q=q)
    if sort and sort in t.c:
        stmt = stmt.order_by(t.c[sort].desc() if desc else t.c[sort])
    elif "loaded_at" in t.c:  # newest rows first by default
        stmt = stmt.order_by(t.c.loaded_at.desc())
    # The data first, the platform's bookkeeping columns last.
    cols = [c for c in t.columns if c.name not in SYSTEM_COLUMNS] + [
        c for c in t.columns if c.name in SYSTEM_COLUMNS
    ]
    stmt = stmt.with_only_columns(*cols)
    with engine.connect() as conn:
        total = conn.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ).scalar_one()
        rows = conn.execute(stmt.offset((page - 1) * per_page).limit(per_page)).all()
    return {
        "columns": [c.name for c in cols],
        "types": {c.name: type_name(c.type) for c in cols},
        "rows": [list(r) for r in rows],
        "total": int(total),
        "page": page,
        "per_page": per_page,
    }


def schema(engine, table: str) -> list[dict]:
    t = reflect(engine, table)
    unique = {
        col.name for u in t.constraints if isinstance(u, UniqueConstraint) for col in u.columns
    }
    return [
        {
            "name": c.name,
            "type": type_name(c.type),
            "nullable": c.nullable,
            "system": c.name in SYSTEM_COLUMNS,
            "unique": c.name in unique,
        }
        for c in t.columns
    ]


def iter_export(
    engine, table: str, *, batch_ids=None, include_system: bool = False, chunk: int = 5000
) -> tuple[list[str], Iterator[list]]:
    """Headers plus a row iterator over the (optionally batch-filtered) table."""
    t = reflect(engine, table)
    cols = [c for c in t.columns if include_system or c.name not in SYSTEM_COLUMNS]
    stmt = _filtered(t, batch_ids=batch_ids).with_only_columns(*cols)
    if "loaded_at" in t.c:
        stmt = stmt.order_by(t.c.loaded_at)

    def rows():
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(stmt)
            while batch := result.fetchmany(chunk):
                yield from (list(r) for r in batch)

    return [c.name for c in cols], rows()
