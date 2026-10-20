"""A real database error in one chunk (spec s6 step 7, s14 acceptance criterion).

No mocks: a Postgres trigger raises on T4008, which sits in the second chunk
(rows 6-10 at chunk size 5) of load_error.csv.
"""

import pytest

import expected as E
from harness import fixture, reprocess, upload_and_wait, wait_for_batch

pytestmark = pytest.mark.integration

TRIGGER_FN = """
CREATE OR REPLACE FUNCTION test_fail_on_t4008() RETURNS trigger AS $$
BEGIN
    IF NEW.txn_id = 'T4008' THEN
        RAISE EXCEPTION 'simulated database failure on T4008';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
TRIGGER = """
CREATE TRIGGER test_fail_on_t4008 BEFORE INSERT OR UPDATE ON test_transactions
FOR EACH ROW EXECUTE FUNCTION test_fail_on_t4008()
"""
DROP_TRIGGER = "DROP TRIGGER IF EXISTS test_fail_on_t4008 ON test_transactions"


@pytest.fixture
def failing_chunk(db):
    db.execute(TRIGGER_FN)
    db.execute(DROP_TRIGGER)
    db.execute(TRIGGER)
    yield db
    db.execute(DROP_TRIGGER)


def test_other_chunks_commit_when_one_chunk_fails(failing_chunk, api):
    db = failing_chunk
    batch = upload_and_wait(api, fixture("load_error.csv"), "txn_test@1")
    batch_id = batch["batch_id"]

    b = db.batch(batch_id)
    assert b["status"] == "partial"
    assert b["total_rows"] == E.LOAD_ERROR_TOTAL
    assert b["accepted_rows"] == 10
    assert b["rejected_rows"] == 5

    loaded = {r["txn_id"] for r in db.target("test_transactions")}
    assert loaded == {f"T40{n:02d}" for n in (1, 2, 3, 4, 5, 11, 12, 13, 14, 15)}


def test_failed_chunk_is_marked_with_its_error(failing_chunk, api):
    db = failing_chunk
    batch_id = upload_and_wait(api, fixture("load_error.csv"), "txn_test@1")["batch_id"]

    chunks = db.chunks(batch_id)
    assert len(chunks) == 3
    assert sorted(c["status"] for c in chunks) == ["committed", "committed", "failed"]
    failed = next(c for c in chunks if c["status"] == "failed")
    assert failed["error"]
    # tolerate 0/1-based and inclusive/exclusive bounds, but it must be the 6..10 chunk
    assert failed["row_start"] in (5, 6) and failed["row_end"] in (9, 10, 11)
    assert all(not c["error"] for c in chunks if c["status"] == "committed")


def test_rows_of_the_failed_chunk_are_recorded_as_load_error(failing_chunk, api):
    db = failing_chunk
    batch_id = upload_and_wait(api, fixture("load_error.csv"), "txn_test@1")["batch_id"]

    recs = db.rejections(batch_id)
    assert {r["reason_code"] for r in recs} == {"LOAD_ERROR"}
    assert {r["row_number"] for r in recs} == E.LOAD_ERROR_FAILED_ROWS
    assert len(recs) == len(E.LOAD_ERROR_FAILED_ROWS)
    for r in recs:
        assert r["original_row"]["TxnId"] == f"T40{r['row_number']:02d}"
        assert r["reason_message"]


def test_reprocess_after_the_database_issue_is_fixed_lands_the_rows(failing_chunk, api):
    db = failing_chunk
    batch_id = upload_and_wait(api, fixture("load_error.csv"), "txn_test@1")["batch_id"]
    db.execute(DROP_TRIGGER)

    resp = reprocess(api, batch_id, "txn_test@1")
    assert resp.status_code in (200, 202), resp.text
    wait_for_batch(api, batch_id, retry_attempts=1)

    b = db.batch(batch_id)
    assert (b["status"], b["accepted_rows"], b["rejected_rows"]) == ("completed", 15, 0)
    assert db.count("test_transactions") == 15
    assert db.rejections(batch_id) == []
    assert {c["status"] for c in db.chunks(batch_id)} == {"committed"}
