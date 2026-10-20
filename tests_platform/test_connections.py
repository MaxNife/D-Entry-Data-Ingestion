"""External databases: register, test, validate/import a source table, load into a destination.

SQLite and PostgreSQL always run (the PostgreSQL "external" database is the test database
itself, reached through a registered connection). SQL Server runs when INGEST_TEST_MSSQL is
set to ``host:port:user:password`` (a ``CREATE DATABASE`` right is needed).
"""

import io
import json
import os

import pandas as pd
import pytest
from harness import TEST_DATABASE_URL, fixture, upload_and_wait, wait_for_batch
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ingest.core import secrets

pytestmark = pytest.mark.integration

ORDERS = {
    "description": "orders into an external database",
    "source": {"formats": ["csv"], "encoding": "utf-8", "delimiter": ","},
    "target": {"key": ["txn_id"], "on_conflict": "update"},
    "columns": [
        {"name": "txn_id", "source_name": "TxnId", "type": "str", "required": True},
        {"name": "amount", "source_name": "Amount", "type": "float", "required": True, "min": 0},
        {"name": "txn_type", "source_name": "Type", "type": "str", "required": True},
        {"name": "customer_id", "source_name": "CustomerId", "type": "str", "required": True},
        {"name": "txn_date", "source_name": "TxnDate", "type": "date", "required": True},
        {"name": "memo", "source_name": "Memo", "type": "str"},
    ],
    "rules": [],
}


@pytest.fixture(autouse=True)
def clean_connections(db, monkeypatch):
    from ingest.core import connections

    monkeypatch.setenv("INGEST_SECRET_KEY", "tGzv3JOkF0XG5Qx2gyPtTaQZGpNBR1-bdvZuJgU3NZI=")
    monkeypatch.setattr(secrets, "_cached", None)
    db.execute("DELETE FROM connections")
    connections._engines.clear()
    yield
    db.execute("DELETE FROM connections")


def add(api, **form):
    resp = api.post("/connections/save", data=form, follow_redirects=False)
    assert resp.status_code == 303, resp.text[:500]
    return resp


def contract(api, name, table, connection):
    spec = {
        **ORDERS,
        "contract": name,
        "target": {**ORDERS["target"], "table": table, "connection": connection},
    }
    resp = api.post(
        "/contracts/save",
        data={"mode": "new", "editor": "form", "spec_json": json.dumps(spec)},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:500]


def pg_form(name="self"):
    url = make_url(TEST_DATABASE_URL)
    return dict(
        name=name,
        kind="postgresql",
        host=url.host,
        port=str(url.port or 5432),
        database=url.database,
        username=url.username,
        password=url.password or "",
    )


# ---- registry ---------------------------------------------------------------------------


def test_password_is_encrypted_at_rest_and_kept_when_left_blank(api, db):
    add(api, **pg_form())
    stored = db.scalar("SELECT password_enc FROM connections WHERE name = 'self'")
    assert stored and make_url(TEST_DATABASE_URL).password not in stored
    assert db.scalar("SELECT last_test_ok FROM connections WHERE name = 'self'") is True
    add(api, **{**pg_form(), "password": "", "original": "self"})  # edit, password blank
    assert db.scalar("SELECT password_enc FROM connections WHERE name = 'self'") == stored


def test_connection_test_reports_a_wrong_password(api):
    resp = api.post("/connections/test", data={**pg_form(), "password": "definitely-wrong"})
    assert 'data-ok="0"' in resp.text
    assert "password" in resp.text.lower()


def test_invalid_connection_form_is_refused(api):
    resp = api.post("/connections/save", data={"name": "Bad Name", "kind": "postgresql"})
    assert resp.status_code == 400
    assert "Name must start" in resp.text


# ---- sources ----------------------------------------------------------------------------


def test_source_table_validates_without_loading_and_imports_as_a_batch(api, db):
    db.execute("DROP TABLE IF EXISTS src_orders")
    db.execute(
        'CREATE TABLE src_orders ("TxnId" text, "Amount" text, "Type" text, "CustomerId" text, "TxnDate" date, "Memo" text)'
    )
    db.execute("""INSERT INTO src_orders VALUES
        ('T5001','10.00','PAYMENT','C001','2026-10-01','ok'),
        ('T5002','-1.00','PAYMENT','C002','2026-10-01','negative'),
        ('T5003','5.00','WIRE','C003','2026-10-02','bad type')""")
    add(api, **pg_form())
    resp = api.post(
        "/connections/self/use",
        data={"action": "validate", "table": "src_orders", "contract": "txn_test@1"},
        follow_redirects=False,
    )
    assert resp.headers["location"].startswith("/validations/")
    assert db.batches() == []
    resp = api.post(
        "/connections/self/use",
        data={"action": "import", "table": "src_orders", "contract": "txn_test@1"},
        follow_redirects=False,
    )
    batch_id = resp.headers["location"].split("/batches/")[1]
    b = wait_for_batch(api, batch_id)
    assert (b["status"], b["accepted_rows"], b["rejected_rows"]) == ("partial", 1, 2)
    again = api.post(
        "/connections/self/use",
        data={"action": "import", "table": "src_orders", "contract": "txn_test@1"},
        follow_redirects=False,
    )
    assert again.headers["location"] == f"/batches/{batch_id}"  # same data, same SHA-256: skipped


