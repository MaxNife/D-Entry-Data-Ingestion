"""Initial schema: batches, batch_chunks, rejected_records.

Target tables are NOT migrated: they are created at runtime from the contract
(see core.loader.ensure_target_table), one per contract.
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("batch_id", sa.String(36), primary_key=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("contract_name", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("total_rows", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("accepted_rows", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("rejected_rows", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("retry_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("file_sha256", "contract_name", "contract_version"),
    )
    op.create_index("ix_batches_sha", "batches", ["file_sha256"])
    op.create_index("ix_batches_status_created", "batches", ["status", "created_at"])

    op.create_table(
        "batch_chunks",
        sa.Column("chunk_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("batches.batch_id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("row_start", sa.BigInteger(), nullable=False),
        sa.Column("row_end", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_table(
        "rejected_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("batches.batch_id", ondelete="CASCADE"), nullable=False),
        sa.Column("row_number", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason_message", sa.Text(), nullable=False),
        sa.Column("column_name", sa.Text(), nullable=True),
        sa.Column("offending_value", sa.Text(), nullable=True),
        sa.Column("original_row", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_rejected_batch", "rejected_records", ["batch_id"])
    op.create_index("ix_rejected_reason_batch", "rejected_records", ["reason_code", "batch_id"])


def downgrade() -> None:
    op.drop_index("ix_rejected_reason_batch", table_name="rejected_records")
    op.drop_index("ix_rejected_batch", table_name="rejected_records")
    op.drop_table("rejected_records")
    op.drop_table("batch_chunks")
    op.drop_index("ix_batches_status_created", table_name="batches")
    op.drop_index("ix_batches_sha", table_name="batches")
    op.drop_table("batches")
