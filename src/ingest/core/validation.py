"""Chunk validation → structured rejections. src/ingest/core/validation.py — spec §7."""

from dataclasses import dataclass, field

import pandas as pd

from ingest.core.contracts import Contract
from ingest.core.hashing import sha256_bytes

BUILTIN_FALLBACK = "TYPE_MISMATCH"


@dataclass
class Rejection:
    row_number: int
    reason_code: str
    reason_message: str
    column_name: str | None = None
    offending_value: str | None = None


@dataclass
class ChunkResult:
    accepted: pd.DataFrame
    rejections: list[Rejection] = field(default_factory=list)
    rejected_row_numbers: set[int] = field(default_factory=set)


def _check_to_code(check: str | None) -> str:
    c = str(check or "").lower()
    if "not_null" in c or "nullable" in c:
        return "MISSING_REQUIRED"
    if "coerce" in c or "dtype" in c or "type" in c:
        return "TYPE_MISMATCH"
    if c in (
        "greater_than_or_equal_to",
        "less_than_or_equal_to",
        "in_range",
        "greater_than",
        "less_than",
    ):
        return "OUT_OF_RANGE"
    if "isin" in c:
        return "NOT_IN_SET"
    if "str_matches" in c or "pattern" in c:
        return "PATTERN_MISMATCH"
    if "str_length" in c or "length" in c:
        return "OUT_OF_RANGE"
    if "unique" in c:
        return "DUPLICATE_KEY_IN_CHUNK"
    return BUILTIN_FALLBACK


def _row_key(row: pd.Series, contract: Contract) -> str:
    keys = contract.key_columns
    if keys is None:
        vals = "|".join(
            "" if pd.isna(row.get(c.name)) else str(row.get(c.name)) for c in contract.columns
        )
        return sha256_bytes(vals.encode("utf-8"))
    return "|".join("" if pd.isna(row.get(k)) else str(row.get(k)) for k in keys)


def _apply_comparison_rule(df: pd.DataFrame, col: str, check: str) -> pd.Series:
    """Return boolean mask of VALID rows for checks like '>= 0', '< 1000000'."""
    import operator
    import re

    m = re.match(r"\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$", check)
    if not m:
        raise ValueError(f"Unsupported check expression: {check!r}")
    op_s, raw = m.groups()
    ops = {
        ">=": operator.ge,
        "<=": operator.le,
        "==": operator.eq,
        "!=": operator.ne,
        ">": operator.gt,
        "<": operator.lt,
    }
    op = ops[op_s]
    try:
        val: object = float(raw)
    except ValueError:
        val = raw.strip("'\"")
    s = df[col]
    if isinstance(val, float):
        return s.apply(
            lambda v: False if pd.isna(v) else bool(op(float(v), val)) if _is_num(v) else False
        )
    return s.apply(lambda v: False if pd.isna(v) else bool(op(str(v), val)))


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _looks_like_comparison(check: str) -> bool:
    import re

    return bool(re.match(r"\s*(>=|<=|==|!=|>|<)\s*", check))


def _load_custom(fn_path: str):
    import importlib

    mod, _, fn = fn_path.rpartition(".")
    if not mod:
        raise ValueError(f"custom rule must be dotted path, got {fn_path!r}")
    return getattr(importlib.import_module(mod), fn)


