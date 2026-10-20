# Acceptance test suite

This suite checks the whole platform, end to end, against the System Design Spec.
It was written before the implementation, from the spec alone. It covers the core
pipeline, Postgres storage, the HTTP API, the `ingest` CLI, and the ops dashboard
in a real browser.

The build agent must make every test pass and record the result of every test in
`test_logbook.md` at the repo root. The rules are in spec section 15 and repeated
below.

## Running it

Prerequisites:

1. A Postgres you can create a database on. The suite uses
   `INGEST_TEST_DATABASE_URL`, which defaults to
   `postgresql+psycopg://ingest:ingest@localhost:5432/ingest_test`. It creates the
   database if it can. The database name must contain `test`, because the suite
   drops and recreates the `public` schema at the start of every run.
2. The package installed with its dev dependencies. That means pytest, httpx,
   openpyxl, pytest-playwright and a Postgres driver, plus the `ingest` console
   script on PATH.
3. A Playwright browser: `playwright install chromium`.

Commands:

```bash
docker compose up -d db            # or any reachable Postgres
pytest                             # everything, including the browser tests
pytest -m "not e2e"                # quick loop without the browser
pytest -m e2e --headed             # watch the dashboard tests run
```

To run the browser tests against a stack you started yourself (for example a
separate frontend), set `INGEST_E2E_BASE_URL`. That origin must serve both the
dashboard routes and `/api`.

## Layout

| Path | What it covers | Spec |
| --- | --- | --- |
| `unit/test_validation_unit.py` | `validate_chunk`: shape, row numbering with `row_offset`, every built-in reason code, custom rules, lazy collection, no mutation | s7 |
| `unit/test_architecture.py` | `core` never imports `api`, `cli`, or web/CLI frameworks | s3 |
| `integration/test_pipeline.py` | `run_pipeline` against Postgres: statuses, counters, rejections, original rows, chunks, file-level failures, Excel, contract resolution and hashing | s4–s7 |
| `integration/test_idempotency.py` | File-hash identity, row upserts (`update` and `skip`), concurrent uploads | s8 |
| `integration/test_load_errors.py` | A real DB error in one chunk (a Postgres trigger, not a mock) → `LOAD_ERROR`, other chunks commit | s6, s14 |
| `integration/test_reprocess.py` | Reprocess after a contract fix, idempotent reprocess, replaced rejections, refusals | s6, s8, s14 |
| `integration/test_api_*.py` | Every `/api` route: upload, list/search/paginate, rejections, summary, cross-batch audit, contracts, metrics, health, error body | s9 |
| `integration/test_reject_report.py` | Reject CSV: name, column order, one row per record, joined reasons, original values exactly as read | s11 |
| `integration/test_cli.py` | `ingest upload / batches / reprocess / contracts validate` as real subprocesses | s9 |
| `e2e/test_dashboard.py` | Playwright: home metrics, history, badges, search and filters, upload panel, detail, click-to-drill, download, reprocess, audit search, empty and error states | s10 |
| `test_repo_deliverables.py` | Required files, CI workflow, README sections, git hygiene, project contracts validate, ruff | s12–s14 |
| `fixtures/` | Hand-made CSVs, test contracts (`txn_test` v1/v2, `score_test` v1), `custom_rules.py` | s12 |
| `expected.py` | Hand-derived ground truth for every fixture (which row fails with which code) | |
| `harness.py` | **The only place** where names the spec leaves open are assumed | |

## Rules for the build agent

1. **Do not edit, skip, xfail, delete or weaken any test file, `expected.py`, or
   anything under `fixtures/`.** That includes changing an assertion, loosening a
   tolerance, adding `pytest.skip`, or filtering tests out by marker in CI.
2. **`harness.py` is the only file you may adapt.** It exists so that a different
   (but spec-compatible) name does not force a test edit: an import path, a
   function name, an env var name, a JSON envelope. Each adaptation must keep the
   behaviour the tests check, and must be logged in `test_logbook.md` under
   "Harness adaptations".
3. **If you believe a test is wrong** (unreasonable, illogical, contradicting the
   spec, or not suited to a decision recorded in `docs/decisions.md`), write it up
   under "Disputed tests" in `test_logbook.md`. Give the test ID, quote the spec,
   explain why it's wrong, and say what you would change it to. Leave the test
   as it is. A disputed test stays failing until a human rules on it.
4. **Keep `test_logbook.md` current.** After every full run, update the status of
   every test and append a row to the run history.

## What the suite assumes

These are the choices the suite makes where the spec leaves something open. All
of them can be changed in `harness.py`, except the ones marked **fixed**, which
the spec itself requires.

