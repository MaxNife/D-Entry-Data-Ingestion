"""Write side of the contract registry: author, version, infer and delete contracts.

Contracts stay immutable once written (spec §5): every save creates ``v<N+1>``. A
contract is validated with the same rules ``load_contract`` applies *before* the
file is written, so an invalid contract never lands on disk.
"""

import os
import re
from pathlib import Path

import pandas as pd
import yaml

from ingest.core.contracts import (
    Contract,
    _contracts_dir,
    _validate_and_build,
    contract_file,
    list_contracts,
    load_contract,
)
from ingest.core.app_tables import APP_TABLES
from ingest.core.validation import comparison_predicate

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
RESERVED_TABLES = {"batches", "rejected_records", *APP_TABLES}
COLUMN_CHECKS = ("required", "in_set", "pattern", "min", "max", "max_length")


def _empty(v) -> bool:
    """Unset form/YAML value. Not ``v in (None, False, ...)``: that would also drop ``0``."""
    return v is None or (isinstance(v, str | list) and len(v) == 0)


def versions_of(name: str, base: str | Path | None = None) -> list[int]:
    return sorted(v for n, v in list_contracts(base) if n == name)


def contract_to_dict(c: Contract) -> dict:
    """The YAML shape of a loaded contract (round-trips through save_new_version)."""
    columns = []
    for col in c.columns:
        d = {"name": col.name, "source_name": col.source_name, "type": col.type}
        for k in COLUMN_CHECKS:
            v = getattr(col, k)
            if not _empty(v) and v is not False:
                d[k] = v
        columns.append(d)
    rules = []
    for r in c.rules:
        d = {"code": r.code, "check": r.check, "message": r.message}
        for k in ("column", "reference", "function", "custom"):
            v = getattr(r, k)
            if not _empty(v):
                d[k] = v
        rules.append(d)
    return {
        "contract": c.name,
        "version": c.version,
        "description": c.description,
        "source": {
            "formats": list(c.source_formats),
            "encoding": c.encoding,
            "delimiter": c.delimiter,
        },
        "target": {
            "table": c.target_table,
            "key": c.key,
            "on_conflict": c.on_conflict,
            **({"connection": c.connection} if c.connection else {}),
        },
        "columns": columns,
        "rules": rules,
    }


def _clean(data: dict) -> dict:
    """Drop empty optional values the form sends, and coerce numbers."""
    out = {k: v for k, v in data.items() if k not in ("columns", "rules")}
    if isinstance(out.get("target"), dict):
        out["target"] = {k: v for k, v in out["target"].items() if not _empty(v)}
    cols = []
    for c in data.get("columns") or []:
        c = {k: v for k, v in c.items() if not _empty(v)}
        for k in ("min", "max"):
            if k in c and isinstance(c[k], str):
                c[k] = float(c[k]) if "." in c[k] else int(c[k])
        if "max_length" in c:
            c["max_length"] = int(c["max_length"])
        if isinstance(c.get("in_set"), str):
            c["in_set"] = [s.strip() for s in c["in_set"].split(",") if s.strip()]
        c["required"] = bool(c.get("required"))
        c.setdefault("source_name", c.get("name"))
        cols.append(c)
    out["columns"] = cols
    out["rules"] = [{k: v for k, v in r.items() if not _empty(v)} for r in data.get("rules") or []]
    return out


def check_contract(data: dict) -> list[str]:
    """Problems that would make a contract unusable, beyond what the loader checks."""
    problems = []
    name = str(data.get("contract") or "")
    if not NAME_RE.match(name):
        problems.append(
            "Contract name must start with a letter and use lowercase letters, digits and underscores."
        )
    table = str((data.get("target") or {}).get("table") or "")
    if not TABLE_RE.match(table):
        problems.append("Target table must be a plain identifier (letters, digits, underscores).")
    elif table.lower() in RESERVED_TABLES:
        problems.append(f"“{table}” is reserved by the platform; choose another table name.")
    conn = (data.get("target") or {}).get("connection")
    if conn and not NAME_RE.match(str(conn)):
        problems.append("Destination connection must be a connection name.")
    if not data.get("columns"):
        problems.append("Add at least one column.")
    names = [c.get("name") for c in data.get("columns") or []]
    for n in names:
        if n and not TABLE_RE.match(str(n)):
            problems.append(f"Column name “{n}” must be a plain identifier.")
    headers = [c.get("source_name") or c.get("name") for c in data.get("columns") or []]
    if len(set(headers)) != len(headers):
        problems.append("Two columns read the same file header.")
    key = (data.get("target") or {}).get("key")
    if isinstance(key, list):
        for k in key:
            if k not in names:
                problems.append(f"Key column “{k}” is not one of the contract's columns.")
    for r in data.get("rules") or []:
        check = str(r.get("check") or "")
        if check not in ("exists_in", "custom") and not (r.get("function") or r.get("custom")):
            try:
                comparison_predicate(check)
            except Exception:
                problems.append(f"Rule {r.get('code')}: “{check}” is not a comparison like “>= 0”.")
    return problems


