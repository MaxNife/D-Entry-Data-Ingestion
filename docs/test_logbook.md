# Test Logbook

This file records the status of every test run performed by the implementing agent.
The test suite was authored independently and defines the acceptance criteria for the platform.

## Rules for the implementing agent

1. After implementing or modifying any module, run the relevant tests and record results here.
2. Format each entry as shown in the template below.
3. If you believe a test is unreasonable, illogical, or unsuited, state so explicitly in your entry with reasoning. Do NOT silently skip tests.
4. All tests must pass before the platform is considered complete.

---

## Entry template

```
### [DATE/TIME] — [AGENT/SESSION ID]

**Tests run:** [subset or "full suite"]
**Command:** `[exact command used]`

| Test file | Test name | Status | Notes |
|-----------|-----------|--------|-------|
| tests/unit/test_hashing.py | test_sha256_file_matches_stdlib | PASS | |
| ... | ... | ... | ... |

**Summary:** X passed, Y failed, Z skipped
**Failures (if any):**
- `test_name`: [error message]

**Objections / concerns (if any):**
- [If a test seems unreasonable, explain why here]

---
```

---

## Log entries

### 2026-10-15 ~14:45 UTC — build agent

**Tests run:** `tests/unit` + `tests/repo` (DB-free), then the four integration files that
are complete enough to import and execute.
**Command:**
`uv run pytest tests/unit tests/repo -q --tb=line -p no:cacheprovider`
`uv run pytest tests/integration/test_pipeline.py tests/integration/test_idempotency.py tests/integration/test_chunk_failure.py -q --tb=line -p no:cacheprovider`

**Results**

| Area | Status | Notes |
|------|--------|-------|
| tests/unit (28) | PASS | incl. exact EXPECTED code map, custom rule, non-mutation |
| tests/repo (13) | PASS | structure, README sections, workflows, contracts validate, ruff |
| tests/integration/test_pipeline.py | 15 pass / 8 fail | all 8 attributable to corrupt fixtures + one suite contradiction (below) |
| tests/integration/test_idempotency.py | 8 pass / 1 fail | one fail: `test_completed_batch_cannot_be_reprocessed` (corrupt fixture makes the batch `partial`) |
| tests/integration/test_chunk_failure.py | PASS | LOAD_ERROR path, other chunks commit |

**Summary:** 51 passed, 9 failed, 0 skipped in the runnable set.
The remaining test modules (`test_api_batches`, `test_api_misc`, `test_api_rejections`,
`test_load_errors`, `test_reject_report`, `test_reprocess`, `integration/test_cli`) cannot be
collected at all — see Blockers 1 and 2.

**Implementation bug found and fixed (real, in my code):**

- `core/loader.py` bound accepted values as text into typed Postgres columns. With the
  suite's target tables, every chunk failed with
  `column "batch_id" is of type uuid but expression is of type character varying` (and a
  numeric/date string would hit the same wall), so every row became `LOAD_ERROR`.
  Fix: reflect the real target table in `ensure_target_table` (so the actual column types
  drive binding) and coerce cells to Python values in `_norm` (`int`/`float`/`date`/`bool`).
  Verified directly against Postgres: `clean_scores.csv` → 10/10 `completed`;
  `dirty_scores.csv` → 12 rows, accepted 5, rejected 7 with codes
  MISSING_REQUIRED / TYPE_MISMATCH / OUT_OF_RANGE / GRADE_SCORE_MISMATCH / NOT_IN_SET.
  This fix is required regardless of the test-delivery problems below.

**Blockers (test suite cannot be run as delivered — re-delivery requested):**

1. **`tests/harness.py` is truncated.** It ends at `Db.target_count`. The integration/api/e2e
   modules import a large set of names that are not defined, and `pytest --collect-only`
   fails with import errors on 7 modules. Missing:
   `upload`, `upload_and_wait`, `wait_for_batch`, `reprocess`, `fixture`, `page_items`,
   `page_total`, `summary_counts`, `contract_versions`, `assert_error_body`, `cli_command`,
   `sha256_file`, `METRIC_KEYS`, `ENV_CONTRACTS_DIR`, `FIXTURE_CONTRACTS`, `UVICORN_APP`,
   `UVICORN_FACTORY`, and the `Db` methods `batches`, `target`, `codes_by_row`, `rejections`,
   `chunks`, `execute`.
