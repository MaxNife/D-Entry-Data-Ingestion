"""Live-server fixtures for the Playwright dashboard tests.

By default a uvicorn subprocess is started against the test database. To test a
stack you started yourself (e.g. a separate frontend), set INGEST_E2E_BASE_URL;
it must serve both the dashboard routes and /api on that origin.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

import harness


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server_url(db_engine, tmp_path_factory):
    external = os.environ.get("INGEST_E2E_BASE_URL")
    if external:
        yield external.rstrip("/")
        return

    port = _free_port()
    cmd = [sys.executable, "-m", "uvicorn", harness.UVICORN_APP, "--host", "127.0.0.1", "--port", str(port)]
    if harness.UVICORN_FACTORY:
        cmd.append("--factory")
    log_path = tmp_path_factory.mktemp("server") / "uvicorn.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd, cwd=harness.REPO_ROOT, env=harness.subprocess_env(), stdout=log, stderr=subprocess.STDOUT
        )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            pytest.fail(f"server did not start; log:\n{log_path.read_text(encoding='utf-8')}", pytrace=False)
        time.sleep(0.25)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def live(server_url, db):
    """httpx client against the live server, with a clean database."""
    with httpx.Client(base_url=server_url, timeout=30) as client:
        yield client


@pytest.fixture(autouse=True)
def _playwright_timeouts(request):
    if "page" in request.fixturenames:
        from playwright.sync_api import expect

        expect.set_options(timeout=15_000)
