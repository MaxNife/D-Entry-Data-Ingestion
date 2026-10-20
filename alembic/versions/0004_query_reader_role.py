"""A read-only role for the SQL editor, without access to the app's own tables.

App tables (accounts, sessions, tokens, audit log, settings, stored connections, chunk
and migration bookkeeping; see ``ingest.core.app_tables``) are never queryable from the
editor. The editor checks table names itself; on
PostgreSQL this migration makes the database enforce it too: editor queries run as
``ingest_reader``, which may read every table in ``public`` except the app tables.

Creating a role needs the CREATEROLE privilege. Without it this migration does nothing
and the editor relies on its own name check (System status says which is in force).
"""

from alembic import op

revision = "0004_query_reader_role"
down_revision = "0003_users_and_audit"
branch_labels = None
depends_on = None

APP_TABLES = (
    "alembic_version",
    "connections",
    "users",
    "user_sessions",
    "api_tokens",
    "workspace_settings",
    "audit_events",
    "batch_chunks",
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    revoke = "\n".join(f"    REVOKE ALL ON TABLE public.{t} FROM ingest_reader;" for t in APP_TABLES)
    op.execute(f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_reader') THEN
        CREATE ROLE ingest_reader NOLOGIN;
    END IF;
    GRANT ingest_reader TO CURRENT_USER;
    GRANT USAGE ON SCHEMA public TO ingest_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO ingest_reader;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ingest_reader;
{revoke}
EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'ingest_reader not created (no CREATEROLE); the SQL editor uses its name check only';
END
$$;
""")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ingest_reader') THEN
        ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM ingest_reader;
        REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ingest_reader;
        REVOKE USAGE ON SCHEMA public FROM ingest_reader;
    END IF;
END
$$;
""")
