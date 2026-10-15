"""Chunk loading: upsert accepted rows + write rejections, one tx per chunk.
src/ingest/core/loader.py — spec §8."""

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


def ensure_target_table(engine, contract: Contract) -> Table:
    """Create the contract's target table if missing; return the real Table.

    The table may already exist (created by a migration, the test suite, or an
    operator) with its own column types — e.g. a ``uuid`` ``batch_id``. We build
    and create our own definition for the missing case, then reflect what is
    actually there so inserts bind against the true column types.
    """
    meta = MetaData()
    cols = [
        Column("batch_id", String(36), nullable=False),
        Column("row_key", Text, nullable=False, unique=True),
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
    return Table(contract.target_table, reflected, autoload_with=engine)


def row_key_for(record: dict, contract: Contract) -> str:
    keys = contract.key_columns
    if keys is None:
        vals = "|".join(
            "" if record.get(c.name) is None else str(record.get(c.name)) for c in contract.columns
        )
        return sha256_bytes(vals.encode("utf-8"))
    return "|".join("" if record.get(k) is None else str(record.get(k)) for k in keys)


def records_from_frame(contract: Contract, df: pd.DataFrame, batch_id: str) -> list[dict]:
    now = datetime.now(UTC)
    out = []
    for _, row in df.iterrows():
        rec = {c.name: _norm(row.get(c.name), c.type) for c in contract.columns}
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

    with engine.connect() as conn:
        for table, column in pairs:
            try:
                rows = conn.execute(text(f"SELECT DISTINCT {column} FROM {table}")).fetchall()
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
        for rn in range(row_start, row_end + 1):
            conn.execute(
                RejectedRecord.__table__.insert().values(
                    batch_id=batch_id,
                    row_number=rn,
                    reason_code="LOAD_ERROR",
                    reason_message=f"chunk failed at database level: {error}"[:2000],
                    column_name=None,
                    offending_value=None,
                    original_row=originals.get(rn, {}),
                )
            )
    return row_end - row_start + 1


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
) -> tuple[int, int]:
    """Load one chunk in its own transaction. Returns (accepted_rows, rejected_rows).

    rejected_rows counts distinct rejected *rows* (one per row_number), so
    accepted + rejected = total holds even when a row carries several reasons.
    On DB-level failure: roll back, mark chunk failed, record every row in the
    chunk as LOAD_ERROR in a separate transaction, and let the caller continue.
    """
    table = ensure_target_table(engine, contract)
    records = records_from_frame(contract, accepted, batch_id)

    try:
        with engine.begin() as conn:
            if records:
                if engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as pg_insert

                    stmt = pg_insert(table)
                else:
                    from sqlalchemy.dialects.sqlite import insert as lite_insert

                    stmt = lite_insert(table)
                if contract.on_conflict == "skip":
                    stmt = stmt.on_conflict_do_nothing(index_elements=["row_key"])
                else:
                    update = {
                        c.name: stmt.excluded[c.name] for c in table.c if c.name not in ("row_key",)
                    }
                    update["batch_id"] = stmt.excluded["batch_id"]
                    update["loaded_at"] = stmt.excluded["loaded_at"]
                    stmt = stmt.on_conflict_do_update(index_elements=["row_key"], set_=update)
                conn.execute(stmt, records)
            for r in rejections:
                conn.execute(
                    RejectedRecord.__table__.insert().values(
                        batch_id=batch_id,
                        row_number=r.row_number,
                        reason_code=r.reason_code,
                        reason_message=r.reason_message,
                        column_name=r.column_name,
                        offending_value=r.offending_value,
                        original_row=originals.get(r.row_number, {}),
                    )
                )
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
