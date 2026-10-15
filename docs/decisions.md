# Decisions (spec §23: proposed items we settle, recorded here)

- 2026-10-15: API runs pipeline in FastAPI BackgroundTask, no queue in v1.
- 2026-10-15: Raw files kept at `data/uploads/<sha256>/<name>`.
- 2026-10-15: Batch with all rows rejected stays `partial` with accepted=0 (no separate `rejected` status).
- 2026-10-15: Reprocess under newer version updates existing batch row (no child batch).
- 2026-10-15: Excel read fully into memory; reject files >50MB with clear error, advise CSV.
- 2026-10-15: Dashboard is Jinja2 + HTMX, no separate frontend.
- 2026-10-15: Cross-chunk dupes resolved by DB upsert only; no in-memory key set.
- 2026-10-15: `uv` for env/deps (per user instruction), SQLite default locally, Postgres via `INGEST_DATABASE_URL` / Compose.
