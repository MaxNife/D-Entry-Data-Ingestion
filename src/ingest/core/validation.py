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
    import re

    c = re.sub(r"\(.*\)$", "", str(check or "").lower().strip())
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


def _is_blank(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        return False
    return isinstance(v, str) and v.strip() == ""


def _looks_like_comparison(check: str) -> bool:
    import re

    return bool(re.match(r"\s*(>=|<=|==|!=|>|<)\s*", check))


def _load_custom(fn_path: str):
    """Resolve 'pkg.mod:attr' or 'pkg.mod.attr' to a callable."""
    import importlib

    path = fn_path.strip()
    if ":" in path:
        mod, _, attr = path.partition(":")
    else:
        mod, _, attr = path.rpartition(".")
    if not mod or not attr:
        raise ValueError(f"custom rule must be a dotted path, got {fn_path!r}")
    try:
        module = importlib.import_module(mod)
    except ImportError as e:
        raise ImportError(f"custom rule {fn_path!r}: cannot import {mod!r}: {e}") from e
    try:
        return getattr(module, attr)
    except AttributeError:
        raise ImportError(f"custom rule {fn_path!r}: {mod!r} has no attribute {attr!r}") from None


def _maybe_rename(df: pd.DataFrame, contract: Contract) -> pd.DataFrame:
    """Rename source headers to contract names on a copy (pipeline already renames)."""
    internal = {c.name for c in contract.columns}
    if internal & set(df.columns):
        return df
    mapping = {c.source_name: c.name for c in contract.columns if c.source_name in df.columns}
    return df.rename(columns=mapping) if mapping else df


def validate_chunk(
    df: pd.DataFrame,
    contract: Contract,
    row_offset: int,
    ref_values: dict[tuple[str, str], set] | None = None,
) -> ChunkResult:
    from ingest.core.contracts import build_pandera_schema

    df = _maybe_rename(df, contract)
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

    # 1b. Column attributes, element-wise (pandera only checks these when the
    # whole column coerces, so string frames need explicit checks).
    for c in contract.columns:
        if c.name not in df.columns:
            continue
        s = df[c.name]
        if c.in_set is not None:
            allowed = set(c.in_set) | {str(x) for x in c.in_set}
            for idx, v in s.items():
                if _is_blank(v):
                    continue
                if v not in allowed and str(v) not in allowed:
                    reject(idx, "NOT_IN_SET", f"{c.name} must be one of {list(c.in_set)}", c.name)
        if c.pattern is not None:
            import re as _re

            rx = _re.compile(c.pattern)
            for idx, v in s.items():
                if _is_blank(v):
                    continue
                if not rx.search(str(v)):
                    reject(idx, "PATTERN_MISMATCH", f"{c.name} does not match {c.pattern}", c.name)
        if c.min is not None or c.max is not None:
            for idx, v in s.items():
                if _is_blank(v) or not _is_num(v):
                    continue
                f = float(v)
                if (c.min is not None and f < float(c.min)) or (
                    c.max is not None and f > float(c.max)
                ):
                    reject(idx, "OUT_OF_RANGE", f"{c.name} out of range", c.name)
        if c.max_length is not None:
            for idx, v in s.items():
                if _is_blank(v):
                    continue
                if len(str(v)) > c.max_length:
                    reject(idx, "OUT_OF_RANGE", f"{c.name} exceeds max length", c.name)

    # 2. Pandera schema (lazy) for types / sets / patterns / ranges.
    # Validated on a copy: coercion must never mutate the caller's frame.
    schema = build_pandera_schema(contract)
    try:
        schema.validate(df.copy(), lazy=True)
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
        fn_path = rule.function or rule.custom
        is_custom = rule.check == "custom" or fn_path is not None
        if rule.check == "exists_in":
            if col not in df.columns:
                continue
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
        elif is_custom:
            import warnings

            path = fn_path or rule.check
            try:
                fn = _load_custom(path)
            except (ImportError, ValueError) as e:
                warnings.warn(f"skipping custom rule {rule.code}: {e}")
                continue
            try:
                mask = fn(df)
            except Exception as e:
                warnings.warn(f"skipping custom rule {rule.code}: {e}")
                continue
            for idx, ok in mask.items():
                if not bool(ok):
                    reject(idx, rule.code, rule.message, col)
        else:
            if col not in df.columns:
                continue
            try:
                mask = _apply_comparison_rule(df, col, rule.check)
            except ValueError:
                continue
            for idx, v in df[col].items():
                if pd.isna(v) or (isinstance(v, str) and v.strip() == ""):
                    continue  # nulls already reported as MISSING_REQUIRED
                if not bool(mask.get(idx, True)):
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

    # 5. One root cause per cell: a missing value or a type error shadows any
    # other failure on the same (row, column) — e.g. 'abc' is TYPE_MISMATCH,
    # not also OUT_OF_RANGE.
    shadowed: set[tuple[int, str | None]] = {
        (r.row_number, r.column_name)
        for r in rejections
        if r.reason_code in ("MISSING_REQUIRED", "TYPE_MISMATCH")
    }
    if shadowed:
        rejections = [
            r
            for r in rejections
            if (r.row_number, r.column_name) not in shadowed
            or r.reason_code in ("MISSING_REQUIRED", "TYPE_MISMATCH")
        ]
        row_to_idx = {row_offset + p + 1: idx for p, idx in enumerate(df.index)}
        bad_idx = {row_to_idx[n] for n in {r.row_number for r in rejections} if n in row_to_idx}

    rejected_numbers = {r.row_number for r in rejections}
    accepted = df.drop(index=list(bad_idx), errors="ignore")
    return ChunkResult(
        accepted=accepted, rejections=rejections, rejected_row_numbers=rejected_numbers
    )