def test_source_query_must_be_read_only(api, db):
    add(api, **pg_form())
    resp = api.post(
        "/connections/self/use",
        data={"action": "validate", "sql": "DELETE FROM batches", "contract": "txn_test"},
        follow_redirects=False,
    )
    assert "Only+read+queries" in resp.headers["location"]


# ---- destinations ---------------------------------------------------------------------------


def _load_and_check(api, name, key):
    first = upload_and_wait(api, fixture("clean_transactions.csv"), name)
    assert first["status"] == "completed", first
    page = api.get(f"/tables/{key}")
    assert page.status_code == 200 and "5 rows" in page.text
    export = api.get(f"/tables/{key}/export?format=parquet")
    assert len(pd.read_parquet(io.BytesIO(export.content))) == 5
    return first


def test_sqlite_destination_receives_the_rows(api, db, tmp_path):
    path = tmp_path / "dest.sqlite"
    add(api, name="lite", kind="sqlite", database=str(path))
    contract(api, "orders_lite", "orders_lite", "lite")
    _load_and_check(api, "orders_lite", "orders_lite@lite")
    rows = (
        create_engine(f"sqlite:///{path}")
        .connect()
        .execute(text("SELECT count(*) FROM orders_lite"))
        .scalar()
    )
    assert rows == 5
    assert db.target_count("rejected_records") == 0  # audit trail stays in the app database


def test_destination_in_use_cannot_be_deleted(api, tmp_path):
    add(api, name="lite", kind="sqlite", database=str(tmp_path / "d.sqlite"))
    contract(api, "orders_lite", "orders_lite", "lite")
    resp = api.post("/connections/lite/delete", follow_redirects=False)
    assert "destination+of+orders_lite" in resp.headers["location"]


def test_sql_editor_reads_a_connection(api, tmp_path):
    add(api, name="lite", kind="sqlite", database=str(tmp_path / "q.sqlite"))
    contract(api, "orders_lite", "orders_lite", "lite")
    upload_and_wait(api, fixture("clean_transactions.csv"), "orders_lite")
    ok = api.post(
        "/api/sql/run", json={"db": "lite", "sql": "SELECT count(*) AS n FROM orders_lite"}
    )
    assert ok.status_code == 200 and ok.json()["rows"] == [[5]]
    schema = api.get("/api/sql/schema", params={"db": "lite"}).json()
    assert schema["dialect"] == "sqlite"
    assert {"name": "orders_lite", "kind": "target"}.items() <= next(
        t for t in schema["tables"] if t["name"] == "orders_lite"
    ).items()
    blocked = api.post("/api/sql/run", json={"db": "lite", "sql": "DELETE FROM orders_lite"})
    assert blocked.status_code == 400 and blocked.json()["ok"] is False


@pytest.mark.skipif(
    not os.environ.get("INGEST_TEST_MSSQL"), reason="set INGEST_TEST_MSSQL=host:port:user:password"
)
def test_sql_server_destination_with_upsert(api, db, tmp_path):
    import pymssql

    host, port, user, password = os.environ["INGEST_TEST_MSSQL"].split(":", 3)
    admin = pymssql.connect(
        server=host, port=int(port), user=user, password=password, autocommit=True
    )
    cur = admin.cursor()
    cur.execute(
        "IF DB_ID('ingest_platform_test') IS NOT NULL BEGIN ALTER DATABASE ingest_platform_test SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE ingest_platform_test END"
    )
    cur.execute("CREATE DATABASE ingest_platform_test")
    add(
        api,
        name="sqlsrv",
        kind="mssql",
        host=host,
        port=port,
        database="ingest_platform_test",
        username=user,
        password=password,
    )
    contract(api, "orders_sql", "orders_sql", "sqlsrv")
    _load_and_check(api, "orders_sql", "orders_sql@sqlsrv")
    changed = tmp_path / "changed_orders.csv"
    changed.write_text(
        fixture("clean_transactions.csv")
        .read_text(encoding="utf-8")
        .replace("100.50", "111.11", 1),
        encoding="utf-8",
    )
    second = upload_and_wait(api, changed, "orders_sql")
    assert second["status"] == "completed"
    cur.execute(
        "SELECT count(*), sum(CASE WHEN amount = 111.11 THEN 1 ELSE 0 END) FROM ingest_platform_test.dbo.orders_sql"
    )
    assert cur.fetchone() == (5, 1)  # updated in place, not duplicated
