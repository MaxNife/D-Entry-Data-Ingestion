"""FastAPI app: JSON under /api + Jinja2/HTMX dashboard. Spec sections 9-10."""

import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ingest.core.contracts import list_contracts, load_contract
from ingest.core.pipeline import execute_batch, get_batch, run_pipeline, stored_path, submit_upload
from ingest.db.models import Batch, RejectedRecord

HERE = Path(__file__).parent


def err(code: str, detail: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status_code=status)


def batch_out(b: Batch) -> dict:
    return {
        "batch_id": b.batch_id,
        "file_name": b.file_name,
        "file_sha256": b.file_sha256,
        "contract_name": b.contract_name,
        "contract_version": b.contract_version,
        "contract_ref": f"{b.contract_name}@v{b.contract_version}",
        "contract_hash": b.contract_hash,
        "status": b.status,
        "failure_reason": b.failure_reason,
        "total_rows": b.total_rows,
        "accepted_rows": b.accepted_rows,
        "rejected_rows": b.rejected_rows,
        "retry_attempts": b.retry_attempts,
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "started_at": b.started_at.isoformat() if b.started_at else None,
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
    }


def rejection_out(r: RejectedRecord) -> dict:
    return {
        "id": r.id,
        "batch_id": r.batch_id,
        "row_number": r.row_number,
        "reason_code": r.reason_code,
        "reason_message": r.reason_message,
        "column_name": r.column_name,
        "offending_value": r.offending_value,
        "original_row": r.original_row,
    }


