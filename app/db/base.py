from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _build_engine() -> Engine:
    from sqlalchemy import create_engine

    connect_args: dict[str, object] = {}
    if _is_sqlite(settings.DATABASE_URL):
        # Allow use across threads (FastAPI sync sessions in threadpool, APScheduler workers).
        connect_args["check_same_thread"] = False

    eng = create_engine(
        settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if _is_sqlite(settings.DATABASE_URL):
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()

    return eng


engine: Engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)
