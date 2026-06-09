from __future__ import annotations

import contextlib
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlalchemy import Delete, Insert, Update, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.orm import Session as SaSession
from sqlalchemy.pool import QueuePool

from app.config import settings
from app.logging_config import get_logger

log = get_logger("db")

# Recycle pooled libsql connections this many seconds after creation. Must stay
# safely BELOW Turso's Hrana idle-stream TTL (~10s) so we never check out a
# connection whose server-side stream has already expired.
LIBSQL_POOL_RECYCLE_SECONDS = 5


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _is_libsql(url: str) -> bool:
    """Turso / libSQL remote DB. Connection is over HTTPS — no local file, no PRAGMAs."""
    return url.startswith("sqlite+libsql:")


def _is_local_sqlite(url: str) -> bool:
    """Local SQLite file (sqlite:///path or sqlite://). Excludes the libsql variant."""
    return url.startswith("sqlite:") and not _is_libsql(url)


def _split_libsql_url(url: str) -> tuple[str, dict[str, object]]:
    """Lift the `authToken` query param into connect_args (libsql_experimental expects
    `auth_token` kwarg). Keep `secure=true` in the URL — the dialect uses it to switch
    the underlying scheme to https://, which Turso requires.
    """
    parts = urlsplit(url)
    qs = parse_qs(parts.query)
    connect_args: dict[str, object] = {}

    token = qs.pop("authToken", []) or qs.pop("auth_token", [])
    if token:
        connect_args["auth_token"] = token[0]

    new_query = "&".join(f"{k}={v[0]}" for k, v in qs.items() if v)
    cleaned = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return cleaned, connect_args


def _patch_libsql_dialect_for_turso() -> None:
    """Turso's Hrana protocol returns 405 on `PRAGMA read_uncommitted`, which SQLAlchemy's
    standard SQLite dialect runs during `initialize()` to detect the default isolation level.
    Override the inherited probe to return a constant so initialize() doesn't blow up."""
    from sqlalchemy_libsql.libsql import SQLiteDialect_libsql

    if getattr(SQLiteDialect_libsql, "_knock_isolation_patched", False):
        return

    def _get_isolation_level(self, dbapi_connection):  # type: ignore[no-untyped-def]
        return "SERIALIZABLE"

    def _set_isolation_level(self, dbapi_connection, level):  # type: ignore[no-untyped-def]
        return None

    SQLiteDialect_libsql.get_isolation_level = _get_isolation_level  # type: ignore[assignment]
    SQLiteDialect_libsql.set_isolation_level = _set_isolation_level  # type: ignore[assignment]
    SQLiteDialect_libsql._knock_isolation_patched = True  # type: ignore[attr-defined]


def _attach_connect_pragmas(eng: Engine, url: str) -> None:
    """Per-connection PRAGMAs. Local SQLite gets WAL + FK; remote libsql gets a
    best-effort foreign_keys=ON (Hrana may reject it — must not fail setup)."""
    if _is_local_sqlite(url):
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    elif _is_libsql(url):
        @event.listens_for(eng, "connect")
        def _libsql_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            try:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
            except Exception as exc:  # pragma: no cover — depends on Turso server
                log.warning("libsql.foreign_keys_pragma_failed", error=str(exc))


