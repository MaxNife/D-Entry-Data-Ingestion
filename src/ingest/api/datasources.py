"""External database connections in the UI: register, test, browse, and use a table
(or a read-only query) as a source to validate, import, or draft a contract from."""

import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from fastapi import BackgroundTasks, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import MetaData, Table, func, select
from starlette.concurrency import run_in_threadpool

from ingest.api.pages import contract_choices
from ingest.api.workspace import editor_context
from ingest.core import app_tables, connections, contract_store, dryrun
from ingest.core.contracts import load_contract
from ingest.core.errors import readable
from ingest.core.pipeline import execute_batch, submit_upload


def _to(path: str, **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _reason(e: Exception) -> str:
    return readable(e, 300)


def used_by(name: str) -> list[str]:
    """Contracts whose latest version loads into connection ``name``."""
    out = []
    for c in contract_choices():
        try:
            if load_contract(f"{c['name']}@v{c['latest']}").connection == name:
                out.append(c["name"])
        except (FileNotFoundError, ValueError):
            continue
    return out


def register_datasources(app, eng, templates) -> None:
    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def not_found(request: Request, name: str):
        detail = f"There is no connection named “{name}”."
        return render(request, "error.html", {"title": "Not found", "detail": detail}, 404)

    def form_data(form) -> dict:
        keys = ("name", "kind", "host", "port", "database", "username", "password")
        return {k: str(form.get(k) or "").strip() for k in keys}

    def edit_page(request, data: dict, mode: str, *, error=None, status=200):
        drivers = {k: connections.driver_available(k) for k in connections.KINDS}
        ctx = {
            "data": data,
            "mode": mode,
            "error": error,
            "kinds": connections.KINDS,
            "drivers": drivers,
        }
        return render(request, "connection_edit.html", ctx, status)

    @app.get("/connections", response_class=HTMLResponse)
    def connections_page(request: Request, deleted: str | None = None, error: str | None = None):
        rows = [{"c": c, "used_by": used_by(c.name)} for c in connections.list_all(eng)]
        flash = None
        if error:
            flash = {"kind": "error", "text": error}
        elif deleted:
            flash = {"kind": "info", "text": f"Deleted {deleted}"}
        ctx = {"rows": rows, "flash": flash, "kinds": connections.KINDS}
        return render(request, "connections.html", ctx)

    @app.get("/connections/new", response_class=HTMLResponse)
    def connection_new(request: Request, kind: str = "postgresql"):
        port = connections.KINDS.get(kind, connections.KINDS["postgresql"])["port"]
        return edit_page(request, {"kind": kind, "port": port or ""}, "new")

    @app.get("/connections/{name}/edit", response_class=HTMLResponse)
    def connection_edit(request: Request, name: str):
        c = connections.get(eng, name)
        if c is None:
            return not_found(request, name)
        data = {
            "name": c.name,
            "kind": c.kind,
            "host": c.host or "",
            "port": c.port or "",
            "database": c.database or "",
            "username": c.username or "",
            "has_password": bool(c.password_enc),
        }
        return edit_page(request, data, "edit")

    @app.post("/connections/test", response_class=HTMLResponse)
    async def connection_test(request: Request):
        """Try the form's settings without saving them (HTMX partial)."""
        form = await request.form()
        original = str(form.get("original") or "")
        try:
            existing = connections.get(eng, original) if original else None
            c = connections.build(form_data(form), existing)
            ok, message = await run_in_threadpool(connections.test, c)
        except ValueError as e:
            ok, message = False, str(e)
        return render(request, "_connection_test.html", {"ok": ok, "message": message})

    @app.post("/connections/save", response_class=HTMLResponse)
    async def connection_save(request: Request):
        form = await request.form()
        data = form_data(form)
        original = str(form.get("original") or "")
        mode = "edit" if original else "new"
        try:
            existing = connections.get(eng, original) if original else None
            c = connections.build(data, existing)
            if original and original != c.name and used_by(original):
                users = ", ".join(used_by(original))
                raise ValueError(
                    f"“{original}” is the destination of {users}; it cannot be renamed."
                )
            saved = connections.save(eng, c, replace=original or None)
        except ValueError as e:
            data = {**data, "has_password": bool(original)}
            return edit_page(request, data, mode, error=str(e), status=400)
        ok, message = await run_in_threadpool(connections.test, saved)
        connections.record_test(eng, saved.name, ok, message)
        return _to(f"/connections/{saved.name}", saved=1)

    @app.post("/connections/{name}/delete")
    def connection_delete(name: str):
        users = used_by(name)
        if users:
            msg = (
                f"“{name}” is the destination of {', '.join(users)}; change those contracts first."
            )
            return _to("/connections", error=msg)
        connections.delete(eng, name)
        return _to("/connections", deleted=name)

    @app.post("/connections/{name}/retest")
    async def connection_retest(name: str):
        c = connections.get(eng, name)
        if c is not None:
            ok, message = await run_in_threadpool(connections.test, c)
            connections.record_test(eng, name, ok, message)
        return _to(f"/connections/{name}")

    @app.get("/connections/{name}", response_class=HTMLResponse)
    def connection_page(
        request: Request,
        name: str,
        saved: int | None = None,
        table: str | None = None,
        error: str | None = None,
    ):
        c = connections.get(eng, name)
        if c is None:
            return not_found(request, name)
        ctx = {
            "c": c,
            "kinds": connections.KINDS,
            "used_by": used_by(name),
            "contracts": contract_choices(),
            "table": table,
            "tables": [],
            "preview": None,
            "flash": None,
        }
        if error:
            ctx["flash"] = {"kind": "error", "text": error}
        elif saved:
            ctx["flash"] = {"kind": "info", "text": f"Saved. {c.last_test_message or ''}"}
        try:
            engine = connections.engine_for(eng, name)
            ctx["tables"] = connections.browse(engine)
            if app_tables.same_database(engine, eng):  # never a way round the app-table rules
                ctx["tables"] = [t for t in ctx["tables"] if t["name"] not in app_tables.APP_TABLES]
            if table and table in {t["name"] for t in ctx["tables"]}:
                t = Table(table, MetaData(), autoload_with=engine)
                with engine.connect() as conn:
                    rows = conn.execute(select(t).limit(50)).all()
                    count = conn.execute(select(func.count()).select_from(t)).scalar_one()
                ctx["preview"] = {
                    "columns": [col.name for col in t.columns],
                    "rows": [list(r) for r in rows],
                    "count": int(count),
                }
        except Exception as e:
            ctx["flash"] = {"kind": "error", "text": _reason(e)}
        return render(request, "connection.html", ctx)

    @app.post("/connections/{name}/use")
    async def connection_use(name: str, request: Request, background: BackgroundTasks):
        """Snapshot a table or query, then validate it, import it, or draft a contract from it."""
        form = await request.form()
        action = str(form.get("action") or "validate")
        table = str(form.get("table") or "") or None
        sql = str(form.get("sql") or "") or None
        contract = str(form.get("contract") or "")
        back = {"table": table} if table else {}
        needs = {"draft": "contracts", "import": "ingest", "validate": "ingest"}.get(
            action, "ingest"
        )
        if needs not in request.state.perms:
            return _to(f"/connections/{name}", **back, error="Your role does not allow this.")
        if connections.get(eng, name) is None:
            return _to("/connections", error=f"There is no connection named “{name}”.")
        stem = table or "query"
        folder = Path(tempfile.mkdtemp(prefix="ingest-src-"))
        snap = folder / f"{name}.{stem}.csv"
        try:
            engine = connections.engine_for(eng, name)
            if app_tables.same_database(engine, eng):
                app_tables.refuse(table or sql or "")
            rows = await run_in_threadpool(connections.snapshot, engine, snap, table=table, sql=sql)
            if action == "draft":
                spec = await run_in_threadpool(
                    contract_store.infer_contract,
                    snap,
                    re.sub(r"\W", "_", stem),
                    5000,
                    f"{name}.{stem}",
                )
                spec["description"] = (
                    f"Drafted from {name}.{stem} ({rows} rows); review the types and checks."
                )
                note = f"Drafted from {name}.{stem}. The headers are the source's column names."
                return render(
                    request, "contract_edit.html", editor_context(eng, spec, "new", note=note)
                )
            if not contract:
                return _to(
                    f"/connections/{name}",
                    **back,
                    error="Choose a contract to check the rows against.",
                )
            if action == "import":
                submitted = submit_upload(snap, contract, engine=eng, file_name=snap.name)
                if submitted["skipped"]:
                    return _to(f"/batches/{submitted['batch_id']}")
                background.add_task(execute_batch, eng, submitted["batch_id"])
                return RedirectResponse(
                    f"/batches/{submitted['batch_id']}", status_code=303, background=background
                )
            started = dryrun.start_validation(snap, contract, file_name=snap.name)
            background.add_task(dryrun.run_validation, started["id"], engine=eng)
            return RedirectResponse(
                f"/validations/{started['id']}", status_code=303, background=background
            )
        except Exception as e:
            return _to(f"/connections/{name}", **back, error=_reason(e))
        finally:
            shutil.rmtree(folder, ignore_errors=True)
