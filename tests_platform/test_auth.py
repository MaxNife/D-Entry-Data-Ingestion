"""Sign-in, roles and the audit log, with INGEST_AUTH=on."""

import re

import harness
import pytest
from fastapi.testclient import TestClient
from harness import fixture, upload_and_wait

pytestmark = pytest.mark.integration
ADMIN = {"email": "ada@example.com", "name": "Ada Admin", "password": "correct horse battery"}


@pytest.fixture
def auth_on(db, monkeypatch):
    from ingest.api import security

    monkeypatch.setenv("INGEST_AUTH", "on")
    for table in ("audit_events", "api_tokens", "user_sessions", "workspace_settings", "users"):
        db.execute(f"DELETE FROM {table}")
    security._failures.clear()
    yield
    for table in ("audit_events", "api_tokens", "user_sessions", "workspace_settings", "users"):
        db.execute(f"DELETE FROM {table}")


def client() -> TestClient:
    return TestClient(harness.get_app(), follow_redirects=False)


def setup_admin() -> TestClient:
    c = client()
    resp = c.post(
        "/setup", data={**ADMIN, "confirm": ADMIN["password"], "workspace_name": "Acme Data"}
    )
    assert resp.status_code == 303, resp.text[:300]
    return c


def add_user(admin: TestClient, email: str, role: str) -> str:
    page = admin.post(
        "/admin/users", data={"email": email, "name": email.split("@")[0], "role": role}
    )
    assert page.status_code == 200, page.text[:300]
    return re.search(r"data-temp-password>([^<]+)<", page.text).group(1)


def sign_in(email: str, password: str, new_password: str | None = None) -> TestClient:
    c = client()
    resp = c.post("/login", data={"email": email, "password": password})
    assert resp.status_code == 303, resp.text[:300]
    if new_password:  # first sign-in with a temporary password
        assert c.get("/").headers["location"] == "/settings/profile?must=1"
        resp = c.post(
            "/settings/password",
            data={"current": password, "new": new_password, "confirm": new_password},
        )
        assert resp.headers["location"] == "/settings/profile?saved=password"
    return c


def test_first_visit_goes_to_setup_and_creates_the_admin(auth_on, db):
    c = client()
    assert c.get("/").headers["location"] == "/setup"
    assert c.get("/api/batches").status_code == 503
    admin = setup_admin()
    home = admin.get("/")
    assert home.status_code == 200 and "Acme Data" in home.text
    assert db.scalar("SELECT role FROM users") == "admin"
    assert client().get("/setup").headers["location"] == "/"  # setup is closed once used


def test_anonymous_requests_are_refused_but_health_stays_public(auth_on):
    setup_admin()
    anon = client()
    assert anon.get("/").headers["location"].startswith("/login?next=")
    assert anon.get("/api/batches").status_code == 401
    assert anon.get("/docs").status_code in (303, 401)
    assert anon.get("/health").status_code == 200


def test_wrong_password_is_audited_and_throttled(auth_on, db):
    setup_admin()
    c = client()
    for _ in range(5):
        assert (
            c.post(
                "/login", data={"email": ADMIN["email"], "password": "nope-nope-nope"}
            ).status_code
            == 401
        )
    assert (
        c.post("/login", data={"email": ADMIN["email"], "password": ADMIN["password"]}).status_code
        == 429
    )
    assert db.scalar("SELECT count(*) FROM audit_events WHERE action = 'Failed to sign in'") == 5


def test_viewer_sees_but_cannot_change_anything(auth_on, db):
    admin = setup_admin()
    viewer = sign_in(
        "val@example.com", add_user(admin, "val@example.com", "viewer"), "viewer pass 2026"
    )
    assert viewer.get("/").status_code == 200
    assert 'data-open="new-ingestion"' not in viewer.get("/").text  # no upload button
    assert viewer.get("/upload").status_code == 403
    with fixture("clean.csv").open("rb") as fp:
        assert (
            viewer.post(
                "/api/batches", files={"file": ("clean.csv", fp)}, data={"contract": "txn_test"}
            ).status_code
            == 403
        )
    assert viewer.get("/contracts/new").status_code == 403
    assert viewer.get("/connections/new").status_code == 403
    assert viewer.get("/admin/users").status_code == 403
    assert viewer.get("/docs").status_code == 403
    ok = viewer.post("/api/sql/run", json={"sql": "select 1 as one"})
    assert ok.status_code == 200 and ok.json()["rows"] == [[1]]
    assert db.scalar("SELECT count(*) FROM audit_events WHERE outcome = 'denied'") >= 5


