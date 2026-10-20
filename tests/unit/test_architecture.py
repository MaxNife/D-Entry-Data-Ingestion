"""spec s3: one package with a web-free core; `core` must not import from `api` or `cli`."""

import ast
from pathlib import Path

import pytest

import harness

pytestmark = pytest.mark.unit

CORE = harness.REPO_ROOT / "src" / "ingest" / "core"
FORBIDDEN_PREFIXES = ("ingest.api", "ingest.cli", "fastapi", "starlette", "typer", "jinja2")
SPEC_MODULES = ["hashing", "contracts", "validation", "loader", "pipeline", "reports"]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ["ingest", "core", *path.parent.relative_to(CORE).parts]
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # resolve relative imports against the file's package
                parts = package[: len(package) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
                names.append(base)
                names.extend(f"{base}.{alias.name}" for alias in node.names)
            elif node.module:
                names.append(node.module)
    return names


def test_core_package_exists_with_spec_modules():
    assert CORE.is_dir(), f"{CORE} missing (spec s13 layout: src/ingest/core)"
    for name in SPEC_MODULES:
        assert (CORE / f"{name}.py").exists() or (CORE / name / "__init__.py").exists(), name


def test_core_never_imports_web_or_cli_layers():
    offenders = []
    for path in CORE.rglob("*.py"):
        for name in _imports(path):
            if name.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{path.relative_to(harness.REPO_ROOT)} imports {name}")
    assert offenders == []
