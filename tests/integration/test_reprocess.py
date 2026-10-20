"""Reprocessing (spec s6 transitions, s8 layers 3-4 and reprocess semantics, s14)."""

import uuid

import pytest

import expected as E
from harness import assert_error_body, fixture, reprocess, upload, upload_and_wait, wait_for_batch

pytestmark = [pytest.mark.integration, pytest.mark.api]


@pytest.fixture
def partial_batch(api):
    batch = upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")
    assert batch["status"] == "partial"
    return batch["batch_id"]


def _reprocess_and_wait(api, batch_id, contract=None, attempts=1):
    resp = reprocess(api, batch_id, contract)
    assert resp.status_code in (200, 202), resp.text
    return wait_for_batch(api, batch_id, retry_attempts=attempts)


@pytest.mark.parametrize("contract", ["txn_test@2", None], ids=["explicit-v2", "default-latest"])
def test_reprocess_with_fixed_contract_lands_previously_rejected_rows(api, db, partial_batch, contract):
    before = db.batch(partial_batch)
    after = _reprocess_and_wait(api, partial_batch, contract)

    assert after["retry_attempts"] == 1
    assert after["contract_version"] == 2
    assert after["status"] == "partial"
    assert (after["accepted_rows"], after["rejected_rows"]) == (E.MIXED_V2_ACCEPTED, E.MIXED_V2_REJECTED)
    assert after["accepted_rows"] + after["rejected_rows"] == after["total_rows"] == E.MIXED_TOTAL

    rows = db.target("test_transactions")
    assert {r["txn_id"] for r in rows} == E.MIXED_V2_ACCEPTED_IDS  # T1004 (REFUND) landed
    assert len(rows) == len({r["txn_id"] for r in rows})  # no duplicates
    assert len(db.batches()) == 1  # the existing batch row was updated, not a second one
    assert db.batch(partial_batch)["contract_hash"] != before["contract_hash"]


def test_reprocess_stores_the_new_contract_hash(api, db, partial_batch):
    reference = upload_and_wait(api, fixture("clean.csv"), "txn_test@2")  # any v2 batch
    _reprocess_and_wait(api, partial_batch, "txn_test@2")
    assert db.batch(partial_batch)["contract_hash"] == db.batch(reference["batch_id"])["contract_hash"]


def test_rejections_and_chunks_are_replaced_not_appended(api, db, partial_batch):
    _reprocess_and_wait(api, partial_batch, "txn_test@2")
    assert db.codes_by_row(partial_batch) == E.MIXED_V2_CODES
    assert len(db.rejections(partial_batch)) == E.MIXED_V2_RECORDS
    assert len(db.chunks(partial_batch)) == 4


def test_reprocess_under_same_contract_is_idempotent(api, db, partial_batch):
    target_before = sorted(r["row_key"] for r in db.target("test_transactions"))
    _reprocess_and_wait(api, partial_batch, "txn_test@1", attempts=1)
    after = _reprocess_and_wait(api, partial_batch, "txn_test@1", attempts=2)

    assert after["retry_attempts"] == 2
    assert (after["accepted_rows"], after["rejected_rows"]) == (E.MIXED_V1_ACCEPTED, E.MIXED_V1_REJECTED)
    assert sorted(r["row_key"] for r in db.target("test_transactions")) == target_before
    assert len(db.rejections(partial_batch)) == E.MIXED_V1_RECORDS
    assert len(db.chunks(partial_batch)) == 4


def test_reject_report_reflects_the_latest_attempt(api, partial_batch):
    import csv
    import io

    _reprocess_and_wait(api, partial_batch, "txn_test@2")
    resp = api.get(f"/api/batches/{partial_batch}/reject-report.csv")
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8"))))
    assert {int(r["row_number"]) for r in rows} == set(E.MIXED_V2_CODES)


def test_reupload_under_the_new_version_is_skipped_after_reprocess(api, db, partial_batch):
    # After reprocess the batch identity is (sha, txn_test, 2) -- spec s8.
    _reprocess_and_wait(api, partial_batch, "txn_test@2")
    resp = upload(api, fixture("mixed.csv"), "txn_test@2")
    assert resp.status_code in (200, 202), resp.text
    assert resp.json()["skipped"] is True
    assert resp.json()["batch_id"] == partial_batch
    assert len(db.batches()) == 1


def test_completed_batch_cannot_be_reprocessed(api, db):
    batch_id = upload_and_wait(api, fixture("clean.csv"), "txn_test@1")["batch_id"]
    resp = reprocess(api, batch_id)
    assert resp.status_code in (400, 409, 422), resp.text
    assert_error_body(resp)
    b = db.batch(batch_id)
    assert (b["status"], b["retry_attempts"]) == ("completed", 0)


def test_failed_batch_can_be_reprocessed(api, db):
    batch_id = upload_and_wait(api, fixture("missing_header.csv"), "txn_test@1")["batch_id"]
    after = _reprocess_and_wait(api, batch_id, "txn_test@1")
    # still structurally broken, but the attempt is recorded
    assert after["status"] == "failed"
    assert after["retry_attempts"] == 1
    assert after["failure_reason"]
    assert len(db.batches()) == 1


def test_reprocess_unknown_batch_is_404(api):
    resp = reprocess(api, str(uuid.uuid4()))
    assert resp.status_code == 404
    assert_error_body(resp)


def test_reprocess_with_unknown_contract_is_refused(api, db, partial_batch):
    resp = reprocess(api, partial_batch, "txn_test@99")
    assert 400 <= resp.status_code < 500, resp.text
    assert_error_body(resp)
    assert db.batch(partial_batch)["retry_attempts"] == 0
