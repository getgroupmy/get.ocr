from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (JSON, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, TypeDecorator,
                        create_engine, event)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from . import crypto
from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EncryptedJSON(TypeDecorator):
    """JSON column encrypted with the storage key (holds the text that was read)."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        return None if value is None else crypto.encrypt(json.dumps(value).encode())

    def process_result_value(self, value: bytes | None, dialect: Any) -> Any:
        return None if value is None else json.loads(crypto.decrypt(value))


class EncryptedBytes(TypeDecorator):
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: bytes | None, dialect: Any) -> bytes | None:
        return None if value is None else crypto.encrypt(value)

    def process_result_value(self, value: bytes | None, dialect: Any) -> bytes | None:
        return None if value is None else crypto.decrypt(value)


class Base(DeclarativeBase):
    pass


class ApiClient(Base):
    """An app that may call the API. The key is stored hashed and shown once."""
    __tablename__ = "api_clients"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    webhook_secret: Mapped[bytes | None] = mapped_column(EncryptedBytes)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    """A file queued for reading. The file is deleted once read; the result is
    kept encrypted until the retention period ends or the client deletes it."""
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("api_clients.id"), index=True)
    external_ref: Mapped[str | None] = mapped_column(String(200), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")  # queued|processing|completed|failed
    options: Mapped[dict | None] = mapped_column(JSON)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int | None] = mapped_column(Integer)  # pages read
    result: Mapped[dict | None] = mapped_column(EncryptedJSON)
    error: Mapped[str | None] = mapped_column(String(60))  # machine readable code, no content
    error_message: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    result_purged: Mapped[bool] = mapped_column(Boolean, default=False)

    client: Mapped[ApiClient] = relationship()


class Usage(Base):
    """One row per reading: counts only, never file names or text."""
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("api_clients.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(10))  # sync | job
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


_engine = None
_Session: sessionmaker[Session] | None = None


def init_db() -> None:
    global _engine, _Session
    s = get_settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.inputs_dir.mkdir(parents=True, exist_ok=True)
    url = s.db_url
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _pragmas(conn, _):  # noqa: ANN001
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    assert _Session is not None, "init_db() not called"
    db = _Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    with session_scope() as db:
        yield db
