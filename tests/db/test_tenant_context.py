"""Offline tests for tenant-context helpers (Milestone 8.2.4).

These tests verify that the Python tenant-context layer correctly issues
parameterized ``set_config``/``current_setting`` calls and validates UUID
values.

SQLite does not implement PostgreSQL's ``set_config``/``current_setting``
GUC functions, so the database call is mocked. The tests verify:
- The correct parameterized SQL is used (no string interpolation).
- UUID validation rejects non-UUID values.
- Missing context returns None (fail closed).
- Malformed context values raise ValueError (fail closed).

Live PostgreSQL context behavior (transaction-locality, pool isolation)
requires live PostgreSQL validation and is documented separately.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, Mock

import pytest

from app.db.tenant import (
    set_organization_context,
    clear_organization_context,
    get_organization_context,
    current_organization_id,
    GUC_KEY,
)

from sqlalchemy import text


def test_guc_key_is_constant() -> None:
    """The tenant-context GUC key matches the architecture specification."""
    assert GUC_KEY == "app.current_organization_id"


def test_set_organization_context_issues_parameterized_sql() -> None:
    """set_organization_context calls set_config with bind parameters."""
    org_id = uuid.uuid4()
    session = AsyncMock()

    import asyncio

    async def _run() -> None:
        await set_organization_context(session, org_id)

    asyncio.run(_run())

    session.execute.assert_awaited_once()
    call_args = session.execute.await_args
    args, _ = call_args
    # First positional arg is the TextClause; second is the params dict.
    assert len(args) == 2
    sql_clause = args[0]
    params = args[1]
    assert "set_config" in str(sql_clause)
    assert params == {
        "key": "app.current_organization_id",
        "value": str(org_id),
    }


def test_clear_organization_context_issues_parameterized_sql() -> None:
    """clear_organization_context calls set_config with NULL value."""
    session = AsyncMock()

    import asyncio

    async def _run() -> None:
        await clear_organization_context(session)

    asyncio.run(_run())

    session.execute.assert_awaited_once()
    call_args = session.execute.await_args
    args, _ = call_args
    assert len(args) == 2
    sql_clause = args[0]
    params = args[1]
    assert "set_config" in str(sql_clause)
    assert params == {"key": "app.current_organization_id"}


def test_set_organization_context_validates_uuid() -> None:
    """set_organization_context rejects non-UUID values."""
    session = AsyncMock()

    import asyncio

    async def _run() -> None:
        with pytest.raises(ValueError, match="organization_id must be a uuid.UUID"):
            await set_organization_context(session, "not-a-uuid")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_get_organization_context_missing_returns_none() -> None:
    """Reading context before it is set returns None (fail closed)."""
    session = AsyncMock()
    session.execute.return_value.scalar = Mock(return_value=None)

    import asyncio

    async def _run() -> None:
        result = await get_organization_context(session)
        assert result is None

    asyncio.run(_run())


def test_get_organization_context_returns_uuid() -> None:
    """Reading context returns the stored UUID."""
    org_id = uuid.uuid4()
    session = AsyncMock()
    session.execute.return_value.scalar = Mock(return_value=str(org_id))

    import asyncio

    async def _run() -> None:
        result = await get_organization_context(session)
        assert result == org_id

    asyncio.run(_run())


def test_current_organization_id_is_alias() -> None:
    """current_organization_id() delegates to get_organization_context()."""
    org_id = uuid.uuid4()
    session = AsyncMock()
    session.execute.return_value.scalar = Mock(return_value=str(org_id))

    import asyncio

    async def _run() -> None:
        result = await current_organization_id(session)
        assert result == org_id

    asyncio.run(_run())


def test_get_malformed_context_raises() -> None:
    """Reading a malformed context value raises ValueError (fail closed)."""
    session = AsyncMock()
    session.execute.return_value.scalar = Mock(return_value="not-a-uuid")

    import asyncio

    async def _run() -> None:
        with pytest.raises(ValueError, match="Malformed organization context"):
            await get_organization_context(session)

    asyncio.run(_run())
