"""Chunk loading: upsert accepted rows + write rejections, one tx per chunk.
src/ingest/core/loader.py — spec §8."""

import json
from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    select,
)

from ingest.core.contracts import Contract
from ingest.core.hashing import sha256_bytes
from ingest.db.models import TARGET_TYPE_MAP, BatchChunk, RejectedRecord


def _norm(v, col_type: str = "str"):
    """Coerce a validated cell to a Python value the DB column can bind.

    Files are read as text; without this, a numeric string bound to a bigint
    or date column reaches Postgres as text and fails the assignment cast.
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.date() if col_type == "date" else v.to_pydatetime()
    if col_type == "int":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    if col_type in ("float", "decimal"):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if col_type == "bool":
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "t", "y")
        return bool(v)
    if col_type in ("date", "datetime"):
        try:
            ts = pd.to_datetime(v)
        except (TypeError, ValueError):
            return None
        if pd.isna(ts):
            return None
        return ts.date() if col_type == "date" else ts.to_pydatetime()
    if hasattr(v, "item"):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    return v


#: Reflected target tables, keyed by (database URL, table name). Reflection is
#: several catalog round-trips; the table shape does not change mid-process, so
#: doing it once per table instead of once per chunk matters on wide files.
_TABLE_CACHE: dict[tuple[str, str], Table] = {}


def ensure_target_table(engine, contract: Contract) -> Table:
    """Create the contract's target table if missing; return the real Table.

    The table may already exist (created by a migration, the test suite, or an
    operator) with its own column types — e.g. a ``uuid`` ``batch_id``. We build
    and create our own definition for the missing case, then reflect what is
    actually there so inserts bind against the true column types.
    """
    key = (str(engine.url), contract.target_table)
    cached = _TABLE_CACHE.get(key)
    if cached is not None:
        return cached
    meta = MetaData()
    # SQL Server cannot index (n)varchar(max); 450 characters is its unique-index ceiling.
    key_type = Text if engine.dialect.name in ("postgresql", "sqlite") else String(450)
    cols = [
        Column("batch_id", String(36), nullable=False),
        Column("row_key", key_type, nullable=False, unique=True),
        Column("loaded_at", DateTime(timezone=True), default=lambda: datetime.now(UTC)),
    ]
    for c in contract.columns:
        sa_type = TARGET_TYPE_MAP[c.type]()
        cols.append(Column(c.name, sa_type, nullable=True))
    table = Table(
        contract.target_table,
        meta,
        *cols,
        UniqueConstraint("row_key", name=f"uq_{contract.target_table}_row_key"),
        extend_existing=True,
    )
    meta.create_all(bind=engine, tables=[table])
    reflected = MetaData()
    result = Table(contract.target_table, reflected, autoload_with=engine)
    _TABLE_CACHE[key] = result
    return result


def row_key_for(record: dict, contract: Contract) -> str:
    keys = contract.key_columns
    if keys is None:
        vals = "|".join(
            "" if record.get(c.name) is None else str(record.get(c.name)) for c in contract.columns
        )
        return sha256_bytes(vals.encode("utf-8"))
    return "|".join("" if record.get(k) is None else str(record.get(k)) for k in keys)


def _normalized_column(series: pd.Series, col_type: str) -> list:
    """``_norm`` over a column, computed once per distinct value."""
    cache: dict = {}
    out = []
    for v in series.tolist():
        if v not in cache:
            cache[v] = _norm(v, col_type)
        out.append(cache[v])
    return out


def records_from_frame(contract: Contract, df: pd.DataFrame, batch_id: str) -> list[dict]:
    now = datetime.now(UTC)
    names = [c.name for c in contract.columns]
    columns = [
        _normalized_column(df[c.name], c.type) if c.name in df.columns else [None] * len(df)
        for c in contract.columns
    ]
    out = []
    for values in zip(*columns, strict=True):
        rec = dict(zip(names, values, strict=True))
        rec["row_key"] = row_key_for(rec, contract)
        rec["batch_id"] = batch_id
        rec["loaded_at"] = now
        out.append(rec)
    return out


def fetch_ref_values(engine, contract: Contract) -> dict[tuple[str, str], set]:
    """One bulk lookup per distinct (table, column) used by exists_in rules."""
    out: dict[tuple[str, str], set] = {}
    pairs = {
        (r.reference["table"], r.reference["column"])
        for r in contract.rules
        if r.check == "exists_in" and r.reference
    }
    if not pairs:
        return out
    from sqlalchemy import text

    quote = engine.dialect.identifier_preparer.quote
    with engine.connect() as conn:
        for table, column in pairs:
            table_sql = ".".join(quote(part) for part in table.split("."))  # schema.table
            try:
                rows = conn.execute(
                    text(f"SELECT DISTINCT {quote(column)} FROM {table_sql}")
                ).fetchall()
            except Exception:
                continue  # reference table missing → validation skips the rule
            out[(table, column)] = {r[0] for r in rows}
    return out


def record_chunk_failure(
    engine,
    *,
    batch_id: str,
    chunk_index: int,
    row_start: int,
    row_end: int,
    originals: dict[int, dict],
    error: BaseException | str,
) -> int:
    """Mark one chunk failed and record every row in it as LOAD_ERROR.

    Runs in its own transaction, separate from the failed chunk write, so the
    pipeline can continue with the next chunk. Returns the rejected row count.
    """
    with engine.begin() as conn:
        conn.execute(
            delete(BatchChunk).where(
                BatchChunk.batch_id == batch_id, BatchChunk.chunk_index == chunk_index
            )
        )
        conn.execute(
            BatchChunk.__table__.insert().values(
                batch_id=batch_id,
                chunk_index=chunk_index,
                row_start=row_start,
                row_end=row_end,
                status="failed",
                error=str(error)[:2000],
            )
        )
        _insert_many(
            conn,
            RejectedRecord.__table__,
            [
                {
                    "batch_id": batch_id,
                    "row_number": rn,
                    "reason_code": "LOAD_ERROR",
                    "reason_message": f"chunk failed at database level: {error}"[:2000],
                    "column_name": None,
                    "offending_value": None,
                    "original_row": originals.get(rn, {}),
                }
                for rn in range(row_start, row_end + 1)
            ],
        )
    return row_end - row_start + 1


def _copy_rows(conn, table_sql: str, columns: list[str], rows: list[dict]) -> None:
    """Stream dict rows into a Postgres table with COPY, inside ``conn``'s transaction."""
    quote = conn.dialect.identifier_preparer.quote
    col_sql = ", ".join(quote(c) for c in columns)
    with (
        conn.connection.driver_connection.cursor() as cur,
        cur.copy(f"COPY {table_sql} ({col_sql}) FROM STDIN") as copy,
    ):
        for row in rows:
            copy.write_row(
                [json.dumps(v) if isinstance(v, dict) else v for v in map(row.get, columns)]
            )


