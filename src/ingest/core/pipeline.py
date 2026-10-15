"""Orchestration: hash → idempotency → validate → load → finalize.
src/ingest/core/pipeline.py — spec §6. One function runs the whole flow."""

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ingest.config import settings
from ingest.core.contracts import Contract, load_contract
from ingest.core.hashing import sha256_file
from ingest.core.loader import clear_attempt, fetch_ref_values, load_chunk
from ingest.core.validation import validate_chunk
from ingest.db.models import Batch

EXCEL_MAX_BYTES = 50 * 1024 * 1024


def _utcnow():
    return datetime.now(UTC)


def store_upload(src: Path, sha: str, upload_dir: str | Path | None = None) -> Path:
    dest_dir = Path(upload_dir or settings.upload_dir) / sha
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if not dest.exists():
        shutil.copyfile(src, dest)
    return dest


def stored_path(batch: Batch, upload_dir: str | Path | None = None) -> Path:
    root = Path(upload_dir or settings.upload_dir)
    mates = (
        sorted((root / batch.file_sha256).glob("*")) if (root / batch.file_sha256).exists() else []
    )
    if mates:
        return mates[0]
    return root / batch.file_sha256 / batch.file_name


def get_batch(engine, batch_id: str) -> Batch | None:
    with Session(engine) as s:
        return s.execute(select(Batch).where(Batch.batch_id == batch_id)).scalars().one_or_none()


def _fail(engine, batch_id: str, reason: str) -> dict:
    with engine.begin() as conn:
        conn.execute(
            Batch.__table__.update()
            .where(Batch.batch_id == batch_id)
            .values(status="failed", failure_reason=reason[:2000], finished_at=_utcnow())
        )
    return {"batch_id": batch_id, "status": "failed", "skipped": False, "failure_reason": reason}


def _rename(df: pd.DataFrame, contract: Contract) -> pd.DataFrame:
    mapping = {c.source_name: c.name for c in contract.columns if c.source_name in df.columns}
    return df.rename(columns=mapping)


def _originals(df_source: pd.DataFrame, row_offset: int) -> dict[int, dict]:
    """row_number → full original row (original headers), values as read."""
    out = {}
    for i, (_, row) in enumerate(df_source.iterrows()):
        out[row_offset + i + 1] = {k: (None if pd.isna(v) else v) for k, v in row.items()}
    return out


def _chunk_frames(path: Path, contract: Contract, chunk_size: int):
    """Yield (df_internal, df_source) chunks. Raises ValueError on structural problems."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        if path.stat().st_size > EXCEL_MAX_BYTES:
            raise ValueError(
                f"Excel file exceeds 50MB limit ({path.stat().st_size} bytes); convert to CSV"
            )
        full = pd.read_excel(path, engine="openpyxl", dtype=str, keep_default_na=True)
        for start in range(0, len(full), chunk_size):
            src = full.iloc[start : start + chunk_size].reset_index(drop=True)
            yield _rename(src.copy(), contract), src
    else:
        reader = pd.read_csv(
            path,
            chunksize=chunk_size,
            dtype=str,
            keep_default_na=True,
            encoding=contract.encoding,
            delimiter=contract.delimiter,
        )
        for src in reader:
            src = src.reset_index(drop=True)
            yield _rename(src.copy(), contract), src


def _check_header(path: Path, contract: Contract) -> str | None:
    """Return failure reason if the header doesn't match, else None."""
    try:
        if path.suffix.lower() in (".xlsx", ".xls"):
            header = list(pd.read_excel(path, engine="openpyxl", nrows=0).columns)
        else:
            header = list(
                pd.read_csv(
                    path, nrows=0, encoding=contract.encoding, delimiter=contract.delimiter
                ).columns
            )
    except Exception as e:
        return f"cannot open file: {e}"
    missing = [c.source_name for c in contract.columns if c.source_name not in header]
    if missing:
        return f"missing required header(s): {', '.join(missing)}"
    return None


def _process(engine, batch_id: str, stored: Path, contract: Contract, chunk_size: int) -> dict:
    """Structural check → chunk loop → finalize. Shared by run_pipeline and execute_batch."""
    # --- structural check ---
    problem = _check_header(stored, contract)
    if problem:
        return _fail(engine, batch_id, problem)

    ref_values = fetch_ref_values(engine, contract)
    total = accepted = rejected = 0
    chunk_index = 0
    try:
        for internal, source in _chunk_frames(stored, contract, chunk_size):
            # drop extra file columns not in the contract
            keep = [c.name for c in contract.columns if c.name in internal.columns]
            internal = internal[keep]
            offset = total  # 0-based count of rows before this chunk
            result = validate_chunk(internal, contract, offset, ref_values)
            originals = _originals(source, offset)
            a, r = load_chunk(
                engine,
                batch_id=batch_id,
                chunk_index=chunk_index,
                row_start=offset + 1,
                row_end=offset + len(source),
                contract=contract,
                accepted=result.accepted,
                rejections=result.rejections,
                originals=originals,
            )
            accepted += a
            rejected += r
            total += len(source)
            chunk_index += 1
    except Exception as e:
        return _fail(engine, batch_id, f"pipeline error: {e}")

    status = "completed" if rejected == 0 else "partial"
    with engine.begin() as conn:
        conn.execute(
            Batch.__table__.update()
            .where(Batch.batch_id == batch_id)
            .values(
                total_rows=total,
                accepted_rows=accepted,
                rejected_rows=rejected,
                status=status,
                finished_at=_utcnow(),
            )
        )
    return {
        "batch_id": batch_id,
        "status": status,
        "skipped": False,
        "total_rows": total,
        "accepted_rows": accepted,
        "rejected_rows": rejected,
    }


