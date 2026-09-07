"""Async engine, session factory, and the FastAPI dependency.

SQLite is the default (zero-setup demo); the same code runs on Postgres by
changing DATABASE_URL — asyncpg is a drop-in for aiosqlite here.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import Base

log = get_logger(__name__)

_is_sqlite = settings.database_url.startswith("sqlite")
_is_memory = ":memory:" in settings.database_url

_engine_kwargs: dict = {"echo": settings.db_echo, "future": True}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    if _is_memory:
        # Tests share one in-memory DB across sessions.
        _engine_kwargs["poolclass"] = StaticPool
else:
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_pre_ping=True)

engine = create_async_engine(settings.database_url, **_engine_kwargs)

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


if _is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        """WAL + enforced foreign keys — SQLite defaults are unsafe for this workload."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped session. Commits are explicit in service code."""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables from metadata. Alembic takes over in a real deployment."""
    from app import models  # noqa: F401  (registers all mappers)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database ready", extra={"url": settings.database_url.split("://")[0]})


async def healthcheck() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.error("db healthcheck failed", extra={"error": str(exc)})
        return False
