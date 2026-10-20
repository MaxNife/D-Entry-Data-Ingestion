"""Shared fixtures. Integration tests run against a real Postgres (spec s13: no DB mocks).

The test database is reset at session start (schema dropped, `alembic upgrade head`
re-run), so migrations are exercised from scratch on every run. The database name
must contain "test" as a guard against pointing this at real data.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Make tests/ importable (harness, expected) under every pytest import mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

harness.configure_process_environment()

MARKERS = {
    "unit": "pure tests, no database",
    "integration": "pipeline/DB tests against a real Postgres",
    "api": "HTTP API tests (in-process FastAPI TestClient, real Postgres)",
    "cli": "tests that run the `ingest` console script as a subprocess",
    "e2e": "browser tests (Playwright) against a live server",
    "repo": "repository deliverables (files, CI, lint)",
}

# Target and reference tables for the test contracts, shaped exactly as spec s4 says:
# the contract's columns + batch_id, row_key (unique), loaded_at.
TEST_TABLES_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ref_customers (
        customer_id text PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_transactions (
        txn_id      text,
        amount      double precision,
        txn_type    text,
        customer_id text,
        txn_date    date,
        memo        text,
        batch_id    uuid NOT NULL,
        row_key     text NOT NULL UNIQUE,
        loaded_at   timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_scores (
        name      text,
        score     bigint,
        grade     text,
        batch_id  uuid NOT NULL,
        row_key   text NOT NULL UNIQUE,
        loaded_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

TRUNCATE = (
    "TRUNCATE batch_chunks, rejected_records, batches, test_transactions, test_scores "
    "RESTART IDENTITY CASCADE"
)


def pytest_configure(config):
    for name, help_text in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {help_text}")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(harness.UPLOAD_DIR, ignore_errors=True)


def _ensure_database(url) -> None:
    from sqlalchemy import create_engine, text

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except Exception:
        pass  # no rights on the maintenance DB; the connection check below reports clearly
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def db_engine():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(harness.TEST_DATABASE_URL)
    if "test" not in (url.database or ""):
        pytest.fail(f"refusing to run: test database name {url.database!r} must contain 'test'", pytrace=False)

    _ensure_database(url)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.fail(
            f"cannot reach the test Postgres at {url.render_as_string(hide_password=True)}: {exc}\n"
            "Start Postgres (e.g. `docker compose up -d db`) or set INGEST_TEST_DATABASE_URL.",
            pytrace=False,
        )

    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=harness.REPO_ROOT,
        env=harness.subprocess_env(),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.fail(f"`alembic upgrade head` failed:\n{proc.stdout}\n{proc.stderr}", pytrace=False)

    from expected import REF_CUSTOMERS

    with engine.begin() as conn:
        for ddl in TEST_TABLES_DDL:
            conn.execute(text(ddl))
        for customer_id in REF_CUSTOMERS:
            conn.execute(
                text("INSERT INTO ref_customers (customer_id) VALUES (:c) ON CONFLICT DO NOTHING"),
                {"c": customer_id},
            )

    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine) -> harness.Db:
    """Empty control and target tables plus an empty upload dir, for every test that asks."""
    from sqlalchemy import text

    with db_engine.begin() as conn:
        conn.execute(text(TRUNCATE))
    harness.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for child in harness.UPLOAD_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    return harness.Db(db_engine)


@pytest.fixture
def api(db):
    from fastapi.testclient import TestClient

    with TestClient(harness.get_app()) as client:
        yield client
