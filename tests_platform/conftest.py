"""Tests for the workspace features added on 2026-10-20 (contract authoring, row fixing,
dry runs, tables, SQL, connections).

They live outside ``tests/`` because that suite is owned by the test author and must not
be modified. The shared Postgres fixtures (``db_engine``, ``db``, ``api``) are loaded from
``tests/conftest.py`` unchanged, so both suites exercise the same database setup.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent.parent / "tests"
sys.path.insert(0, str(TESTS))

import harness  # noqa: E402  (configures INGEST_* env before the app is imported)

_spec = importlib.util.spec_from_file_location("ingest_suite_conftest", TESTS / "conftest.py")
_suite = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_suite)
db_engine, db, api = _suite.db_engine, _suite.db, _suite.api


@pytest.fixture(autouse=True)
def contracts_dir(tmp_path, monkeypatch):
    """A private copy of the repo's contracts, so authoring tests never touch contracts/."""
    from ingest.config import settings

    root = tmp_path / "contracts"
    shutil.copytree(harness.REPO_ROOT / "contracts", root)
    monkeypatch.setattr(settings, "contracts_dir", str(root))
    return root


def pytest_configure(config):
    for name, help_text in _suite.MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {help_text}")
