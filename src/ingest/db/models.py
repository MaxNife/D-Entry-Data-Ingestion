"""SQLAlchemy models. src/ingest/db/models.py — spec §4."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    batch_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    file_name: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(String(64))
    contract_name: Mapped[str] = mapped_column(Text)
    contract_version: Mapped[int] = mapped_column(Integer)
    contract_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(Text, default="pending")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    total_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    accepted_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    rejected_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    retry_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    __table_args__ = (
        UniqueConstraint("file_sha256", "contract_name", "contract_version"),
        Index("ix_batches_sha", "file_sha256"),
        Index("ix_batches_status_created", "status", "created_at"),
    )


class BatchChunk(Base):
    __tablename__ = "batch_chunks"

    chunk_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("batches.batch_id", ondelete="CASCADE")
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    row_start: Mapped[int] = mapped_column(BigInteger)
    row_end: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)  # committed | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)


class RejectedRecord(Base):
    __tablename__ = "rejected_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("batches.batch_id", ondelete="CASCADE")
    )
    row_number: Mapped[int] = mapped_column(BigInteger)
    reason_code: Mapped[str] = mapped_column(Text)
    reason_message: Mapped[str] = mapped_column(Text)
    column_name: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    offending_value: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    original_row: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_rejected_batch", "batch_id"),
        Index("ix_rejected_reason_batch", "reason_code", "batch_id"),
    )


# Map contract column types to SQLAlchemy types for dynamic target tables.
TARGET_TYPE_MAP = {
    "int": Integer,
    "float": Float,
    "str": Text,
    "bool": Boolean,
    "date": Date,
    "datetime": DateTime,
    "decimal": Numeric,
}
