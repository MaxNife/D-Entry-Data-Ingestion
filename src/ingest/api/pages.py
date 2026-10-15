"""Server-rendered ops dashboard (Jinja2 + HTMX). Spec §10."""

import os
import shutil
import tempfile
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ingest.api.main import batch_out
from ingest.core.contracts import list_contracts
from ingest.core.pipeline import execute_batch, get_batch, run_pipeline, stored_path, submit_upload
from ingest.db.models import Batch, RejectedRecord

STATUS_CLASS = {
    "completed": "ok",
    "partial": "warn",
    "failed": "bad",
    "pending": "idle",
    "processing": "idle",
}


def register_pages(app, eng, templates) -> None:
    def sess() -> Session:
        return Session(eng)

    def ctx_common() -> dict:
        return {"status_class": STATUS_CLASS}

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        q: str | None = None,
        status: str | None = None,
        contract: str | None = None,
    ):
        stmt = select(Batch).order_by(Batch.created_at.desc()).limit(100)
        if status:
            stmt = stmt.where(Batch.status == status)
        if contract:
            stmt = stmt.where(Batch.contract_name == contract)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Batch.file_name.like(like) | Batch.batch_id.like(like))
        today = func.date(Batch.created_at) == func.date(func.now())
        with sess() as s:
            batches = s.execute(stmt).scalars().all()
            metrics = {
                "batches_today": s.execute(
                    select(func.count()).select_from(Batch).where(today)
                ).scalar_one(),
                "rows_accepted_today": int(
                    s.execute(
                        select(func.coalesce(func.sum(Batch.accepted_rows), 0)).where(today)
                    ).scalar_one()
                    or 0
                ),
                "rows_rejected_today": int(
                    s.execute(
                        select(func.coalesce(func.sum(Batch.rejected_rows), 0)).where(today)
                    ).scalar_one()
                    or 0
                ),
                "failed_today": s.execute(
                    select(func.count()).select_from(Batch).where(Batch.status == "failed", today)
                ).scalar_one(),
            }
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "batches": [batch_out(b) for b in batches],
                "metrics": metrics,
                "q": q or "",
                "status": status or "",
                "contract": contract or "",
                "contracts": sorted({n for n, _ in list_contracts()}),
                **ctx_common(),
            },
        )

    @app.get("/batches/{batch_id}", response_class=HTMLResponse)
    def batch_page(request: Request, batch_id: str, reason_code: str | None = None):
        b = get_batch(eng, batch_id)
        if b is None:
            return HTMLResponse(f"<h1>Unknown batch {batch_id}</h1>", status_code=404)
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
            rej_stmt = (
                select(RejectedRecord)
                .where(RejectedRecord.batch_id == batch_id)
                .order_by(RejectedRecord.row_number, RejectedRecord.id)
                .limit(200)
            )
            if reason_code:
                rej_stmt = rej_stmt.where(RejectedRecord.reason_code == reason_code)
            rows = s.execute(rej_stmt).scalars().all()
        reasons = [
            {"reason_code": code, "count": n, "pct": round(100 * n / n_rows, 1) if n_rows else 0.0}
            for code, n in counts
        ]
        return templates.TemplateResponse(
            request,
            "batch.html",
            {
                "b": batch_out(b),
                "reasons": reasons,
                "rows": rows,
                "reason_code": reason_code or "",
                **ctx_common(),
            },
        )

    @app.get("/rejections", response_class=HTMLResponse)
    def audit_page(
        request: Request,
        reason_code: str | None = None,
        contract: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        from datetime import datetime

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
            .limit(100)
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
            rows = s.execute(stmt).all()
            codes = sorted(
                s.execute(select(func.distinct(RejectedRecord.reason_code))).scalars().all()
            )
        return templates.TemplateResponse(
            request,
            "rejections.html",
            {
                "rows": rows,
                "codes": sorted(codes),
                "reason_code": reason_code or "",
                "contract": contract or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
                "contracts": sorted({n for n, _ in list_contracts()}),
                **ctx_common(),
            },
        )

    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(request: Request):
        return templates.TemplateResponse(
            request, "upload.html", {"contracts": sorted(list_contracts()), **ctx_common()}
        )

    @app.post("/upload")
    async def upload_form(request: Request):
        form = await request.form()
        upload = form.get("file")
        contract_ref = form.get("contract")
        fd, tmp_path = tempfile.mkstemp(suffix=Path(upload.filename).suffix)
        os.close(fd)
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(upload.file, f)
        await upload.close()
        submitted = submit_upload(Path(tmp_path), contract_ref, engine=eng)
        Path(tmp_path).unlink(missing_ok=True)
        if not submitted["skipped"]:
            execute_batch(eng, submitted["batch_id"])
        return RedirectResponse(f"/batches/{submitted['batch_id']}", status_code=303)

    @app.post("/batches/{batch_id}/reprocess", response_class=HTMLResponse)
    def reprocess_form(batch_id: str, request: Request):
        b = get_batch(eng, batch_id)
        if b is None:
            return HTMLResponse("unknown batch", status_code=404)
        if b.status not in ("partial", "failed"):
            return HTMLResponse("only partial/failed batches", status_code=409)
        run_pipeline(
            stored_path(b),
            f"{b.contract_name}@v{b.contract_version}",
            engine=eng,
            reprocess_batch_id=batch_id,
        )
        return RedirectResponse(f"/batches/{batch_id}", status_code=303)

    @app.get("/partials/batch-status/{batch_id}", response_class=HTMLResponse)
    def batch_status_partial(batch_id: str):
        b = get_batch(eng, batch_id)
        if b is None:
            return HTMLResponse("<span>unknown</span>", status_code=404)
        cls = STATUS_CLASS.get(b.status, "idle")
        terminal = b.status in ("completed", "partial", "failed")
        poll = (
            ""
            if terminal
            else f'hx-get="/partials/batch-status/{batch_id}" hx-trigger="every 2s" hx-swap="outerHTML"'
        )
        return HTMLResponse(
            f'<span class="badge {cls}" {poll}>{b.status}</span><span class="htmx-indicator">…</span>'
        )
