"""Chunk validation → structured rejections. src/ingest/core/validation.py — spec §7.

Every check is a plain Python predicate on one cell value. Predicates run once
per *distinct* value in a column, never once per cell, and the failing values
are then mapped back onto the column with a vectorised ``isin``. Real feeds are
highly repetitive (types, codes, dates), so this keeps validation cheap without
re-implementing each check in pandas idiom — and without changing a single
verdict. Rejections are emitted check by check, column by column, row by row,
so the order (which the reject report joins on) is deterministic.
"""

import operator
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from ingest.core.contracts import Contract
from ingest.core.hashing import sha256_bytes


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


# ---- cell predicates --------------------------------------------------------

_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    "<": operator.lt,
}
_COMPARISON = re.compile(r"\s*(>=|<=|==|!=|>|<)\s*(.+?)\s*$")

#: Words a bool column accepts (either truth value).
_BOOL_WORDS = {"1", "0", "true", "false", "t", "f", "yes", "no", "y", "n"}


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


def _type_ok(v, col_type: str) -> bool:
    """True if a non-blank cell can be coerced to the contract column type."""
    if col_type == "int":
        try:
            int(float(v))
            return True
        except (TypeError, ValueError, OverflowError):
            return False
    if col_type in ("float", "decimal"):
        return _is_num(v)
    if col_type == "bool":
        if isinstance(v, str):
            return v.strip().lower() in _BOOL_WORDS
        return isinstance(v, (bool, int, float))
    if col_type in ("date", "datetime"):
        try:
            ts = pd.to_datetime(v)
        except (TypeError, ValueError, OverflowError):
            return False
        try:
            return not pd.isna(ts)
        except (TypeError, ValueError):
            return True
    return True


def comparison_predicate(check: str) -> Callable[[object], bool]:
    """Compile a rule like ``'>= 0'`` into a predicate on one cell value.

    Numeric thresholds compare numerically and fail non-numeric cells; quoted
    or bare non-numeric thresholds compare as strings.
    """
    m = _COMPARISON.match(check)
    if not m:
        raise ValueError(f"Unsupported check expression: {check!r}")
    op = _OPS[m.group(1)]
    raw = m.group(2)
    try:
        threshold = float(raw)
    except ValueError:
        text = raw.strip("'\"")
        return lambda v: bool(op(str(v), text))
    return lambda v: _is_num(v) and bool(op(float(v), threshold))


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


# ---- column helpers ---------------------------------------------------------


def _blank_mask(s: pd.Series) -> pd.Series:
    """Vectorised ``_is_blank``: NA, or a string that is empty after strip."""
    na = s.isna()
    is_str = s.map(type).eq(str) if s.dtype == object else pd.Series(False, index=s.index)
    if s.dtype == "string":
        is_str = ~na
    stripped_empty = s.where(is_str & ~na, "x").astype(str).str.strip().eq("")
    return (na | stripped_empty).astype(bool)


def _failing(s: pd.Series, skip: pd.Series, ok: Callable[[object], bool]) -> pd.Series:
    """Mask of cells (outside ``skip``) for which ``ok`` is False.

    ``ok`` runs once per distinct value, not once per cell.
    """
    candidates = s[~skip]
    if candidates.empty:
        return pd.Series(False, index=s.index)
    bad = [v for v in pd.unique(candidates.to_numpy(dtype=object)) if not ok(v)]
    if not bad:
        return pd.Series(False, index=s.index)
    return s.isin(bad) & ~skip


def _cell_text(df: pd.DataFrame, name: str) -> pd.Series:
    """Column as text with NA → '' (a missing column is all '')."""
    if name not in df.columns:
        return pd.Series("", index=df.index, dtype=object)
    s = df[name]
    return s.astype(object).where(s.notna(), "").astype(str)


def chunk_keys(df: pd.DataFrame, contract: Contract) -> pd.Series:
    """The idempotency key of every row: the key columns joined with '|', or a
    SHA-256 of every contract column when the contract keys on ``row_hash``."""
    keys = contract.key_columns
    names = [c.name for c in contract.columns] if keys is None else keys
    joined = _cell_text(df, names[0])
    for name in names[1:]:
        joined = joined + "|" + _cell_text(df, name)
    if keys is None:
        return joined.map(lambda k: sha256_bytes(k.encode("utf-8")))
    return joined


def _maybe_rename(df: pd.DataFrame, contract: Contract) -> pd.DataFrame:
    """Rename source headers to contract names on a copy (pipeline already renames)."""
    internal = {c.name for c in contract.columns}
    if internal & set(df.columns):
        return df
    mapping = {c.source_name: c.name for c in contract.columns if c.source_name in df.columns}
    return df.rename(columns=mapping) if mapping else df


# ---- the check ---------------------------------------------------------------


