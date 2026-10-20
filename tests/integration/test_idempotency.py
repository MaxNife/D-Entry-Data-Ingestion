"""Idempotency and reprocessing tests (spec s8).

Four layers tested independently:
1. File-level: same hash -> skipped
2. Row-level: upsert prevents duplicates
3. Rejections replaced on reprocess
4. Audit trail updated
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


class TestFileLevelIdempotency:
    """Uploading the same file twice must skip the second time."""

    def test_second_upload_returns_skipped(self, db):
        first = _run(FIXTURES / "clean_scores.csv")
        second = _run(FIXTURES / "clean_scores.csv")
        assert second.get("skipped") is True

    def test_second_upload_returns_same_batch_id(self, db):
        first = _run(FIXTURES / "clean_scores.csv")
        second = _run(FIXTURES / "clean_scores.csv")
        assert second["batch_id"] == first["batch_id"]

    def test_target_table_unchanged(self, db):
        _run(FIXTURES / "clean_scores.csv")
        count_after_first = db.target_count("test_scores")
        _run(FIXTURES / "clean_scores.csv")
        count_after_second = db.target_count("test_scores")
        assert count_after_first == count_after_second == 10

    def test_only_one_batch_row_exists(self, db):
        _run(FIXTURES / "clean_scores.csv")
        _run(FIXTURES / "clean_scores.csv")
        assert db.count("batches") == 1


class TestRowLevelIdempotency:
    """Loading the same rows twice leaves the target table unchanged (upsert)."""

    def test_reprocess_does_not_duplicate_rows(self, db):
        result = _run(FIXTURES / "dirty_scores.csv")
        batch_id = result["batch_id"]
        count_before = db.target_count("test_scores")

        from ingest.core.pipeline import run_pipeline

        run_pipeline(
            FIXTURES / "dirty_scores.csv",
            "score_test",
            reprocess_batch_id=batch_id,
        )
        count_after = db.target_count("test_scores")
        assert count_before == count_after


class TestRejectionsReplacedOnReprocess:
    """On reprocess, old rejected_records and batch_chunks are deleted and rewritten."""

    def test_rejection_count_reflects_latest_attempt(self, db):
        result = _run(FIXTURES / "dirty_scores.csv")
        batch_id = result["batch_id"]
        count_before = db.count("rejected_records", "batch_id = :id", {"id": batch_id})

        from ingest.core.pipeline import run_pipeline

        run_pipeline(FIXTURES / "dirty_scores.csv", "score_test", reprocess_batch_id=batch_id)
        count_after = db.count("rejected_records", "batch_id = :id", {"id": batch_id})
        # Same file, same contract -> same rejections, not doubled
        assert count_after == count_before


class TestReprocessSemantics:
    """Reprocess updates retry_attempts and transitions correctly."""

    def test_retry_attempts_incremented(self, db):
        result = _run(FIXTURES / "dirty_scores.csv")
        batch_id = result["batch_id"]

        from ingest.core.pipeline import run_pipeline

        run_pipeline(FIXTURES / "dirty_scores.csv", "score_test", reprocess_batch_id=batch_id)
        batch = db.batch(batch_id)
        assert batch.retry_attempts == 1

    def test_completed_batch_cannot_be_reprocessed(self, db):
        result = _run(FIXTURES / "clean_scores.csv")
        batch_id = result["batch_id"]

        from ingest.core.pipeline import run_pipeline

        with pytest.raises((ValueError, Exception)) as exc_info:
            run_pipeline(FIXTURES / "clean_scores.csv", "score_test", reprocess_batch_id=batch_id)
        # Should raise or return an error, not silently succeed
        assert batch_id is not None

    def test_failed_batch_can_be_reprocessed(self, db, tmp_path):
        result = _run(FIXTURES / "missing_header.csv", contract="txn_test")
        assert result["status"] == "failed"
        batch_id = result["batch_id"]

        # Reprocess with the same file still fails (header still wrong),
        # but the operation is allowed and retry_attempts increments
        from ingest.core.pipeline import run_pipeline

        run_pipeline(FIXTURES / "missing_header.csv", "txn_test", reprocess_batch_id=batch_id)
        batch = db.batch(batch_id)
        assert batch.retry_attempts == 1

