"""Repository deliverables the spec requires (s3, s12, s13, s14)."""

import os
import subprocess
import sys

import pytest

import harness

pytestmark = pytest.mark.repo

ROOT = harness.REPO_ROOT


def _read(rel: str) -> str:
    path = ROOT / rel
    assert path.exists(), f"{rel} is missing"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "rel",
    [
        "pyproject.toml",
        "docker-compose.yml",
        "alembic.ini",
        "README.md",
        "docs/architecture.md",
        "docs/decisions.md",
        "scripts/make_dirty_sample.py",
        "src/ingest/config.py",
        "src/ingest/cli.py",
        "src/ingest/db/models.py",
        "src/ingest/api/main.py",
    ],
)
def test_required_file_exists(rel):
    assert (ROOT / rel).exists(), f"{rel} is missing (spec s13 layout)"


def test_alembic_has_migrations():
    assert list((ROOT / "alembic" / "versions").glob("*.py")), "no Alembic migrations"


def test_git_repository_is_initialised():
    assert (ROOT / ".git").exists(), "spec s13: git init in the very first step"


def test_data_dir_is_git_ignored_and_not_committed():
    lines = [line.strip() for line in _read(".gitignore").splitlines()]
    assert any(line.rstrip("/") in ("data", "/data") for line in lines)
    proc = subprocess.run(["git", "ls-files", "data"], cwd=ROOT, capture_output=True, text=True)
    assert proc.stdout.strip() == "", "datasets must not be committed (spec s12)"


def test_docker_compose_defines_postgres_and_app():
    text = _read("docker-compose.yml").lower()
    assert "postgres" in text
    assert "services:" in text


def test_ci_runs_lint_contract_validation_and_tests():
    workflows = list((ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert workflows, "no GitHub Actions workflow"
    text = "\n".join(w.read_text(encoding="utf-8") for w in workflows)
    assert "ruff" in text
    assert "contracts validate" in text
    assert "pytest" in text
    assert "postgres" in text.lower()


def test_readme_has_required_sections():
    text = _read("README.md").lower()
    for phrase in ("docker compose up", "design decision", "known limitation", "peak memory", "kaggle"):
        assert phrase in text, f"README.md must mention {phrase!r} (spec s13/s14)"


def test_project_contracts_exist_for_at_least_two_feeds():
    contracts = [p for p in (ROOT / "contracts").iterdir() if p.is_dir() and list(p.glob("v*.yaml"))]
    assert len(contracts) >= 2, "spec s12: at least PaySim and USA Banking contracts"


def test_project_contracts_validate():
    env = harness.subprocess_env()
    env.pop(harness.ENV_CONTRACTS_DIR, None)  # use the real contracts/ directory
    proc = subprocess.run(
        [*harness.cli_command(), "contracts", "validate"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_dirty_sample_script_runs():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_dirty_sample.py"), "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_source_passes_ruff():
    proc = subprocess.run([sys.executable, "-m", "ruff", "check", "src"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
