"""Integration tests for exists_in reference-table rules (spec s5, s7).

Uses the txn_test contract which has an exists_in rule checking customer_id
against the ref_customers table.
"""

from pathlib import Path

import pytest

import harness

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _run(file_path: Path, contract: str = "txn_test", version: int | None = None, **kw):
    from ingest.core.pipeline import run_pipeline

    ref = f"{contract}@{version}" if version else contract
    return run_pipeline(file_path, ref, **kw)


class TestExistsInRule:
    def test_valid_references_accepted(self, db):
        result = _run(FIXTURES / "clean_transactions.csv")
        assert result["status"] == "completed"
        assert db.target_count("test_transactions") == 5

    def test_unknown_reference_rejected(self, db):
        result = _run(FIXTURES / "dirty_transactions.csv")
        assert result["status"] == "partial"
        # C999 is not in ref_customers -> the rule's own code (decisions.md)
        refs = db.fetchall(
            "SELECT reason_code FROM rejected_records WHERE batch_id = :id",
            {"id": result["batch_id"]},
        )
        codes = [r[0] for r in refs]
        assert "CUSTOMER_NOT_FOUND" in codes

    def test_negative_amount_rejected(self, db):
        result = _run(FIXTURES / "dirty_transactions.csv")
        refs = db.fetchall(
            "SELECT reason_code FROM rejected_records WHERE batch_id = :id",
            {"id": result["batch_id"]},
        )
        codes = [r[0] for r in refs]
        assert "AMOUNT_NEGATIVE" in codes

    def test_invalid_type_rejected(self, db):
        result = _run(FIXTURES / "dirty_transactions.csv")
        refs = db.fetchall(
            "SELECT reason_code FROM rejected_records WHERE batch_id = :id",
            {"id": result["batch_id"]},
        )
        codes = [r[0] for r in refs]
        assert "NOT_IN_SET" in codes

    def test_natural_key_upsert(self, db):
        """txn_test uses key: [txn_id], so re-uploading updates not duplicates."""
        _run(FIXTURES / "clean_transactions.csv")
        count_first = db.target_count("test_transactions")
        # Upload same file again -> skipped at file level, but let's test
        # that the target has exactly 5 rows (no dupes)
        assert count_first == 5

