# Decisions (spec §23: proposed items we settle, recorded here)

- 2026-10-15: API runs pipeline in FastAPI BackgroundTask, no queue in v1.
- 2026-10-15: Raw files kept at `data/uploads/<sha256>/<name>`.
- 2026-10-15: Batch with all rows rejected stays `partial` with accepted=0 (no separate `rejected` status).
- 2026-10-15: Reprocess under newer version updates existing batch row (no child batch).
- 2026-10-15: Excel read fully into memory; reject files >50MB with clear error, advise CSV.
- 2026-10-15: Dashboard is Jinja2 + HTMX, no separate frontend.
- 2026-10-15: Cross-chunk dupes resolved by DB upsert only; no in-memory key set.
- 2026-10-15: `uv` for env/deps (per user instruction), SQLite default locally, Postgres via `INGEST_DATABASE_URL` / Compose.
- 2026-10-15: Reprocess defaults to the latest contract version (spec §8), not the batch's own version.
- 2026-10-15: Missing upload file is 422 (within the 4xx error-body envelope).
- 2026-10-15: `exists_in` failures report the rule's own `code` (tests/README); a cell with
  MISSING_REQUIRED/TYPE_MISMATCH reports only that root cause.
- 2026-10-15: Unloadable custom-rule functions are skipped with a warning, never crash the pipeline.
- 2026-10-15: `txn_test` v2 = v1 plus REFUND in the Type set (the minimal contract fix the reprocess demo needs).
- 2026-10-15: Root `contracts/txn_test/v1.yaml` uses fixture-style headers (TxnId/…) to match every
  fixture CSV, the report header test and the contracts text test.
- 2026-10-15: Metrics key is `failed_batches_today`; contracts list returns a bare JSON list.
- 2026-10-15: Target tables are reflected before insert so real column types drive binding
  (e.g. `test_transactions.batch_id uuid`); accepted cells are coerced to Python types
  (`int/float/date/bool`) rather than bound as text. Fixes `LOAD_ERROR` on every chunk.
- 2026-10-15 (resolved by test author): suite re-delivered complete — full
  `harness.py`/`expected.py`, eight fixtures cleaned of the `</content>`
  trailer (plus stray BOMs), txn fixtures realigned to `TxnId/CustomerId/
  TxnDate` with 4-digit ids. Eve,85,A stays rejected (unit test upheld);
  `TestDirtyFile` 7/5 holds because the row-8 duplicate is cross-chunk and
  collapses via upsert (test rewritten accordingly). Duplicate semantics:
  within-chunk code, cross-chunk upsert. `missing_header.csv` is txn-style;
  score-style header tests use their own tmp file.
- 2026-10-15 (disputed, superceded by the ruling above): ~~`test_pipeline.TestDirtyFile`
  expects `Eve,85,A` accepted while `test_validation_unit` expects it rejected; and
  the row-8 duplicate sits in a different chunk from its original, so
  `DUPLICATE_KEY_IN_CHUNK` cannot be produced by within-chunk detection. Logged as
  disputed; behaviour unchanged pending a ruling.~~
