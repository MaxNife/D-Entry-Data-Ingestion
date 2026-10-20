"""Repository deliverable checks (spec s13, s14).

Verifies the repo structure, required files, and CI configuration exist.
"""

from pathlib import Path

import pytest

import harness

pytestmark = pytest.mark.repo

ROOT = harness.REPO_ROOT


class TestRepoStructure:
    def test_src_layout(self):
        assert (ROOT / "src" / "ingest").is_dir()

    def test_core_modules_exist(self):
        core = ROOT / "src" / "ingest" / "core"
        assert core.is_dir()
        for mod in ["hashing", "contracts", "validation", "loader", "pipeline", "reports"]:
            assert (core / f"{mod}.py").exists() or (core / mod / "__init__.py").exists(), f"core/{mod} missing"

    def test_db_package_exists(self):
        db_dir = ROOT / "src" / "ingest" / "db"
        assert db_dir.is_dir(), "src/ingest/db/ must exist"
        assert (db_dir / "models.py").exists() or (db_dir / "__init__.py").exists()

    def test_api_package_exists(self):
        api_dir = ROOT / "src" / "ingest" / "api"
        assert api_dir.is_dir(), "src/ingest/api/ must exist"
        assert (api_dir / "main.py").exists()

    def test_cli_module_exists(self):
        assert (ROOT / "src" / "ingest" / "cli.py").exists()

    def test_contracts_directory(self):
        contracts = ROOT / "contracts"
        assert contracts.is_dir()
        # At least one contract with versioned YAML
        yamls = list(contracts.rglob("*.yaml")) + list(contracts.rglob("*.yml"))
        assert len(yamls) >= 1

    def test_alembic_config(self):
        assert (ROOT / "alembic.ini").exists()
        assert (ROOT / "alembic").is_dir()
        versions = ROOT / "alembic" / "versions"
        assert versions.is_dir()
        migrations = list(versions.glob("*.py"))
        assert len(migrations) >= 1, "At least one Alembic migration required"

    def test_docker_compose_exists(self):
        assert (ROOT / "docker-compose.yml").exists() or (ROOT / "compose.yml").exists()

    def test_test_fixtures_exist(self):
        fixtures = ROOT / "tests" / "fixtures"
        assert fixtures.is_dir()
        csvs = list(fixtures.glob("*.csv"))
        assert len(csvs) >= 4, "Need at least 4 fixture CSVs (clean, dirty, missing_header, dup_keys)"


class TestREADME:
    def test_readme_exists(self):
        assert (ROOT / "README.md").exists()

    def test_readme_has_content(self):
        content = (ROOT / "README.md").read_text(encoding="utf-8")
        assert len(content) > 200, "README must be substantive (pitch, quickstart, etc.)"

    def test_readme_mentions_quickstart(self):
        content = (ROOT / "README.md").read_text(encoding="utf-8").lower()
        assert "docker" in content or "quick" in content or "start" in content or "compose" in content


class TestCIConfig:
    def test_github_actions_exists(self):
        workflows = ROOT / ".github" / "workflows"
        assert workflows.is_dir(), ".github/workflows/ must exist"
        ymls = list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml"))
        assert len(ymls) >= 1, "At least one CI workflow required"

