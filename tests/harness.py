"""Test harness: shared helpers, DB wrapper, and environment setup for the suite.

This module is imported by conftest.py and individual test files. It provides:
- Environment configuration (paths, DB URL, upload dir, chunk size)
- Contract loading from YAML
- validate_chunk / read_chunk wrappers (delegating to the real implementation)
- HTTP helpers: fixture, upload, upload_and_wait, wait_for_batch, reprocess
- Response helpers: page_items, page_total, summary_counts, contract_versions,
  assert_error_body
- CLI helper: cli_command
- Hashing helper: sha256_file (delegating to the real implementation)
- Dashboard/API constants: METRIC_KEYS, ENV_CONTRACTS_DIR, FIXTURE_CONTRACTS,
  UVICORN_APP, UVICORN_FACTORY
- A Db helper with dict-style rows for direct SQL assertions

Frozen contract: the build agent must NOT edit this file (spec s15,
tests/README.md rules). Behavioural assumptions the spec leaves open live
here; each is documented next to the constant it affects.

Assumed names (change only with user approval, logged in test_logbook.md):
- FastAPI app at ingest.api.main:app (factory: ingest.api.main.create_app)
- Pipeline entry: ingest.core.pipeline.run_pipeline
- Validation entry: ingest.core.validation.validate_chunk
- Contracts entry: ingest.core.contracts.load_contract
- Console script: `ingest` (fallback: python -m ingest)
- Suite chunk size: 5 (spec default is 10000; 5 keeps multi-chunk tests fast)
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

# Ensure the tests directory is importable (for `expected` module)
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# Ensure src is importable
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# ---- names the spec leaves open (see module docstring) ----------------------
#: Env var the app reads for the contracts directory (matches
#: Settings(env_prefix="INGEST_") + field `contracts_dir` in
#: src/ingest/config.py).
ENV_CONTRACTS_DIR = "INGEST_CONTRACTS_DIR"

#: Copy of the test contracts used for the broken-contract CLI test.
FIXTURE_CONTRACTS = FIXTURES_DIR / "contracts"

#: Uvicorn target for the live-server e2e fixture (module:app, no factory).
UVICORN_APP = "ingest.api.main:app"
UVICORN_FACTORY = False

#: Metric keys the dashboard/API summary uses (spec s10 cards). Maps the
#: short test name to the JSON key returned by GET /api/metrics/summary.
METRIC_KEYS = {
    "batches": "batches_today",
    "accepted": "rows_accepted_today",
    "rejected": "rows_rejected_today",
    "failed": "failed_batches_today",
}

TERMINAL_STATUSES = {"completed", "partial", "failed"}


def configure_process_environment() -> None:
    """Set env vars needed before any app module is imported."""
    os.environ.setdefault("INGEST_CHUNK_SIZE", "5")  # small chunks for tests
    os.environ.setdefault("INGEST_UPLOAD_DIR", str(REPO_ROOT / "data" / "test_uploads"))
    os.environ.setdefault("INGEST_CONTRACTS_DIR", str(REPO_ROOT / "contracts"))
    # Test DB must contain 'test' in the name (conftest enforces this)
    if not os.environ.get("INGEST_TEST_DATABASE_URL"):
        os.environ.setdefault(
            "INGEST_TEST_DATABASE_URL",
            "postgresql+psycopg://postgres:postgres@localhost:5432/ingest_test",
        )
    # The app's own DB URL points at the test DB during tests
    os.environ.setdefault(
        "INGEST_DATABASE_URL",
        os.environ["INGEST_TEST_DATABASE_URL"],
    )


# Call it at import time so conftest can rely on these being set
configure_process_environment()

UPLOAD_DIR = Path(os.environ.get("INGEST_UPLOAD_DIR", REPO_ROOT / "data" / "test_uploads"))
TEST_DATABASE_URL = os.environ["INGEST_TEST_DATABASE_URL"]


def subprocess_env(**overrides: str) -> dict[str, str]:
    """Environment dict for subprocess calls (CLI tests, alembic, uvicorn).

    Accepts overrides, e.g. subprocess_env(INGEST_CONTRACTS_DIR=str(tmp)).
    """
    env = os.environ.copy()
    env["INGEST_DATABASE_URL"] = TEST_DATABASE_URL
    env["INGEST_TEST_DATABASE_URL"] = TEST_DATABASE_URL
    env["INGEST_UPLOAD_DIR"] = str(UPLOAD_DIR)
    env["INGEST_CONTRACTS_DIR"] = os.environ.get("INGEST_CONTRACTS_DIR", str(REPO_ROOT / "contracts"))
    env["INGEST_CHUNK_SIZE"] = os.environ.get("INGEST_CHUNK_SIZE", "5")
    env["PYTHONPATH"] = str(SRC_DIR)
    env.update(overrides)
    return env


def get_app():
    """Import and return the FastAPI application (spec s9 entry point)."""
    import ingest.api.main as api_main

    return api_main.app


def load_contract(name: str, version: int):
    """Load a contract via the core.contracts module (spec s5 entry point)."""
    from ingest.core.contracts import load_contract as _load

    return _load(name, version)


def read_chunk(csv_text: str) -> pd.DataFrame:
    """Read a CSV string into a DataFrame (simulates one chunk from the pipeline)."""
    return pd.read_csv(io.StringIO(csv_text), dtype=str)


def validate_chunk(df: pd.DataFrame, contract, row_offset: int):
    """Delegate to core.validation.validate_chunk (spec s7 entry point)."""
    from ingest.core.validation import validate_chunk as _validate

    return _validate(df, contract, row_offset)


def column_is(actual: str | None, contract_name: str, source_name: str) -> bool:
    """Check that a column_name field matches either the contract or source name."""
    return actual in (contract_name, source_name)


def sha256_file(path: str | Path) -> str:
    """Delegate to core.hashing.sha256_file (spec s3 entry point)."""
    from ingest.core.hashing import sha256_file as _sha256_file

    return _sha256_file(path)


def cli_command() -> list[str]:
    """Console-script invocation for the `ingest` CLI (spec s9)."""
    if shutil.which("ingest"):
        return ["ingest"]
    return [sys.executable, "-m", "ingest"]


def fixture(name: str) -> Path:
    """Path to a file under tests/fixtures/."""
    path = FIXTURES_DIR / name
    assert path.exists(), f"fixture {name!r} is missing under {FIXTURES_DIR}"
    return path


# ---- HTTP helpers (spec s9) --------------------------------------------------
def upload(api, path: str | Path, contract: str, filename: str | None = None):
    """POST one file to /api/batches. Returns the raw response."""
    path = Path(path)
    with open(path, "rb") as f:
        return api.post(
            "/api/batches",
            files={"file": (filename or path.name, f, "text/csv")},
            data={"contract": contract},
        )


def wait_for_batch(api, batch_id: str, retry_attempts: int | None = None, timeout: float = 60.0) -> dict:
    """Poll GET /api/batches/{id} until the batch reaches a terminal status.

    When retry_attempts is given, also wait until the batch has been
    reprocessed that many times (for reprocess tests).
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        resp = api.get(f"/api/batches/{batch_id}")
        assert resp.status_code == 200, f"GET batch {batch_id}: {resp.status_code} {resp.text}"
        last = resp.json()
        terminal = last.get("status") in TERMINAL_STATUSES
        retries_ok = retry_attempts is None or last.get("retry_attempts", 0) >= retry_attempts
        if terminal and retries_ok:
            return last
        if time.monotonic() > deadline:
            raise TimeoutError(f"batch {batch_id} did not finish in {timeout}s (last: {last})")
        time.sleep(0.2)