def validate_chunk(
    df: pd.DataFrame,
    contract: Contract,
    row_offset: int,
    ref_values: dict[tuple[str, str], set] | None = None,
) -> ChunkResult:
    from ingest.core.contracts import build_pandera_schema

    ref_values = ref_values or {}
    rejections: list[Rejection] = []
    bad_idx: set = set()

    def reject(idx, code: str, msg: str, col: str | None = None):
        pos = df.index.get_loc(idx) if idx in df.index else 0
        row_number = row_offset + (pos if isinstance(pos, int) else 0) + 1
        try:
            off = df.at[idx, col] if col else None
        except KeyError:
            off = None
        off_s = None if off is None or (isinstance(off, float) and pd.isna(off)) else str(off)
        rejections.append(Rejection(row_number, code, msg, col, off_s))
        bad_idx.add(idx)

    # 1. Required missing (explicit, deterministic across pandera versions).
    for c in contract.columns:
        if c.required and c.name in df.columns:
            s = df[c.name]
            missing = s.isna() | (s.astype(str).str.strip() == "") if c.type == "str" else s.isna()
            for idx in df.index[missing.fillna(False)]:
                reject(idx, "MISSING_REQUIRED", f"{c.name} is required", c.name)

    # 2. Pandera schema (lazy) for types / sets / patterns / ranges.
    schema = build_pandera_schema(contract)
    try:
        schema.validate(df, lazy=True)
    except Exception as e:  # pandera SchemaErrors
        cases = getattr(e, "failure_cases", None)
        if cases is not None:
            for _, r in cases.iterrows():
                col = r.get("column")
                col = None if col in (None, "SCHEMA") else str(col)
                code = _check_to_code(r.get("check"))
                # Prefer MISSING_REQUIRED when a required col is null.
                if code == "TYPE_MISMATCH" and col:
                    spec = next((c for c in contract.columns if c.name == col), None)
                    if spec and spec.required:
                        try:
                            if pd.isna(r.get("failure_case")):
                                code = "MISSING_REQUIRED"
                        except (TypeError, ValueError):
                            pass
                idx = r.get("index")
                # dedupe identical (idx, code, col) from our explicit pass
                if idx in df.index and not any(
                    x.reason_code == code
                    and x.column_name == col
                    and x.row_number == row_offset + df.index.get_loc(idx) + 1
                    for x in rejections
                ):
                    msg_full = f"{col} {r.get('check')} failed" if col else str(r.get("check"))
                    reject(idx, code, msg_full, col)

    # 3. Contract rules (own code/message).
    for rule in contract.rules:
        col = rule.column
        if col not in df.columns:
            continue
        if rule.check == "exists_in":
            valid = ref_values.get(
                ((rule.reference or {}).get("table"), (rule.reference or {}).get("column"))
            )
            if valid is None:
                continue  # pipeline supplies; missing means skip (no reference loaded)
            s = df[col]
            for idx, v in s.items():
                if pd.isna(v) or (isinstance(v, str) and v.strip() == ""):
                    continue  # nulls already reported as MISSING_REQUIRED
                if str(v) not in {str(x) for x in valid} and v not in valid:
                    reject(idx, rule.code, rule.message, col)
        elif rule.custom or ("." in rule.check and not _looks_like_comparison(rule.check)):
            # custom dotted path takes precedence if set; otherwise fall through to comparison
            fn_path = rule.custom or rule.check
            fn = _load_custom(fn_path)
            mask = fn(df)
            for idx, ok in mask.items():
                if not bool(ok):
                    reject(idx, rule.code, rule.message, col)
        else:
            try:
                mask = _apply_comparison_rule(df, col, rule.check)
            except ValueError:
                continue
            for idx, ok in mask.items():
                if not bool(ok):
                    reject(idx, rule.code, rule.message, col)

    # 4. Duplicate keys inside this chunk: keep first, reject rest.
    seen: dict[str, object] = {}
    for idx, row in df.iterrows():
        if idx in bad_idx:
            continue
        k = _row_key(row, contract)
        if k in seen:
            reject(idx, "DUPLICATE_KEY_IN_CHUNK", f"duplicate key within chunk: {k[:16]}…", None)
        else:
            seen[k] = idx

    rejected_numbers = {r.row_number for r in rejections}
    accepted = df.drop(index=list(bad_idx), errors="ignore")
    return ChunkResult(
        accepted=accepted, rejections=rejections, rejected_row_numbers=rejected_numbers
    )
