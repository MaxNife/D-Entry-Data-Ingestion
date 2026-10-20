"""Sign-in, access control and the audit trail for every request.

``AccessMiddleware`` resolves who is calling (session cookie, or ``Authorization: Bearer``
personal access token), checks the request against ``RULES`` and writes changes to the
audit log. With sign-in off (``INGEST_AUTH`` unset) every request runs as the built-in
local admin and is still audited.
"""

import re
import time
from collections import defaultdict, deque
from urllib.parse import quote, urlparse

from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ingest.core import auth

PUBLIC = re.compile(r"^/(health|static/.*|login|logout|setup|favicon\.ico)$")

# First match wins. Anything unlisted: GET needs "view", every other method needs "admin".
RULES: list[tuple[str, re.Pattern, tuple[str, ...]]] = [
    (m, re.compile(p), perms)
    for m, p, perms in [
        ("*", r"^/(docs|redoc|openapi\.json)$", ("admin",)),
        ("*", r"^/admin(/.*)?$", ("admin",)),
        ("*", r"^/settings(/.*)?$", ()),  # any signed-in user
        ("*", r"^/connections/(new|test|save)$", ("connections",)),
        ("*", r"^/connections/[^/]+/(edit|delete|retest)$", ("connections",)),
        ("POST", r"^/connections/[^/]+/use$", ("use_connections",)),
        ("GET", r"^/query/export$", ("sql", "download")),
        ("*", r"^/(query|api/sql/.*)$", ("sql",)),
        (
            "GET",
            r"^/api/batches/[^/]+/(reject-report\.csv|rejected-rows|source-file)$",
            ("download",),
        ),
        ("GET", r"^/tables/[^/]+/export$", ("download",)),
        ("GET", r"^/validations/[^/]+/report\.csv$", ("download",)),
        ("GET", r"^/upload$", ("ingest",)),
        ("POST", r"^/(upload|validate)$", ("ingest",)),
        ("POST", r"^/api/batches$", ("ingest",)),
        ("POST", r"^/(api/)?batches/[^/]+/(reprocess|fix/.+)$", ("ingest",)),
        ("GET", r"^/batches/[^/]+/fix$", ("ingest",)),
        ("POST", r"^/validations/[^/]+/ingest$", ("ingest",)),
        ("*", r"^/contracts/(new|save|infer)$", ("contracts",)),
        ("*", r"^/contracts/[^/]+/(edit|delete)$", ("contracts",)),
    ]
]

# What a change or download means, for the audit log. Paths not listed are not recorded
# (reads), or are recorded by their own handler with more detail (sign-in, admin, settings).
AUDIT = [
    (m, re.compile(p), label)
    for m, p, label in [
        ("POST", r"^/upload$", "Uploaded a file"),
        ("POST", r"^/api/batches$", "Uploaded a file through the API"),
        ("POST", r"^/validate$", "Validated a file"),
        ("POST", r"^/validations/[^/]+/ingest$", "Ingested a validated file"),
        ("POST", r"^/(api/)?batches/([^/]+)/reprocess$", "Reprocessed a run"),
        ("POST", r"^/batches/([^/]+)/fix/submit$", "Resubmitted fixed rows"),
        ("POST", r"^/contracts/save$", "Saved a contract version"),
        ("POST", r"^/contracts/([^/]+)/delete$", "Deleted a contract version"),
        ("POST", r"^/connections/save$", "Saved a connection"),
        ("POST", r"^/connections/([^/]+)/delete$", "Deleted a connection"),
        ("POST", r"^/connections/([^/]+)/use$", "Read data from a connection"),
        ("GET", r"^/tables/([^/]+)/export$", "Exported a table"),
        ("GET", r"^/query/export$", "Exported query results"),
        (
            "GET",
            r"^/api/batches/([^/]+)/(reject-report\.csv|rejected-rows|source-file)$",
            "Downloaded run data",
        ),
        ("GET", r"^/validations/([^/]+)/report\.csv$", "Downloaded a validation report"),
    ]
]


def client_ip(request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)


