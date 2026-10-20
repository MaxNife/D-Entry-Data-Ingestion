"""GET /api/batches/{id}/reject-report.csv (spec s11, s14)."""

import csv
import io

import pytest

import expected as E
from harness import assert_error_body, fixture, upload_and_wait

pytestmark = [pytest.mark.integration, pytest.mark.api]

REPORT_HEADER = ["row_number", "reason_codes", "reasons", *E.TXN_HEADERS]


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(";")]


@pytest.fixture
def mixed_report(api):
    batch = upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")
    resp = api.get(f"/api/batches/{batch['batch_id']}/reject-report.csv")
    assert resp.status_code == 200, resp.text
    return batch, resp


def _parse(resp) -> tuple[list[str], list[dict]]:
    text = resp.content.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    rows = [dict(zip(header, r, strict=True)) for r in reader if r]
    return header, rows


def test_report_is_a_named_csv_download(mixed_report):
    batch, resp = mixed_report
    assert resp.headers["content-type"].startswith("text/csv")
    assert f"reject_report_{batch['batch_id']}.csv" in resp.headers.get("content-disposition", "")


def test_report_columns_are_in_spec_order(mixed_report):
    header, _ = _parse(mixed_report[1])
    assert header == REPORT_HEADER


def test_report_has_one_row_per_rejected_record_sorted(mixed_report):
    # spec s14 acceptance criterion: exactly rejected_rows distinct data rows
    batch, resp = mixed_report
    _, rows = _parse(resp)
    numbers = [int(r["row_number"]) for r in rows]
    assert len(rows) == batch["rejected_rows"] == E.MIXED_V1_REJECTED
    assert numbers == sorted(set(numbers))
    assert set(numbers) == set(E.MIXED_V1_CODES)


def test_report_joins_all_reasons_per_row_in_matching_order(mixed_report):
    _, rows = _parse(mixed_report[1])
    by_row = {int(r["row_number"]): r for r in rows}
    for number, codes in E.MIXED_V1_CODES.items():
        got_codes = _split(by_row[number]["reason_codes"])
        got_reasons = _split(by_row[number]["reasons"])
        assert set(got_codes) == codes, number
        assert len(got_reasons) == len(got_codes), number
    row9 = by_row[9]
    codes, reasons = _split(row9["reason_codes"]), _split(row9["reasons"])
    assert reasons[codes.index("AMOUNT_NEGATIVE")] == E.MESSAGES["AMOUNT_NEGATIVE"]


def test_report_keeps_original_values_exactly_as_read(mixed_report):
    _, rows = _parse(mixed_report[1])
    by_row = {int(r["row_number"]): r for r in rows}
    assert {h: by_row[2][h] for h in E.TXN_HEADERS} == E.MIXED_ROW_2
    assert by_row[3]["Amount"] == "abc"
    assert by_row[5]["CustomerId"] == ""
    assert by_row[6]["Memo"] == "café"
    assert by_row[7]["TxnId"] == "X1007"
    assert by_row[10]["TxnDate"] == "not-a-date"


def test_clean_batch_report_is_header_only(api):
    batch = upload_and_wait(api, fixture("clean.csv"), "txn_test@1")
    resp = api.get(f"/api/batches/{batch['batch_id']}/reject-report.csv")
    assert resp.status_code == 200
    header, rows = _parse(resp)
    assert header == REPORT_HEADER
    assert rows == []


def test_failed_batch_report_is_404_with_failure_reason(api):
    batch = upload_and_wait(api, fixture("missing_header.csv"), "txn_test@1")
    resp = api.get(f"/api/batches/{batch['batch_id']}/reject-report.csv")
    assert resp.status_code == 404
    body = assert_error_body(resp)
    assert batch["failure_reason"] in str(body["detail"])
