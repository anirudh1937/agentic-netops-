"""Alembic environment configuration for Agentic NetOps.

Reads the database URL from the ``NETOPS_DATABASE_URL`` environment variable
and configures SQLAlchemy to connect to the target database for migrations.
"""

from __future__ import annotations

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import the SQLAlchemy Base and models so Alembic can detect table
# definitions for autogeneration.
from app.db.models import Base  # noqa: F401
from app.auth.models import Session  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# disable_existing_loggers=False preserves existing application loggers
# (e.g., app.api.agent) so that pytest caplog capture works across test modules.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

logger = logging.getLogger("alembic.env")

# Add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def get_database_url() -> str:
    """Return the database URL from the environment variable."""
    import os

    url = os.environ.get("NETOPS_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "NETOPS_DATABASE_URL environment variable is not set. "
            "Set it to a PostgreSQL connection string (e.g. "
            "postgresql+asyncpg://user:password@host:5432/dbname) "
            "to run Alembic migrations."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations using a synchronous connection callback.

    Alembic's migration context is synchronous. When using an async engine,
    ``connection.run_sync()`` invokes this callback on the greenlet-backed
    synchronous connection.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations online using the application's async SQLAlchemy URL.

    ``NETOPS_DATABASE_URL`` is the same environment variable used by the
    application and may use an async driver such as ``postgresql+asyncpg``.
    """
    configuration = config.get_section(config.config_ini_section)
    if configuration is None:
        raise RuntimeError(
            f"Alembic configuration section "
            f"{config.config_ini_section!r} was not found."
        )

    configuration["sqlalchemy.url"] = get_database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
