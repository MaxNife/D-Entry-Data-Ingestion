"""Simulated database error in one chunk (spec s14 acceptance criterion).

A failing chunk must:
- Leave all other chunks committed
- Mark that chunk as failed in batch_chunks
- Record every row in the failed chunk as LOAD_ERROR rejections
- Pipeline continues and finalizes
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import harness

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _make_large_csv(rows: int = 12) -> str:
    """Generate a CSV large enough to span multiple chunks (chunk size is 5 in tests)."""
    lines = ["Name,Score,Grade"]
    for i in range(rows):
        grade = "A" if (i % 5 == 0) else "B"
        score = 90 + (i % 10) if grade == "A" else 70 + (i % 10)
        lines.append(f"User{i},{score},{grade}")
    return "\n".join(lines) + "\n"


def test_failed_chunk_marks_load_error(tmp_path, db):
    """Simulate a DB error on chunk index 1; chunks 0 and 2+ must still commit."""
    csv_file = tmp_path / "multi_chunk.csv"
    csv_file.write_text(_make_large_csv(12), encoding="utf-8")

    call_count = {"n": 0}
    original_load = None

    def _sabotage_chunk(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:  # fail the second chunk
            raise RuntimeError("simulated DB connection lost")
        return original_load(*args, **kwargs)

    from ingest.core import loader

    original_load = loader.load_chunk

    with patch.object(loader, "load_chunk", side_effect=_sabotage_chunk):
        from ingest.core.pipeline import run_pipeline

        result = run_pipeline(csv_file, "score_test")

    # Pipeline should still complete (partial because of LOAD_ERROR rejections)
    assert result["status"] == "partial"

    # Check batch_chunks: at least one chunk is 'failed'
    failed_chunks = db.fetchall(
        "SELECT chunk_index, status, error FROM batch_chunks "
        "WHERE batch_id = :id AND status = 'failed'",
        {"id": result["batch_id"]},
    )
    assert len(failed_chunks) >= 1
    assert failed_chunks[0][2] is not None  # error message recorded

    # LOAD_ERROR rejections exist for the failed chunk's rows
    load_errors = db.count(
        "rejected_records",
        "batch_id = :id AND reason_code = 'LOAD_ERROR'",
        {"id": result["batch_id"]},
    )
    assert load_errors > 0

    # Other chunks committed: target table has rows from non-failed chunks
    assert db.target_count("test_scores") > 0

    # Counts still add up
    batch = db.batch(result["batch_id"])
    assert batch.accepted_rows + batch.rejected_rows == batch.total_rows