def save_new_version(data: dict, base: str | Path | None = None) -> Contract:
    """Validate ``data`` and write it as the next version. Raises ValueError with readable text."""
    data = _clean(data)
    problems = check_contract(data)
    if problems:
        raise ValueError(" ".join(problems))
    name = data["contract"]
    existing = versions_of(name, base)
    version = (max(existing) if existing else 0) + 1
    data["version"] = version
    path = contract_file(name, version, base)
    _validate_and_build(data, path)  # same rules the loader enforces
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=None)
    with open(path, "x", encoding="utf-8") as fp:  # "x": never overwrite a version
        fp.write(text)
    return load_contract(f"{name}@v{version}", base=base)


def delete_version(name: str, version: int, base: str | Path | None = None) -> None:
    path = contract_file(name, version, base)
    if not path.exists():
        raise FileNotFoundError(f"unknown contract {name}@v{version}")
    path.unlink()
    folder = path.parent
    if folder.is_dir() and not any(folder.iterdir()):
        os.rmdir(folder)


# ---- inference ----------------------------------------------------------------

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_BOOL = {"true", "false", "yes", "no"}


def snake(header: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(header).strip())
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s or not s[0].isalpha():
        s = f"col_{s}" if s else "col"
    return s[:63]


def infer_type(values: list[str]) -> str:
    """The most specific type the values fit. A few misfits (≤5%, at least one, once there
    are 3+ values) don't demote the column: they are the rows the contract should reject."""
    if not values:
        return "str"
    allowed = max(1, len(values) // 20) if len(values) >= 3 else 0
    for kind, test in (
        ("int", _INT.match),
        ("float", _FLOAT.match),
        ("date", _DATE.match),
        ("datetime", _DATETIME.match),
        ("bool", lambda v: v.lower() in _BOOL),
    ):
        if sum(1 for v in values if not test(v)) <= allowed:
            return kind
    return "str"


def read_sample(path: Path, rows: int = 5000, delimiter: str = ",", encoding: str = "utf-8"):
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path, engine="openpyxl", dtype=str, nrows=rows)
    return pd.read_csv(path, dtype=str, nrows=rows, delimiter=delimiter, encoding=encoding)


def infer_contract(path: Path, name: str = "", rows: int = 5000, file_name: str = "") -> dict:
    """A draft contract from a sample file: one column per header, types guessed."""
    df = read_sample(path, rows)
    columns, used = [], set()
    for header in df.columns:
        values = [str(v).strip() for v in df[header].dropna() if str(v).strip() != ""]
        col = snake(header)
        while col in used:
            col = f"{col}_2"
        used.add(col)
        spec = {"name": col, "source_name": str(header), "type": infer_type(values)}
        spec["required"] = len(values) == len(df) and len(df) > 0
        if spec["type"] == "str" and values:
            spec["max_length"] = max(64, 1 << (max(len(v) for v in values) - 1).bit_length())
        columns.append(spec)
    key = "row_hash"
    for spec in columns:
        series = df[spec["source_name"]].dropna()
        is_id = spec["name"] == "id" or spec["name"].endswith("_id")
        if is_id and spec["required"] and series.is_unique:
            key = [spec["name"]]
            break
    name = snake(name or Path(file_name or path.name).stem)
    suffix = path.suffix.lower().lstrip(".")
    return {
        "contract": name,
        "version": 1,
        "description": f"Inferred from {file_name or path.name} ({len(df)} sample rows); review the types and checks.",
        "source": {
            "formats": ["xlsx"] if suffix in ("xlsx", "xls") else ["csv"],
            "encoding": "utf-8",
            "delimiter": ",",
        },
        "target": {"table": name, "key": key, "on_conflict": "update"},
        "columns": columns,
        "rules": [],
    }


def contracts_root(base: str | Path | None = None) -> Path:
    return _contracts_dir(base)