def validate_chunk(
    df: pd.DataFrame,
    contract: Contract,
    row_offset: int,
    ref_values: dict[tuple[str, str], set] | None = None,
) -> ChunkResult:
    df = _maybe_rename(df, contract)
    ref_values = ref_values or {}
    rejections: list[Rejection] = []
    bad_idx: set = set()
    position = {idx: pos for pos, idx in enumerate(df.index)}

    def reject(idx, code: str, msg: str, col: str | None = None):
        row_number = row_offset + position.get(idx, 0) + 1
        off = df.at[idx, col] if col and col in df.columns else None
        off_s = None if _is_blank(off) and not isinstance(off, str) else str(off)
        rejections.append(Rejection(row_number, code, msg, col, off_s))
        bad_idx.add(idx)

    def reject_mask(mask: pd.Series, code: str, msg: str, col: str | None) -> None:
        for idx in df.index[mask.to_numpy(dtype=bool)]:
            reject(idx, code, msg, col)

    present = [c for c in contract.columns if c.name in df.columns]
    blank = {c.name: _blank_mask(df[c.name]) for c in present}

    # 1. Required missing. Strings count whitespace-only as missing.
    for c in present:
        if c.required:
            missing = blank[c.name] if c.type == "str" else df[c.name].isna()
            reject_mask(missing, "MISSING_REQUIRED", f"{c.name} is required", c.name)

    # 1b. Column attributes.
    for c in present:
        s, skip = df[c.name], blank[c.name]
        if c.in_set is not None:
            allowed = set(c.in_set) | {str(x) for x in c.in_set}
            reject_mask(
                _failing(s, skip, lambda v, a=allowed: v in a or str(v) in a),
                "NOT_IN_SET",
                f"{c.name} must be one of {list(c.in_set)}",
                c.name,
            )
        if c.pattern is not None:
            rx = re.compile(c.pattern)
            reject_mask(
                _failing(s, skip, lambda v, rx=rx: bool(rx.search(str(v)))),
                "PATTERN_MISMATCH",
                f"{c.name} does not match {c.pattern}",
                c.name,
            )
        if c.min is not None or c.max is not None:
            lo = None if c.min is None else float(c.min)
            hi = None if c.max is None else float(c.max)

            def in_range(v, lo=lo, hi=hi) -> bool:
                if not _is_num(v):
                    return True  # a type problem, reported as TYPE_MISMATCH
                f = float(v)
                return not ((lo is not None and f < lo) or (hi is not None and f > hi))

            reject_mask(
                _failing(s, skip, in_range), "OUT_OF_RANGE", f"{c.name} out of range", c.name
            )
        if c.max_length is not None:
            too_long = ~skip & s.astype(str).str.len().gt(c.max_length)
            reject_mask(too_long, "OUT_OF_RANGE", f"{c.name} exceeds max length", c.name)

    # 1c. Type coercion for non-string columns.
    for c in present:
        if c.type != "str":
            reject_mask(
                _failing(df[c.name], blank[c.name], lambda v, t=c.type: _type_ok(v, t)),
                "TYPE_MISMATCH",
                f"{c.name} is not a valid {c.type}",
                c.name,
            )

    # 2. Contract rules (own code/message). Blank cells are skipped: a missing
    # value is already reported as MISSING_REQUIRED.
    for rule in contract.rules:
        col = rule.column
        fn_path = rule.function or rule.custom
        if rule.check == "exists_in":
            ref = rule.reference or {}
            valid = ref_values.get((ref.get("table"), ref.get("column")))
            if col not in df.columns or valid is None:
                continue  # no reference loaded → rule skipped
            valid_text = {str(x) for x in valid}
            reject_mask(
                _failing(df[col], blank[col], lambda v: str(v) in valid_text or v in valid),
                rule.code,
                rule.message,
                col,
            )
        elif rule.check == "custom" or fn_path is not None:
            path = fn_path or rule.check
            try:
                mask = _load_custom(path)(df)
            except Exception as e:  # a broken custom rule must not sink the chunk
                warnings.warn(f"skipping custom rule {rule.code}: {e}", stacklevel=2)
                continue
            failing = ~mask.reindex(df.index, fill_value=True).astype(bool)
            reject_mask(failing, rule.code, rule.message, col)
        else:
            if col not in df.columns:
                continue
            try:
                ok = comparison_predicate(rule.check)
            except ValueError:
                continue
            reject_mask(_failing(df[col], blank[col], ok), rule.code, rule.message, col)

    # 3. Duplicate keys inside this chunk: keep the first clean row, reject the rest.
    clean = ~df.index.isin(list(bad_idx))
    if clean.any():
        keys = chunk_keys(df[clean], contract)
        for idx, k in keys[keys.duplicated(keep="first")].items():
            reject(idx, "DUPLICATE_KEY_IN_CHUNK", f"duplicate key within chunk: {k[:16]}…", None)

    # 4. One root cause per cell: a missing value or a type error shadows any
    # other failure on the same (row, column) — e.g. 'abc' is TYPE_MISMATCH,
    # not also OUT_OF_RANGE.
    root = ("MISSING_REQUIRED", "TYPE_MISMATCH")
    shadowed = {(r.row_number, r.column_name) for r in rejections if r.reason_code in root}
    if shadowed:
        rejections = [
            r
            for r in rejections
            if r.reason_code in root or (r.row_number, r.column_name) not in shadowed
        ]
        rows_left = {r.row_number for r in rejections}
        bad_idx = {idx for idx, pos in position.items() if row_offset + pos + 1 in rows_left}

    accepted = df.drop(index=list(bad_idx), errors="ignore")
    return ChunkResult(
        accepted=accepted,
        rejections=rejections,
        rejected_row_numbers={r.row_number for r in rejections},
    )