2. **`tests/expected.py` is truncated.** It ends at `STATUSES`; tests reference
   `TXN_HEADERS`, `MIXED_TOTAL`, `MIXED_ROW_2`, `MIXED_V1_ACCEPTED/CODES/COLUMNS/RECORDS/REJECTED/SUMMARY`,
   `MIXED_V2_ACCEPTED/ACCEPTED_IDS/CODES/RECORDS/REJECTED`, `LOAD_ERROR_TOTAL`,
   `LOAD_ERROR_FAILED_ROWS`.
3. **Eight fixture CSVs are corrupt**: each has a literal `</content>` line appended, which
   the pipeline reads as a data row. Affected: `all_rejected.csv`, `clean_scores.csv`,
   `clean_transactions.csv`, `dirty_scores.csv`, `dirty_transactions.csv`, `dup_keys.csv`,
   `empty_file.csv`, `missing_header.csv`. Example: `clean_scores.csv` becomes 11 rows with
   one all-null row → status `partial`; every count in `test_pipeline` is off by that row.
4. **12 test files also carried a trailing `</content>` line** (`harness.py`, `expected.py`,
   `api/test_api.py`, `cli/test_cli.py`, `e2e/test_dashboard.py`, `test_repo_deliverables.py`,
   `integration/test_chunk_failure.py`, `test_idempotency.py`, `test_pipeline.py`,
   `test_reference_rules.py`, `repo/test_repo.py`, `unit/test_contracts.py`,
   `unit/test_hashing.py`). I removed that one line from the `.py` files only (it is not
   valid Python and made the whole suite unimportable); I left the fixtures untouched.

**Harness adaptations (rule 2, `harness.py` is the one adaptable file):**

- Removed the stray trailing `</content>` line from `harness.py` so it parses.
- No behavioural adaptation made; `harness.py` is missing its helpers (Blocker 1), which I
  cannot supply without editing test-author code.

**Disputed tests / suite contradictions (rule 3):**

- **`tests/integration/test_pipeline.py::TestDirtyFile` vs `tests/unit/test_validation_unit.py`.**
  The unit test asserts row 6 / 106 `Eve,85,A` is rejected `GRADE_SCORE_MISMATCH`
  (`test_every_failure_is_collected_with_offset_row_numbers` line 32 and
  `test_contract_rules_use_their_own_code_and_message`). `TestDirtyFile.test_known_rejection_count`
  (docstring and `== 7`) instead requires `Eve,85,A` to be accepted, and
  `test_target_table_has_only_accepted` requires 5 distinct accepted rows. With the rule the
  unit test pins down, `dirty_scores.csv` yields accepted 4 / rejected 8 (rows 2,3,4,5,6,7,10,
  and row 8 if duplicates are rejected). Both cannot hold for the same contract and data.
  I have followed the unit test (it is self-consistent and pins the exact codes); the three
  `TestDirtyFile` count assertions cannot pass until a human rules.
- **Duplicate semantics.** `test_multiple_rejections_per_row_stored` expects the row-8
  duplicate of row 1 to be rejected `DUPLICATE_KEY_IN_CHUNK`, but with chunk size 3 rows 1
  and 8 are in different chunks — i.e. it requires cross-chunk duplicate rejection, while the
  built-in code names it "in chunk". `docs/decisions.md` recorded upsert-only handling.
  Left unchanged pending re-delivery.

**Objections / concerns (if any):**

- The suite cannot be treated as the independent acceptance spec in its delivered state: it
  does not import, two of its own files and eight fixtures are truncated/corrupt, and the
  fixture contracts and CSVs disagree on header names (`clean_transactions.csv` /
  `dirty_transactions.csv` use `TxnID,CustomerID,Date`; the `txn_test` fixture contract and
  all other transaction fixtures use `TxnId,CustomerId,TxnDate`), so `test_reference_rules.py`
  cannot pass against either contract as written.
- Per the user's ruling, I have paused test-infrastructure work and am waiting for the
  complete `tests/harness.py`, `tests/expected.py`, and clean fixtures.

---