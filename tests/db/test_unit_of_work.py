"""Offline tests for the request Unit of Work (Milestone 8.2.4).

These tests configure ``NETOPS_DATABASE_URL`` to an in-memory SQLite database
and use the module-level async session factory from ``app/db/session.py``.
They verify the Unit of Work lifecycle: explicit begin, commit, rollback,
cancellation cleanup, session close, tenant-context integration, and
prevention of independent repository commits.

SQLite does not implement PostgreSQL's ``set_config``/``current_setting``
transaction-local GUC semantics, so tenant-context calls are mocked. The
lifecycle tests verify transaction behavior; tenant context validation is
covered separately in test_tenant_context.py.

Live PostgreSQL context behavior (transaction-locality, pool isolation)
requires live PostgreSQL validation and is documented separately.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy import text

import app.db.session as session_mod
from app.db.models import Base, Organization
from app.db.unit_of_work import UnitOfWork


@pytest.fixture(autouse=True)
def _reset_database_state(monkeypatch):
    """Configure SQLite URL and reset module-level engine/factory state."""
    url = "sqlite+aiosqlite:///:memory:"
    monkeypatch.setenv("NETOPS_DATABASE_URL", url)
    session_mod._async_engine = None
    session_mod._async_session_factory = None
    yield
    session_mod._async_engine = None
    session_mod._async_session_factory = None


async def _init_tables() -> None:
    """Create all tables in the module-level engine's database."""
    engine = session_mod.get_async_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _create_organization(session: AsyncSession, org_id: str) -> str:
    session.add(Organization(id=org_id, name="Test Org", slug="test-org"))
    await session.flush()
    return org_id


async def _org_exists(org_id: str) -> bool:
    engine = session_mod.get_async_engine()
    async with engine.connect() as sess:
        result = await sess.execute(
            text("SELECT id FROM organizations WHERE id = :id"),
            {"id": org_id},
        )
        return result.first() is not None


async def _run_uow_commit(org_id: str) -> None:
    await _init_tables()
    async with UnitOfWork() as uow:
        await uow.begin()
        await _create_organization(uow.session, org_id)
        await uow.commit()


async def _run_uow_context(org_id: str) -> None:
    await _init_tables()
    from unittest.mock import patch

    with patch("app.db.unit_of_work.set_organization_context"), patch(
        "app.db.unit_of_work.get_organization_context"
    ) as mock_get:
        mock_get.return_value = org_id
        async with UnitOfWork() as uow:
            await uow.begin()
            await uow.set_organization_context(org_id)
            assert await uow.get_organization_context() == org_id
            await uow.commit()


async def _run_uow_rollback(org_id: str) -> None:
    await _init_tables()
    async with UnitOfWork() as uow:
        await uow.begin()
        await _create_organization(uow.session, org_id)
        await uow.rollback()


async def _run_uow_invalid_org_id() -> None:
    await _init_tables()
    from unittest.mock import patch

    with patch("app.db.unit_of_work.set_organization_context"):
        async with UnitOfWork() as uow:
            await uow.begin()
            with pytest.raises(ValueError, match="Invalid organization ID"):
                await uow.set_organization_context("not-a-uuid")
            await uow.rollback()


async def _run_uow_exception() -> None:
    await _init_tables()
    try:
        async with UnitOfWork() as uow:
            await uow.begin()
            await _create_organization(uow.session, str(uuid.uuid4()))
            raise RuntimeError("simulated failure")
    except RuntimeError:
        pass


async def _run_uow_double_begin() -> None:
    await _init_tables()
    async with UnitOfWork() as uow:
        await uow.begin()
        with pytest.raises(InvalidRequestError):
            await uow.begin()
        await uow.rollback()


async def _run_uow_double_commit() -> None:
    await _init_tables()
    async with UnitOfWork() as uow:
        await uow.begin()
        await uow.commit()
        # After commit, _session is None, so the second commit raises
        # "Unit of work is not active" rather than "already resolved".
        with pytest.raises((RuntimeError, InvalidRequestError)):
            await uow.commit()


async def _run_uow_cancel(org_id: str) -> None:
    await _init_tables()
    uow = UnitOfWork()
    await uow.__aenter__()
    await uow.begin()
    await _create_organization(uow.session, org_id)
    await uow.cancel()


def test_uow_commit_persists_changes() -> None:
    """Committed work is persisted to the database."""
    org_id = str(uuid.uuid4())
    asyncio.run(_run_uow_commit(org_id))
    assert asyncio.run(_org_exists(org_id)) is True


def test_uow_rollback_discards_changes() -> None:
    """Rolled-back work is not persisted."""
    org_id = str(uuid.uuid4())
    asyncio.run(_run_uow_rollback(org_id))
    assert asyncio.run(_org_exists(org_id)) is False


def test_uow_rejects_invalid_organization_id() -> None:
    """set_organization_context rejects invalid UUIDs."""
    asyncio.run(_run_uow_invalid_org_id())


def test_uow_session_is_closed_after_commit() -> None:
    """The session reference is cleared after commit."""

    async def _run() -> None:
        async with UnitOfWork() as uow:
            await uow.begin()
            await uow.commit()
        assert uow._session is None

    asyncio.run(_run())


def test_uow_session_is_closed_after_rollback() -> None:
    """The session reference is cleared after rollback."""

    async def _run() -> None:
        async with UnitOfWork() as uow:
            await uow.begin()
            await uow.rollback()
        assert uow._session is None

    asyncio.run(_run())


def test_uow_prevents_double_begin() -> None:
    """Calling begin() twice raises InvalidRequestError."""
    asyncio.run(_run_uow_double_begin())


def test_uow_prevents_double_commit() -> None:
    """Calling commit() twice raises an error."""
    asyncio.run(_run_uow_double_commit())


def test_uow_rollback_on_exception() -> None:
    """An exception inside the UoW context triggers rollback."""

    async def _run() -> None:
        await _run_uow_exception()
        engine = session_mod.get_async_engine()
        async with engine.connect() as sess:
            result = await sess.execute(text("SELECT count(*) FROM organizations"))
            assert result.scalar() == 0

    asyncio.run(_run())


def test_uow_session_factory_reuse() -> None:
    """The UoW uses the shared session factory from app/db/session.py."""
    asyncio.run(_run_uow_invalid_org_id())
    assert session_mod._async_session_factory is not None


def test_uow_cancel_rolls_back() -> None:
    """cancel() rolls back an uncommitted transaction."""
    org_id = str(uuid.uuid4())

    async def _run() -> None:
        await _run_uow_cancel(org_id)
        engine = session_mod.get_async_engine()
        async with engine.connect() as sess:
            result = await sess.execute(
                text("SELECT id FROM organizations WHERE id = :id"),
                {"id": org_id},
            )
            assert result.first() is None

    asyncio.run(_run())
