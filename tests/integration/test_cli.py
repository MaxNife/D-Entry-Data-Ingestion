"""The `ingest` console script (spec s9), run as a real subprocess."""

import shutil
import subprocess

import pytest

import expected as E
import harness
from harness import fixture

pytestmark = [pytest.mark.integration, pytest.mark.cli]


def ingest(*args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*harness.cli_command(), *args],
        cwd=harness.REPO_ROOT,
        env=harness.subprocess_env(**env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )


def _output(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def test_help_lists_the_commands():
    proc = ingest("--help")
    assert proc.returncode == 0, _output(proc)
    for command in ("upload", "reprocess", "batches", "contracts"):
        assert command in proc.stdout


def test_upload_runs_synchronously_and_prints_a_summary(db):
    proc = ingest("upload", str(fixture("mixed.csv")), "--contract", "txn_test@1")
    assert proc.returncode == 0, _output(proc)
    [batch] = db.batches()
    assert batch["status"] == "partial"  # synchronous: finished when the command returns
    out = _output(proc)
    assert str(batch["batch_id"])[:8] in out
    assert "partial" in out.lower()
    assert str(E.MIXED_V1_ACCEPTED) in out and str(E.MIXED_V1_REJECTED) in out


def test_second_upload_reports_skipped(db):
    assert ingest("upload", str(fixture("mixed.csv")), "--contract", "txn_test@1").returncode == 0
    proc = ingest("upload", str(fixture("mixed.csv")), "--contract", "txn_test@1")
    assert proc.returncode == 0, _output(proc)
    assert "skip" in _output(proc).lower() or "already" in _output(proc).lower()
    assert len(db.batches()) == 1


def test_upload_of_a_broken_file_records_a_failed_batch(db):
    ingest("upload", str(fixture("missing_header.csv")), "--contract", "txn_test@1")
    [batch] = db.batches()
    assert batch["status"] == "failed" and batch["failure_reason"]


def test_upload_with_unknown_contract_fails(db):
    proc = ingest("upload", str(fixture("clean.csv")), "--contract", "no_such_contract")
    assert proc.returncode != 0
    assert "Traceback" not in _output(proc), "errors must be readable, not a stack trace"
    assert db.batches() == []


def test_batches_lists_and_filters_by_status(db):
    ingest("upload", str(fixture("mixed.csv")), "--contract", "txn_test@1")
    ingest("upload", str(fixture("clean.csv")), "--contract", "txn_test@1")
    by_status = {b["status"]: str(b["batch_id"])[:8] for b in db.batches()}

    proc = ingest("batches", "--status", "partial")
    assert proc.returncode == 0, _output(proc)
    assert by_status["partial"] in proc.stdout
    assert by_status["completed"] not in proc.stdout

    proc = ingest("batches")
    assert by_status["partial"] in proc.stdout and by_status["completed"] in proc.stdout


def test_reprocess_with_a_newer_contract(db):
    ingest("upload", str(fixture("mixed.csv")), "--contract", "txn_test@1")
    batch_id = str(db.batches()[0]["batch_id"])

    proc = ingest("reprocess", batch_id, "--contract", "txn_test@2")
    assert proc.returncode == 0, _output(proc)
    b = db.batch(batch_id)
    assert (b["retry_attempts"], b["contract_version"]) == (1, 2)
    assert (b["accepted_rows"], b["rejected_rows"]) == (E.MIXED_V2_ACCEPTED, E.MIXED_V2_REJECTED)


def test_reprocess_of_a_completed_batch_is_refused(db):
    ingest("upload", str(fixture("clean.csv")), "--contract", "txn_test@1")
    batch_id = str(db.batches()[0]["batch_id"])
    proc = ingest("reprocess", batch_id)
    assert proc.returncode != 0
    assert "Traceback" not in _output(proc)
    assert db.batch(batch_id)["retry_attempts"] == 0


# ---- contracts validate ---------------------------------------------------
def test_contracts_validate_passes_on_valid_contracts():
    proc = ingest("contracts", "validate")
    assert proc.returncode == 0, _output(proc)


BROKEN_CONTRACTS = {
    "invalid_yaml": "contract: broken\nversion: 1\ncolumns: [unclosed\n",
    "unknown_type": """
contract: broken
version: 1
target: {table: t, key: [a], on_conflict: update}
columns:
  - {name: a, source_name: A, type: money, required: true}
""",
    "duplicate_rule_code": """
contract: broken
version: 1
target: {table: t, key: [a], on_conflict: update}
columns:
  - {name: a, source_name: A, type: int, required: true}
rules:
  - {code: SAME, column: a, check: ">= 0", message: one}
  - {code: SAME, column: a, check: "< 10", message: two}
""",
    "rule_on_unknown_column": """
contract: broken
version: 1
target: {table: t, key: [a], on_conflict: update}
columns:
  - {name: a, source_name: A, type: int, required: true}
rules:
  - {code: R1, column: nope, check: ">= 0", message: bad column}
""",
    "missing_target": """
contract: broken
version: 1
columns:
  - {name: a, source_name: A, type: int, required: true}
""",
    "version_does_not_match_file_name": """
contract: broken
version: 7
target: {table: t, key: [a], on_conflict: update}
columns:
  - {name: a, source_name: A, type: int, required: true}
""",
}


@pytest.mark.parametrize("case", sorted(BROKEN_CONTRACTS))
def test_contracts_validate_fails_fast_with_a_readable_error(tmp_path, case):
    contracts = tmp_path / "contracts"
    shutil.copytree(harness.FIXTURE_CONTRACTS, contracts)
    (contracts / "broken").mkdir()
    (contracts / "broken" / "v1.yaml").write_text(BROKEN_CONTRACTS[case], encoding="utf-8")

    proc = ingest("contracts", "validate", **{harness.ENV_CONTRACTS_DIR: str(contracts)})
    out = _output(proc)
    assert proc.returncode != 0, out
    assert "broken" in out, "the error must name the offending contract"
    assert "Traceback" not in out, "errors must be readable, not a stack trace"