def paged(items: list, total: int, page: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def default_contract() -> str:
    names = sorted({n for n, _ in list_contracts()})
    if len(names) == 1:
        return names[0]
    raise HTTPException(400, "contract is required (multiple contracts available)")


def create_app(engine=None) -> FastAPI:
    from ingest.db.session import get_engine

    eng = engine or get_engine()
    app = FastAPI(title="Ingest Platform")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError):
        return err("VALIDATION_ERROR", str(exc)[:1000], 422)

    def sess() -> Session:
        return Session(eng)

    @app.get("/health")
    def health():
        try:
            with sess() as s:
                s.execute(select(func.count()).select_from(Batch))
            db = "ok"
        except Exception as e:  # liveness must report, not raise
            return {"status": "degraded", "db": str(e)[:200]}
        return {"status": "ok", "db": db}

    # ---- batches ----
    @app.post("/api/batches", status_code=202)
    async def upload_batch(request: Request, background: BackgroundTasks):
        form = await request.form()
        upload = form.get("file")
        contract_ref = form.get("contract") or None
        if upload is None or not hasattr(upload, "filename"):
            return err("NO_FILE", "multipart field 'file' is required", 422)
        if not contract_ref:
            try:
                contract_ref = default_contract()
            except HTTPException:
                return err("CONTRACT_REQUIRED", "contract is required", 400)
        try:
            load_contract(contract_ref)
        except (FileNotFoundError, ValueError) as e:
            return err("INVALID_CONTRACT", str(e), 400)
        fd, tmp_path = tempfile.mkstemp(suffix=Path(upload.filename or "upload.csv").suffix)
        os.close(fd)
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        await upload.close()

        try:
            submitted = submit_upload(
                Path(tmp_path), contract_ref, engine=eng, file_name=upload.filename
            )
        except (FileNotFoundError, ValueError) as e:
            Path(tmp_path).unlink(missing_ok=True)
            return err("INVALID_CONTRACT", str(e), 400)
        Path(tmp_path).unlink(missing_ok=True)  # stored copy lives under data/uploads
        if submitted["skipped"]:
            return JSONResponse(
                {
                    "batch_id": submitted["batch_id"],
                    "status": submitted["status"],
                    "skipped": True,
                },
                status_code=202,
            )
        background.add_task(execute_batch, eng, submitted["batch_id"])
        return JSONResponse(
            {"batch_id": submitted["batch_id"], "status": "processing", "skipped": False},
            status_code=202,
            background=background,
        )

    @app.get("/api/batches")
    def list_batches(
        status: str | None = None,
        contract: str | None = None,
        q: str | None = None,
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
        page: int = 1,
        page_size: int = 20,
    ):
        stmt = select(Batch).order_by(Batch.created_at.desc())
        if status:
            stmt = stmt.where(Batch.status == status)
        if contract:
            if "@" in contract:
                cname, _, cver = contract.partition("@")
                cver = cver.lstrip("v")
                stmt = stmt.where(Batch.contract_name == cname)
                if cver.isdigit():
                    stmt = stmt.where(Batch.contract_version == int(cver))
            else:
                stmt = stmt.where(Batch.contract_name == contract)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Batch.file_name.ilike(like) | Batch.batch_id.ilike(like))
        if date_from:
            stmt = stmt.where(Batch.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            stmt = stmt.where(Batch.created_at <= datetime.fromisoformat(date_to))
        with sess() as s:
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = s.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        return paged([batch_out(b) for b in rows], total, page, page_size)

    @app.get("/api/batches/{batch_id}")
    def batch_detail(batch_id: str):
        with sess() as s:
            b = s.execute(select(Batch).where(Batch.batch_id == batch_id)).scalars().one_or_none()
        if b is None:
            return err("BATCH_NOT_FOUND", f"unknown batch {batch_id}", 404)
        return batch_out(b)

    @app.get("/api/batches/{batch_id}/rejections")
    def batch_rejections(
        batch_id: str,
        reason_code: str | None = None,
        column: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ):
        if get_batch(eng, batch_id) is None:
            return err("BATCH_NOT_FOUND", f"unknown batch {batch_id}", 404)
        stmt = (
            select(RejectedRecord)
            .where(RejectedRecord.batch_id == batch_id)
            .order_by(RejectedRecord.row_number, RejectedRecord.id)
        )
        if reason_code:
            stmt = stmt.where(RejectedRecord.reason_code == reason_code)
        if column:
            stmt = stmt.where(RejectedRecord.column_name == column)
        with sess() as s:
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = s.execute(stmt.offset((page - 1) * page_size).limit(page_size)).scalars().all()
        return paged([rejection_out(r) for r in rows], total, page, page_size)

    @app.get("/api/batches/{batch_id}/rejections/summary")
    def rejections_summary(batch_id: str):
        b = get_batch(eng, batch_id)
        if b is None:
            return err("BATCH_NOT_FOUND", f"unknown batch {batch_id}", 404)
        with sess() as s:
            counts = s.execute(
                select(RejectedRecord.reason_code, func.count())
                .where(RejectedRecord.batch_id == batch_id)
                .group_by(RejectedRecord.reason_code)
                .order_by(func.count().desc())
            ).all()
            n_rows = s.execute(
                select(func.count(func.distinct(RejectedRecord.row_number))).where(
                    RejectedRecord.batch_id == batch_id
                )
            ).scalar_one()
        reasons = [
            {"reason_code": code, "count": n, "pct": round(100 * n / n_rows, 1) if n_rows else 0.0}
            for code, n in counts
        ]
        return {"batch_id": batch_id, "rejected_rows": n_rows, "reasons": reasons}

    @app.get("/api/batches/{batch_id}/reject-report.csv")
    def reject_report(batch_id: str):
        from ingest.core.reports import rejected_row_groups, report_fieldnames

        b = get_batch(eng, batch_id)
        if b is None:
            return err("BATCH_NOT_FOUND", f"unknown batch {batch_id}", 404)
        if b.status == "failed":
            return err(
                "REPORT_UNAVAILABLE", b.failure_reason or "batch failed; no per-row report", 404
            )
        groups = rejected_row_groups(eng, batch_id)
        if groups:
            headers = list(groups[0]["original"].keys())
        else:
            headers = load_contract(f"{b.contract_name}@v{b.contract_version}").source_names

        def gen():
            import csv
            import io

            buf = io.StringIO()
            w = csv.DictWriter(
                buf, fieldnames=report_fieldnames(groups, headers), extrasaction="ignore"
            )
            w.writeheader()
            yield buf.getvalue()
            for g in groups:
                buf.seek(0)
                buf.truncate()
                w.writerow(
                    {
                        "row_number": g["row_number"],
                        "reason_codes": "; ".join(g["codes"]),
                        "reasons": " ;  ".join(g["messages"]),
                        **{h: (g["original"] or {}).get(h, "") for h in headers},
                    }
                )
                yield buf.getvalue()

        return StreamingResponse(
            gen(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=reject_report_{batch_id}.csv"},
        )

    @app.post("/api/batches/{batch_id}/reprocess")
    async def reprocess(batch_id: str, request: Request):
        try:
            body = (
                await request.json()
                if request.headers.get("content-type", "").startswith("application/json")
                else {}
            )
        except Exception:  # empty/invalid body means defaults
            body = {}
        b = get_batch(eng, batch_id)
        if b is None:
            return err("BATCH_NOT_FOUND", f"unknown batch {batch_id}", 404)
        if b.status not in ("partial", "failed"):
            return err(
                "REPROCESS_NOT_ALLOWED", f"only partial/failed batches (got {b.status})", 409
            )
        before = {"accepted_rows": b.accepted_rows, "rejected_rows": b.rejected_rows}
        ref = (body or {}).get("contract") or b.contract_name  # bare name → latest
        try:
            run_pipeline(stored_path(b), ref, engine=eng, reprocess_batch_id=batch_id)
        except ValueError as e:
            return err("INVALID_CONTRACT", str(e), 400)
        out = batch_out(get_batch(eng, batch_id))
        out["before"] = before
        return out

    # ---- cross-batch audit ----
    @app.get("/api/rejections")
    def rejections_cross(
        reason_code: str | None = None,
        contract: str | None = None,
        date_from: str | None = Query(None, alias="from"),
        date_to: str | None = Query(None, alias="to"),
        page: int = 1,
        page_size: int = 20,
    ):
        stmt = (
            select(
                RejectedRecord.batch_id,
                Batch.file_name,
                Batch.contract_name,
                Batch.contract_version,
                func.count().label("count"),
            )
            .join(Batch, Batch.batch_id == RejectedRecord.batch_id)
            .group_by(
                RejectedRecord.batch_id,
                Batch.file_name,
                Batch.contract_name,
                Batch.contract_version,
            )
            .order_by(func.count().desc())
        )
        if reason_code:
            stmt = stmt.where(RejectedRecord.reason_code == reason_code)
        if contract:
            stmt = stmt.where(Batch.contract_name == contract)
        if date_from:
            stmt = stmt.where(Batch.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            stmt = stmt.where(Batch.created_at <= datetime.fromisoformat(date_to))
        with sess() as s:
            total = s.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
            rows = s.execute(stmt.offset((page - 1) * page_size).limit(page_size)).all()
        return paged(
            [
                {
                    "batch_id": r[0],
                    "file_name": r[1],
                    "contract_name": r[2],
                    "contract_version": r[3],
                    "count": r[4],
                }
                for r in rows
            ],
            total,
            page,
            page_size,
        )

    # ---- contracts ----
    @app.get("/api/contracts")
    def contracts_list():
        return [{"name": n, "version": v, "ref": f"{n}@v{v}"} for n, v in sorted(list_contracts())]

    @app.get("/api/contracts/{name}/{version}")
    def contract_detail(name: str, version: str):
        try:
            c = load_contract(f"{name}@{version.lstrip('v')}")
        except (FileNotFoundError, ValueError) as e:
            return err("INVALID_CONTRACT", str(e), 404)
        return {
            "contract": c.name,
            "name": c.name,
            "version": c.version,
            "description": c.description,
            "target_table": c.target_table,
            "target_key": c.key,
            "on_conflict": c.on_conflict,
            "hash": c.contract_hash,
            "columns": [vars(col) for col in c.columns],
            "rules": [vars(r) for r in c.rules],
        }

    # ---- metrics ----
    @app.get("/api/metrics/summary")
    def metrics():
        today = date.today().isoformat()
        with sess() as s:
            batches_today = s.execute(
                select(func.count()).select_from(Batch).where(func.date(Batch.created_at) == today)
            ).scalar_one()
            acc = s.execute(
                select(func.coalesce(func.sum(Batch.accepted_rows), 0)).where(
                    func.date(Batch.created_at) == today
                )
            ).scalar_one()
            rej = s.execute(
                select(func.coalesce(func.sum(Batch.rejected_rows), 0)).where(
                    func.date(Batch.created_at) == today
                )
            ).scalar_one()
            failed = s.execute(
                select(func.count())
                .select_from(Batch)
                .where(Batch.status == "failed", func.date(Batch.created_at) == today)
            ).scalar_one()
        return {
            "batches_today": batches_today,
            "rows_accepted_today": int(acc or 0),
            "rows_rejected_today": int(rej or 0),
            "failed_batches_today": failed,
        }

    # ---- dashboard pages ----
    from ingest.api.pages import register_pages

    register_pages(app, eng, templates)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    from ingest.db.session import init_db

    init_db()
    uvicorn.run("ingest.api.main:app", host="0.0.0.0", port=8000, reload=False)
