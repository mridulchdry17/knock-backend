from __future__ import annotations

import contextlib
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import settings
from app.logging_config import get_logger

log = get_logger("db")

# Recycle pooled libsql connections this many seconds after creation. Must stay
# safely BELOW Turso's Hrana idle-stream TTL (~10s) so we never check out a
# connection whose server-side stream has already expired. Tunable if Turso
# changes the TTL or we observe stale-stream errors creeping back.
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


def _use_embedded_replica() -> bool:
    """Embedded-replica mode is on when DATABASE_URL is libsql AND a writable
    LIBSQL_REPLICA_PATH is configured. Off by default → talk to remote directly."""
    return _is_libsql(settings.DATABASE_URL) and bool(settings.LIBSQL_REPLICA_PATH)


def _build_embedded_replica_engine() -> Engine:
    """Engine backed by a local SQLite replica that syncs from the Turso primary.

    Reads hit the local file (microseconds); writes are forwarded to the remote
    primary by the libsql driver; the replica pulls changes every
    LIBSQL_SYNC_INTERVAL seconds. We override connection *creation* with a
    `creator` (the dialect's URL parser doesn't know about replica mode) but
    keep the libsql dialect for SQL compilation.

    NOTE: each pooled connection is its own replica handle against the same
    local file, each running its own background sync. Validated single-process;
    load-test concurrency before relying on it under heavy parallel traffic.
    """
    import libsql_experimental as libsql
    from sqlalchemy import create_engine

    _patch_libsql_dialect_for_turso()

    parts = urlsplit(settings.DATABASE_URL)
    sync_url = f"https://{parts.netloc}"
    _, ca = _split_libsql_url(settings.DATABASE_URL)
    auth_token = ca.get("auth_token")
    replica_path = settings.LIBSQL_REPLICA_PATH
    sync_interval = settings.LIBSQL_SYNC_INTERVAL

    def _creator():  # type: ignore[no-untyped-def]
        conn = libsql.connect(
            replica_path,
            sync_url=sync_url,
            auth_token=auth_token,
            sync_interval=sync_interval,
            check_same_thread=False,
        )
        # Pull the latest frames once on connect so a fresh connection never
        # serves a stale/empty local file before the first background sync.
        with contextlib.suppress(Exception):  # sync hiccup must not block startup
            conn.sync()
        return conn

    return create_engine(
        "sqlite+libsql://",  # dialect only; `creator` builds the real connection
        creator=_creator,
        echo=settings.DB_ECHO,
        future=True,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        isolation_level="AUTOCOMMIT",
    )


def _build_engine() -> Engine:
    from sqlalchemy import create_engine

    if _use_embedded_replica():
        return _build_embedded_replica_engine()

    url = settings.DATABASE_URL
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
        # libsql remote (Hrana over HTTPS) doesn't speak the standard SQLite BEGIN/ROLLBACK
        # transaction protocol. AUTOCOMMIT lets SQLAlchemy treat each statement as committed
        # at the connection level; the ORM Session still groups writes via flush+commit, and
        # the underlying connection autocommits them on dispatch.
        engine_kwargs["isolation_level"] = "AUTOCOMMIT"

        # Hrana streams are stateful and the server TTL-expires them after a short
        # idle window (~10s). We previously used NullPool to dodge stale-stream
        # 404s ("stream not found: <id>") by opening a fresh connection per
        # request — correct, but it pays a full TLS + Hrana handshake to the
        # remote DB on EVERY request (~hundreds of ms each), which is the bulk
        # of the per-request latency at v0.
        #
        # Switch to the default QueuePool with pool_recycle BELOW the Hrana idle
        # TTL: a connection younger than LIBSQL_POOL_RECYCLE_SECONDS is reused
        # (no handshake), anything older is transparently discarded + reopened on
        # checkout — so we never hand a stale stream to a query. This makes
        # bursty traffic (a page firing several queries, or back-to-back
        # navigations) reuse one warm connection instead of N cold ones.
        # pool_pre_ping stays on as a belt-and-suspenders for the rare race where
        # the server kills a stream inside the recycle window.
        # The libsql dialect defaults to SingletonThreadPool (one shared
        # connection, no overflow knobs); force QueuePool so pool_size /
        # max_overflow / pool_recycle apply.
        engine_kwargs["poolclass"] = QueuePool
        engine_kwargs["pool_recycle"] = LIBSQL_POOL_RECYCLE_SECONDS
        engine_kwargs["pool_size"] = 5
        engine_kwargs["max_overflow"] = 10

        # Suppress the connection-return ROLLBACK. With AUTOCOMMIT isolation
        # there is nothing to roll back; the dialect issues a `rollback()` on
        # session close anyway, which then trips a fresh Hrana stream lookup on
        # an already-dead stream and noisily 404s in the logs (see "Exception
        # during reset or similar").
        engine_kwargs["pool_reset_on_return"] = None

    eng = create_engine(url, **engine_kwargs)

    if _is_local_sqlite(settings.DATABASE_URL):
        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    elif _is_libsql(settings.DATABASE_URL):
        @event.listens_for(eng, "connect")
        def _libsql_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            # SQLite/libSQL default foreign_keys=OFF, so every ondelete=CASCADE /
            # SET NULL in the models is silently a no-op on Turso unless we enable
            # it per connection. Hrana rejects some PRAGMAs with HTTP 405 (see
            # _patch_libsql_dialect_for_turso for read_uncommitted) — foreign_keys
            # may be one of them — so attempt it defensively. A rejection must NOT
            # fail connection setup; worst case FKs stay off, exactly as before.
            try:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
            except Exception as exc:  # pragma: no cover — depends on Turso server
                log.warning("libsql.foreign_keys_pragma_failed", error=str(exc))

    return eng


engine: Engine = _build_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)