def submit_upload(
    file_path: str | Path,
    contract_ref: str,
    *,
    engine=None,
) -> dict:
    """Hash + store + idempotency check + create pending batch. Returns immediately.

    Used by the API: the response goes out with the new batch_id while the
    heavy work runs in a background task via execute_batch.
    """
    from ingest.db.session import get_engine

    engine = engine or get_engine()
    src = Path(file_path)
    contract: Contract = load_contract(contract_ref)

    sha = sha256_file(src)
    store_upload(src, sha)

    with Session(engine) as s:
        existing = (
            s.execute(
                select(Batch).where(
                    Batch.file_sha256 == sha,
                    Batch.contract_name == contract.name,
                    Batch.contract_version == contract.version,
                )
            )
            .scalars()
            .one_or_none()
        )
    if existing is not None and existing.status in ("completed", "partial"):
        return {
            "batch_id": existing.batch_id,
            "status": existing.status,
            "skipped": True,
            "total_rows": existing.total_rows,
            "accepted_rows": existing.accepted_rows,
            "rejected_rows": existing.rejected_rows,
        }
    batch_id = str(uuid.uuid4())
    try:
        with engine.begin() as conn:
            conn.execute(
                Batch.__table__.insert().values(
                    batch_id=batch_id,
                    file_name=src.name,
                    file_sha256=sha,
                    contract_name=contract.name,
                    contract_version=contract.version,
                    contract_hash=contract.hash,
                    status="pending",
                )
            )
    except IntegrityError:
        # Lost a concurrent-upload race → return the winner as skipped.
        with Session(engine) as s:
            winner = (
                s.execute(
                    select(Batch).where(
                        Batch.file_sha256 == sha,
                        Batch.contract_name == contract.name,
                        Batch.contract_version == contract.version,
                    )
                )
                .scalars()
                .one()
            )
        return {
            "batch_id": winner.batch_id,
            "status": winner.status,
            "skipped": True,
            "total_rows": winner.total_rows,
            "accepted_rows": winner.accepted_rows,
            "rejected_rows": winner.rejected_rows,
        }
    return {"batch_id": batch_id, "status": "pending", "skipped": False}


def execute_batch(engine, batch_id: str, chunk_size: int | None = None) -> dict:
    """Run the heavy work for a submitted batch (background-task entry point)."""
    chunk_size = chunk_size or settings.chunk_size
    batch = get_batch(engine, batch_id)
    if batch is None:
        raise ValueError(f"unknown batch {batch_id}")
    contract = load_contract(f"{batch.contract_name}@v{batch.contract_version}")
    stored = stored_path(batch)
    if not stored.exists():
        return _fail(engine, batch_id, f"stored raw file missing: {stored}")
    with engine.begin() as conn:
        conn.execute(
            Batch.__table__.update()
            .where(Batch.batch_id == batch_id)
            .values(status="processing", started_at=_utcnow(), finished_at=None)
        )
    return _process(engine, batch_id, stored, contract, chunk_size)


def run_pipeline(
    file_path: str | Path,
    contract_ref: str,
    *,
    reprocess_batch_id: str | None = None,
    engine=None,
    chunk_size: int | None = None,
) -> dict:
    """Run the whole flow synchronously (CLI entry point)."""
    from ingest.db.session import get_engine

    engine = engine or get_engine()
    chunk_size = chunk_size or settings.chunk_size
    contract: Contract = load_contract(contract_ref)

    # --- reprocess path ---
    if reprocess_batch_id:
        batch = get_batch(engine, reprocess_batch_id)
        if batch is None:
            raise ValueError(f"unknown batch {reprocess_batch_id}")
        if batch.status not in ("partial", "failed"):
            raise ValueError(f"only partial/failed batches can be reprocessed (got {batch.status})")
        with engine.begin() as conn:
            conn.execute(
                Batch.__table__.update()
                .where(Batch.batch_id == batch.batch_id)
                .values(
                    status="processing",
                    contract_name=contract.name,
                    contract_version=contract.version,
                    contract_hash=contract.hash,
                    failure_reason=None,
                    started_at=_utcnow(),
                    finished_at=None,
                    retry_attempts=(batch.retry_attempts + 1),
                )
            )
        clear_attempt(engine, batch.batch_id)
        batch_id = batch.batch_id
        stored = stored_path(batch)
        if not stored.exists():
            return _fail(engine, batch_id, f"stored raw file missing: {stored}")
        return _process(engine, batch_id, stored, contract, chunk_size)

    src = Path(file_path)
    submitted = submit_upload(src, contract_ref, engine=engine)
    if submitted["skipped"]:
        return submitted
    return execute_batch(engine, submitted["batch_id"], chunk_size)
