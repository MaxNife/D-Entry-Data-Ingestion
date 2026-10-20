"""Server-rendered ops dashboard (Jinja2 + HTMX). Spec §10.

Every query comes from ``ingest.api.queries`` so the pages and the JSON API
always agree. Templates are autoescaped; no HTML is built from strings here.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ingest.api import queries
from ingest.api.main import batch_out, spool_upload
from ingest.core import dryrun, progress
from ingest.core.contracts import list_contracts, load_contract
from ingest.core.pipeline import (
    execute_batch,
    get_batch,
    run_pipeline,
    stored_path,
    submit_upload,
    twin_batch,
)
from ingest.db.models import BatchChunk, RejectedRecord

STATUS_CLASS = {
    "completed": "ok",
    "partial": "warn",
    "failed": "bad",
    "pending": "idle",
    "processing": "run",
}

#: Plain-language names for the built-in reason codes. Contract rules carry
#: their own message, which the breakdown shows instead.
REASON_LABELS = {
    "MISSING_REQUIRED": "Required value is missing",
    "TYPE_MISMATCH": "Value has the wrong type",
    "OUT_OF_RANGE": "Value is out of range",
    "NOT_IN_SET": "Value is not an allowed option",
    "PATTERN_MISMATCH": "Value does not match the pattern",
    "DUPLICATE_KEY_IN_CHUNK": "Duplicate key in the file",
    "LOAD_ERROR": "Database rejected the chunk",
}

RECORDS_PER_PAGE = 50


# ---- template helpers -------------------------------------------------------


def fmt_num(n) -> str:
    return "—" if n is None else f"{int(n):,}"


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    ts = datetime.fromisoformat(value) if isinstance(value, str) else value
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def fmt_ago(value) -> str:
    ts = _parse_ts(value)
    if ts is None:
        return "—"
    secs = int((datetime.now(UTC) - ts).total_seconds())
    if secs < 45:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"{secs // size} {unit} ago"
    return "just now"


def fmt_utc(value) -> str:
    ts = _parse_ts(value)
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else ""


def fmt_duration(batch: dict) -> str:
    start, end = _parse_ts(batch.get("started_at")), _parse_ts(batch.get("finished_at"))
    if not start or not end:
        return "—"
    secs = (end - start).total_seconds()
    if secs < 1:
        return f"{secs * 1000:.0f} ms"
    if secs < 60:
        return f"{secs:.1f} s"
    return f"{int(secs // 60)} min {int(secs % 60)} s"


def fmt_cell(v) -> str:
    """A table cell: datetimes to the second, everything else as text."""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def pct(part, whole) -> float:
    return round(100 * (part or 0) / whole, 1) if whole else 0.0


def reason_label(code: str, sample: str | None = None) -> str:
    return REASON_LABELS.get(code) or sample or code


def contract_choices() -> list[dict]:
    """One entry per contract name: its versions, newest first."""
    by_name: dict[str, list[int]] = {}
    for name, version in list_contracts():
        by_name.setdefault(name, []).append(version)
    return [
        {"name": n, "versions": sorted(vs, reverse=True), "latest": max(vs)}
        for n, vs in sorted(by_name.items())
    ]


def query_string(**params) -> str:
    return urlencode({k: v for k, v in params.items() if v not in (None, "")})


def fmt_seconds(secs: float | None) -> str:
    if secs is None:
        return "—"
    return fmt_duration(
        {
            "started_at": datetime.fromtimestamp(0, UTC),
            "finished_at": datetime.fromtimestamp(secs, UTC),
        }
    )


#: Time-range presets for the runs page, as (value, label, hours back).
SINCE = (
    ("24h", "Last 24 hours", 24),
    ("7d", "Last 7 days", 24 * 7),
    ("30d", "Last 30 days", 24 * 30),
)

_CHECKS = ("type", "required", "in_set", "pattern", "min", "max", "max_length")


def contract_diff(old, new) -> dict:
    """What changed between two versions of a contract, column by column."""
    before = {c.name: c for c in old.columns}
    after = {c.name: c for c in new.columns}
    changed = []
    for name in after.keys() & before.keys():
        diffs = [
            (k, getattr(before[name], k), getattr(after[name], k))
            for k in _CHECKS
            if getattr(before[name], k) != getattr(after[name], k)
        ]
        if diffs:
            changed.append({"column": name, "diffs": diffs})
    old_rules = {r.code for r in old.rules}
    new_rules = {r.code for r in new.rules}
    return {
        "added": [c for c in new.columns if c.name not in before],
        "removed": [c for c in old.columns if c.name not in after],
        "changed": sorted(changed, key=lambda d: d["column"]),
        "rules_added": sorted(new_rules - old_rules),
        "rules_removed": sorted(old_rules - new_rules),
    }


# ---- routes -------------------------------------------------------------------


def register_pages(app, eng, templates) -> None:
    env = templates.env
    env.filters.update(
        num=fmt_num,
        ago=fmt_ago,
        utc=fmt_utc,
        duration=fmt_duration,
        seconds=fmt_seconds,
        cell=fmt_cell,
    )
    env.globals.update(
        status_class=STATUS_CLASS,
        statuses=queries.STATUSES,
        reason_label=reason_label,
        pct=pct,
        qs=query_string,
        since_options=SINCE,
        now_utc=lambda: datetime.now(UTC).strftime("%H:%M UTC"),
        contract_choices=contract_choices,
    )

    def sess() -> Session:
        return Session(eng)

    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def not_found(request: Request, what: str):
        return render(request, "error.html", {"title": "Not found", "detail": what}, 404)

    # -- home ----------------------------------------------------------------
    def home_context(
        q: str | None,
        status: str | None,
        contract: str | None,
        message: dict | None,
        since: str | None = None,
    ) -> dict:
        hours = next((h for value, _, h in SINCE if value == since), None)
        date_from = (datetime.now(UTC) - timedelta(hours=hours)).isoformat() if hours else None
        stmt = queries.batches_stmt(status=status, contract=contract, q=q, date_from=date_from)
        with sess() as s:
            batches = s.execute(stmt.limit(100)).scalars().all()
            metrics = queries.metrics_today(s)
            days = queries.runs_by_day(s)
        return {
            "batches": [batch_out(b) for b in batches],
            "metrics": metrics,
            "days": days,
            "day_max": max((d["total"] for d in days), default=0),
            "q": q or "",
            "status": status or "",
            "contract": contract or "",
            "since": since if hours else "",
            "filtered": bool(q or status or contract or hours),
            "contracts": contract_choices(),
            "message": message,
        }

    def skipped_message(batch_id: str | None, skipped: int | None) -> dict | None:
        if batch_id and skipped:
            return {
                "kind": "info",
                "text": "This file was already processed under that contract version",
                "batch_id": batch_id,
            }
        return None

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        q: str | None = None,
        status: str | None = None,
        contract: str | None = None,
        uploaded: str | None = None,
        skipped: int | None = None,
        since: str | None = None,
        error: str | None = None,
    ):
        if contract and "@" in contract:
            contract = contract.split("@")[0]
        message = {"kind": "error", "text": error} if error else skipped_message(uploaded, skipped)
        ctx = home_context(q, status, contract, message, since)
        return render(request, "index.html", ctx)

    # -- batch detail ------------------------------------------------------------
    @app.get("/batches/{batch_id}", response_class=HTMLResponse)
    def batch_page(
        request: Request,
        batch_id: str,
        reason_code: str | None = None,
        page: int = 1,
        reprocessed: int | None = None,
        before_a: int | None = None,
        before_r: int | None = None,
        error: str | None = None,
        fixed_from: str | None = None,
    ):
        b = get_batch(eng, batch_id)
        if b is None:
            return not_found(request, f"There is no batch with id “{batch_id}”.")
        page = max(page, 1)
        rec_stmt = (
            select(RejectedRecord)
            .where(RejectedRecord.batch_id == batch_id)
            .order_by(RejectedRecord.row_number, RejectedRecord.id)
        )
        if reason_code:
            rec_stmt = rec_stmt.where(RejectedRecord.reason_code == reason_code)
        with sess() as s:
            n_rows, reasons = queries.reason_breakdown(s, batch_id)
            n_records = queries.count(s, rec_stmt)
            rows = (
                s.execute(rec_stmt.offset((page - 1) * RECORDS_PER_PAGE).limit(RECORDS_PER_PAGE))
                .scalars()
                .all()
            )
        try:
            contract = load_contract(f"{b.contract_name}@v{b.contract_version}")
            source_of = {c.name: c.source_name for c in contract.columns}
        except (FileNotFoundError, ValueError):
            contract, source_of = None, {}
        versions = next(
            (c["versions"] for c in contract_choices() if c["name"] == b.contract_name), []
        )
        # Reprocess moves the batch to the latest version; if this file already has its own
        # batch there, the move would collide with it, so the page points there instead.
        twin = twin_batch(eng, b, b.contract_name, versions[0]) if versions else None
        reprocess_result = None
        if reprocessed and before_a is not None and before_r is not None:
            reprocess_result = {
                "before_accepted": before_a,
                "before_rejected": before_r,
                "after_accepted": b.accepted_rows,
                "after_rejected": b.rejected_rows,
            }
        live = b.status not in queries.TERMINAL
        prog = chunks = None
        if live:
            expected = progress.estimate_rows(stored_path(b))
            prog = progress.snapshot(b.total_rows, expected, b.started_at or b.created_at, False)
            with sess() as s:
                chunks = queries.count(s, select(BatchChunk).where(BatchChunk.batch_id == batch_id))
        return render(
            request,
            "batch.html",
            {
                "b": batch_out(b),
                "prog": prog,
                "chunks": chunks,
                "reasons": reasons,
                "rejected_row_count": n_rows,
                "rows": rows,
                "source_of": source_of,
                "reason_code": reason_code or "",
                "page": page,
                "n_records": n_records,
                "per_page": RECORDS_PER_PAGE,
                "reprocess_result": reprocess_result,
                "error": error,
                "live": live,
                "contract": contract,
                "latest_version": versions[0] if versions else None,
                "twin": batch_out(twin) if twin else None,
                "fixed_from": fixed_from if fixed_from and get_batch(eng, fixed_from) else None,
            },
        )

    # -- contract catalog --------------------------------------------------------
    @app.get("/contracts", response_class=HTMLResponse)
    def contracts_page(request: Request, deleted: str | None = None):
        with sess() as s:
            health = queries.contract_health(s)
        catalog = []
        for c in contract_choices():
            try:
                latest = load_contract(f"{c['name']}@v{c['latest']}")
            except (FileNotFoundError, ValueError):
                latest = None
            catalog.append({**c, "contract": latest, "health": health.get(c["name"])})
        flash = {"kind": "info", "text": f"Deleted {deleted}"} if deleted else None
        ctx = {"catalog": catalog, "c": None, "tree": contract_choices(), "flash": flash}
        return render(request, "contracts.html", ctx)

    @app.get("/contracts/{name}", response_class=HTMLResponse)
    def contract_page(
        request: Request,
        name: str,
        v: int | None = None,
        tab: str = "columns",
        saved: int | None = None,
        deleted: int | None = None,
        error: str | None = None,
    ):
        entry = next((c for c in contract_choices() if c["name"] == name), None)
        if entry is None:
            return not_found(request, f"There is no contract named “{name}”.")
        version = v if v in entry["versions"] else entry["latest"]
        try:
            c = load_contract(f"{name}@v{version}")
        except (FileNotFoundError, ValueError) as e:
            return render(
                request, "error.html", {"title": "Invalid contract", "detail": str(e)}, 422
            )
        tab = tab if tab in ("columns", "rules", "versions", "runs", "quality") else "columns"
        ref = f"{name}@v{version}"
        ctx = {"c": c, "entry": entry, "version": version, "tab": tab, "tree": contract_choices()}
        if error:
            ctx["flash"] = {"kind": "error", "text": error}
        elif saved:
            ctx["flash"] = {"kind": "info", "text": f"Saved {ref}"}
        elif deleted:
            ctx["flash"] = {"kind": "info", "text": f"Deleted {name} v{deleted}"}
        with sess() as s:
            ctx["health"] = queries.contract_health(s).get(name)
            ctx["version_runs"] = queries.count(s, queries.batches_stmt(contract=ref))
            if tab == "runs":
                stmt = queries.batches_stmt(contract=ref).limit(100)
                ctx["batches"] = [batch_out(b) for b in s.execute(stmt).scalars().all()]
            if tab == "quality":
                ctx["totals"] = queries.reason_totals(s, contract=ref)
                ctx["columns"] = queries.column_totals(s, ref)
                ctx["source_of"] = {col.name: col.source_name for col in c.columns}
        if tab == "versions":
            history = []
            for older, newer in zip(entry["versions"][1:], entry["versions"], strict=False):
                try:
                    diff = contract_diff(
                        load_contract(f"{name}@v{older}"), load_contract(f"{name}@v{newer}")
                    )
                except (FileNotFoundError, ValueError):
                    diff = None
                history.append({"version": newer, "previous": older, "diff": diff})
            history.append({"version": entry["versions"][-1], "previous": None, "diff": None})
            ctx["history"] = history
        return render(request, "contracts.html", ctx)

    # -- cross-batch audit -------------------------------------------------------
    @app.get("/rejections", response_class=HTMLResponse)
    def audit_page(
        request: Request,
        reason_code: str | None = None,
        contract: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ):
        filters = {"contract": contract, "date_from": date_from, "date_to": date_to}
        error = None
        try:
            stmt = queries.audit_stmt(reason_code=reason_code, **filters).limit(100)
            with sess() as s:
                rows = s.execute(stmt).all()
                totals = queries.reason_totals(s, **filters)
        except ValueError:
            error = "Dates must be in YYYY-MM-DD format."
            rows, totals = [], []
        with sess() as s:
            codes = sorted(
                s.execute(select(func.distinct(RejectedRecord.reason_code))).scalars().all()
            )
        return render(
            request,
            "rejections.html",
            {
                "rows": rows,
                "totals": totals,
                "total_rejections": sum(n for _, n in totals),
                "codes": codes,
                "reason_code": reason_code or "",
                "contract": contract or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
                "filtered": bool(reason_code or contract or date_from or date_to),
                "contracts": contract_choices(),
                "error": error,
            },
            400 if error else 200,
        )

    # -- upload ----------------------------------------------------------------
    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(
        request: Request,
        uploaded: str | None = None,
        skipped: int | None = None,
        contract: str | None = None,
        error: str | None = None,
    ):
        choices = contract_choices()
        selected = contract or (choices[0]["name"] if choices else "")
        message = {"kind": "error", "text": error} if error else skipped_message(uploaded, skipped)
        return render(
            request,
            "upload.html",
            {
                "contracts": choices,
                "selected": selected,
                "preview": contract_preview(selected),
                "message": message,
                "validations": dryrun.recent(contract=selected) if selected else [],
            },
        )

    @app.post("/upload")
    async def upload_form(request: Request, background: BackgroundTasks):
        form = await request.form()
        upload = form.get("file")
        contract_ref = str(form.get("contract") or "")
        origin = "/upload" if form.get("origin") == "upload" else "/"

        def fail(text: str):
            message = {"kind": "error", "text": text}
            if request.headers.get("x-upload"):  # the in-page uploader follows a redirect
                # on the Runs page "contract" is a filter, so only Add data gets it back
                params = {"error": text, "contract": contract_ref if origin == "/upload" else ""}
                return RedirectResponse(f"{origin}?{query_string(**params)}", status_code=303)
            if origin == "/upload":
                choices = contract_choices()
                ctx = {
                    "contracts": choices,
                    "selected": contract_ref,
                    "preview": contract_preview(contract_ref),
                    "message": message,
                }
                return render(request, "upload.html", ctx, 400)
            return render(request, "index.html", home_context(None, None, None, message), 400)

        if upload is None or not getattr(upload, "filename", ""):
            return fail("Choose a file to ingest.")
        if not contract_ref:
            return fail("Choose a contract to validate the file against.")
        try:
            load_contract(contract_ref)
        except (FileNotFoundError, ValueError) as e:
            return fail(f"Unknown contract: {e}")
        tmp_path = await spool_upload(upload)
        try:
            submitted = submit_upload(tmp_path, contract_ref, engine=eng, file_name=upload.filename)
        except (FileNotFoundError, ValueError) as e:
            return fail(str(e))
        finally:
            tmp_path.unlink(missing_ok=True)
        if submitted["skipped"]:
            target = f"{origin}?uploaded={submitted['batch_id']}&skipped=1"
            return RedirectResponse(target, status_code=303)
        background.add_task(execute_batch, eng, submitted["batch_id"])
        return RedirectResponse(
            f"/batches/{submitted['batch_id']}", status_code=303, background=background
        )

    # -- reprocess -------------------------------------------------------------
    @app.post("/batches/{batch_id}/reprocess", response_class=HTMLResponse)
    def reprocess_form(batch_id: str, request: Request):
        b = get_batch(eng, batch_id)
        if b is None:
            return not_found(request, f"There is no batch with id “{batch_id}”.")
        if b.status not in ("partial", "failed"):
            return RedirectResponse(
                f"/batches/{batch_id}?"
                + query_string(
                    error=f"Only partial or failed batches can be reprocessed ({b.status})."
                ),
                status_code=303,
            )
        before_a, before_r = b.accepted_rows, b.rejected_rows
        try:
            run_pipeline(stored_path(b), b.contract_name, engine=eng, reprocess_batch_id=batch_id)
        except (FileNotFoundError, ValueError) as e:
            return RedirectResponse(
                f"/batches/{batch_id}?" + query_string(error=str(e)), status_code=303
            )
        return RedirectResponse(
            f"/batches/{batch_id}?reprocessed=1&before_a={before_a}&before_r={before_r}",
            status_code=303,
        )

    # -- HTMX partials -----------------------------------------------------------
    def contract_preview(ref: str | None) -> dict | None:
        if not ref:
            return None
        try:
            return load_contract(ref)
        except (FileNotFoundError, ValueError):
            return None

    @app.get("/partials/contract", response_class=HTMLResponse)
    def contract_partial(request: Request, contract: str = ""):
        ctx = {
            "preview": contract_preview(contract),
            "validations": dryrun.recent(contract=contract) if contract else [],
        }
        return render(request, "_contract.html", ctx)

    @app.get("/partials/batch-row/{batch_id}", response_class=HTMLResponse)
    def batch_row_partial(request: Request, batch_id: str):
        b = get_batch(eng, batch_id)
        if b is None:
            return HTMLResponse("", status_code=404)
        return render(request, "_batch_row.html", {"b": batch_out(b)})

    @app.get("/partials/batch-status/{batch_id}", response_class=HTMLResponse)
    def batch_status_partial(request: Request, batch_id: str):
        b = get_batch(eng, batch_id)
        if b is None:
            return HTMLResponse("", status_code=404)
        return render(request, "_status_badge.html", {"b": batch_out(b), "poll": True})