def test_workspace_settings_restrict_viewers(auth_on):
    admin = setup_admin()
    viewer = sign_in(
        "vic@example.com", add_user(admin, "vic@example.com", "viewer"), "viewer pass 2026"
    )
    admin.post(
        "/admin/workspace",
        data={
            "workspace_name": "Acme",
            "session_hours": "8",
            "password_min_length": "12",
            "downloads": "admins",
        },
    )
    # the checkbox is absent: viewers can no longer query
    assert viewer.post("/api/sql/run", json={"sql": "select 1"}).status_code == 403
    assert viewer.get("/query").status_code == 403
    assert viewer.get("/tables/test_transactions/export").status_code == 403
    assert admin.get("/tables/test_transactions/export").status_code in (
        200,
        404,
    )  # allowed; may not exist yet
    assert "Changed workspace settings" in admin.get("/admin/audit").text


def test_editor_can_ingest_but_not_administer(auth_on, api):
    admin = setup_admin()
    editor = sign_in(
        "eve@example.com", add_user(admin, "eve@example.com", "editor"), "editor pass 2026"
    )
    b = upload_and_wait(editor, fixture("clean_transactions.csv"), "txn_test@1")
    assert b["status"] == "completed"
    assert editor.get("/contracts/new").status_code == 200
    assert editor.get("/admin/users").status_code == 403
    assert editor.get("/connections/new").status_code == 403
    assert editor.get("/admin/audit").status_code == 403
    audit = admin.get("/admin/audit", params={"user": "eve@"}).text
    assert "Uploaded a file through the API" in audit and "Signed in" in audit


def test_personal_access_token_works_until_revoked(auth_on):
    admin = setup_admin()
    page = admin.post("/settings/tokens", data={"name": "nightly", "days": "30"})
    token = re.search(r"data-token>([^<]+)<", page.text).group(1)
    api = client()
    headers = {"Authorization": f"Bearer {token}"}
    assert api.get("/api/batches", headers=headers).status_code == 200
    token_id = re.search(
        r"/settings/tokens/([^/]+)/revoke", admin.get("/settings/tokens").text
    ).group(1)
    admin.post(f"/settings/tokens/{token_id}/revoke")
    assert api.get("/api/batches", headers=headers).status_code == 401


def test_the_last_admin_cannot_be_removed(auth_on, db):
    admin = setup_admin()
    me = db.scalar("SELECT id FROM users")
    resp = admin.post(f"/admin/users/{me}/role", data={"role": "viewer"})
    assert "at+least+one+active+admin" in resp.headers["location"]
    resp = admin.post(f"/admin/users/{me}/active", data={"active": "0"})
    assert "own+account" in resp.headers["location"]
    assert db.scalar("SELECT role FROM users") == "admin"


def test_deactivating_a_user_ends_their_sessions(auth_on, db):
    admin = setup_admin()
    editor = sign_in(
        "ed@example.com", add_user(admin, "ed@example.com", "editor"), "editor pass 2026"
    )
    assert editor.get("/").status_code == 200
    uid = db.scalar("SELECT id FROM users WHERE email = 'ed@example.com'")
    admin.post(f"/admin/users/{uid}/active", data={"active": "0"})
    assert editor.get("/").headers["location"].startswith("/login")
    assert (
        client()
        .post("/login", data={"email": "ed@example.com", "password": "editor pass 2026"})
        .status_code
        == 401
    )


def test_cross_site_posts_are_refused(auth_on):
    admin = setup_admin()
    resp = admin.post(
        "/admin/workspace",
        data={"workspace_name": "Evil"},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_admin_can_still_reach_the_api_reference(auth_on):
    admin = setup_admin()
    assert admin.get("/docs").status_code == 200
    assert "/docs" not in admin.get("/").text  # but the app never links to it
