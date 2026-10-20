"""Users, roles, sessions, API tokens, workspace settings and the audit log.

Sign-in is turned on with ``INGEST_AUTH=on`` (the Compose file sets it). With it off,
the app runs as a single built-in local admin, which is how local development and the
test suite run.

Roles (decision 2026-10-20, modelled on Fabric workspace roles):
  viewer  see everything; read-only SQL and downloads if the workspace allows them
  editor  + upload, validate, fix rows, reprocess, author contracts, use connections
  admin   + users, workspace settings, connections, audit log, system status, API docs
"""

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ingest.db.models import ApiToken, AuditEvent, User, UserSession, WorkspaceSetting

ROLES = {
    "viewer": "Viewer",
    "editor": "Editor",
    "admin": "Admin",
}
ROLE_HELP = {
    "viewer": "Sees runs, contracts, tables, validations and the audit trail.",
    "editor": "Can also upload and validate files, fix rows, reprocess and author contracts.",
    "admin": "Can also manage people, connections and workspace settings.",
}
BASE_PERMS = {
    "viewer": {"view"},
    "editor": {"view", "ingest", "contracts", "use_connections"},
    "admin": {"view", "ingest", "contracts", "use_connections", "connections", "admin"},
}
DEFAULT_SETTINGS = {
    "workspace_name": "Ingest",
    "session_hours": 12,
    "password_min_length": 10,
    "viewers_can_query": True,
    "downloads": "everyone",  # everyone | editors | admins
}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_COOKIE = "ingest_session"
TOKEN_PREFIX = "ing_"


def auth_enabled() -> bool:
    return os.environ.get("INGEST_AUTH", "off").strip().lower() in ("on", "1", "true", "yes")


