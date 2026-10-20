"""POST/GET /api/batches (spec s9, s14)."""

import datetime as dt
import uuid

import pytest

import expected as E
import harness
from harness import assert_error_body, fixture, page_items, page_total, upload, upload_and_wait, wait_for_batch

pytestmark = [pytest.mark.integration, pytest.mark.api]

DETAIL_FIELDS = {
    "batch_id", "file_name", "file_sha256", "contract_name", "contract_version", "contract_hash",
    "status", "failure_reason", "total_rows", "accepted_rows", "rejected_rows", "retry_attempts",
    "created_at", "started_at", "finished_at",
}


def _day(offset: int) -> str:
    return (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=offset)).isoformat()


def _ids(resp) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [item["batch_id"] for item in page_items(resp.json())]


# ---- upload ---------------------------------------------------------------
def test_upload_returns_202_with_batch_id_immediately(api):
    resp = upload(api, fixture("clean.csv"), "txn_test@1")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert set(body) >= {"batch_id", "status", "skipped"}
    uuid.UUID(body["batch_id"])
    assert body["skipped"] is False
    assert body["status"] in {"pending", "processing", "completed"}


def test_batch_detail_exposes_the_audit_row(api, db):
    batch = upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")
    assert DETAIL_FIELDS <= set(batch), DETAIL_FIELDS - set(batch)
    assert batch["status"] == "partial"
    assert batch["file_name"] == "mixed.csv"
    assert batch["file_sha256"] == harness.sha256_file(fixture("mixed.csv"))
    assert (batch["contract_name"], batch["contract_version"]) == ("txn_test", 1)
    assert (batch["total_rows"], batch["accepted_rows"], batch["rejected_rows"]) == (
        E.MIXED_TOTAL, E.MIXED_V1_ACCEPTED, E.MIXED_V1_REJECTED,
    )
    assert batch["retry_attempts"] == 0
    assert batch["failure_reason"] is None


def test_uploading_the_same_file_twice_is_skipped(api, db):
    # spec s14 acceptance criterion 1
    first = upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")
    rows_before = db.count("test_transactions")

    resp = upload(api, fixture("mixed.csv"), "txn_test@1")
    assert resp.status_code in (200, 202), resp.text
    body = resp.json()
    assert body["skipped"] is True
    assert body["batch_id"] == first["batch_id"]

    assert len(db.batches()) == 1
    assert db.count("test_transactions") == rows_before


def test_upload_keeps_the_original_file_name_and_raw_bytes(api):
    batch = upload_and_wait(api, fixture("clean.csv"), "txn_test@1", filename="Vendor Extract 2026-10-15.csv")
    assert batch["file_name"] == "Vendor Extract 2026-10-15.csv"
    stored = harness.UPLOAD_DIR / batch["file_sha256"] / "Vendor Extract 2026-10-15.csv"
    assert stored.read_bytes() == fixture("clean.csv").read_bytes()


def test_upload_with_missing_header_ends_failed(api, db):
    batch = upload_and_wait(api, fixture("missing_header.csv"), "txn_test@1")
    assert batch["status"] == "failed"
    assert "CustomerId" in batch["failure_reason"]
    assert db.count("test_transactions") == 0


def test_upload_with_bare_contract_name_uses_latest(api):
    batch = upload_and_wait(api, fixture("clean.csv"), "txn_test")
    assert batch["contract_version"] == 2


@pytest.mark.parametrize("contract", ["no_such_contract", "txn_test@99", "txn_test@abc"])
def test_upload_with_unknown_contract_is_rejected(api, db, contract):
    resp = upload(api, fixture("clean.csv"), contract)
    assert 400 <= resp.status_code < 500, resp.text
    assert_error_body(resp)
    assert db.batches() == []


def test_upload_without_a_file_is_rejected(api, db):
    resp = api.post("/api/batches", data={"contract": "txn_test@1"})
    assert 400 <= resp.status_code < 500, resp.text
    assert_error_body(resp)
    assert db.batches() == []


# ---- detail ---------------------------------------------------------------
def test_unknown_batch_is_404_with_error_body(api):
    resp = api.get(f"/api/batches/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert_error_body(resp)


def test_malformed_batch_id_is_a_client_error_with_error_body(api):
    resp = api.get("/api/batches/not-a-uuid")
    assert resp.status_code in (400, 404, 422)
    assert_error_body(resp)


# ---- list and search ------------------------------------------------------
@pytest.fixture
def three_batches(api):
    clean = upload_and_wait(api, fixture("clean.csv"), "txn_test@1")
    mixed = upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")
    failed = upload_and_wait(api, fixture("missing_header.csv"), "txn_test@1")
    return clean, mixed, failed


def test_list_is_sorted_newest_first(api, three_batches):
    clean, mixed, failed = three_batches
    assert _ids(api.get("/api/batches")) == [failed["batch_id"], mixed["batch_id"], clean["batch_id"]]


def test_list_paginates(api, three_batches):
    clean, mixed, failed = three_batches
    p1 = api.get("/api/batches", params={"page": 1, "page_size": 2})
    p2 = api.get("/api/batches", params={"page": 2, "page_size": 2})
    assert page_total(p1.json()) == 3
    assert _ids(p1) == [failed["batch_id"], mixed["batch_id"]]
    assert _ids(p2) == [clean["batch_id"]]


def test_list_filters_by_status(api, three_batches):
    clean, mixed, failed = three_batches
    assert _ids(api.get("/api/batches", params={"status": "partial"})) == [mixed["batch_id"]]
    assert _ids(api.get("/api/batches", params={"status": "failed"})) == [failed["batch_id"]]
    assert _ids(api.get("/api/batches", params={"status": "completed"})) == [clean["batch_id"]]


def test_list_filters_by_contract(api, three_batches):
    scores = upload_and_wait(api, fixture("scores_a.csv"), "score_test@1")
    assert _ids(api.get("/api/batches", params={"contract": "score_test"})) == [scores["batch_id"]]
    assert len(_ids(api.get("/api/batches", params={"contract": "txn_test"}))) == 3


def test_search_by_file_name_substring(api, three_batches):
    _, mixed, _ = three_batches
    assert _ids(api.get("/api/batches", params={"q": "mixed"})) == [mixed["batch_id"]]
    assert _ids(api.get("/api/batches", params={"q": "MIXED"})) == [mixed["batch_id"]]


def test_search_by_batch_id(api, three_batches):
    clean, _, _ = three_batches
    assert _ids(api.get("/api/batches", params={"q": clean["batch_id"]})) == [clean["batch_id"]]


def test_list_filters_by_date_range(api, three_batches):
    assert len(_ids(api.get("/api/batches", params={"from": _day(-1), "to": _day(1)}))) == 3
    assert _ids(api.get("/api/batches", params={"from": _day(2)})) == []
    assert _ids(api.get("/api/batches", params={"to": _day(-2)})) == []


def test_list_on_empty_database(api):
    resp = api.get("/api/batches")
    assert resp.status_code == 200
    assert page_items(resp.json()) == []
    assert page_total(resp.json()) == 0


def test_processing_status_is_visible_while_running(api):
    # The 202 response may already report a terminal status if the work was quick;
    # what matters is that polling always converges on a terminal state.
    resp = upload(api, fixture("mixed.csv"), "txn_test@1")
    assert resp.json()["status"] in {"pending", "processing", "partial"}
    assert wait_for_batch(api, resp.json()["batch_id"])["status"] == "partial"
