"""External database connections (sources and destinations).

Passwords are stored encrypted (``password_enc``); see ingest.core.secrets.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_connections"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("name", sa.String(63), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),  # postgresql | mssql | sqlite
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("database", sa.Text(), nullable=True),  # database name, or SQLite file path
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("password_enc", sa.Text(), nullable=True),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("connections")