def _insert_many(conn, table, rows: list[dict]) -> None:
    """Append rows: COPY on Postgres, one executemany elsewhere."""
    if not rows:
        return
    if conn.dialect.name == "postgresql":
        # COPY bypasses SQLAlchemy, so apply the model's Python-side defaults here.
        defaults = {
            c.name: c.default.arg(None) if c.default.is_callable else c.default.arg
            for c in table.c
            if c.name not in rows[0]
            and c.default is not None
            and (c.default.is_scalar or c.default.is_callable)
        }
        if defaults:
            rows = [{**defaults, **r} for r in rows]
        table_sql = conn.dialect.identifier_preparer.format_table(table)
        _copy_rows(conn, table_sql, [c for c in rows[0] if c in table.c], rows)
    else:
        conn.execute(table.insert(), rows)


def _upsert(conn, table: Table, contract: Contract, records: list[dict]) -> None:
    """Insert accepted rows, resolving ``row_key`` conflicts per ``on_conflict``.

    On Postgres the rows are COPY'd into a transaction-scoped staging table and
    merged with one ``INSERT ... SELECT ... ON CONFLICT``. That is one round
    trip per chunk instead of one per row, and the target's triggers and
    constraints still fire on the real insert.
    """
    columns = [c for c in records[0] if c in table.c]
    if conn.dialect.name not in ("postgresql", "sqlite"):
        _upsert_portable(conn, table, contract, records)
        return
    if conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as lite_insert

        stmt = lite_insert(table)
        if contract.on_conflict == "skip":
            stmt = stmt.on_conflict_do_nothing(index_elements=["row_key"])
        else:
            update = {c.name: stmt.excluded[c.name] for c in table.c if c.name != "row_key"}
            stmt = stmt.on_conflict_do_update(index_elements=["row_key"], set_=update)
        conn.execute(stmt, records)
        return

    quote = conn.dialect.identifier_preparer.quote
    target = conn.dialect.identifier_preparer.format_table(table)
    col_sql = ", ".join(quote(c) for c in columns)
    conn.exec_driver_sql(
        f"CREATE TEMP TABLE _ingest_stage ON COMMIT DROP AS "
        f"SELECT {col_sql} FROM {target} WITH NO DATA"
    )
    _copy_rows(conn, "_ingest_stage", columns, records)
    if contract.on_conflict == "skip":
        action = "DO NOTHING"
    else:
        sets = ", ".join(f"{quote(c)} = EXCLUDED.{quote(c)}" for c in columns if c != "row_key")
        action = f"DO UPDATE SET {sets}"
    conn.exec_driver_sql(
        f"INSERT INTO {target} ({col_sql}) SELECT {col_sql} FROM _ingest_stage "
        f"ON CONFLICT (row_key) {action}"
    )


