"""Modelo de dados (SQLAlchemy 2). Tipos portáveis para migrar a PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from nikki.settings import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Promotion(Base):
    __tablename__ = "promotions"
    __table_args__ = (UniqueConstraint("store", "external_id", name="uq_promo_store_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    store: Mapped[str] = mapped_column(String(60), index=True)
    sku: Mapped[str | None] = mapped_column(String(120))
    product_key: Mapped[str] = mapped_column(String(300), index=True)
    product_name: Mapped[str] = mapped_column(String(500))
    name_key: Mapped[str] = mapped_column(String(200), index=True, default="")
    category: Mapped[str | None] = mapped_column(String(120))
    original_price: Mapped[float | None] = mapped_column(Float)
    current_price: Mapped[float] = mapped_column(Float)
    discount_percent: Mapped[float | None] = mapped_column(Float)
    coupon: Mapped[str | None] = mapped_column(String(80))
    product_url: Mapped[str] = mapped_column(Text)
    affiliate_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    popularity: Mapped[float | None] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    tier: Mapped[str | None] = mapped_column(String(20))
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # rede -> formatos
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    variants: Mapped[list[ContentVariant]] = relationship(back_populates="promotion")
    jobs: Mapped[list[PublicationJob]] = relationship(back_populates="promotion")


class AggregatePost(Base):
    __tablename__ = "aggregate_posts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(300))
    promotion_ids: Mapped[list[Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ContentVariant(Base):
    __tablename__ = "content_variants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promotion_id: Mapped[int | None] = mapped_column(ForeignKey("promotions.id"), index=True)
    aggregate_id: Mapped[int | None] = mapped_column(ForeignKey("aggregate_posts.id"), index=True)
    network: Mapped[str] = mapped_column(String(20))
    format: Mapped[str] = mapped_column(String(20))
    title: Mapped[str | None] = mapped_column(String(300))
    caption: Mapped[str] = mapped_column(Text)
    group_key: Mapped[str] = mapped_column(String(40))
    group_url: Mapped[str] = mapped_column(Text)
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    promotion: Mapped[Promotion | None] = relationship(back_populates="variants")


class MediaAsset(Base):
    __tablename__ = "media_assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promotion_id: Mapped[int | None] = mapped_column(ForeignKey("promotions.id"), index=True)
    aggregate_id: Mapped[int | None] = mapped_column(ForeignKey("aggregate_posts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # feed | story | video
    source: Mapped[str] = mapped_column(String(20))  # canva | local | ffmpeg
    local_path: Mapped[str] = mapped_column(Text)
    public_url: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    external_ref: Mapped[str | None] = mapped_column(Text)  # ex.: design id do Canva
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicationJob(Base):
    __tablename__ = "publication_jobs"
    __table_args__ = (
        UniqueConstraint("promotion_id", "aggregate_id", "network", "format", name="uq_job_target"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(40), index=True)
    promotion_id: Mapped[int | None] = mapped_column(ForeignKey("promotions.id"), index=True)
    aggregate_id: Mapped[int | None] = mapped_column(ForeignKey("aggregate_posts.id"), index=True)
    variant_id: Mapped[int] = mapped_column(ForeignKey("content_variants.id"))
    media_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    network: Mapped[str] = mapped_column(String(20), index=True)
    format: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=4)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)  # horário local (tz da config)
    external_id: Mapped[str | None] = mapped_column(String(120))
    external_uuid: Mapped[str | None] = mapped_column(String(120))
    external_status: Mapped[str | None] = mapped_column(String(40))
    public_url: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(String(20))  # retryable | non_retryable
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    promotion: Mapped[Promotion | None] = relationship(back_populates="jobs")
    attempts_log: Mapped[list[PublicationAttempt]] = relationship(back_populates="job")


class PublicationAttempt(Base):
    __tablename__ = "publication_attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("publication_jobs.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    retryable: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    job: Mapped[PublicationJob] = relationship(back_populates="attempts_log")


class ChannelDestination(Base):
    """Espelho auditável de config/whatsapp_groups.yaml (a config é a fonte de verdade)."""

    __tablename__ = "channel_destinations"
    __table_args__ = (UniqueConstraint("network", "role", name="uq_dest_net_role"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    network: Mapped[str] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(20))
    url: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10))
    event: Mapped[str] = mapped_column(String(60), index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(40), index=True)
    promotion_id: Mapped[int | None] = mapped_column(Integer, index=True)
    job_id: Mapped[int | None] = mapped_column(Integer)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class KeyValue(Base):
    """Estado pequeno persistente (tokens OAuth do Canva, cursores, status de integrações)."""

    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshots"
    __table_args__ = (UniqueConstraint("network", "post_id", name="uq_analytics_post"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    network: Mapped[str] = mapped_column(String(20), index=True)
    post_id: Mapped[str] = mapped_column(String(120))
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[str | None] = mapped_column(String(40))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


_engine = None
_Session: sessionmaker[Session] | None = None


def init_engine(url: str | None = None):
    global _engine, _Session
    url = url or get_settings().database_url
    if url.startswith("sqlite:///"):
        from pathlib import Path

        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    _engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _Session is None:
        init_engine()
    assert _Session is not None
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def kv_get(s: Session, key: str) -> dict[str, Any] | None:
    row = s.get(KeyValue, key)
    return row.value if row else None


def kv_set(s: Session, key: str, value: dict[str, Any]) -> None:
    row = s.get(KeyValue, key)
    if row:
        row.value = value
    else:
        s.add(KeyValue(key=key, value=value))
