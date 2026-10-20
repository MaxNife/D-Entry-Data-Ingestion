"""Rejection endpoints: per batch, summary, and cross-batch audit (spec s9, s14)."""

import datetime as dt
import uuid

import pytest

import expected as E
from harness import assert_error_body, fixture, page_items, page_total, summary_counts, upload_and_wait

pytestmark = [pytest.mark.integration, pytest.mark.api]

ITEM_FIELDS = {"row_number", "reason_code", "reason_message", "column_name", "offending_value", "original_row"}


def _day(offset: int) -> str:
    return (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=offset)).isoformat()


@pytest.fixture
def mixed(api):
    return upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")["batch_id"]


def _all_rejections(api, batch_id, **params):
    items, page = [], 1
    while True:
        resp = api.get(f"/api/batches/{batch_id}/rejections", params={**params, "page": page, "page_size": 5})
        assert resp.status_code == 200, resp.text
        chunk = page_items(resp.json())
        items.extend(chunk)
        if len(chunk) < 5:
            return items, page_total(resp.json())
        page += 1


def test_rejections_list_every_record_with_details(api, mixed):
    items, total = _all_rejections(api, mixed)
    assert total == E.MIXED_V1_RECORDS
    assert len(items) == E.MIXED_V1_RECORDS
    for item in items:
        assert ITEM_FIELDS <= set(item), ITEM_FIELDS - set(item)
    keys = [(i["row_number"], i["reason_code"]) for i in items]
    assert len(set(keys)) == len(keys), "pagination returned a record twice"
    row2 = next(i for i in items if i["row_number"] == 2)
    assert row2["original_row"] == E.MIXED_ROW_2


def test_rejections_filter_by_reason_code(api, mixed):
    items, total = _all_rejections(api, mixed, reason_code="NOT_IN_SET")
    assert total == 3
    assert {i["row_number"] for i in items} == {4, 9, 19}
    assert {i["reason_code"] for i in items} == {"NOT_IN_SET"}


def test_rejections_filter_by_column(api, mixed):
    everything, _ = _all_rejections(api, mixed)
    amount_col = next(
        i["column_name"] for i in everything if (i["row_number"], i["reason_code"]) == (2, "AMOUNT_NEGATIVE")
    )
    items, _ = _all_rejections(api, mixed, column=amount_col)
    assert {(i["row_number"], i["reason_code"]) for i in items} == {
        key for key, col in E.MIXED_V1_COLUMNS.items() if col == "amount"
    }


def test_rejection_summary_counts_per_reason_code(api, mixed):
    resp = api.get(f"/api/batches/{mixed}/rejections/summary")
    assert resp.status_code == 200, resp.text
    assert summary_counts(resp.json()) == E.MIXED_V1_SUMMARY


def test_rejection_summary_of_clean_batch_is_empty(api):
    clean = upload_and_wait(api, fixture("clean.csv"), "txn_test@1")["batch_id"]
    resp = api.get(f"/api/batches/{clean}/rejections/summary")
    assert resp.status_code == 200
    assert summary_counts(resp.json()) == {}


@pytest.mark.parametrize("suffix", ["rejections", "rejections/summary", "reject-report.csv"])
def test_rejection_endpoints_404_for_unknown_batch(api, suffix):
    resp = api.get(f"/api/batches/{uuid.uuid4()}/{suffix}")
    assert resp.status_code == 404
    assert_error_body(resp)


# ---- cross-batch audit ----------------------------------------------------
@pytest.fixture
def audit_batches(api, tmp_path):
    variant = tmp_path / "mixed_resend.csv"
    variant.write_text(
        fixture("mixed.csv").read_text(encoding="utf-8") + "T1021,26.00,PAYMENT,C002,2026-10-06,\n",
        encoding="utf-8",
    )
    a = upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")["batch_id"]
    b = upload_and_wait(api, fixture("clean.csv"), "txn_test@1")["batch_id"]
    c = upload_and_wait(api, variant, "txn_test@1")["batch_id"]
    d = upload_and_wait(api, fixture("scores_b.csv"), "score_test@1")["batch_id"]
    return a, b, c, d


def _audit_batch_ids(api, **params) -> set[str]:
    resp = api.get("/api/rejections", params={**params, "page_size": 100})
    assert resp.status_code == 200, resp.text
    return {item["batch_id"] for item in page_items(resp.json())}


def test_audit_finds_every_batch_that_failed_on_a_reason(api, audit_batches):
    # spec s14 acceptance criterion: GET /api/rejections?reason_code=X across batches in a date range
    a, b, c, d = audit_batches
    found = _audit_batch_ids(api, reason_code="CUSTOMER_NOT_FOUND", **{"from": _day(-1), "to": _day(1)})
    assert found == {a, c}


def test_audit_respects_the_date_range(api, audit_batches):
    assert _audit_batch_ids(api, reason_code="CUSTOMER_NOT_FOUND", **{"from": _day(2), "to": _day(3)}) == set()
    assert _audit_batch_ids(api, reason_code="CUSTOMER_NOT_FOUND", **{"from": _day(-3), "to": _day(-2)}) == set()


def test_audit_filters_by_contract(api, audit_batches):
    a, b, c, d = audit_batches
    assert _audit_batch_ids(api, reason_code="GRADE_SCORE_MISMATCH", contract="score_test") == {d}
    assert _audit_batch_ids(api, reason_code="CUSTOMER_NOT_FOUND", contract="score_test") == set()


def test_audit_with_no_matches_is_empty(api, audit_batches):
    assert _audit_batch_ids(api, reason_code="LOAD_ERROR") == set()