def _upsert_portable(conn, table: Table, contract: Contract, records: list[dict]) -> None:
    """Upsert for databases without ``ON CONFLICT`` (SQL Server): within the chunk's
    transaction, replace (update) or keep (skip) rows whose ``row_key`` already exists.
    Keys go in batches under SQL Server's 2,100-parameter limit."""
    keys = [r["row_key"] for r in records]
    existing: set = set()
    for i in range(0, len(keys), 1000):
        part = keys[i : i + 1000]
        if contract.on_conflict == "skip":
            existing |= set(
                conn.execute(select(table.c.row_key).where(table.c.row_key.in_(part))).scalars()
            )
        else:
            conn.execute(table.delete().where(table.c.row_key.in_(part)))
    rows = [r for r in records if r["row_key"] not in existing]
    if rows:
        conn.execute(table.insert(), [{k: v for k, v in r.items() if k in table.c} for r in rows])


def load_chunk(
    engine,
    *,
    batch_id: str,
    chunk_index: int,
    row_start: int,
    row_end: int,
    contract: Contract,
    accepted: pd.DataFrame,
    rejections,
    originals: dict[int, dict],
    target_engine=None,
) -> tuple[int, int]:
    """Load one chunk in its own transaction. Returns (accepted_rows, rejected_rows).

    rejected_rows counts distinct rejected *rows* (one per row_number), so
    accepted + rejected = total holds even when a row carries several reasons.
    On DB-level failure: roll back, mark chunk failed, record every row in the
    chunk as LOAD_ERROR in a separate transaction, and let the caller continue.
    """
    target_engine = target_engine or engine
    table = ensure_target_table(target_engine, contract)
    records = records_from_frame(contract, accepted, batch_id)
    rejection_rows = [
        {
            "batch_id": batch_id,
            "row_number": r.row_number,
            "reason_code": r.reason_code,
            "reason_message": r.reason_message,
            "column_name": r.column_name,
            "offending_value": r.offending_value,
            "original_row": originals.get(r.row_number, {}),
        }
        for r in rejections
    ]

    def audit(conn) -> None:
        if rejection_rows:
            _insert_many(conn, RejectedRecord.__table__, rejection_rows)
        conn.execute(
            BatchChunk.__table__.insert().values(
                batch_id=batch_id,
                chunk_index=chunk_index,
                row_start=row_start,
                row_end=row_end,
                status="committed",
                error=None,
            )
        )

    try:
        if target_engine is engine:  # one database: rows and audit trail commit together
            with engine.begin() as conn:
                if records:
                    _upsert(conn, table, contract, records)
                audit(conn)
        else:
            # A destination database: two transactions, target rows first. The upsert is
            # keyed on row_key, so replaying a chunk (reprocess) never duplicates rows.
            if records:
                with target_engine.begin() as conn:
                    _upsert(conn, table, contract, records)
            with engine.begin() as conn:
                audit(conn)
        return len(records), len({r.row_number for r in rejections})
    except Exception as e:  # DB-level chunk failure → LOAD_ERROR path
        return 0, record_chunk_failure(
            engine,
            batch_id=batch_id,
            chunk_index=chunk_index,
            row_start=row_start,
            row_end=row_end,
            originals=originals,
            error=e,
        )


def clear_attempt(engine, batch_id: str) -> None:
    """Delete old rejections + chunk rows before a reprocess (spec §8.3)."""
    with engine.begin() as conn:
        conn.execute(delete(RejectedRecord).where(RejectedRecord.batch_id == batch_id))
        conn.execute(delete(BatchChunk).where(BatchChunk.batch_id == batch_id))


def chunk_statuses(engine, batch_id: str) -> list[BatchChunk]:
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        return list(
            s.execute(
                select(BatchChunk)
                .where(BatchChunk.batch_id == batch_id)
                .order_by(BatchChunk.chunk_index)
            ).scalars()
        )
