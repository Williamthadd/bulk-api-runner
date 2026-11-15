"""ORM models. Schema itself is owned by Alembic (migrations/versions)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, BigInteger)
from sqlalchemy.dialects.mysql import DATETIME, MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DT = DATETIME(fsp=3)
LongText = Text().with_variant(MEDIUMTEXT(), "mysql")


def utcnow() -> datetime:
    """Naive UTC with millisecond precision (stored in DATETIME(3))."""
    n = datetime.now(timezone.utc).replace(tzinfo=None)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)


class Base(DeclarativeBase):
    pass


class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    auth_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DT, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DT, default=utcnow, onupdate=utcnow)

    requests: Mapped[list["Request"]] = relationship(back_populates="folder", passive_deletes=True)


class Request(Base):
    __tablename__ = "requests"
    __table_args__ = (UniqueConstraint("folder_id", "name", name="uq_requests_folder_name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[int] = mapped_column(ForeignKey("folders.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(10), default="GET")
    url: Mapped[str] = mapped_column(String(2000), default="")
    headers: Mapped[list] = mapped_column(JSON, default=list)
    params: Mapped[list] = mapped_column(JSON, default=list)
    auth_config: Mapped[dict] = mapped_column(JSON, default=dict)
    data_source: Mapped[dict] = mapped_column(JSON, default=dict)
    script: Mapped[str] = mapped_column(LongText, default="")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DT, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DT, default=utcnow, onupdate=utcnow)

    folder: Mapped[Folder] = relationship(back_populates="requests")


class Environment(Base):
    __tablename__ = "environments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    variables: Mapped[list["EnvironmentVariable"]] = relationship(
        back_populates="environment", cascade="all, delete-orphan", passive_deletes=True,
        order_by="EnvironmentVariable.id")


class EnvironmentVariable(Base):
    __tablename__ = "environment_variables"
    __table_args__ = (UniqueConstraint("environment_id", "key", name="uq_envvar_env_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(200))
    value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    environment: Mapped[Environment] = relationship(back_populates="variables")


class AuthProfile(Base):
    """OAuth2 token endpoint used to fetch Bearer tokens automatically (client credentials)."""
    __tablename__ = "auth_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    token_url: Mapped[str] = mapped_column(String(2000), default="")
    client_id: Mapped[str] = mapped_column(String(500), default="")
    client_secret: Mapped[str | None] = mapped_column(Text, nullable=True)   # Fernet-encrypted
    grant_type: Mapped[str] = mapped_column(String(50), default="client_credentials")
    scope: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DT, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DT, default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("requests.id", ondelete="SET NULL"), nullable=True)
    request_name: Mapped[str] = mapped_column(String(200))
    environment_name: Mapped[str] = mapped_column(String(100), default="")
    mode: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    total: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DT, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DT, nullable=True)


class RunItem(Base):
    __tablename__ = "run_items"
    __table_args__ = (Index("ix_run_items_run_seq", "run_id", "seq"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(500), default="")
    method: Mapped[str] = mapped_column(String(10))
    url: Mapped[str] = mapped_column(String(2000))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    message: Mapped[str] = mapped_column(Text, default="")
    request_body: Mapped[str] = mapped_column(LongText, default="")
    response_body: Mapped[str] = mapped_column(LongText, default="")
    created_at: Mapped[datetime] = mapped_column(DT, default=utcnow)


class Upload(Base):
    __tablename__ = "uploads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("requests.id", ondelete="SET NULL"), nullable=True)
    original_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DT, default=utcnow)
