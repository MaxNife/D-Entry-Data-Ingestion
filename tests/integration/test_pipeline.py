"""Integration tests for the full pipeline (spec s6).

Each test runs run_pipeline() against a real Postgres database.
Fixtures provide an empty DB (truncated tables) and clean upload dir.
"""

from pathlib import Path

import pytest

import harness

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _run(file_path: Path, contract: str = "score_test", version: int | None = None, **kw):
    from ingest.core.pipeline import run_pipeline

    ref = f"{contract}@{version}" if version else contract
    return run_pipeline(file_path, ref, **kw)


class TestCleanFile:
    """A file where every row passes validation."""

    def test_status_is_completed(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        assert result["status"] == "completed"

    def test_all_rows_accepted(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        batch = db.batch(result["batch_id"])
        assert batch.total_rows == 10
        assert batch.accepted_rows == 10
        assert batch.rejected_rows == 0

    def test_target_table_populated(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        assert db.target_count("test_scores") == 10

    def test_no_rejections_stored(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        assert db.count("rejected_records", "batch_id = :id", {"id": result["batch_id"]}) == 0

    def test_batch_chunks_recorded(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        chunks = db.count("batch_chunks", "batch_id = :id", {"id": result["batch_id"]})
        assert chunks >= 1

    def test_raw_file_stored_on_disk(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        batch = db.batch(result["batch_id"])
        upload_path = harness.UPLOAD_DIR / batch.file_sha256
        assert upload_path.exists(), "raw file must be stored under data/uploads/<sha256>/"


class TestDirtyFile:
    """A file with known bad rows -> partial success."""

    def test_status_is_partial(self, db):
        result = _run(FIXTURES / "dirty_scores.csv")
        assert result["status"] == "partial"

    def test_counts_add_up(self, db):
        result = _run(FIXTURES / "dirty_scores.csv")
        batch = db.batch(result["batch_id"])
        assert batch.accepted_rows + batch.rejected_rows == batch.total_rows

    def test_known_rejection_count(self, db):
        """dirty_scores.csv has 12 rows; rows 2,3,4,5,6,7,10 are rejected (7 rows).

        Row 6 (Eve,85,A) is rejected GRADE_SCORE_MISMATCH, exactly as
        unit/test_validation_unit.py pins. Row 8 duplicates row 1 but sits in
        a different chunk (suite chunk size is 5), so within-chunk duplicate
        detection cannot fire; the DB upsert collapses it instead.
        """
        result = _run(FIXTURES / "dirty_scores.csv")
        batch = db.batch(result["batch_id"])
        assert batch.rejected_rows == 7
        assert batch.accepted_rows == 5

    def test_target_table_has_only_accepted(self, db):
        # 5 rows accepted but rows 1 and 8 share one row_hash key, so the
        # upsert leaves 4 distinct target rows.
        result = _run(FIXTURES / "dirty_scores.csv")
        assert db.target_count("test_scores") == 4

    def test_rejected_records_have_correct_codes(self, db):
        result = _run(FIXTURES / "dirty_scores.csv")
        codes = db.fetchall(
            "SELECT DISTINCT reason_code FROM rejected_records WHERE batch_id = :id",
            {"id": result["batch_id"]},
        )
        code_set = {r[0] for r in codes}
        assert "MISSING_REQUIRED" in code_set
        assert "TYPE_MISMATCH" in code_set
        assert "OUT_OF_RANGE" in code_set
        assert "NOT_IN_SET" in code_set
        assert "GRADE_SCORE_MISMATCH" in code_set  # row 6, Eve,85,A (see unit test)

    def test_cross_chunk_duplicate_collapses_via_upsert(self, db):
        """Rows 1 and 8 are identical but land in different chunks (size 5),
        so DUPLICATE_KEY_IN_CHUNK cannot fire; the row_key upsert keeps one
        target row (decisions.md: cross-chunk dupes resolved by DB upsert)."""
        result = _run(FIXTURES / "dirty_scores.csv")
        by_row = db.codes_by_row(result["batch_id"])
        assert "DUPLICATE_KEY_IN_CHUNK" not in by_row.get(8, set())
        alices = db.fetchall("SELECT count(*) FROM test_scores WHERE name = 'Alice'")
        assert alices[0][0] == 1


class TestMissingHeader:
    """A file missing a required column -> failed.

    Uses its own score-style file: the shared missing_header.csv fixture is
    txn-style (it must name CustomerId for the txn API tests).
    """

    @pytest.fixture
    def broken_file(self, tmp_path):
        p = tmp_path / "missing_header_scores.csv"
        p.write_text("Name,Points,Grade\nAlice,95,A\nBob,82,B\n", encoding="utf-8")
        return p

    def test_status_is_failed(self, db, broken_file):
        result = _run(broken_file)
        assert result["status"] == "failed"

    def test_failure_reason_set(self, db, broken_file):
        result = _run(FIXTURES / "dirty_scores.csv")  # ensure DB is warm
        result = _run(broken_file)
        batch = db.batch(result["batch_id"])
        assert batch.failure_reason is not None
        assert "Score" in batch.failure_reason or "header" in batch.failure_reason.lower()

    def test_zero_rows_loaded(self, db, broken_file):
        result = _run(broken_file)
        assert db.target_count("test_scores") == 0

    def test_no_rejections(self, db, broken_file):
        result = _run(broken_file)
        assert db.count("rejected_records", "batch_id = :id", {"id": result["batch_id"]}) == 0


class TestAllRejected:
    """Every row fails -> still partial (spec: no separate 'rejected' status)."""

    def test_status_is_partial_not_failed(self, db):
        result = _run(FIXTURES / "all_rejected.csv")
        assert result["status"] == "partial"

    def test_zero_accepted(self, db):
        result = _run(FIXTURES / "all_rejected.csv")
        batch = db.batch(result["batch_id"])
        assert batch.accepted_rows == 0
        assert batch.rejected_rows == 3


class TestEmptyFile:
    """Header only, no data rows."""

    def test_completed_with_zero_rows(self, db):
        result = _run(FIXTURES / "empty_file.csv")
        assert result["status"] == "completed"
        batch = db.batch(result["batch_id"])
        assert batch.total_rows == 0
        assert batch.accepted_rows == 0