@dataclass
class CurrentUser:
    id: str
    email: str
    name: str
    role: str
    preferences: dict = field(default_factory=dict)
    must_change_password: bool = False
    is_local: bool = False  # the built-in admin used when sign-in is off

    @property
    def initials(self) -> str:
        parts = [p for p in re.split(r"[\s._@-]+", self.name or self.email) if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    @property
    def role_label(self) -> str:
        return ROLES.get(self.role, self.role)


LOCAL_ADMIN = CurrentUser(
    id="local", email="local", name="Local admin", role="admin", is_local=True
)


def _current(u: User) -> CurrentUser:
    return CurrentUser(
        id=u.id,
        email=u.email,
        name=u.name,
        role=u.role,
        preferences=dict(u.preferences or {}),
        must_change_password=bool(u.must_change_password),
    )


def permissions(user: CurrentUser | None, settings: dict) -> set[str]:
    if user is None:
        return set()
    perms = set(BASE_PERMS.get(user.role, set()))
    if user.role != "viewer" or settings.get("viewers_can_query", True):
        perms.add("sql")
    downloads = settings.get("downloads", "everyone")
    if (
        downloads == "everyone"
        or (downloads == "editors" and user.role != "viewer")
        or user.role == "admin"
    ):
        perms.add("download")
    return perms


# ---- passwords -------------------------------------------------------------------------

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            dklen=32,
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return hmac.compare_digest(got.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str, email: str, min_length: int) -> str | None:
    if len(password) < min_length:
        return f"Use at least {min_length} characters."
    if (
        password.lower() == (email or "").lower()
        or password.lower() == (email or "").split("@")[0].lower()
    ):
        return "The password cannot be your email address."
    if len(set(password)) < 4:
        return "Use a less repetitive password."
    return None


def temporary_password() -> str:
    return secrets.token_urlsafe(9)


# ---- users -------------------------------------------------------------------------------


def user_count(engine) -> int:
    with Session(engine) as s:
        return int(s.execute(select(func.count()).select_from(User)).scalar_one())


def list_users(engine) -> list[User]:
    with Session(engine) as s:
        return list(s.execute(select(User).order_by(User.active.desc(), User.name)).scalars())


def get_user(engine, user_id: str) -> User | None:
    with Session(engine) as s:
        return s.get(User, user_id)


def find_user(engine, email: str) -> User | None:
    with Session(engine) as s:
        return (
            s.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
            .scalars()
            .first()
        )


def create_user(
    engine, *, email: str, name: str, role: str, password: str, must_change: bool
) -> User:
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address.")
    if role not in ROLES:
        raise ValueError("Choose a role.")
    if find_user(engine, email):
        raise ValueError(f"{email} already has an account.")
    u = User(
        email=email,
        name=name.strip() or email.split("@")[0],
        role=role,
        password_hash=hash_password(password),
        must_change_password=must_change,
    )
    with Session(engine) as s, s.begin():
        s.add(u)
    return find_user(engine, email)


def update_user(engine, user_id: str, **values) -> None:
    if "password" in values:
        values["password_hash"] = hash_password(values.pop("password"))
    with Session(engine) as s, s.begin():
        s.execute(update(User).where(User.id == user_id).values(**values))


def active_admins(engine) -> int:
    with Session(engine) as s:
        q = (
            select(func.count())
            .select_from(User)
            .where(User.role == "admin", User.active.is_(True))
        )
        return int(s.execute(q).scalar_one())


def authenticate(engine, email: str, password: str) -> User | None:
    u = find_user(engine, email or "")
    if u is None or not u.active:
        verify_password(password or "", hash_password("timing-equaliser"))  # same cost either way
        return None
    return u if verify_password(password or "", u.password_hash) else None


# ---- sessions ---------------------------------------------------------------------------------


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def start_session(engine, user_id: str, *, hours: int, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        s.add(
            UserSession(
                token_hash=_digest(token),
                user_id=user_id,
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=hours),
                user_agent=(user_agent or "")[:300],
            )
        )
        s.execute(update(User).where(User.id == user_id).values(last_login_at=now))
    return token


def session_user(engine, token: str | None) -> CurrentUser | None:
    if not token:
        return None
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        row = s.get(UserSession, _digest(token))
        if row is None:
            return None
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
        if expires <= now:
            s.delete(row)
            return None
        u = s.get(User, row.user_id)
        if u is None or not u.active:
            return None
        seen = row.last_seen_at if row.last_seen_at.tzinfo else row.last_seen_at.replace(tzinfo=UTC)
        if now - seen > timedelta(minutes=5):  # don't write on every request
            row.last_seen_at = now
        return _current(u)


def end_session(engine, token: str | None) -> None:
    if token:
        with Session(engine) as s, s.begin():
            s.execute(delete(UserSession).where(UserSession.token_hash == _digest(token)))


def end_user_sessions(engine, user_id: str, *, keep: str | None = None) -> None:
    with Session(engine) as s, s.begin():
        stmt = delete(UserSession).where(UserSession.user_id == user_id)
        if keep:
            stmt = stmt.where(UserSession.token_hash != _digest(keep))
        s.execute(stmt)


def user_sessions(engine, user_id: str) -> list[UserSession]:
    with Session(engine) as s:
        q = (
            select(UserSession)
            .where(UserSession.user_id == user_id)
            .order_by(UserSession.last_seen_at.desc())
        )
        return list(s.execute(q).scalars())


# ---- personal access tokens ---------------------------------------------------------------------


def create_token(engine, user_id: str, name: str, days: int | None) -> str:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with Session(engine) as s, s.begin():
        s.add(
            ApiToken(
                user_id=user_id,
                name=(name or "API token").strip()[:80],
                prefix=token[:10],
                token_hash=_digest(token),
                expires_at=datetime.now(UTC) + timedelta(days=days) if days else None,
            )
        )
    return token


def token_user(engine, token: str) -> CurrentUser | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        row = (
            s.execute(select(ApiToken).where(ApiToken.token_hash == _digest(token)))
            .scalars()
            .first()
        )
        if row is None:
            return None
        if row.expires_at is not None:
            exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
            if exp <= now:
                return None
        u = s.get(User, row.user_id)
        if u is None or not u.active:
            return None
        row.last_used_at = now
        return _current(u)


def list_tokens(engine, user_id: str) -> list[ApiToken]:
    with Session(engine) as s:
        q = select(ApiToken).where(ApiToken.user_id == user_id).order_by(ApiToken.created_at.desc())
        return list(s.execute(q).scalars())


def revoke_token(engine, user_id: str, token_id: str) -> None:
    with Session(engine) as s, s.begin():
        s.execute(delete(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user_id))


# ---- workspace settings --------------------------------------------------------------------------


def get_settings(engine) -> dict:
    out = dict(DEFAULT_SETTINGS)
    try:
        with Session(engine) as s:
            for row in s.execute(select(WorkspaceSetting)).scalars():
                out[row.key] = row.value
    except Exception:  # before the migration runs, the defaults stand
        pass
    return out


def set_settings(engine, values: dict, by: str) -> list[str]:
    """Store the changed settings. Returns the keys that changed."""
    current = get_settings(engine)
    changed = [k for k, v in values.items() if k in DEFAULT_SETTINGS and current.get(k) != v]
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        for k in changed:
            row = s.get(WorkspaceSetting, k)
            if row is None:
                s.add(WorkspaceSetting(key=k, value=values[k], updated_at=now, updated_by=by))
            else:
                row.value, row.updated_at, row.updated_by = values[k], now, by
    return changed


# ---- audit log ---------------------------------------------------------------------------------------


def record(
    engine,
    *,
    user: str | None,
    action: str,
    target: str | None = None,
    outcome: str = "ok",
    ip: str | None = None,
    detail: dict | None = None,
) -> None:
    try:
        with Session(engine) as s, s.begin():
            s.add(
                AuditEvent(
                    user_email=user,
                    action=action,
                    target=target,
                    outcome=outcome,
                    ip=ip,
                    detail=detail,
                )
            )
    except Exception:  # the audit trail must never break the request it describes
        pass


def audit_events(
    engine,
    *,
    user: str = "",
    action: str = "",
    outcome: str = "",
    page: int = 1,
    per_page: int = 50,
):
    stmt = select(AuditEvent).order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
    if user:
        stmt = stmt.where(AuditEvent.user_email.ilike(f"%{user}%"))
    if action:
        stmt = stmt.where(AuditEvent.action.ilike(f"%{action}%"))
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    with Session(engine) as s:
        total = s.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ).scalar_one()
        rows = list(s.execute(stmt.offset((page - 1) * per_page).limit(per_page)).scalars())
    return rows, int(total)
