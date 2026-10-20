"""validate_chunk() in isolation (spec s7). Uses score_test, which has no exists_in
rule, so no database is needed."""

import math

import pandas as pd
import pytest

import harness
from expected import MESSAGES

pytestmark = pytest.mark.unit

CHUNK = """Name,Score,Grade
Alice,95,A
,50,C
Bob,abc,B
Cara,150,B
Dan,-1,C
Eve,85,A
Finn,70,Z
Alice,95,A
Gus,60,
"""

# With row_offset=100 the first data row of this chunk is row 101.
EXPECTED = {
    102: {"MISSING_REQUIRED"},
    103: {"TYPE_MISMATCH"},
    104: {"OUT_OF_RANGE"},
    105: {"OUT_OF_RANGE"},
    106: {"GRADE_SCORE_MISMATCH"},
    107: {"NOT_IN_SET"},
    108: {"DUPLICATE_KEY_IN_CHUNK"},  # identical to row 101 -> same row_hash key
}


@pytest.fixture(scope="module")
def contract():
    return harness.load_contract("score_test", 1)


def _codes_by_row(result) -> dict[int, set[str]]:
    out: dict[int, set[str]] = {}
    for r in result.rejections:
        out.setdefault(r.row_number, set()).add(r.reason_code)
    return out


def _value(df: pd.DataFrame, row: int, contract_name: str, source_name: str):
    col = contract_name if contract_name in df.columns else source_name
    return df.iloc[row][col]


def test_chunk_result_has_the_spec_shape(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 100)
    assert isinstance(result.accepted, pd.DataFrame)
    assert isinstance(result.rejections, list)
    assert isinstance(result.rejected_row_numbers, set)
    for r in result.rejections:
        for field in ("row_number", "reason_code", "reason_message", "column_name", "offending_value"):
            assert hasattr(r, field), field
        assert isinstance(r.row_number, int)
        assert r.reason_code and r.reason_message


def test_every_failure_is_collected_with_offset_row_numbers(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 100)
    assert _codes_by_row(result) == EXPECTED
    assert result.rejected_row_numbers == set(EXPECTED)


def test_row_offset_zero_means_first_data_row_is_1(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 0)
    assert result.rejected_row_numbers == {n - 100 for n in EXPECTED}


def test_accepted_holds_exactly_the_rows_with_zero_rejections(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 100)
    assert len(result.accepted) == 2
    names = {str(_value(result.accepted, i, "name", "Name")) for i in range(len(result.accepted))}
    assert names == {"Alice", "Gus"}


def test_rejections_only_reference_rejected_rows(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 100)
    assert {r.row_number for r in result.rejections} == result.rejected_row_numbers


def test_one_row_can_carry_several_rejections(contract):
    chunk = "Name,Score,Grade\nZed,150,Z\n"
    result = harness.validate_chunk(harness.read_chunk(chunk), contract, 0)
    assert _codes_by_row(result) == {1: {"OUT_OF_RANGE", "NOT_IN_SET"}}
    assert len(result.rejections) == 2


def test_offending_values_are_reported(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 100)
    by_key = {(r.row_number, r.reason_code): r for r in result.rejections}
    assert by_key[(103, "TYPE_MISMATCH")].offending_value == "abc"
    assert by_key[(107, "NOT_IN_SET")].offending_value == "Z"
    assert math.isclose(float(by_key[(104, "OUT_OF_RANGE")].offending_value), 150)
    assert harness.column_is(by_key[(103, "TYPE_MISMATCH")].column_name, "score", "Score")
    assert harness.column_is(by_key[(107, "NOT_IN_SET")].column_name, "grade", "Grade")


def test_contract_rules_use_their_own_code_and_message(contract):
    result = harness.validate_chunk(harness.read_chunk(CHUNK), contract, 100)
    custom = [r for r in result.rejections if r.reason_code == "GRADE_SCORE_MISMATCH"]
    assert len(custom) == 1
    assert custom[0].reason_message == MESSAGES["GRADE_SCORE_MISMATCH"]


def test_validation_does_not_mutate_the_input(contract):
    df = harness.read_chunk(CHUNK)
    before = df.copy(deep=True)
    harness.validate_chunk(df, contract, 100)
    pd.testing.assert_frame_equal(df, before)


def test_clean_chunk_has_no_rejections(contract):
    chunk = "Name,Score,Grade\nAda,99,A\nBen,0,C\nCy,100,\n"
    result = harness.validate_chunk(harness.read_chunk(chunk), contract, 0)
    assert result.rejections == []
    assert result.rejected_row_numbers == set()
    assert len(result.accepted) == 3