def _build_single_engine(url: str) -> Engine:
    """Build one engine for a URL: local SQLite file OR remote libsql (Turso).
    This is the non-replica engine, and also serves as the WRITE engine in
    read/write-split mode."""
    from sqlalchemy import create_engine

    connect_args: dict[str, object] = {}
    if _is_local_sqlite(url):
        connect_args["check_same_thread"] = False
    elif _is_libsql(url):
        _patch_libsql_dialect_for_turso()
        url, connect_args = _split_libsql_url(url)

    engine_kwargs: dict[str, object] = dict(
        echo=settings.DB_ECHO,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    if _is_libsql(settings.DATABASE_URL):
        # AUTOCOMMIT: libsql remote (Hrana) doesn't speak standard BEGIN/ROLLBACK.
        engine_kwargs["isolation_level"] = "AUTOCOMMIT"
        # QueuePool + pool_recycle below Turso's Hrana idle TTL: reuse warm
        # connections within a burst, discard+reopen stale ones on checkout.
        engine_kwargs["poolclass"] = QueuePool
        engine_kwargs["pool_recycle"] = LIBSQL_POOL_RECYCLE_SECONDS
        engine_kwargs["pool_size"] = 5
        engine_kwargs["max_overflow"] = 10
        engine_kwargs["pool_reset_on_return"] = None

    eng = create_engine(url, **engine_kwargs)
    _attach_connect_pragmas(eng, settings.DATABASE_URL)
    return eng


# ─────────────────────── read/write split (Turso embedded replica) ───────────────────────
#
# When LIBSQL_REPLICA_PATH is set we run a read/write split:
#   - READS  → a pool of PLAIN local-file connections (no sync) → fast + concurrent.
#   - WRITES → the remote Turso primary (source of truth).
#   - ONE syncer connection keeps the local file fresh: an initial sync at boot,
#     a background re-sync every LIBSQL_SYNC_INTERVAL, AND a sync right after each
#     write (read-your-writes). ALL syncs serialized through one lock on one
#     connection — the earlier crash (`wal_insert_begin failed`) came from
#     MULTIPLE connections syncing the same file; here there is exactly one.
# Off by default (empty path) → single remote engine, current behavior.

_READ_ENGINE: Engine | None = None
_WRITE_ENGINE: Engine | None = None
_syncer = None  # the single libsql syncer connection
_sync_lock = threading.Lock()


def _use_embedded_replica() -> bool:
    return _is_libsql(settings.DATABASE_URL) and bool(settings.LIBSQL_REPLICA_PATH)


def _build_local_read_engine() -> Engine:
    """Pool of plain local-file connections (NO sync_url) — concurrent fast reads
    of the replica file the syncer keeps fresh. Many readers are safe (WAL)."""
    import libsql_experimental as libsql
    from sqlalchemy import create_engine

    _patch_libsql_dialect_for_turso()
    replica_path = settings.LIBSQL_REPLICA_PATH

    def _connect():  # type: ignore[no-untyped-def]
        return libsql.connect(replica_path, check_same_thread=False)

    return create_engine(
        "sqlite+libsql://",  # dialect only; creator opens the local file
        creator=_connect,
        echo=settings.DB_ECHO,
        future=True,
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
        isolation_level="AUTOCOMMIT",
    )


def trigger_sync() -> None:
    """Pull remote → local. Serialized through _sync_lock on the single syncer
    connection (one writer to the local WAL → no contention). Best-effort: a
    sync hiccup must never break a request."""
    if _syncer is None:
        return
    with contextlib.suppress(Exception), _sync_lock:
        _syncer.sync()


def _start_syncer() -> None:
    """Open the single syncer, do a blocking initial sync (populate the file),
    then a daemon thread re-syncs every LIBSQL_SYNC_INTERVAL."""
    global _syncer
    import libsql_experimental as libsql

    parts = urlsplit(settings.DATABASE_URL)
    sync_url = f"https://{parts.netloc}"
    _, ca = _split_libsql_url(settings.DATABASE_URL)
    auth_token = ca.get("auth_token")
    interval = max(1, settings.LIBSQL_SYNC_INTERVAL)

    # No sync_interval kwarg → we drive ALL syncs manually under the lock (the
    # libsql background timer would be a second, unsynchronized syncer).
    _syncer = libsql.connect(
        settings.LIBSQL_REPLICA_PATH, sync_url=sync_url, auth_token=auth_token
    )
    with contextlib.suppress(Exception), _sync_lock:
        _syncer.sync()  # one-time blocking populate before serving

    def _loop() -> None:
        while True:
            time.sleep(interval)
            trigger_sync()

    threading.Thread(target=_loop, name="libsql-syncer", daemon=True).start()
    log.info("db.replica_syncer_started", interval_s=interval)


class RoutingSession(SaSession):
    """Routes reads to the local replica engine and writes to the remote engine.

    A flush, a Core INSERT/UPDATE/DELETE, OR any read AFTER this session has
    already written (read-your-writes within the txn) → write engine. Plain
    reads → local replica engine.
    """

    def get_bind(self, mapper=None, clause=None, **kw):  # type: ignore[override]
        if (
            self._flushing
            or isinstance(clause, (Update, Insert, Delete))
            or self.info.get("_wrote")
        ):
            return _WRITE_ENGINE
        return _READ_ENGINE


@event.listens_for(RoutingSession, "after_flush")
def _mark_wrote(session, _flush_context):  # type: ignore[no-untyped-def]
    # Once a session writes, route its later reads to the write engine so the
    # request sees its own uncommitted changes.
    session.info["_wrote"] = True


@event.listens_for(RoutingSession, "after_commit")
def _sync_after_write(session):  # type: ignore[no-untyped-def]
    # After a committed write, pull it into the local replica so the NEXT
    # request's local reads see it (read-your-writes across requests).
    if session.info.pop("_wrote", False):
        trigger_sync()


# ─────────────────────────── wiring ───────────────────────────

if _use_embedded_replica():
    _WRITE_ENGINE = _build_single_engine(settings.DATABASE_URL)  # remote primary
    _READ_ENGINE = _build_local_read_engine()  # local replica pool
    _start_syncer()
    engine: Engine = _WRITE_ENGINE  # migrations / metadata use the real primary
    SessionLocal = sessionmaker(
        class_=RoutingSession,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
    log.info("db.read_write_split_active", replica_path=settings.LIBSQL_REPLICA_PATH)
else:
    engine = _build_single_engine(settings.DATABASE_URL)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        future=True,
    )
