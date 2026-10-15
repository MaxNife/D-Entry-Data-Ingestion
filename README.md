# Ingest Platform

Batch file ingestion front door for an internal ops/data team: drop a CSV/Excel
extract, have it validated against a versioned schema contract, land only the
trusted rows in Postgres — and keep every rejected row with queryable reasons.
Partial success, idempotent reprocessing, and an audit trail are built in.

## Quick start

```bash
docker compose up            # app on :8000, Postgres on :5432
alembic upgrade head         # control tables (or let the app create them)
ingest contracts validate    # lint every contract
ingest upload file.csv --contract paysim_transactions
```

Local development without Docker:

```bash
uv sync
uv run uvicorn ingest.api.main:app --reload   # dashboard at http://127.0.0.1:8000
```

Demo data comes from public Kaggle datasets (PaySim mobile-money transactions,
USA Banking Transactions, Bank_Dataset for reference tables) — verify each
dataset's license on its Kaggle page. Datasets are never committed; see
`data/` (git-ignored). `scripts/make_dirty_sample.py` turns a clean sample
into a predictably dirty one for demos and manual checks.

## Design decisions and trade-offs

- **Idempotency over file names.** Identity is the SHA-256 of content, enforced
  by a unique constraint, so the same file can never create two batches — even
  under concurrent uploads. Rows upsert on a deterministic `row_key`.
- **Partial success by default.** Each chunk loads in its own transaction, so
  good rows commit even when other rows fail. A chunk-level database error
  becomes per-row `LOAD_ERROR` rejections instead of a lost batch.
- **Queryable rejections, readable exports.** Storage is one row per
  (record, reason) for filtering by code/column; the CSV export aggregates back
  to one row per record with joined codes and messages plus the full original
  row, so nobody cross-references.
- **Contracts as code.** Validation lives in versioned YAML (`contracts/<name>/v<N>.yaml`),
  never in hardcoded checks. Fixing a rule means a new version; reprocessing a
  batch under the new version lands the previously rejected rows without
  duplicating anything already loaded.
- **One pipeline function.** The CLI and the API call the same
  `run_pipeline`; the dashboard is server-rendered (Jinja2 + HTMX), no JS build.

## Known limitations

- Excel files read fully into memory; files over 50 MB are rejected — convert
  to CSV first.
- No message queue: API uploads run as FastAPI background tasks in-process.
- Memory stays flat on large CSVs (chunked streaming); peak memory on the full
  PaySim file: _to be recorded after the first full-scale run (see test_logbook)._
- Out of scope for v1: end-user self-correction UI, multi-tenancy, streaming,
  resumable uploads, RBAC, scheduled ingestion.