def upload_and_wait(api, path: str | Path, contract: str, filename: str | None = None) -> dict:
    """Upload a file and wait until background processing finishes."""
    resp = upload(api, path, contract, filename=filename)
    assert resp.status_code in (200, 202), resp.text
    body = resp.json()
    assert set(body) >= {"batch_id", "status", "skipped"}, body
    uuid.UUID(body["batch_id"])
    return wait_for_batch(api, body["batch_id"])


def reprocess(api, batch_id: str, contract: str | None = None):
    """POST /api/batches/{id}/reprocess, optionally under another contract."""
    if contract is None:
        return api.post(f"/api/batches/{batch_id}/reprocess")
    return api.post(f"/api/batches/{batch_id}/reprocess", json={"contract": contract})


# ---- response-shape helpers ---------------------------------------------------
def page_items(envelope) -> list:
    """Items of a paged list envelope {"items", "total", "page", "page_size"}."""
    if isinstance(envelope, list):  # tolerant: a bare list is its own items
        return envelope
    return envelope["items"]


def page_total(envelope) -> int:
    """Total of a paged list envelope."""
    if isinstance(envelope, list):
        return len(envelope)
    return envelope["total"]


def summary_counts(body: dict) -> dict[str, int]:
    """reason_code -> count from GET .../rejections/summary.

    The endpoint returns {"batch_id", "rejected_rows", "reasons":
    [{"reason_code", "count", "pct"}]}.
    """
    if isinstance(body, list):  # tolerant: a bare list of {reason_code, count}
        return {r["reason_code"]: r["count"] for r in body}
    return {r["reason_code"]: r["count"] for r in body.get("reasons", [])}


