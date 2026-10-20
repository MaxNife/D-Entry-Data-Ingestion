"""Expected constants used across the test suite.

These define the ground-truth values that the implementation must produce.
The build agent must NOT modify this file to make tests pass; instead the
implementation must conform to these expectations.

Provenance (hand-derived, NOT copied from implementation output):
- MIXED_* comes from reading tests/fixtures/mixed.csv (20 data rows) against
  contracts/txn_test/v1.yaml (v1: Type in {PAYMENT, TRANSFER, CASH_OUT},
  Amount max 1000000, rule AMOUNT_NEGATIVE >= 0, rule CUSTOMER_NOT_FOUND
  exists_in ref_customers) and v2 (adds REFUND to the Type set).
- LOAD_ERROR_* comes from tests/fixtures/load_error.csv (15 valid rows) with
  chunk size 5, so chunks are rows 1-5 / 6-10 / 11-15; the trigger fails the
  middle chunk (rows 6-10).
- MESSAGES mirrors the contract YAML rule messages word-for-word.
"""

# Reference customers for the exists_in rule (txn_test contract).
# conftest.py seeds ref_customers with exactly these ids.
REF_CUSTOMERS = ["C001", "C002", "C003", "C004", "C005"]

# Expected reason messages: must equal the contract rule messages verbatim.
MESSAGES = {
    "GRADE_SCORE_MISMATCH": "grade A requires score >= 90",
    "AMOUNT_NEGATIVE": "amount must be non-negative",
    "CUSTOMER_NOT_FOUND": "customer_id not found in ref_customers",
}

# Built-in reason codes (spec section 5)
BUILTIN_CODES = [
    "MISSING_REQUIRED",
    "TYPE_MISMATCH",
    "OUT_OF_RANGE",
    "PATTERN_MISMATCH",
    "NOT_IN_SET",
    "REFERENCE_NOT_FOUND",
    "DUPLICATE_KEY_IN_CHUNK",
    "LOAD_ERROR",
]

# Batch statuses
STATUSES = ["pending", "processing", "completed", "partial", "failed"]

# Source headers of the txn fixtures, in file order. The reject-report CSV
# must append exactly these columns after reason_codes/reasons (spec s11).
TXN_HEADERS = ["TxnId", "Amount", "Type", "CustomerId", "TxnDate", "Memo"]

# ---- mixed.csv under txn_test v1 -------------------------------------------
# Row-by-row (1-based data row):
#   1 T1001 ok -> accept
#   2 T1002 amount -50.00 -> AMOUNT_NEGATIVE
#   3 T1003 amount abc -> TYPE_MISMATCH
#   4 T1004 type REFUND not in v1 set -> NOT_IN_SET
#   5 T1005 blank CustomerId (required) -> MISSING_REQUIRED
#   6 T1006 customer C999 unknown -> CUSTOMER_NOT_FOUND
#   7 X1007 txn id fails ^T[0-9]{4}$ -> PATTERN_MISMATCH
#   8 T1008 amount 2000000 > max -> OUT_OF_RANGE
#   9 T1009 amount -5.00 + REFUND type -> AMOUNT_NEGATIVE + NOT_IN_SET
#  10 T1010 date not-a-date -> TYPE_MISMATCH
#  11-13 T1011..T1013 ok -> accept
#  14 T1014 first occurrence -> accept (kept)
#  15 T1014 second occurrence, same chunk -> DUPLICATE_KEY_IN_CHUNK
#  16 T1016 ok -> accept
#  17 T1017 blank Amount (required) -> MISSING_REQUIRED
#  18 T1018 ok -> accept
#  19 T1019 DEBIT type + C999 customer -> NOT_IN_SET + CUSTOMER_NOT_FOUND
#  20 T1020 ok -> accept
MIXED_TOTAL = 20
MIXED_V1_ACCEPTED = 8
MIXED_V1_REJECTED = 12
# Rejected RECORDS (one row per record+reason): 12 rows + extras on rows
# 9 and 19, which each carry two codes.
MIXED_V1_RECORDS = 14

