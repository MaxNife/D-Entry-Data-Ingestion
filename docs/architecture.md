# Architecture

One Python package with a web-free core library; the CLI and the FastAPI app
are thin wrappers that call the same pipeline function (`run_pipeline`).

```
CSV/Excel ──▶ core.pipeline ──┬──▶ validate (pandera, per chunk)
                              ├──▶ load (upsert accepted + rejections, 1 tx/chunk)
                              └──▶ finalize (counters, status)
        ▲ CLI (ingest) and API + dashboard both call into core.pipeline.
```

## Components

| Component | Responsibility |
| --- | --- |
| `core.hashing` | Streaming SHA-256 of an uploaded file |
| `core.contracts` | Load, validate and hash versioned YAML contracts |
| `core.validation` | Validate one chunk; accepted rows plus structured rejections |
| `core.loader` | Upsert accepted rows and write rejections, one transaction per chunk |
| `core.pipeline` | Orchestrate: hash, idempotency check, read in chunks, validate, load, finalize |
| `core.reports` | Reject report CSV from `rejected_records` |
| `ingest.rules` | Custom contract rule functions (`check: custom` + `function:`) |
| `db` | SQLAlchemy models, session handling, Alembic migrations |
| `api` | FastAPI JSON routes plus the Jinja2/HTMX dashboard |
| `cli` | `ingest` command (Typer) |

Rules: `core` never imports from `api` or `cli`. All configuration comes from
environment variables (`INGEST_*`, see `.env.example`).

## Data flow

1. Upload (API background task or CLI) → file hashed (SHA-256) and stored at
   `data/uploads/<sha256>/<original_name>`.
2. Idempotency: `(file_sha256, contract_name, contract_version)` lookup. A
   re-upload of a finished batch returns it with `skipped: true`.
3. Structural check on the header; on mismatch the batch is `failed` with a
   `failure_reason` and nothing loads.
4. The file streams in chunks (default 10,000 rows; CSV via `read_csv(chunksize)`;
   Excel capped at 50 MB). Each chunk is validated with pandera `lazy=True` so
   every failure is collected, then loaded in its own transaction: accepted rows
   are upserted on `row_key`, rejections go to `rejected_records`, and a
   `batch_chunks` row records the outcome. A chunk-level database error rolls
   back that chunk, records its rows as `LOAD_ERROR`, and continues.
5. Finalize writes counters; status is `completed` (zero rejections), `partial`
   (at least one rejection, even if all rows were rejected) or `failed`.

## Idempotency layers

File hash (+ unique constraint, race-safe), deterministic `row_key` upserts,
rejections/chunks replaced (not appended) on reprocess, and the `batches`
audit row with `retry_attempts`.
