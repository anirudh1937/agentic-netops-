"""Tests for database connection lifecycle and pool management.

Tests the async engine creation, session factory, and connection cleanup
without requiring a live PostgreSQL database (uses SQLite+aiosqlite for
validation where a real database URL is not available).
"""

import asyncio
import os
import sys

import pytest
from sqlalchemy import text


def test_engine_creation_without_url_raises():
    """get_async_engine() raises RuntimeError when NETOPS_DATABASE_URL is unset."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.pop("NETOPS_DATABASE_URL", None)
    try:
        with pytest.raises(RuntimeError, match="NETOPS_DATABASE_URL"):
            session_mod.get_async_engine()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_session_factory_without_url_raises():
    """get_async_session_factory() raises RuntimeError when URL is unset."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.pop("NETOPS_DATABASE_URL", None)
    try:
        with pytest.raises(RuntimeError, match="NETOPS_DATABASE_URL"):
            session_mod.get_async_session_factory()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_init_db_creates_engine_and_factory():
    """init_db() creates engine and session factory from env URL."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.get("NETOPS_DATABASE_URL")
    try:
        os.environ["NETOPS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

        import asyncio

        async def _run():
            await session_mod.init_db()
            assert session_mod._async_engine is not None
            assert session_mod._async_session_factory is not None

        asyncio.run(_run())
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_dispose_db_closes_engine():
    """dispose_db() closes the engine and resets module state."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.get("NETOPS_DATABASE_URL")
    try:
        os.environ["NETOPS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

        import asyncio

        async def _run():
            await session_mod.init_db()
            assert session_mod._async_engine is not None
            await session_mod.dispose_db()
            assert session_mod._async_engine is None
            assert session_mod._async_session_factory is None

        asyncio.run(_run())
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_get_async_session_yields_session():
    """get_async_session() yields a usable async session."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.get("NETOPS_DATABASE_URL")
    try:
        os.environ["NETOPS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

        import asyncio

        async def _run():
            await session_mod.init_db()
            async for session in session_mod.get_async_session():
                assert session is not None
                # Verify we can run a simple query
                result = await session.execute(text("SELECT 1"))
                assert result is not None
                break  # Only need one iteration

        asyncio.run(_run())
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_create_db_and_tables_creates_tables():
    """create_db_and_tables() creates all tables from SQLAlchemy metadata."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.get("NETOPS_DATABASE_URL")
    try:
        os.environ["NETOPS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

        import asyncio

        async def _run():
            await session_mod.init_db()
            await session_mod.create_db_and_tables()

            # Verify tables exist by querying
            async for session in session_mod.get_async_session():
                result = await session.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                tables = {row[0] for row in result.fetchall()}
                assert "organizations" in tables
                assert "users" in tables
                assert "organization_memberships" in tables
                break

        asyncio.run(_run())
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_pool_cleanup_on_dispose():
    """dispose_db() closes all pooled connections."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.get("NETOPS_DATABASE_URL")
    try:
        os.environ["NETOPS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

        import asyncio

        async def _run():
            await session_mod.init_db()
            engine = session_mod.get_async_engine()
            assert engine is not None

            # Create and close a session to use the pool
            async for session in session_mod.get_async_session():
                await session.execute(text("SELECT 1"))
                break

            await session_mod.dispose_db()
            assert session_mod._async_engine is None

        asyncio.run(_run())
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_app_imports_without_database():
    """Importing app.main works without DATABASE_URL configured."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    old = os.environ.pop("NETOPS_DATABASE_URL", None)
    try:
        # Import the app module - it should not require DATABASE_URL
        import app.main  # noqa: F401

        assert app.main.app is not None
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        session_mod._async_engine = None
        session_mod._async_session_factory = None

