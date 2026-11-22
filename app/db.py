"""Database models. Connectors never touch these; only the runtime does (Appendix A)."""
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Destination(Base):
    """One catalogue row = one destination (one forum/community/site). Config, not code."""
    __tablename__ = "destinations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    platform_key: Mapped[str] = mapped_column(String(50))
    surface: Mapped[str] = mapped_column(String(50), default="topic")
    category: Mapped[str] = mapped_column(String(100), default="")
    audience_tags: Mapped[Any] = mapped_column(JSON, default=list)
    language: Mapped[str] = mapped_column(String(20), default="en")
    geo: Mapped[str] = mapped_column(String(50), default="global")
    est_audience: Mapped[int] = mapped_column(Integer, default=0)
    rules_url: Mapped[str] = mapped_column(String(500), default="")
    frequency_cap: Mapped[Any] = mapped_column(JSON, default=dict)  # {"max_posts":1,"window_hours":168}
    self_promo_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    link_policy: Mapped[str] = mapped_column(String(20), default="allowed")  # allowed|forbidden|max:N
    disclosure_required: Mapped[bool] = mapped_column(Boolean, default=False)
    disclosure_text: Mapped[str] = mapped_column(String(300), default="This content was created with AI assistance.")
    automation_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    connector_key: Mapped[str] = mapped_column(String(50))
    relevance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    relevance_reason: Mapped[str] = mapped_column(String(500), default="")
    level: Mapped[str] = mapped_column(String(4), default="L0")  # L0..L4
    last_certified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # runtime fields
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|demoted|revoked
    credentials_ref: Mapped[str] = mapped_column(String(100), default="")  # NAME of an env var, never the secret
    options: Mapped[Any] = mapped_column(JSON, default=dict)  # base_url, community, username, ...
    certified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[Any] = mapped_column(JSON, default=dict)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[Any] = mapped_column(JSON, nullable=True)
    callback_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(100), default="-")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idem_scope_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Post(Base):
    """One publish attempt, unique per idempotency key => never a second post."""
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_post_key"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    destination_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="pending")  # pending|unknown|published|deleted|failed
    platform_post_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    live_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    promotional: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RuleEvent(Base):
    __tablename__ = "rule_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    destination_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(200), default="")
    verdict: Mapped[str] = mapped_column(String(20))  # allow|block|reschedule
    violations: Mapped[Any] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Certification(Base):
    __tablename__ = "certifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    destination_id: Mapped[str] = mapped_column(String(64))
    connector_key: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20))  # passed|failed
    environment: Mapped[str] = mapped_column(String(30))  # simulation|local-instance
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[Any] = mapped_column(JSON, default=dict)
    evidence_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectorState(Base):
    __tablename__ = "connector_state"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


_engine = None
_Session = None


def _harden_sqlite(engine) -> None:
    """File-based SQLite shared by several processes: WAL (readers never block the writer), a 30 s busy timeout, and
    BEGIN IMMEDIATE so a transaction takes the write lock up front instead of failing later with 'database is locked'
    when a read lock cannot be upgraded. Postgres and the in-memory test database are not affected."""
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        dbapi_conn.isolation_level = None  # we emit BEGIN ourselves (see _on_begin)
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def get_engine():
    global _engine, _Session
    if _engine is None:
        url = get_settings().database_url
        kwargs: dict[str, Any] = {}
        file_sqlite = url.startswith("sqlite") and ":memory:" not in url
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False, "timeout": 30}}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        _engine = create_engine(url, pool_pre_ping=True, **kwargs)
        if file_sqlite:
            _harden_sqlite(_engine)  # several processes (API + worker) share one file in the single-container demo
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(get_engine())


def reset_db_engine() -> None:
    global _engine, _Session
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _Session = None


def session_scope() -> Session:
    get_engine()
    return _Session()  # type: ignore[misc]


def get_session() -> Iterator[Session]:
    s = session_scope()
    try:
        yield s
    finally:
        s.close()
