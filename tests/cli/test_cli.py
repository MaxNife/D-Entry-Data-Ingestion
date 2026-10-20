"""CLI tests (spec s9): run the `ingest` console script as a subprocess."""

import subprocess
import sys

import pytest

import harness

pytestmark = pytest.mark.cli

FIXTURES = str(harness.TESTS_DIR / "fixtures")


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ingest.cli", *args],
        capture_output=True,
        text=True,
        env=harness.subprocess_env(),
        cwd=str(harness.REPO_ROOT),
        timeout=60,
    )


class TestUpload:
    def test_upload_clean_file(self, db):
        result = _run_cli("upload", f"{FIXTURES}/clean_scores.csv", "--contract", "score_test")
        assert result.returncode == 0
        assert "completed" in result.stdout.lower() or "batch" in result.stdout.lower()

    def test_upload_prints_summary(self, db):
        result = _run_cli("upload", f"{FIXTURES}/dirty_scores.csv", "--contract", "score_test")
        assert result.returncode == 0
        # Should mention partial or show counts
        output = result.stdout.lower()
        assert "partial" in output or "rejected" in output or "batch" in output

    def test_upload_nonexistent_file_fails(self, db):
        result = _run_cli("upload", "/nonexistent/file.csv", "--contract", "score_test")
        assert result.returncode != 0


class TestReprocess:
    def test_reprocess_partial_batch(self, db):
        # First upload
        upload = _run_cli("upload", f"{FIXTURES}/dirty_scores.csv", "--contract", "score_test")
        assert upload.returncode == 0
        # Extract batch_id from output (implementation should print it)
        # Then reprocess
        lines = upload.stdout.strip().split("\n")
        batch_id = None
        for line in lines:
            if "batch_id" in line.lower() or "batch" in line.lower():
                # Try to extract UUID
                import re
                match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", line)
                if match:
                    batch_id = match.group()
                    break

        if batch_id:
            result = _run_cli("reprocess", batch_id)
            assert result.returncode == 0


class TestBatches:
    def test_list_batches(self, db):
        _run_cli("upload", f"{FIXTURES}/clean_scores.csv", "--contract", "score_test")
        result = _run_cli("batches")
        assert result.returncode == 0

    def test_list_batches_with_status_filter(self, db):
        _run_cli("upload", f"{FIXTURES}/clean_scores.csv", "--contract", "score_test")
        result = _run_cli("batches", "--status", "completed")
        assert result.returncode == 0


class TestContractsValidate:
    def test_validates_all_contracts(self, db):
        result = _run_cli("contracts", "validate")
        assert result.returncode == 0
        assert "score_test" in result.stdout.lower() or "valid" in result.stdout.lower()

