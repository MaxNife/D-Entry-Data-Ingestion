"""Workspace features on top of the ingest core: contract authoring, row fixing,
dry-run validation, table browsing and export, and read-only SQL.

Registered before ``pages`` so literal paths such as ``/contracts/new`` win over
``/contracts/{name}``.
"""

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlencode

import yaml
from fastapi import BackgroundTasks, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from ingest.api.main import batch_out, spool_upload
from ingest.api.pages import contract_choices
from ingest.api.pages import fmt_cell as pages_cell
from ingest.config import settings
from ingest.core import (
    connections,
    app_tables,
    contract_store,
    dryrun,
    exports,
    fixes,
    progress,
    query,
    tables,
)
from ingest.core.contracts import ALLOWED_TYPES, load_contract
from ingest.core.errors import readable
from ingest.core.pipeline import execute_batch, get_batch, stored_path, submit_upload
from ingest.db.models import Batch

SYSTEM_TABLES = ("batches", "rejected_records")


def _to(path: str, **params) -> RedirectResponse:
    """303 redirect with properly encoded query parameters."""
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _download(path: Path, filename: str, fmt: str) -> FileResponse:
    """Serve a temp file and delete its folder once sent."""
    return FileResponse(
        path,
        filename=filename,
        media_type=exports.FORMATS[fmt][0],
        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True),
    )


def _tmp(name: str) -> Path:
    return Path(tempfile.mkdtemp(prefix="ingest-")) / name


def editor_context(eng, spec: dict, mode: str, *, error=None, note=None) -> dict:
    """Everything the contract editor template needs, for a new or an edited contract."""
    hidden = {*SYSTEM_TABLES, *app_tables.APP_TABLES}
    name = spec.get("contract")
    return {
        "spec": spec,
        "spec_yaml": yaml.safe_dump(
            {k: v for k, v in spec.items() if k != "version"}, sort_keys=False, allow_unicode=True
        ),
        "mode": mode,
        "error": error,
        "note": note,
        "types": sorted(ALLOWED_TYPES),
        "existing_tables": sorted(t for t in inspect(eng).get_table_names() if t not in hidden),
        "connections": connections.list_all(eng),
        "next_version": max(contract_store.versions_of(name) or [0]) + 1 if name else 1,
    }


def _reason(e: Exception) -> str:
    return readable(e, 400)