MIXED_V1_CODES: dict[int, set[str]] = {
    2: {"AMOUNT_NEGATIVE"},
    3: {"TYPE_MISMATCH"},
    4: {"NOT_IN_SET"},
    5: {"MISSING_REQUIRED"},
    6: {"CUSTOMER_NOT_FOUND"},
    7: {"PATTERN_MISMATCH"},
    8: {"OUT_OF_RANGE"},
    9: {"AMOUNT_NEGATIVE", "NOT_IN_SET"},
    10: {"TYPE_MISMATCH"},
    15: {"DUPLICATE_KEY_IN_CHUNK"},
    17: {"MISSING_REQUIRED"},
    19: {"NOT_IN_SET", "CUSTOMER_NOT_FOUND"},
}

# (row_number, reason_code) -> contract column name (None for the
# in-chunk duplicate, which the implementation reports without a column).
MIXED_V1_COLUMNS: dict[tuple[int, str], str | None] = {
    (2, "AMOUNT_NEGATIVE"): "amount",
    (3, "TYPE_MISMATCH"): "amount",
    (4, "NOT_IN_SET"): "txn_type",
    (5, "MISSING_REQUIRED"): "customer_id",
    (6, "CUSTOMER_NOT_FOUND"): "customer_id",
    (7, "PATTERN_MISMATCH"): "txn_id",
    (8, "OUT_OF_RANGE"): "amount",
    (9, "AMOUNT_NEGATIVE"): "amount",
    (9, "NOT_IN_SET"): "txn_type",
    (10, "TYPE_MISMATCH"): "txn_date",
    (15, "DUPLICATE_KEY_IN_CHUNK"): None,
    (17, "MISSING_REQUIRED"): "amount",
    (19, "NOT_IN_SET"): "txn_type",
    (19, "CUSTOMER_NOT_FOUND"): "customer_id",
}

# reason_code -> record count (sums to MIXED_V1_RECORDS).
MIXED_V1_SUMMARY: dict[str, int] = {
    "AMOUNT_NEGATIVE": 2,
    "TYPE_MISMATCH": 2,
    "NOT_IN_SET": 3,
    "MISSING_REQUIRED": 2,
    "CUSTOMER_NOT_FOUND": 2,
    "PATTERN_MISMATCH": 1,
    "OUT_OF_RANGE": 1,
    "DUPLICATE_KEY_IN_CHUNK": 1,
}

# Original row values exactly as read (strings, source headers) for row 2.
MIXED_ROW_2: dict[str, str] = {
    "TxnId": "T1002",
    "Amount": "-50.00",
    "Type": "PAYMENT",
    "CustomerId": "C002",
    "TxnDate": "2026-10-01",
    "Memo": "refund, partial",
}

# ---- mixed.csv under txn_test v2 -------------------------------------------
# v2 only widens the Type set with REFUND: row 4 is accepted, row 9 keeps
# just AMOUNT_NEGATIVE. Everything else is unchanged.
MIXED_V2_ACCEPTED = 9
MIXED_V2_REJECTED = 11
MIXED_V2_RECORDS = 12

MIXED_V2_CODES: dict[int, set[str]] = {
    2: {"AMOUNT_NEGATIVE"},
    3: {"TYPE_MISMATCH"},
    5: {"MISSING_REQUIRED"},
    6: {"CUSTOMER_NOT_FOUND"},
    7: {"PATTERN_MISMATCH"},
    8: {"OUT_OF_RANGE"},
    9: {"AMOUNT_NEGATIVE"},
    10: {"TYPE_MISMATCH"},
    15: {"DUPLICATE_KEY_IN_CHUNK"},
    17: {"MISSING_REQUIRED"},
    19: {"NOT_IN_SET", "CUSTOMER_NOT_FOUND"},
}

MIXED_V2_SUMMARY: dict[str, int] = {
    "AMOUNT_NEGATIVE": 2,
    "TYPE_MISMATCH": 2,
    "NOT_IN_SET": 1,
    "MISSING_REQUIRED": 2,
    "CUSTOMER_NOT_FOUND": 2,
    "PATTERN_MISMATCH": 1,
    "OUT_OF_RANGE": 1,
    "DUPLICATE_KEY_IN_CHUNK": 1,
}

# Natural keys present in test_transactions after a v2 load/reprocess.
MIXED_V2_ACCEPTED_IDS = {
    "T1001", "T1004", "T1011", "T1012", "T1013",
    "T1014", "T1016", "T1018", "T1020",
}

# ---- load_error.csv (15 valid rows, chunk size 5) ---------------------------
# The Postgres trigger fails the chunk holding T4008, i.e. rows 6-10.
LOAD_ERROR_TOTAL = 15
LOAD_ERROR_FAILED_ROWS = {6, 7, 8, 9, 10}
