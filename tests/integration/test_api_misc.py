"""Health, OpenAPI, contracts and metrics endpoints (spec s9, s10)."""

import pytest

from harness import METRIC_KEYS, assert_error_body, contract_versions, fixture, upload_and_wait

pytestmark = [pytest.mark.integration, pytest.mark.api]

SPEC_PATHS = {
    "/api/batches": {"get", "post"},
    "/api/batches/{id}": {"get"},
    "/api/batches/{id}/rejections": {"get"},
    "/api/batches/{id}/rejections/summary": {"get"},
    "/api/batches/{id}/reject-report.csv": {"get"},
    "/api/batches/{id}/reprocess": {"post"},
    "/api/rejections": {"get"},
    "/api/contracts": {"get"},
    "/api/contracts/{name}/{version}": {"get"},
    "/api/metrics/summary": {"get"},
    "/health": {"get"},
}


def _normalise(path: str) -> str:
    """Treat any path parameter name as equivalent: /api/batches/{batch_id} == /api/batches/{id}."""
    parts = []
    for part in path.split("/"):
        parts.append("{}" if part.startswith("{") else part)
    return "/".join(parts)


def test_health_reports_ok(api):
    resp = api.get("/health")
    assert resp.status_code == 200
    assert resp.json()


def test_openapi_docs_cover_every_spec_route(api):
    assert api.get("/docs").status_code == 200
    paths = api.get("/openapi.json").json()["paths"]
    published = {_normalise(p): set(ops) for p, ops in paths.items()}
    for path, methods in SPEC_PATHS.items():
        key = _normalise(path)
        assert key in published, f"{path} missing from OpenAPI"
        assert methods <= published[key], f"{path}: {methods - published[key]} missing"


def test_contracts_are_listed_with_versions(api):
    resp = api.get("/api/contracts")
    assert resp.status_code == 200, resp.text
    assert contract_versions(resp.json()) >= {("txn_test", 1), ("txn_test", 2), ("score_test", 1)}


def test_one_contract_version_can_be_shown(api):
    v1 = api.get("/api/contracts/txn_test/1")
    v2 = api.get("/api/contracts/txn_test/2")
    assert v1.status_code == v2.status_code == 200
    assert "TxnId" in v1.text and "AMOUNT_NEGATIVE" in v1.text and "REFUND" not in v1.text
    assert "REFUND" in v2.text


@pytest.mark.parametrize("path", ["/api/contracts/txn_test/99", "/api/contracts/nope/1"])
def test_unknown_contract_version_is_404(api, path):
    resp = api.get(path)
    assert resp.status_code == 404
    assert_error_body(resp)


def test_metrics_are_zero_on_an_empty_day(api):
    resp = api.get("/api/metrics/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert {name: body[key] for name, key in METRIC_KEYS.items()} == {
        "batches": 0, "accepted": 0, "rejected": 0, "failed": 0,
    }


def test_metrics_count_todays_batches_and_rows(api):
    upload_and_wait(api, fixture("clean.csv"), "txn_test@1")
    upload_and_wait(api, fixture("mixed.csv"), "txn_test@1")
    upload_and_wait(api, fixture("missing_header.csv"), "txn_test@1")
    body = api.get("/api/metrics/summary").json()
    assert {name: body[key] for name, key in METRIC_KEYS.items()} == {
        "batches": 3, "accepted": 12 + 8, "rejected": 12, "failed": 1,
    }