def register_workspace(app, eng, templates) -> None:
    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def not_found(request: Request, what: str):
        return render(request, "error.html", {"title": "Not found", "detail": what}, 404)

    def runs_under(name: str, version: int | None = None) -> int:
        stmt = select(func.count()).select_from(Batch).where(Batch.contract_name == name)
        if version is not None:
            stmt = stmt.where(Batch.contract_version == version)
        with Session(eng) as s:
            return int(s.execute(stmt).scalar_one())

    # ---- contract authoring ------------------------------------------------------
    def blank_spec() -> dict:
        return {
            "contract": "",
            "description": "",
            "source": {"formats": ["csv"], "encoding": "utf-8", "delimiter": ","},
            "target": {"table": "", "key": "row_hash", "on_conflict": "update"},
            "columns": [{"name": "", "source_name": "", "type": "str", "required": True}],
            "rules": [],
        }

    def editor(request: Request, spec: dict, mode: str, *, error=None, note=None, status=200):
        ctx = editor_context(eng, spec, mode, error=error, note=note)
        return render(request, "contract_edit.html", ctx, status)

    @app.get("/contracts/new", response_class=HTMLResponse)
    def contract_new(request: Request):
        return editor(request, blank_spec(), "new")

    @app.post("/contracts/infer", response_class=HTMLResponse)
    async def contract_infer(request: Request):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return editor(
                request,
                blank_spec(),
                "new",
                error="Choose a sample file to infer from.",
                status=400,
            )
        tmp = await spool_upload(upload)  # keeps the extension, so CSV vs Excel is detected
        try:
            name = str(form.get("name") or "") or Path(upload.filename).stem
            spec = await run_in_threadpool(
                contract_store.infer_contract, tmp, name, file_name=upload.filename
            )
        except Exception as e:
            return editor(
                request,
                blank_spec(),
                "new",
                error=f"Could not read the sample file: {e}",
                status=400,
            )
        finally:
            tmp.unlink(missing_ok=True)
        note = f"Drafted from {upload.filename}. Check each column's type and checks before saving."
        return editor(request, spec, "new", note=note)

    @app.get("/contracts/{name}/edit", response_class=HTMLResponse)
    def contract_edit(request: Request, name: str, v: int | None = None):
        try:
            c = load_contract(f"{name}@v{v}" if v else name)
        except (FileNotFoundError, ValueError):
            return not_found(request, f"There is no contract named “{name}”.")
        spec = contract_store.contract_to_dict(c)
        note = f"Editing creates v{max(contract_store.versions_of(name)) + 1}; v{c.version} stays as it is."
        return editor(request, spec, "edit", note=note)

    @app.post("/contracts/save", response_class=HTMLResponse)
    async def contract_save(request: Request):
        form = await request.form()
        mode = "edit" if form.get("mode") == "edit" else "new"
        try:
            if form.get("editor") == "yaml":
                spec = yaml.safe_load(str(form.get("spec_yaml") or "")) or {}
                if not isinstance(spec, dict):
                    raise ValueError("The YAML must describe one contract (a mapping).")
            else:
                spec = json.loads(str(form.get("spec_json") or "{}"))
        except (ValueError, yaml.YAMLError) as e:
            return editor(
                request, blank_spec(), mode, error=f"Could not read the contract: {e}", status=400
            )
        name = str(spec.get("contract") or "")
        exists = bool(contract_store.versions_of(name))
        if mode == "new" and exists:
            return editor(
                request,
                spec,
                mode,
                error=f"A contract named “{name}” already exists. Open it and choose Edit to add a version.",
                status=400,
            )
        if name in ("new", "infer", "save"):
            return editor(
                request, spec, mode, error=f"“{name}” is reserved; choose another name.", status=400
            )
        try:
            saved = contract_store.save_new_version(spec)
        except (ValueError, FileExistsError) as e:
            return editor(request, spec, mode, error=str(e), status=400)
        return RedirectResponse(
            f"/contracts/{saved.name}?v={saved.version}&saved=1", status_code=303
        )

    @app.post("/contracts/{name}/delete")
    async def contract_delete(name: str, request: Request):
        form = await request.form()
        try:
            version = int(str(form.get("version")))
        except ValueError:
            return _to(f"/contracts/{name}")
        used = runs_under(name, version)
        if used:
            msg = f"v{version} has {used} batch run(s) and cannot be deleted: batches cite it for audit."
            return _to(f"/contracts/{name}", v=version, error=msg)
        try:
            contract_store.delete_version(name, version)
        except FileNotFoundError:
            return _to("/contracts")
        if contract_store.versions_of(name):
            return _to(f"/contracts/{name}", deleted=version)
        return _to("/contracts", deleted=f"{name}@v{version}")

    # ---- rejected rows: download as-is, fix in the grid --------------------------
    @app.get("/api/batches/{batch_id}/rejected-rows")
    def rejected_rows(batch_id: str, request: Request, format: str = "csv"):
        b = get_batch(eng, batch_id)
        if b is None:
            return not_found(request, f"There is no batch with id “{batch_id}”.")
        fmt = format if format in ("csv", "xlsx") else "csv"
        headers, rows = exports.rejected_rows_as_original(eng, batch_id)
        stem = Path(b.file_name).stem
        path = exports.write_rows(headers, rows, fmt, _tmp(f"{stem}.rejected.{fmt}"))
        return _download(path, path.name, fmt)

    @app.get("/api/batches/{batch_id}/source-file")
    def source_file(batch_id: str, request: Request):
        b = get_batch(eng, batch_id)
        path = stored_path(b) if b else None
        if b is None or not path.exists():
            return not_found(request, "The stored file for this batch is not available.")
        return FileResponse(path, filename=b.file_name)

    def fix_context(b, grid: dict, contract_ref: str, check: dict | None = None) -> dict:
        versions = contract_store.versions_of(b.contract_name)
        return {
            "b": batch_out(b),
            "grid": grid,
            "check": check,
            "contract_ref": contract_ref,
            "refs": [f"{b.contract_name}@v{v}" for v in reversed(versions)],
        }

    def parse_grid(form) -> tuple[list[str], list[dict]]:
        data = json.loads(str(form.get("rows_json") or "{}"))
        headers = [str(h) for h in data.get("headers") or []]
        rows = [
            {
                "row_number": r.get("row_number"),
                "values": ["" if v is None else str(v) for v in r.get("values") or []],
                "include": bool(r.get("include", True)),
            }
            for r in data.get("rows") or []
        ]
        return headers, rows

    @app.get("/batches/{batch_id}/fix", response_class=HTMLResponse)
    def fix_page(batch_id: str, request: Request):
        b = get_batch(eng, batch_id)
        if b is None:
            return not_found(request, f"There is no batch with id “{batch_id}”.")
        try:
            contract = load_contract(f"{b.contract_name}@v{b.contract_version}")
        except (FileNotFoundError, ValueError):
            contract = None
        grid = fixes.fix_grid(eng, batch_id, contract)
        latest = max(contract_store.versions_of(b.contract_name) or [b.contract_version])
        return render(request, "fix.html", fix_context(b, grid, f"{b.contract_name}@v{latest}"))

    @app.post("/batches/{batch_id}/fix/validate", response_class=HTMLResponse)
    async def fix_validate(batch_id: str, request: Request):
        b = get_batch(eng, batch_id)
        if b is None:
            return HTMLResponse("", status_code=404)
        form = await request.form()
        headers, rows = parse_grid(form)
        ref = str(form.get("contract") or b.contract_name)
        contract = load_contract(ref)
        kept = [r for r in rows if r["include"]]
        check = await run_in_threadpool(
            fixes.revalidate, eng, contract, headers, [r["values"] for r in kept]
        )
        # check["errors"] is keyed by position among the *included* rows
        position = {id(r): i for i, r in enumerate(kept)}
        grid_rows = [
            {**r, "errors": check["errors"].get(position[id(r)], {}) if r["include"] else {}}
            for r in rows
        ]
        grid = {"headers": headers, "rows": grid_rows, "total": len(rows), "truncated": False}
        return render(request, "_fix_grid.html", {**fix_context(b, grid, ref, check)})

    @app.post("/batches/{batch_id}/fix/submit")
    async def fix_submit(batch_id: str, request: Request, background: BackgroundTasks):
        b = get_batch(eng, batch_id)
        if b is None:
            return RedirectResponse("/", status_code=303)
        form = await request.form()
        headers, rows = parse_grid(form)
        ref = str(form.get("contract") or b.contract_name)
        kept = [r["values"] for r in rows if r["include"]]
        if not kept:
            return _to(f"/batches/{batch_id}/fix", error="Select at least one row to resubmit.")
        n = (
            sum(1 for x in Path(settings.upload_dir).glob(f"*/{Path(b.file_name).stem}.fixed-*"))
            + 1
        )
        name = f"{Path(b.file_name).stem}.fixed-{n}.csv"
        path = fixes.write_fixed_file(headers, kept, _tmp(name))
        try:
            submitted = submit_upload(path, ref, engine=eng, file_name=name)
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)
        if submitted["skipped"]:
            return RedirectResponse(f"/batches/{submitted['batch_id']}", status_code=303)
        background.add_task(execute_batch, eng, submitted["batch_id"])
        return RedirectResponse(
            f"/batches/{submitted['batch_id']}?fixed_from={batch_id}",
            status_code=303,
            background=background,
        )

    # ---- dry-run validation ---------------------------------------------------------
    @app.post("/validate")
    async def validate_upload(request: Request, background: BackgroundTasks):
        form = await request.form()
        upload = form.get("file")
        ref = str(form.get("contract") or "")
        if upload is None or not getattr(upload, "filename", "") or not ref:
            return _to("/upload", error="Choose a file and a contract to validate.")
        tmp = await spool_upload(upload)
        try:
            started = dryrun.start_validation(tmp, ref, file_name=upload.filename)
        except (FileNotFoundError, ValueError) as e:
            return _to("/upload", contract=ref, error=str(e))
        finally:
            tmp.unlink(missing_ok=True)
        background.add_task(dryrun.run_validation, started["id"], engine=eng)
        return RedirectResponse(
            f"/validations/{started['id']}", status_code=303, background=background
        )

    @app.get("/validations", response_class=HTMLResponse)
    def validations_page(request: Request, contract: str = "", status: str = ""):
        rows = dryrun.recent(200, contract=contract or None, status=status or None)
        ctx = {
            "rows": rows,
            "contract": contract,
            "status": status,
            "contracts": contract_choices(),
        }
        return render(request, "validations.html", ctx)

    @app.get("/validations/{vid}", response_class=HTMLResponse)
    def validation_page(vid: str, request: Request):
        try:
            v = dryrun.load_validation(vid)
        except FileNotFoundError:
            return not_found(request, "That validation result is not available.")
        running = v["status"] == "running"
        prog = progress.snapshot(
            v["total_rows"], v.get("expected_rows"), v["created_at"], not running
        )
        return render(request, "validation.html", {"v": v, "live": running, "prog": prog})

    @app.get("/validations/{vid}/report.csv")
    def validation_report(vid: str, request: Request):
        try:
            v = dryrun.load_validation(vid)
        except FileNotFoundError:
            return not_found(request, "That validation result is not available.")
        originals = exports.original_headers([{"original": r["original"]} for r in v["records"]])
        headers = ["row_number", "reason_code", "reason", "column", "value", *originals]
        rows = (
            [
                r["row_number"],
                r["reason_code"],
                r["reason_message"],
                r["column"],
                r["value"],
                *(r["original"].get(h) for h in originals),
            ]
            for r in v["records"]
        )
        path = exports.write_rows(
            headers, rows, "csv", _tmp(f"{Path(v['file_name']).stem}.validation.csv")
        )
        return _download(path, path.name, "csv")

    @app.post("/validations/{vid}/ingest")
    def validation_ingest(vid: str, background: BackgroundTasks):
        try:
            v = dryrun.load_validation(vid)
        except FileNotFoundError:
            return RedirectResponse("/upload", status_code=303)
        submitted = submit_upload(
            dryrun.stored_file(v), v["contract_ref"], engine=eng, file_name=v["file_name"]
        )
        if submitted["skipped"]:
            return _to("/upload", uploaded=submitted["batch_id"], skipped=1)
        background.add_task(execute_batch, eng, submitted["batch_id"])
        return RedirectResponse(
            f"/batches/{submitted['batch_id']}", status_code=303, background=background
        )

    # ---- tables ---------------------------------------------------------------------
    def table_engine(entry: dict):
        """The database a catalogue entry lives in: the app database or a destination."""
        return connections.engine_for(eng, entry["connection"])

    def unavailable(request: Request, entry: dict, e: Exception):
        detail = f"Cannot reach {entry['connection'] or 'the app database'}: {_reason(e)}"
        return render(
            request, "error.html", {"title": "Database unavailable", "detail": detail}, 502
        )

    def loads_for(entry: dict) -> list[dict]:
        with Session(eng) as s:
            rows = (
                s.execute(
                    select(Batch)
                    .where(Batch.contract_name.in_(entry["contracts"]))
                    .order_by(Batch.created_at.desc())
                    .limit(200)
                )
                .scalars()
                .all()
            )
        return [batch_out(b) for b in rows]

    @app.get("/tables", response_class=HTMLResponse)
    def tables_page(request: Request):
        rows = []
        for entry in tables.target_tables().values():
            try:
                engine = table_engine(entry)
                exists = tables.exists(engine, entry["table"])
                rows.append(
                    {**entry, "stats": tables.stats(engine, entry["table"]) if exists else None}
                )
            except Exception as e:
                rows.append({**entry, "stats": None, "error": _reason(e)[:160]})
        return render(request, "tables.html", {"tables": rows, "t": None})

    @app.get("/tables/{key}", response_class=HTMLResponse)
    def table_page(
        key: str,
        request: Request,
        tab: str = "data",
        q: str | None = None,
        batch: str | None = None,
        sort: str | None = None,
        desc: int = 0,
        page: int = 1,
    ):
        entry = tables.target_tables().get(key)
        if entry is None:
            return not_found(request, f"“{key}” is not a target table of any contract.")
        name = entry["table"]
        ctx = {
            "t": entry,
            "tab": tab if tab in ("data", "schema", "loads") else "data",
            "tables": None,
        }
        ctx.update(
            q=q or "", batch=batch or "", sort=sort or "", desc=bool(desc), formats=exports.FORMATS
        )
        try:
            engine = table_engine(entry)
            if not tables.exists(engine, name):
                ctx["missing"] = True
                return render(request, "tables.html", ctx)
            ctx["stats"] = tables.stats(engine, name)
            if ctx["tab"] == "data":
                ctx["data"] = tables.page(
                    engine, name, page=max(page, 1), batch_id=batch, q=q, sort=sort, desc=bool(desc)
                )
            elif ctx["tab"] == "schema":
                ctx["schema"] = tables.schema(engine, name)
        except Exception as e:
            return unavailable(request, entry, e)
        ctx["loads"] = loads_for(entry)
        return render(request, "tables.html", ctx)

    @app.get("/tables/{key}/export")
    def table_export(
        key: str, request: Request, format: str = "csv", batch: str | None = None, system: int = 0
    ):
        entry = tables.target_tables().get(key)
        if entry is None:
            return not_found(request, f"“{key}” is not a target table of any contract.")
        fmt = format if format in exports.FORMATS else "csv"
        batch_ids = [b for b in (batch or "").split(",") if b] or None
        try:
            engine = table_engine(entry)
            if not tables.exists(engine, entry["table"]):
                return not_found(request, f"“{key}” has not been created yet.")
            headers, rows = tables.iter_export(
                engine, entry["table"], batch_ids=batch_ids, include_system=bool(system)
            )
            path = exports.write_rows(headers, rows, fmt, _tmp(f"{entry['table']}.{fmt}"))
        except Exception as e:
            return unavailable(request, entry, e)
        return _download(path, path.name, fmt)

    # ---- settings / system status -----------------------------------------------------
    def folder_usage(path: Path) -> dict:
        files = [p for p in path.rglob("*") if p.is_file()] if path.exists() else []
        return {
            "path": str(path.resolve()),
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
        }

    @app.get("/admin/system", response_class=HTMLResponse)
    def settings_page(request: Request):
        import os
        import platform
        from importlib.metadata import PackageNotFoundError, version

        from ingest.core import secrets as secrets_mod
        from ingest.core.contracts import list_contracts
        from ingest.core.pipeline import EXCEL_MAX_BYTES

        try:
            app_version = version("ingest-platform")
        except PackageNotFoundError:
            app_version = "unknown"
        db = {"url": eng.url.render_as_string(hide_password=True), "dialect": eng.dialect.name}
        try:
            with eng.connect() as conn:
                db["revision"] = conn.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar()
                for table in ("batches", "rejected_records", "connections"):
                    db[table] = conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar()
            db["ok"] = True
        except Exception as e:
            db.update(ok=False, error=_reason(e))
        contracts_dir = Path(settings.contracts_dir)
        key_file = secrets_mod._key_file()
        ctx = {
            "app_version": app_version,
            "python": platform.python_version(),
            "db": db,
            "uploads": folder_usage(Path(settings.upload_dir)),
            "validations": folder_usage(dryrun.validations_dir()),
            "contracts_dir": str(contracts_dir.resolve()),
            "contracts_writable": os.access(contracts_dir, os.W_OK),
            "contract_versions": len(list_contracts()),
            "secret_source": "INGEST_SECRET_KEY"
            if os.environ.get("INGEST_SECRET_KEY")
            else ("key file" if key_file.exists() else "not created yet"),
            "key_file": str(key_file.resolve()),
            "query_role": reader_role(),
            "drivers": {
                connections.KINDS[k]["label"]: connections.driver_available(k)
                for k in connections.KINDS
            },
            "limits": [
                ("Rows per chunk", f"{settings.chunk_size:,}"),
                ("Largest Excel file", f"{EXCEL_MAX_BYTES // (1024 * 1024)} MB"),
                ("Rows shown in the SQL editor", f"{query.MAX_ROWS:,}"),
                ("Rows in a query export", "100,000"),
                ("Query time limit", f"{query.TIMEOUT_S} s"),
                ("Rows in the fix grid", f"{fixes.MAX_FIX_ROWS:,}"),
            ],
        }
        return render(request, "admin_system.html", {**ctx, "section": "system"})

    # ---- SQL editor ---------------------------------------------------------------------
    DIALECT_MODE = {
        "postgresql": "text/x-pgsql",
        "mssql": "text/x-mssql",
        "sqlite": "text/x-sqlite",
    }

    def is_app_db(engine, db: str) -> bool:
        return not db or app_tables.same_database(engine, eng)

    def reader_role() -> str:
        try:
            with eng.connect() as conn:
                return query.reader_role(conn)
        except Exception:
            return ""

    def schema_tree(engine, db: str) -> list[dict]:
        insp = inspect(engine)
        known = {
            t["table"] for t in tables.target_tables().values() if (t["connection"] or "") == db
        }
        app_db = is_app_db(engine, db)
        out = []
        for t in sorted(insp.get_table_names()):
            if app_db and t in app_tables.APP_TABLES:
                continue
            kind = "target" if t in known else "system" if (app_db and t in SYSTEM_TABLES) else "other"
            keys = set(insp.get_pk_constraint(t).get("constrained_columns") or [])
            try:
                for u in insp.get_unique_constraints(t):
                    keys |= set(u.get("column_names") or [])
            except NotImplementedError:  # SQL Server: reflected as unique indexes instead
                for ix in insp.get_indexes(t):
                    if ix.get("unique"):
                        keys |= set(ix.get("column_names") or [])
            columns = [
                {"name": c["name"], "type": tables.type_name(c["type"]), "key": c["name"] in keys}
                for c in insp.get_columns(t)
            ]
            out.append({"name": t, "kind": kind, "columns": columns})
        order = {"target": 0, "system": 1, "other": 2}
        return sorted(out, key=lambda d: (order[d["kind"]], d["name"]))

    def run(sql: str, db: str, **kw) -> dict:
        """Run a read query. The app's own tables are refused by name here, and on
        PostgreSQL by the database too (the reader role has no grants on them)."""
        engine = connections.engine_for(eng, db)
        app_db = is_app_db(engine, db)
        if app_db:
            app_tables.refuse(sql)
        return query.run_query(engine, sql, app_db=app_db, **kw)

    def json_cell(v):
        if v is None or isinstance(v, bool | int | float | str):
            return v
        if isinstance(v, bytes | bytearray | memoryview):
            return "0x" + bytes(v).hex()
        return pages_cell(v)

    @app.get("/query", response_class=HTMLResponse)
    def query_page(
        request: Request, sql: str = "", run_now: int = Query(0, alias="run"), db: str = ""
    ):
        known = {c.name for c in connections.list_all(eng)}
        ctx = {
            "sql": sql,
            "db": db if db in known else "",
            "autorun": bool(sql and run_now),
            "connections": connections.list_all(eng),
            "kinds": connections.KINDS,
        }
        return render(request, "query.html", ctx)

    @app.get("/api/sql/schema")
    def sql_schema(db: str = ""):
        try:
            engine = connections.engine_for(eng, db)
            dialect = engine.dialect.name
            return {
                "ok": True,
                "db": db,
                "dialect": dialect,
                "mode": DIALECT_MODE.get(dialect, "text/x-sql"),
                "tables": schema_tree(engine, db),
            }
        except Exception as e:
            return JSONResponse({"ok": False, "db": db, "error": _reason(e)}, status_code=502)

    @app.post("/api/sql/run")
    async def sql_run(request: Request):
        """Run one read statement. Errors come back as JSON, never as a page."""
        body = await request.json()
        sql, db = str(body.get("sql") or ""), str(body.get("db") or "")
        try:
            result = await run_in_threadpool(run, sql, db)
        except Exception as e:
            return JSONResponse({"ok": False, "error": _reason(e)}, status_code=400)
        rows = [[json_cell(v) for v in r] for r in result["rows"]]
        return {
            "ok": True,
            "columns": result["columns"],
            "rows": rows,
            "row_count": len(rows),
            "truncated": result["truncated"],
            "elapsed_ms": result["elapsed_ms"],
            "statement": result["sql"],
            "limit": query.MAX_ROWS,
        }

    @app.get("/query/export")
    def query_export(request: Request, sql: str, format: str = "csv", db: str = ""):
        fmt = format if format in exports.FORMATS else "csv"
        try:
            result = run(sql, db, limit=100_000, timeout_s=60)
        except Exception as e:
            detail = f"The query could not be exported: {_reason(e)}"
            return render(request, "error.html", {"title": "Export failed", "detail": detail}, 400)
        name = f"query-{uuid.uuid4().hex[:6]}.{fmt}"
        path = exports.write_rows(result["columns"], result["rows"], fmt, _tmp(name))
        return _download(path, path.name, fmt)
