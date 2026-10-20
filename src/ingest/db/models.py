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


class Connection(Base):
    """An external database: a source to validate/import from, or a contract's destination."""

    __tablename__ = "connections"

    name: Mapped[str] = mapped_column(String(63), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # postgresql | mssql | sqlite
    host: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    database: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    username: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    options: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class User(Base):
    """A person who signs in. Role: admin | editor | viewer (see ingest.core.auth.ROLES)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    preferences: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserSession(Base):
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)


class ApiToken(Base):
    """A personal access token for the HTTP API. Only its SHA-256 is stored."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkspaceSetting(Base):
    __tablename__ = "workspace_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), default="ok")
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (Index("ix_audit_events_at", "at"),)


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
