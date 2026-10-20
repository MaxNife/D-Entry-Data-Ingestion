"""Sign-in, first-run setup, user settings and the admin area (users, workspace
settings, audit log). Access to each path is enforced by ``security.AccessMiddleware``;
the handlers here add the business rules (never remove the last admin, and so on).
"""

from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ingest.api.security import cleared, client_ip, failed, throttled
from ingest.core import auth

THEMES = {"system": "Match the system", "light": "Light", "dark": "Dark"}
THEME_COOKIE = "ingest_theme"


def _to(path: str, **params) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _safe_next(target: str | None) -> str:
    """Only same-site paths: never redirect a signed-in user to another site."""
    if (
        not target
        or not target.startswith("/")
        or target.startswith("//")
        or target.startswith("/\\")
    ):
        return "/"
    return target


def register_accounts(app, eng, templates) -> None:
    def render(request: Request, name: str, ctx: dict, status_code: int = 200):
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def me(request: Request) -> auth.CurrentUser:
        return request.state.user

    def audit(request: Request, action: str, target: str | None = None, outcome="ok", detail=None):
        user = getattr(request.state, "user", None)
        auth.record(
            eng,
            user=user.email if user else None,
            action=action,
            target=target,
            outcome=outcome,
            ip=client_ip(request),
            detail=detail,
        )

    def set_cookie(response, request: Request, token: str, hours: int):
        secure = (
            request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        )
        response.set_cookie(
            auth.SESSION_COOKIE,
            token,
            max_age=hours * 3600,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )

    # ---- sign-in ---------------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", error: str = ""):
        if not auth.auth_enabled():
            return _to("/")
        if auth.user_count(eng) == 0:
            return _to("/setup")
        if request.state.user is not None:
            return _to(_safe_next(next))
        return render(
            request, "login.html", {"next": _safe_next(next), "error": error, "email": ""}
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request):
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        nxt = _safe_next(str(form.get("next") or "/"))
        key = f"{email}|{client_ip(request)}"
        if throttled(key):
            audit(request, "Sign-in blocked after repeated failures", email, "denied")
            msg = (
                "Too many failed attempts. Wait 15 minutes, or ask an admin to reset your password."
            )
            return render(request, "login.html", {"next": nxt, "error": msg, "email": email}, 429)
        user = auth.authenticate(eng, email, str(form.get("password") or ""))
        if user is None:
            failed(key)
            auth.record(
                eng,
                user=email or None,
                action="Failed to sign in",
                outcome="failed",
                ip=client_ip(request),
            )
            msg = "That email and password don't match an active account."
            return render(request, "login.html", {"next": nxt, "error": msg, "email": email}, 401)
        cleared(key)
        hours = int(request.state.settings.get("session_hours", 12))
        token = auth.start_session(
            eng, user.id, hours=hours, user_agent=request.headers.get("user-agent")
        )
        auth.record(eng, user=user.email, action="Signed in", ip=client_ip(request))
        response = RedirectResponse(nxt, status_code=303)
        set_cookie(response, request, token, hours)
        return response

    @app.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(auth.SESSION_COOKIE)
        if request.state.user is not None and not request.state.user.is_local:
            audit(request, "Signed out")
        auth.end_session(eng, token)
        response = _to("/login")
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        if not auth.auth_enabled() or auth.user_count(eng) > 0:
            return _to("/")
        return render(
            request,
            "setup.html",
            {"error": None, "data": {}, "min": request.state.settings["password_min_length"]},
        )

    @app.post("/setup", response_class=HTMLResponse)
    async def setup(request: Request):
        if not auth.auth_enabled() or auth.user_count(eng) > 0:
            return _to("/")
        form = await request.form()
        data = {k: str(form.get(k) or "").strip() for k in ("email", "name", "workspace_name")}
        password, confirm = str(form.get("password") or ""), str(form.get("confirm") or "")
        min_len = int(request.state.settings["password_min_length"])
        problem = auth.password_problem(password, data["email"], min_len)
        if not problem and password != confirm:
            problem = "The two passwords are different."
        if not problem:
            try:
                user = auth.create_user(
                    eng,
                    email=data["email"],
                    name=data["name"],
                    role="admin",
                    password=password,
                    must_change=False,
                )
            except ValueError as e:
                problem = str(e)
        if problem:
            return render(
                request, "setup.html", {"error": problem, "data": data, "min": min_len}, 400
            )
        if data["workspace_name"]:
            auth.set_settings(eng, {"workspace_name": data["workspace_name"][:60]}, by=user.email)
        auth.record(
            eng, user=user.email, action="Created the first admin account", ip=client_ip(request)
        )
        hours = int(request.state.settings.get("session_hours", 12))
        token = auth.start_session(
            eng, user.id, hours=hours, user_agent=request.headers.get("user-agent")
        )
        response = _to("/")
        set_cookie(response, request, token, hours)
        return response

    # ---- user settings ------------------------------------------------------------------------
    @app.get("/settings")
    def settings_root():
        return _to("/settings/profile")

    @app.get("/settings/profile", response_class=HTMLResponse)
    def profile_page(request: Request, saved: str = "", error: str = "", must: int = 0):
        u = me(request)
        record = None if u.is_local else auth.get_user(eng, u.id)
        sessions = [] if u.is_local else auth.user_sessions(eng, u.id)
        ctx = {
            "section": "profile",
            "record": record,
            "saved": saved,
            "error": error,
            "must": must,
            "sessions": sessions,
            "role_help": auth.ROLE_HELP.get(u.role, ""),
            "min": request.state.settings["password_min_length"],
        }
        return render(request, "settings_profile.html", ctx)

    @app.post("/settings/profile")
    async def profile_save(request: Request):
        u = me(request)
        if u.is_local:
            return _to(
                "/settings/profile",
                error="The built-in admin has no profile. Turn on sign-in to use accounts.",
            )
        name = str((await request.form()).get("name") or "").strip()
        if not name:
            return _to("/settings/profile", error="Enter your name.")
        auth.update_user(eng, u.id, name=name[:80])
        audit(request, "Changed their display name")
        return _to("/settings/profile", saved="profile")

    @app.post("/settings/password")
    async def password_save(request: Request):
        u = me(request)
        if u.is_local:
            return _to("/settings/profile")
        form = await request.form()
        current, new, confirm = (str(form.get(k) or "") for k in ("current", "new", "confirm"))
        record = auth.get_user(eng, u.id)
        if not auth.verify_password(current, record.password_hash):
            audit(request, "Failed to change their password", outcome="failed")
            return _to("/settings/profile", error="Your current password is not right.")
        problem = auth.password_problem(
            new, u.email, int(request.state.settings["password_min_length"])
        )
        if not problem and new != confirm:
            problem = "The two new passwords are different."
        if not problem and new == current:
            problem = "Choose a password you have not used here."
        if problem:
            return _to(
                "/settings/profile", error=problem, must=1 if u.must_change_password else None
            )
        auth.update_user(eng, u.id, password=new, must_change_password=False)
        auth.end_user_sessions(eng, u.id, keep=request.cookies.get(auth.SESSION_COOKIE))
        audit(request, "Changed their password")
        return _to("/settings/profile", saved="password")

    @app.post("/settings/sessions/end-others")
    def end_other_sessions(request: Request):
        u = me(request)
        if not u.is_local:
            auth.end_user_sessions(eng, u.id, keep=request.cookies.get(auth.SESSION_COOKIE))
            audit(request, "Signed out of their other sessions")
        return _to("/settings/profile", saved="sessions")

    @app.get("/settings/preferences", response_class=HTMLResponse)
    def preferences_page(request: Request, saved: int = 0):
        return render(
            request,
            "settings_preferences.html",
            {"section": "preferences", "themes": THEMES, "saved": saved},
        )

    @app.post("/settings/preferences")
    async def preferences_save(request: Request):
        theme = str((await request.form()).get("theme") or "system")
        theme = theme if theme in THEMES else "system"
        u = me(request)
        response = _to("/settings/preferences", saved=1)
        if u.is_local:
            response.set_cookie(THEME_COOKIE, theme, max_age=365 * 86400, samesite="lax", path="/")
        else:
            prefs = dict(u.preferences or {}) | {"theme": theme}
            auth.update_user(eng, u.id, preferences=prefs)
        return response

    @app.get("/settings/tokens", response_class=HTMLResponse)
    def tokens_page(request: Request, created: str = ""):
        u = me(request)
        tokens = [] if u.is_local else auth.list_tokens(eng, u.id)
        return render(
            request,
            "settings_tokens.html",
            {"section": "tokens", "tokens": tokens, "new_token": None},
        )

    @app.post("/settings/tokens", response_class=HTMLResponse)
    async def token_create(request: Request):
        u = me(request)
        if u.is_local:
            return _to("/settings/tokens")
        form = await request.form()
        days = {"30": 30, "90": 90, "365": 365, "never": None}.get(
            str(form.get("days") or "90"), 90
        )
        name = str(form.get("name") or "").strip() or "API token"
        token = auth.create_token(eng, u.id, name, days)
        audit(request, "Created a personal access token", name)
        ctx = {"section": "tokens", "tokens": auth.list_tokens(eng, u.id), "new_token": token}
        return render(request, "settings_tokens.html", ctx)

    @app.post("/settings/tokens/{token_id}/revoke")
    def token_revoke(request: Request, token_id: str):
        u = me(request)
        if not u.is_local:
            auth.revoke_token(eng, u.id, token_id)
            audit(request, "Revoked a personal access token", token_id)
        return _to("/settings/tokens")

    # ---- admin: users ---------------------------------------------------------------------------
    @app.get("/admin")
    def admin_root():
        return _to("/admin/users")

    def users_ctx(request: Request, **extra) -> dict:
        q = request.query_params.get("q", "").strip().lower()
        users = [
            u for u in auth.list_users(eng) if not q or q in u.email.lower() or q in u.name.lower()
        ]
        return {
            "section": "users",
            "users": users,
            "q": q,
            "roles": auth.ROLES,
            "role_help": auth.ROLE_HELP,
            "temp": None,
            "error": request.query_params.get("error"),
            "notice": request.query_params.get("notice"),
            **extra,
        }

    @app.get("/admin/users", response_class=HTMLResponse)
    def users_page(request: Request):
        return render(request, "admin_users.html", users_ctx(request))

    @app.post("/admin/users", response_class=HTMLResponse)
    async def user_create(request: Request):
        form = await request.form()
        email, name, role = (str(form.get(k) or "").strip() for k in ("email", "name", "role"))
        temp = auth.temporary_password()
        try:
            user = auth.create_user(
                eng, email=email, name=name, role=role, password=temp, must_change=True
            )
        except ValueError as e:
            return render(request, "admin_users.html", users_ctx(request, error=str(e)), 400)
        audit(request, "Added a user", user.email, detail={"role": role})
        return render(
            request,
            "admin_users.html",
            users_ctx(request, temp={"email": user.email, "password": temp, "new": True}),
        )

    def guard_last_admin(target, becomes_admin: bool, stays_active: bool) -> str | None:
        loses_admin = (
            target.role == "admin" and target.active and not (becomes_admin and stays_active)
        )
        if loses_admin and auth.active_admins(eng) <= 1:
            return "The workspace needs at least one active admin."
        return None

    @app.post("/admin/users/{user_id}/role")
    async def user_role(request: Request, user_id: str):
        role = str((await request.form()).get("role") or "")
        target = auth.get_user(eng, user_id)
        if target is None or role not in auth.ROLES:
            return _to("/admin/users")
        problem = guard_last_admin(target, role == "admin", True)
        if problem:
            return _to("/admin/users", error=problem)
        if role != target.role:
            auth.update_user(eng, user_id, role=role)
            audit(
                request,
                "Changed a user's role",
                target.email,
                detail={"from": target.role, "to": role},
            )
        return _to("/admin/users", notice=f"{target.email} is now {auth.ROLES[role].lower()}.")

    @app.post("/admin/users/{user_id}/active")
    async def user_active(request: Request, user_id: str):
        active = str((await request.form()).get("active")) == "1"
        target = auth.get_user(eng, user_id)
        if target is None:
            return _to("/admin/users")
        if not active and target.id == me(request).id:
            return _to("/admin/users", error="You cannot deactivate your own account.")
        problem = guard_last_admin(target, target.role == "admin", active)
        if problem:
            return _to("/admin/users", error=problem)
        auth.update_user(eng, user_id, active=active)
        if not active:
            auth.end_user_sessions(eng, user_id)
        audit(request, "Reactivated a user" if active else "Deactivated a user", target.email)
        return _to(
            "/admin/users",
            notice=f"{target.email} is {'active again' if active else 'deactivated and signed out'}.",
        )

    @app.post("/admin/users/{user_id}/reset-password", response_class=HTMLResponse)
    def user_reset(request: Request, user_id: str):
        target = auth.get_user(eng, user_id)
        if target is None:
            return _to("/admin/users")
        temp = auth.temporary_password()
        auth.update_user(eng, user_id, password=temp, must_change_password=True)
        auth.end_user_sessions(eng, user_id)
        audit(request, "Reset a user's password", target.email)
        return render(
            request,
            "admin_users.html",
            users_ctx(request, temp={"email": target.email, "password": temp, "new": False}),
        )

    # ---- admin: workspace settings ------------------------------------------------------------------
    @app.get("/admin/workspace", response_class=HTMLResponse)
    def workspace_page(request: Request, saved: int = 0, error: str = ""):
        return render(
            request,
            "admin_workspace.html",
            {"section": "workspace", "s": request.state.settings, "saved": saved, "error": error},
        )

    @app.post("/admin/workspace")
    async def workspace_save(request: Request):
        form = await request.form()
        try:
            values = {
                "workspace_name": (str(form.get("workspace_name") or "").strip() or "Ingest")[:60],
                "session_hours": int(form.get("session_hours") or 12),
                "password_min_length": int(form.get("password_min_length") or 10),
                "viewers_can_query": form.get("viewers_can_query") == "on",
                "downloads": str(form.get("downloads") or "everyone"),
            }
        except ValueError:
            return _to(
                "/admin/workspace", error="Session length and password length must be numbers."
            )
        if not 1 <= values["session_hours"] <= 720:
            return _to("/admin/workspace", error="Session length must be between 1 and 720 hours.")
        if not 8 <= values["password_min_length"] <= 64:
            return _to(
                "/admin/workspace", error="Minimum password length must be between 8 and 64."
            )
        if values["downloads"] not in ("everyone", "editors", "admins"):
            values["downloads"] = "everyone"
        before = request.state.settings
        changed = auth.set_settings(eng, values, by=me(request).email)
        if changed:
            audit(
                request,
                "Changed workspace settings",
                ", ".join(changed),
                detail={k: {"from": before.get(k), "to": values[k]} for k in changed},
            )
        return _to("/admin/workspace", saved=1)

    # ---- admin: audit log ---------------------------------------------------------------------------
    @app.get("/admin/audit", response_class=HTMLResponse)
    def audit_page(
        request: Request, user: str = "", action: str = "", outcome: str = "", page: int = 1
    ):
        rows, total = auth.audit_events(
            eng, user=user, action=action, outcome=outcome, page=max(page, 1)
        )
        ctx = {
            "section": "audit",
            "rows": rows,
            "total": total,
            "page": max(page, 1),
            "per_page": 50,
            "user": user,
            "action": action,
            "outcome": outcome,
        }
        return render(request, "admin_audit.html", ctx)
