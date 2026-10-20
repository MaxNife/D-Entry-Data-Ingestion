"""Unit tests for core.contracts (spec s5): load, validate, hash YAML contracts."""

import pytest

import harness

pytestmark = pytest.mark.unit


def test_load_score_test_contract():
    contract = harness.load_contract("score_test", 1)
    assert contract is not None


def test_contract_has_expected_fields():
    contract = harness.load_contract("score_test", 1)
    assert contract.name == "score_test"
    assert contract.version == 1
    assert contract.target_table == "test_scores"


def test_contract_columns_parsed():
    contract = harness.load_contract("score_test", 1)
    col_names = [c.name for c in contract.columns]
    assert "name" in col_names
    assert "score" in col_names
    assert "grade" in col_names


def test_contract_column_types():
    contract = harness.load_contract("score_test", 1)
    cols = {c.name: c for c in contract.columns}
    assert cols["score"].type == "int"
    assert cols["name"].type == "str"
    assert cols["grade"].type == "str"


def test_contract_required_flags():
    contract = harness.load_contract("score_test", 1)
    cols = {c.name: c for c in contract.columns}
    assert cols["name"].required is True
    assert cols["score"].required is True
    assert cols["grade"].required is False


def test_contract_in_set():
    contract = harness.load_contract("score_test", 1)
    cols = {c.name: c for c in contract.columns}
    assert cols["grade"].in_set == ["A", "B", "C", "D", "F"]


def test_contract_min_max():
    contract = harness.load_contract("score_test", 1)
    cols = {c.name: c for c in contract.columns}
    assert cols["score"].min == 0
    assert cols["score"].max == 100


def test_contract_rules_parsed():
    contract = harness.load_contract("score_test", 1)
    assert len(contract.rules) >= 1
    rule = contract.rules[0]
    assert rule.code == "GRADE_SCORE_MISMATCH"
    assert rule.message == "grade A requires score >= 90"


def test_contract_hash_is_deterministic():
    contract = harness.load_contract("score_test", 1)
    assert contract.contract_hash is not None
    assert len(contract.contract_hash) == 64  # SHA-256 hex

    # Loading again gives same hash
    contract2 = harness.load_contract("score_test", 1)
    assert contract2.contract_hash == contract.contract_hash


def test_nonexistent_contract_raises():
    with pytest.raises((FileNotFoundError, ValueError, KeyError)):
        harness.load_contract("nonexistent_contract", 1)


def test_nonexistent_version_raises():
    with pytest.raises((FileNotFoundError, ValueError, KeyError)):
        harness.load_contract("score_test", 999)


def test_contract_key_strategy():
    contract = harness.load_contract("score_test", 1)
    assert contract.key == "row_hash" or isinstance(contract.key, list)


def test_contract_on_conflict():
    contract = harness.load_contract("score_test", 1)
    assert contract.on_conflict in ("update", "skip")

