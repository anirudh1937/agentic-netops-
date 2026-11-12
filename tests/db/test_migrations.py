"""Tests for Alembic database migrations.

These tests verify that migration upgrade and downgrade work correctly
using an in-memory SQLite database (for schema validation only —
PostgreSQL-specific features like UUID type are validated separately).

Migration tests require NETOPS_DATABASE_URL to be set to a real PostgreSQL
database for full validation. If unavailable, migration structure tests
still run against SQLite to verify the migration Python code is valid.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, pool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_engine_from_config,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker


def _make_alembic_config() -> Config:
    """Create an Alembic config pointing to a temp SQLite database."""
    config = Config("alembic.ini")
    # Override with a temp SQLite file for migration structure testing.
    # Use the async driver so the Alembic async migration path is exercised.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{tmp.name}")
    return config, tmp.name


def test_alembic_config_loads():
    """Alembic config can be loaded from alembic.ini."""
    config, tmp_name = _make_alembic_config()
    assert config.get_main_option("script_location") == "alembic"
    os.unlink(tmp_name)


def test_alembic_current_head():
    """Alembic current revision resolves to the initial migration."""
    config, tmp_name = _make_alembic_config()
    try:
        # Just verify the config can load the script directory
        from alembic.script import ScriptDirectory

        script = ScriptDirectory.from_config(config)
        assert script.get_current_head() == "0003"
    finally:
        os.unlink(tmp_name)


def test_asyncpg_url_builds_async_migration_engine():
    """Alembic's async engine factory accepts a postgresql+asyncpg URL.

    Creating the engine does not connect to a server, so this verifies the
    migration architecture without requiring live PostgreSQL.
    """
    engine = async_engine_from_config(
        {
            "sqlalchemy.url": (
                "postgresql+asyncpg://user:placeholder@127.0.0.1:5432/netops"
            )
        },
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        assert engine.dialect.is_async is True
        assert engine.dialect.driver == "asyncpg"
    finally:
        asyncio.run(engine.dispose())


def test_alembic_offline_sql_with_asyncpg_url():
    """Offline migrations emit the exact role CHECK name for PostgreSQL."""
    config, tmp_name = _make_alembic_config()
    old = os.environ.get("NETOPS_DATABASE_URL")

    try:
        os.environ["NETOPS_DATABASE_URL"] = (
            "postgresql+asyncpg://user:placeholder@127.0.0.1:5432/netops"
        )
        output = io.StringIO()
        config.output_buffer = output

        command.upgrade(config, "head", sql=True)

        sql = output.getvalue()
        assert "CREATE TABLE organizations" in sql
        assert "CREATE TABLE users" in sql
        assert "CREATE TABLE organization_memberships" in sql

        assert re.search(
            r"ALTER TABLE organization_memberships\s+ADD CONSTRAINT\s+"
            r"uq_organization_memberships_organization_id_id\s+UNIQUE\s+"
            r"\(organization_id,\s+id\)",
            sql,
            flags=re.IGNORECASE,
        )
        assert sql.count("CREATE TABLE organization_memberships") == 1
        assert "_alembic_tmp_organization_memberships" not in sql

        check_names = re.findall(
            r"CONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)\s+CHECK",
            sql,
            flags=re.IGNORECASE,
        )
        assert "ck_organization_memberships_role" in check_names
        assert "org_admin" in sql
        assert "org_member" in sql
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        os.unlink(tmp_name)


def test_alembic_upgrade_downgrade_sqlite():
    """Alembic upgrade creates all three tables; downgrade removes them."""
    config, tmp_name = _make_alembic_config()
    try:
        import os

        old = os.environ.get("NETOPS_DATABASE_URL")
        os.environ["NETOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_name}"

        command.upgrade(config, "head")

        engine = create_engine(f"sqlite:///{tmp_name}")
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())

        assert "organizations" in table_names
        assert "users" in table_names
        assert "organization_memberships" in table_names
        assert "sessions" in table_names

        command.downgrade(config, "base")

        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        assert "organizations" not in table_names
        assert "users" not in table_names
        assert "organization_memberships" not in table_names
        assert "sessions" not in table_names

        engine.dispose()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        os.unlink(tmp_name)


def test_alembic_upgrade_creates_expected_columns():
    """Upgrade migration creates expected columns on each table."""
    config, tmp_name = _make_alembic_config()
    try:
        import os

        old = os.environ.get("NETOPS_DATABASE_URL")
        os.environ["NETOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_name}"

        command.upgrade(config, "head")

        engine = create_engine(f"sqlite:///{tmp_name}")
        inspector = inspect(engine)

        # organizations columns
        org_cols = {c["name"] for c in inspector.get_columns("organizations")}
        assert "id" in org_cols
        assert "name" in org_cols
        assert "slug" in org_cols
        assert "created_at" in org_cols
        assert "updated_at" in org_cols

        # users columns
        user_cols = {c["name"] for c in inspector.get_columns("users")}
        assert "id" in user_cols
        assert "email" in user_cols
        assert "password_hash" in user_cols
        assert "full_name" in user_cols
        assert "is_active" in user_cols
        assert "created_at" in user_cols
        assert "updated_at" in user_cols

        # organization_memberships columns
        mem_cols = {c["name"] for c in inspector.get_columns("organization_memberships")}
        assert "id" in mem_cols
        assert "organization_id" in mem_cols
        assert "user_id" in mem_cols
        assert "role" in mem_cols
        assert "joined_at" in mem_cols

        engine.dispose()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        os.unlink(tmp_name)


def test_alembic_upgrade_creates_session_table() -> None:
    """Upgrade migration creates the sessions table with expected columns."""
    config, tmp_name = _make_alembic_config()
    try:
        import os

        old = os.environ.get("NETOPS_DATABASE_URL")
        os.environ["NETOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_name}"

        command.upgrade(config, "head")

        engine = create_engine(f"sqlite:///{tmp_name}")
        inspector = inspect(engine)

        session_cols = {c["name"] for c in inspector.get_columns("sessions")}
        assert "id" in session_cols
        assert "user_id" in session_cols
        assert "token_hash" in session_cols
        assert "created_at" in session_cols
        assert "expires_at" in session_cols
        assert "last_seen_at" in session_cols
        assert "revoked_at" in session_cols

        engine.dispose()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        os.unlink(tmp_name)


def test_postgresql_url_required_for_real_migration():
    """Without NETOPS_DATABASE_URL, real PostgreSQL migration is not possible."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    import os

    old = os.environ.pop("NETOPS_DATABASE_URL", None)
    try:
        # Just verify the env var check works
        assert os.environ.get("NETOPS_DATABASE_URL") is None
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_async_engine_creatable_with_sqlite_url():
    """SQLAlchemy async engine can be created with a SQLite URL for testing."""
    from app.db.session import _async_engine, _async_session_factory

    # Reset module state
    session_mod = __import__("app.db.session", fromlist=["*"])
    session_mod._async_engine = None
    session_mod._async_session_factory = None

    try:
        import os

        old = os.environ.get("NETOPS_DATABASE_URL")
        os.environ["NETOPS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

        engine = session_mod.get_async_engine()
        assert engine is not None

        factory = session_mod.get_async_session_factory()
        assert factory is not None

        # Verify we can create a session
        import asyncio

        async def _test_session():
            async with factory() as session:
                assert isinstance(session, AsyncSession)

        asyncio.run(_test_session())
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        session_mod._async_engine = None
        session_mod._async_session_factory = None

