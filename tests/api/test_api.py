"""HTTP API tests (spec s9). Uses FastAPI TestClient with a real Postgres backend.

Covers all endpoints: upload, list, detail, rejections, reject report,
reprocess, cross-batch query, contracts, metrics, health.
"""

from pathlib import Path

import pytest

import harness

pytestmark = pytest.mark.api

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _upload(api, filename: str, contract: str = "score_test"):
    filepath = FIXTURES / filename
    with open(filepath, "rb") as f:
        resp = api.post(
            "/api/batches",
            files={"file": (filename, f, "text/csv")},
            data={"contract": contract},
        )
    return resp


class TestUpload:
    def test_returns_202_with_batch_id(self, api):
        resp = _upload(api, "clean_scores.csv")
        assert resp.status_code == 202
        body = resp.json()
        assert "batch_id" in body
        assert "status" in body
        assert "skipped" in body

    def test_duplicate_upload_returns_skipped(self, api):
        _upload(api, "clean_scores.csv")
        resp = _upload(api, "clean_scores.csv")
        assert resp.json()["skipped"] is True

    def test_missing_file_returns_422(self, api):
        resp = api.post("/api/batches", data={"contract": "score_test"})
        assert resp.status_code == 422

    def test_invalid_contract_returns_error(self, api):
        filepath = FIXTURES / "clean_scores.csv"
        with open(filepath, "rb") as f:
            resp = api.post(
                "/api/batches",
                files={"file": ("clean.csv", f, "text/csv")},
                data={"contract": "nonexistent_contract"},
            )
        assert resp.status_code in (400, 404, 422)


class TestBatchList:
    def test_returns_list(self, api):
        _upload(api, "clean_scores.csv")
        resp = api.get("/api/batches")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, (list, dict))

    def test_filter_by_status(self, api):
        _upload(api, "clean_scores.csv")
        resp = api.get("/api/batches", params={"status": "completed"})
        assert resp.status_code == 200

    def test_search_by_filename(self, api):
        _upload(api, "clean_scores.csv")
        resp = api.get("/api/batches", params={"q": "clean_scores"})
        assert resp.status_code == 200

    def test_pagination(self, api):
        _upload(api, "clean_scores.csv")
        resp = api.get("/api/batches", params={"page": 1, "page_size": 5})
        assert resp.status_code == 200


class TestBatchDetail:
    def test_returns_batch_info(self, api, db):
        resp = _upload(api, "clean_scores.csv")
        batch_id = resp.json()["batch_id"]
        detail = api.get(f"/api/batches/{batch_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["batch_id"] == batch_id
        assert "status" in body
        assert "total_rows" in body or "counters" in body

    def test_nonexistent_batch_returns_404(self, api):
        resp = api.get("/api/batches/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestRejections:
    def test_returns_rejection_list(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/rejections")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, (list, dict))

    def test_filter_by_reason_code(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/rejections", params={"reason_code": "TYPE_MISMATCH"})
        assert r.status_code == 200

    def test_summary_returns_counts(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/rejections/summary")
        assert r.status_code == 200
        body = r.json()
        # Should have reason_code -> count mapping
        assert isinstance(body, (list, dict))


class TestRejectReport:
    def test_returns_csv(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/reject-report.csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")

    def test_csv_has_correct_columns(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/reject-report.csv")
        lines = r.text.strip().split("\n")
        header = lines[0]
        assert "row_number" in header
        assert "reason_codes" in header
        assert "reasons" in header

    def test_row_count_matches_rejected_rows(self, api, db):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/reject-report.csv")
        lines = r.text.strip().split("\n")
        data_rows = len(lines) - 1  # exclude header
        batch = db.batch(batch_id)
        # One row per rejected row_number (aggregated)
        assert data_rows == batch.rejected_rows

    def test_clean_batch_returns_header_only(self, api):
        resp = _upload(api, "clean_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/reject-report.csv")
        lines = r.text.strip().split("\n")
        assert len(lines) == 1  # header only

    def test_failed_batch_returns_404(self, api):
        resp = _upload(api, "missing_header.csv")
        batch_id = resp.json()["batch_id"]
        r = api.get(f"/api/batches/{batch_id}/reject-report.csv")
        assert r.status_code == 404


class TestReprocess:
    def test_partial_batch_can_be_reprocessed(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.post(f"/api/batches/{batch_id}/reprocess")
        assert r.status_code in (200, 202)

    def test_completed_batch_reprocess_rejected(self, api):
        resp = _upload(api, "clean_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.post(f"/api/batches/{batch_id}/reprocess")
        assert r.status_code in (400, 409, 422)

    def test_reprocess_with_specific_contract_version(self, api):
        resp = _upload(api, "dirty_scores.csv")
        batch_id = resp.json()["batch_id"]
        r = api.post(
            f"/api/batches/{batch_id}/reprocess",
            json={"contract": "score_test@1"},
        )
        assert r.status_code in (200, 202)


class TestCrossBatchRejections:
    def test_returns_affected_batches(self, api):
        _upload(api, "dirty_scores.csv")
        r = api.get("/api/rejections", params={"reason_code": "TYPE_MISMATCH"})
        assert r.status_code == 200


class TestContracts:
    def test_list_contracts(self, api):
        r = api.get("/api/contracts")
        assert r.status_code == 200

    def test_get_specific_contract(self, api):
        r = api.get("/api/contracts/score_test/1")
        assert r.status_code == 200
        body = r.json()
        assert body["contract"] == "score_test" or body["name"] == "score_test"

    def test_nonexistent_contract_returns_404(self, api):
        r = api.get("/api/contracts/fake_contract/99")
        assert r.status_code == 404


class TestMetrics:
    def test_returns_summary(self, api):
        _upload(api, "clean_scores.csv")
        r = api.get("/api/metrics/summary")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)


class TestHealth:
    def test_health_returns_ok(self, api):
        r = api.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok" or body.get("healthy") is True