def required(method: str, path: str) -> tuple[str, ...]:
    for m, pattern, perms in RULES:
        if (m == "*" or m == method) and pattern.match(path):
            return perms
    return ("view",) if method in ("GET", "HEAD") else ("admin",)


def wants_json(request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


class AccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, engine, templates):
        super().__init__(app)
        self.engine = engine
        self.templates = templates

    async def dispatch(self, request, call_next):
        path, method = request.url.path, request.method
        settings = auth.get_settings(self.engine)
        request.state.settings = settings
        request.state.auth_on = auth.auth_enabled()
        user: auth.CurrentUser | None = None
        via_token = False

        if not request.state.auth_on:
            user = auth.LOCAL_ADMIN
        else:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                user, via_token = auth.token_user(self.engine, header[7:].strip()), True
            else:
                user = auth.session_user(self.engine, request.cookies.get(auth.SESSION_COOKIE))
        request.state.user = user
        request.state.perms = auth.permissions(user, settings)

        if PUBLIC.match(path):
            return await call_next(request)

        if request.state.auth_on:
            if auth.user_count(self.engine) == 0:
                if wants_json(request):
                    return JSONResponse(
                        {"error": "SETUP_REQUIRED", "detail": "Create the first admin at /setup."},
                        503,
                    )
                return RedirectResponse("/setup", status_code=303)
            if user is None:
                if method == "GET" and not wants_json(request):
                    return RedirectResponse(
                        f"/login?next={quote(path + ('?' + request.url.query if request.url.query else ''))}",
                        303,
                    )
                return JSONResponse(
                    {
                        "error": "UNAUTHENTICATED",
                        "detail": "Sign in, or send a personal access token.",
                    },
                    401,
                )
            if (
                user.must_change_password
                and not via_token
                and path not in ("/settings/profile", "/settings/password")
            ):
                return RedirectResponse("/settings/profile?must=1", status_code=303)
            # Cookie-authenticated writes must come from this site (SameSite=Lax is the first line).
            origin = request.headers.get("origin")
            writes = method not in ("GET", "HEAD", "OPTIONS") and not via_token
            if writes and origin and urlparse(origin).netloc != request.url.netloc:
                return JSONResponse(
                    {"error": "FORBIDDEN", "detail": "Cross-site request refused."}, 403
                )

        need = required(method, path)
        if any(p not in request.state.perms for p in need):
            auth.record(
                self.engine,
                user=user.email if user else None,
                action="Was refused access",
                target=f"{method} {path}",
                outcome="denied",
                ip=client_ip(request),
            )
            if wants_json(request) or method != "GET":
                return JSONResponse(
                    {"error": "FORBIDDEN", "detail": "Your role does not allow this."}, 403
                )
            ctx = {
                "title": "You don't have access",
                "detail": "Your role does not allow this page. Ask a workspace admin if you need it.",
            }
            return self.templates.TemplateResponse(request, "error.html", ctx, status_code=403)

        response = await call_next(request)

        for m, pattern, label in AUDIT:
            hit = pattern.match(path)
            if m == method and hit:
                target = response.headers.get("location") or (
                    hit.group(hit.lastindex) if hit.lastindex else path
                )
                outcome = "ok" if response.status_code < 400 else "failed"
                auth.record(
                    self.engine,
                    user=user.email if user else None,
                    action=label,
                    target=target,
                    outcome=outcome,
                    ip=client_ip(request),
                    detail={"status": response.status_code},
                )
                break
        return response


# ---- sign-in throttling ----------------------------------------------------------------------

_failures: dict[str, deque] = defaultdict(deque)
MAX_FAILURES, WINDOW_S = 5, 15 * 60


def throttled(key: str) -> bool:
    q = _failures[key]
    now = time.monotonic()
    while q and now - q[0] > WINDOW_S:
        q.popleft()
    return len(q) >= MAX_FAILURES


def failed(key: str) -> None:
    _failures[key].append(time.monotonic())


def cleared(key: str) -> None:
    _failures.pop(key, None)
