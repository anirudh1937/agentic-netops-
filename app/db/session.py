"""Async database session management for Agentic NetOps.

Provides SQLAlchemy async engine, session factory, and lifecycle helpers.

Database configuration is environment-driven via ``NETOPS_DATABASE_URL``.
If the variable is not set, the engine is created lazily and the application
can start without a database connection. Database functionality raises a
clear error when accessed without configuration.
"""

from __future__ import annotations

import logging
import os
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.models import Base

logger = logging.getLogger(__name__)

# Module-level engine and session factory, created lazily.
_async_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _get_database_url() -> Optional[str]:
    """Return the database URL from the environment, or None if unset."""
    return os.environ.get("NETOPS_DATABASE_URL")


def get_async_engine() -> AsyncEngine:
    """Return the async SQLAlchemy engine, creating it on first use.

    Raises:
        RuntimeError: If ``NETOPS_DATABASE_URL`` is not configured.
    """
    global _async_engine
    if _async_engine is None:
        database_url = _get_database_url()
        if not database_url:
            raise RuntimeError(
                "NETOPS_DATABASE_URL is not configured. "
                "Set it to a PostgreSQL connection string (e.g. "
                "postgresql+asyncpg://user:password@host:5432/dbname) "
                "to use database-backed features."
            )
        _connect_args: dict = {}
        _engine_kwargs: dict = {
            "echo": False,
            "pool_size": 5,
            "max_overflow": 10,
            "pool_timeout": 30,
            "pool_recycle": 1800,
            "pool_pre_ping": True,
        }
        if database_url.startswith("sqlite+"):
            # SQLite does not support pool_size/max_overflow/etc.
            _engine_kwargs.pop("pool_size", None)
            _engine_kwargs.pop("max_overflow", None)
            _engine_kwargs.pop("pool_timeout", None)
            _engine_kwargs.pop("pool_recycle", None)
            _engine_kwargs.pop("pool_pre_ping", None)
            _connect_args = {"check_same_thread": False}
        _async_engine = create_async_engine(
            database_url,
            connect_args=_connect_args,
            **_engine_kwargs,
        )
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory, creating it on first use."""
    global _async_session_factory
    if _async_session_factory is None:
        engine = get_async_engine()
        _async_session_factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _async_session_factory


async def init_db() -> None:
    """Initialize the database engine and session factory.

    Does not create tables; use ``create_db_and_tables`` for that.
    Safe to call multiple times.
    """
    get_async_engine()
    get_async_session_factory()


async def create_db_and_tables() -> None:
    """Create all database tables defined in the SQLAlchemy metadata.

    Intended for development and testing. Production should use Alembic
    migrations for schema management.
    """
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_db() -> None:
    """Dispose of the async engine and close all pooled connections.

    Safe to call multiple times. After calling, the engine and session
    factory are reset so a new engine can be created on next use.
    """
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async SQLAlchemy session, closing it when done.

    Intended for use as a FastAPI dependency:
    ``async def endpoint(session: AsyncSession = Depends(get_async_session))``.
    """
    factory = get_async_session_factory()
    async with factory() as session:
        yield session


__all__ = [
    "AsyncEngine",
    "AsyncSession",
    "async_sessionmaker",
    "create_async_engine",
    "create_db_and_tables",
    "dispose_db",
    "get_async_engine",
    "get_async_session",
    "get_async_session_factory",
    "init_db",
]
