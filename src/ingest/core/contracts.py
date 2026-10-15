"""Versioned YAML contracts. src/ingest/core/contracts.py — spec §5."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ingest.core.hashing import sha256_bytes

ALLOWED_TYPES = {"int", "float", "str", "bool", "date", "datetime", "decimal"}


@dataclass
class ColumnSpec:
    name: str
    source_name: str
    type: str
    required: bool = False
    in_set: list | None = None
    pattern: str | None = None
    min: float | None = None
    max: float | None = None
    max_length: int | None = None


@dataclass
class RuleSpec:
    code: str
    column: str
    check: str
    message: str
    reference: dict | None = None
    custom: str | None = None


@dataclass
class Contract:
    name: str
    version: int
    description: str = ""
    source_formats: list = field(default_factory=lambda: ["csv"])
    encoding: str = "utf-8"
    delimiter: str = ","
    target_table: str = ""
    target_key: str | list = "row_hash"
    on_conflict: str = "update"
    columns: list[ColumnSpec] = field(default_factory=list)
    rules: list[RuleSpec] = field(default_factory=list)
    hash: str = ""

    @property
    def source_names(self) -> list[str]:
        return [c.source_name for c in self.columns]

    @property
    def key_columns(self) -> list[str] | None:
        if self.target_key == "row_hash":
            return None
        return [self.target_key] if isinstance(self.target_key, str) else list(self.target_key)


def _contracts_dir(base: str | Path | None = None) -> Path:
    from ingest.config import settings

    return Path(base or settings.contracts_dir)


def parse_ref(ref: str) -> tuple[str, int | None]:
    if "@" in ref:
        name, v = ref.rsplit("@", 1)
        return name, int(v.lstrip("v"))
    return ref, None


def contract_file(name: str, version: int, base: str | Path | None = None) -> Path:
    return _contracts_dir(base) / name / f"v{version}.yaml"


def latest_version(name: str, base: str | Path | None = None) -> int:
    d = _contracts_dir(base) / name
    versions = []
    for p in d.glob("v*.yaml"):
        try:
            versions.append(int(p.stem.lstrip("v")))
        except ValueError:
            continue
    if not versions:
        raise FileNotFoundError(f"No versions for contract '{name}' in {d}")
    return max(versions)


def load_contract(ref: str, base: str | Path | None = None) -> Contract:
    name, ver = parse_ref(ref)
    if ver is None:
        ver = latest_version(name, base)
    path = contract_file(name, ver, base)
    raw = path.read_bytes()
    data = yaml.safe_load(raw.decode("utf-8"))
    contract = _validate_and_build(data, path)
    contract.hash = sha256_bytes(raw)
    return contract


def list_contracts(base: str | Path | None = None) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    root = _contracts_dir(base)
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        for p in sorted(d.glob("v*.yaml")):
            try:
                out.append((d.name, int(p.stem.lstrip("v"))))
            except ValueError:
                continue
    return out


def _validate_and_build(data: dict, path: Path) -> Contract:
    if not isinstance(data, dict):
        raise ValueError(f"{path}: contract must be a mapping")
    for k in ("contract", "version", "target", "columns"):
        if k not in data:
            raise ValueError(f"{path}: missing required key '{k}'")
    cols = []
    seen = set()
    for c in data["columns"]:
        t = c.get("type", "str")
        if t not in ALLOWED_TYPES:
            raise ValueError(f"{path}: column {c.get('name')}: unknown type '{t}'")
        if c["name"] in seen:
            raise ValueError(f"{path}: duplicate column name '{c['name']}'")
        seen.add(c["name"])
        cols.append(
            ColumnSpec(
                name=c["name"],
                source_name=c.get("source_name", c["name"]),
                type=t,
                required=bool(c.get("required", False)),
                in_set=c.get("in_set"),
                pattern=c.get("pattern"),
                min=c.get("min"),
                max=c.get("max"),
                max_length=c.get("max_length"),
            )
        )
    rules = []
    codes = set()
    col_names = {c.name for c in cols}
    for r in data.get("rules", []):
        if r["code"] in codes:
            raise ValueError(f"{path}: duplicate rule code '{r['code']}'")
        codes.add(r["code"])
        if r["column"] not in col_names:
            raise ValueError(f"{path}: rule {r['code']} references unknown column '{r['column']}'")
        rules.append(
            RuleSpec(
                code=r["code"],
                column=r["column"],
                check=str(r["check"]),
                message=r.get("message", r["code"]),
                reference=r.get("reference"),
                custom=r.get("custom"),
            )
        )
    src = data.get("source", {})
    tgt = data["target"]
    oc = tgt.get("on_conflict", "update")
    if oc not in ("update", "skip"):
        raise ValueError(f"{path}: on_conflict must be update|skip, got '{oc}'")
    return Contract(
        name=data["contract"],
        version=int(data["version"]),
        description=data.get("description", ""),
        source_formats=src.get("formats", ["csv"]),
        encoding=src.get("encoding", "utf-8"),
        delimiter=src.get("delimiter", ","),
        target_table=tgt["table"],
        target_key=tgt.get("key", "row_hash"),
        on_conflict=oc,
        columns=cols,
        rules=rules,
    )


def build_pandera_schema(contract: Contract):
    """Build a pandera DataFrameSchema from the contract (nullable/coerce/checks)."""
    import pandera.pandas as pa

    fields = {}
    for c in contract.columns:
        dtype = {"int": "int64", "float": "float64", "str": "string", "bool": "bool"}.get(
            c.type, "string"
        )
        checks = []
        if c.in_set is not None:
            checks.append(pa.Check.isin(c.in_set))
        if c.pattern is not None:
            checks.append(pa.Check.str_matches(c.pattern))
        if c.min is not None:
            checks.append(pa.Check.ge(c.min))
        if c.max is not None:
            checks.append(pa.Check.le(c.max))
        fields[c.name] = pa.Column(
            dtype, checks=checks or None, nullable=not c.required, coerce=True
        )
    return pa.DataFrameSchema(fields, strict=False)