- **Configuration via env vars:** `INGEST_DATABASE_URL`, `INGEST_CONTRACTS_DIR`
  (default `contracts/`), `INGEST_UPLOAD_DIR` (default `data/uploads`) and
  `INGEST_CHUNK_SIZE` (default 10000). The suite runs with chunk size **5**.
  Alembic's `env.py` must read `INGEST_DATABASE_URL`.
- **Database is Postgres (fixed, spec s13).** Integration tests use Postgres-only
  features (uuid casts, a plpgsql trigger). A SQLite default for local
  development is fine, but the tests run on Postgres.
- **Target tables for the test contracts** (`test_transactions`, `test_scores`)
  and the reference table `ref_customers` are created by the suite, shaped as in
  spec s4. The loader must write into an existing target table. It may also
  create missing tables, but it must not fail when they already exist.
- **Entry points:** `ingest.api.main:app`, `ingest.core.pipeline.run_pipeline`,
  `ingest.core.validation.validate_chunk`,
  `ingest.core.contracts.load_contract(path)`, and the console script `ingest`.
- **`run_pipeline` result:** exposes `batch_id` and `skipped`, as attributes or
  dict keys.
- **List endpoints** return `{"items": [...], "total", "page", "page_size"}`.
- **Errors (fixed, spec s9):** every 4xx response body is
  `{"error": "<code>", "detail": "<message>"}`. That includes FastAPI's own
  validation errors (422), so a custom exception handler is needed.
- **Metrics keys:** `batches_today`, `rows_accepted_today`,
  `rows_rejected_today`, `failed_batches_today`.
- **Custom rules** in a contract: `check: custom` plus
  `function: <dotted.path>`. The function receives the chunk DataFrame and
  returns a boolean Series where `True` means the row passes.
- **`rejected_records.column_name`** may be the contract column name or the
  source header; both are accepted.
- **Rule codes:** an `exists_in` rule reports its own `code` (here
  `CUSTOMER_NOT_FOUND`). Built-in codes are used only for column attributes.
- **Status when every row is rejected:** `partial` or `rejected`; both are
  accepted (spec s14 open decision).

## Dashboard test hooks (`data-testid`)

The browser tests find elements only by these attributes, so markup, styling and
rendering approach are up to you. Each hook below must exist and behave as
described.

| Page | `data-testid` | Notes |
| --- | --- | --- |
| `/` | `metric-batches-today`, `metric-rows-accepted-today`, `metric-rows-rejected-today`, `metric-failed-batches-today` | Card text contains the number |
| `/` | `batch-row` | One per batch, newest first, with `data-batch-id="<uuid>"`. Shows the short id (first 8 characters), file name, `contract@version`, and total/accepted/rejected |
| `/`, `/batches/{id}` | `status-badge` | `data-status="completed\|partial\|failed\|processing\|pending"`. The three final states must look different. A failed badge shows `failure_reason` on hover (`title` or a tooltip). In-flight rows refresh on their own until final |
| `/` | `batch-search` | Text input, applied on Enter |
| `/` | `status-filter`, `contract-filter` | `<select>`. Status option values are status names. Contract option values are `name` or `name@version` |
| `/` | `filter-submit` | Optional. Clicked after changing a filter if present |
| `/`, `/upload` | `upload-file`, `upload-contract`, `upload-submit`, `upload-message` | `upload-message` says "already processed" when the upload was skipped |
| `/batches/{id}` | `counter-total`, `counter-accepted`, `counter-rejected`, `contract-version`, `retry-attempts` | Header counters |
| `/batches/{id}` | `reason-row` | One per reason code, `data-reason-code`. Shows the count and the % of rejected rows. Clicking it filters the records table |
| `/batches/{id}` | `rejection-row` | `data-row-number`. Shows reason code, message and offending value. Contains `original-row-toggle`, which reveals the original row |
| `/batches/{id}` | `download-report` | Downloads `reject_report_<batch_id>.csv` |
| `/batches/{id}` | `reprocess-button` | Enabled for partial/failed only; missing or disabled for completed. Asks for confirmation, either a native `confirm()` or a `reprocess-confirm` button |
| `/batches/{id}` | `reprocess-result` | After a reprocess, shows the before and after counters |
| `/rejections` | `audit-reason-code`, `audit-from`, `audit-to`, `audit-submit` | Reason code is an input or a select. Dates accept `YYYY-MM-DD` |
| `/rejections` | `audit-row` | One per affected batch, `data-batch-id`. Shows the count and links to `/batches/{id}` |
| any table | `empty-state` | Shown when a table has no rows |
| `/batches/{unknown}` | `error-state` | Or an HTTP 404 response |

## Not automated

The suite does not check these. Record them by hand in the logbook:

- Memory stays flat on the full PaySim file (spec s14). Record the peak memory in
  the README and the logbook.
- `docker compose up` from a fresh clone (spec s14).
- Loading states on tables. These are too timing-dependent to assert reliably.