def contract_versions(body) -> set[tuple[str, int]]:
    """(name, version) pairs from GET /api/contracts."""
    items = body if isinstance(body, list) else body.get("items", body)
    return {(c["name"], c["version"]) for c in items}


def assert_error_body(resp):
    """Every 4xx body is {"error": "<code>", "detail": "<message>"} (spec s9)."""
    body = resp.json()
    assert isinstance(body, dict), body
    assert "error" in body and "detail" in body, f"error body must have error+detail: {body}"
    assert body["error"] and body["detail"], f"error/detail must be non-empty: {body}"
    return body


# ---- DB wrapper -----------------------------------------------------------------
class Row(dict):
    """A result row supporting both dict-style (r["status"]) and
    attribute-style (r.status) access, so pipeline tests and API tests can
    share the same Db helper."""

    __getattr__ = dict.__getitem__

    def __setattr__(self, key, value):
        self[key] = value


class Db:
    """Thin wrapper around a SQLAlchemy engine for test assertions."""

    def __init__(self, engine):
        self.engine = engine

    # -- low-level -----------------------------------------------------------
    def scalar(self, sql: str, params: dict | None = None):
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).scalar()

    def fetchall(self, sql: str, params: dict | None = None) -> list:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()

    def execute(self, sql: str, params: dict | None = None):
        """Run a statement (e.g. CREATE TRIGGER) in its own transaction."""
        from sqlalchemy import text

        with self.engine.begin() as conn:
            return conn.execute(text(sql), params or {})

    def count(self, table: str, where: str = "", params: dict | None = None) -> int:
        sql = f"SELECT count(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return self.scalar(sql, params) or 0

    # -- batches --------------------------------------------------------------
    @staticmethod
    def _row(keys, values) -> Row:
        row = Row(zip(keys, values))
        # Normalise UUID objects to plain strings for test comparisons.
        for k, v in list(row.items()):
            if isinstance(v, uuid.UUID):
                row[k] = str(v)
        return row

    def _select(self, sql: str, params: dict | None = None) -> list[Row]:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            keys = list(result.keys())
            return [self._row(keys, tuple(r)) for r in result.fetchall()]

    def batches(self) -> list[Row]:
        return self._select("SELECT * FROM batches ORDER BY created_at ASC")

    def batch(self, batch_id: str) -> Row | None:
        rows = self._select("SELECT * FROM batches WHERE batch_id = :id", {"id": batch_id})
        return rows[0] if rows else None

    def batch_status(self, batch_id: str) -> str:
        return self.scalar("SELECT status FROM batches WHERE batch_id = :id", {"id": batch_id})

    # -- targets ----------------------------------------------------------------
    def target(self, table: str) -> list[Row]:
        return self._select(f"SELECT * FROM {table}")

    def target_count(self, table: str) -> int:
        return self.count(table)

    # -- rejections / chunks ------------------------------------------------------
    def rejections(self, batch_id: str) -> list[Row]:
        return self._select(
            "SELECT * FROM rejected_records WHERE batch_id = :id ORDER BY row_number ASC, id ASC",
            {"id": batch_id},
        )

    def chunks(self, batch_id: str) -> list[Row]:
        return self._select(
            "SELECT * FROM batch_chunks WHERE batch_id = :id ORDER BY chunk_index ASC",
            {"id": batch_id},
        )

    def codes_by_row(self, batch_id: str) -> dict[int, set[str]]:
        out: dict[int, set[str]] = {}
        for r in self.rejections(batch_id):
            out.setdefault(r["row_number"], set()).add(r["reason_code"])
        return out
