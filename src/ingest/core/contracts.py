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
    check: str
    message: str
    column: str | None = None
    reference: dict | None = None
    function: str | None = None
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
    key: str | list = "row_hash"
    on_conflict: str = "update"
    columns: list[ColumnSpec] = field(default_factory=list)
    rules: list[RuleSpec] = field(default_factory=list)
    contract_hash: str = ""

    @property
    def source_names(self) -> list[str]:
        return [c.source_name for c in self.columns]

    @property
    def key_columns(self) -> list[str] | None:
        if self.key == "row_hash":
            return None
        return [self.key] if isinstance(self.key, str) else list(self.key)


def _contracts_dir(base: str | Path | None = None) -> Path:
    from ingest.config import settings

    return Path(base or settings.contracts_dir)


def parse_ref(ref: str) -> tuple[str, int | None]:
    if "@" in ref:
        name, v = ref.rsplit("@", 1)
        v = v.strip()
        if v.lower().startswith("v"):
            v = v[1:]
        try:
            return name, int(v)
        except ValueError:
            raise ValueError(f"bad contract ref {ref!r}: version must be an integer") from None
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


def load_contract(
    ref: str, version: int | str | None = None, base: str | Path | None = None
) -> Contract:
    """Load a versioned contract. Accepts 'name', 'name@version', or (name, version)."""
    if version is not None:
        v = str(version).strip()
        if v.lower().startswith("v"):
            v = v[1:]
        ref = f"{ref}@v{v}"
    name, ver = parse_ref(ref)
    if ver is None:
        ver = latest_version(name, base)
    path = contract_file(name, ver, base)
    if not path.exists():
        raise FileNotFoundError(f"unknown contract {ref!r}: {path} does not exist")
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise FileNotFoundError(f"unknown contract {ref!r}: {e}") from e
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: invalid YAML: {e}") from e
    contract = _validate_and_build(data, path)
    if contract.version != ver:
        raise ValueError(f"{path}: version field is {contract.version}, file name says v{ver}")
    if contract.name != name:
        raise ValueError(f"{path}: contract field is {contract.name!r}, directory says {name!r}")
    contract.contract_hash = sha256_bytes(raw)
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
        if not isinstance(r, dict) or "code" not in r or "check" not in r:
            raise ValueError(f"{path}: every rule needs 'code' and 'check'")
        if r["code"] in codes:
            raise ValueError(f"{path}: duplicate rule code '{r['code']}'")
        codes.add(r["code"])
        col = r.get("column")
        if col is not None and col not in col_names:
            raise ValueError(f"{path}: rule {r['code']} references unknown column '{col}'")
        rules.append(
            RuleSpec(
                code=r["code"],
                column=col,
                check=str(r["check"]),
                message=r.get("message", r["code"]),
                reference=r.get("reference"),
                function=r.get("function"),
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
        key=tgt.get("key", "row_hash"),
        on_conflict=oc,
        columns=cols,
        rules=rules,
    )


def build_pandera_schema(contract: Contract):
    """Build a pandera DataFrameSchema from the contract (nullable/coerce/checks)."""
    import pandera.pandas as pa

    fields = {}
    for c in contract.columns:
        dtype = {
            "int": "int64",
            "float": "float64",
            "str": "string",
            "bool": "bool",
            "date": "datetime64[ns]",
            "datetime": "datetime64[ns]",
            "decimal": "float64",
        }.get(c.type, "string")
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
