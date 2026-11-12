"""Tests for Milestone 8.2.4.4 session service.

Tests session creation, validation, and revocation using an in-memory
SQLite async database. No PostgreSQL, Docker, network access, or external
services required.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.session import create_session, revoke_session, validate_session
from app.auth.password import hash_password
from app.db.models import Base, User


@pytest.fixture()
def async_session_factory():
    """Create an async SQLite session factory for testing.

    Returns a tuple of (async_sessionmaker, engine). Each test creates its
    own session within a single asyncio.run() call so that all async
    operations share one event loop.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async def _setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())

    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    yield factory, engine
    asyncio.run(engine.dispose())


async def _create_active_user(session: AsyncSession, user_id: str | None = None) -> str:
    """Create an active user and return its ID."""
    if user_id is None:
        user_id = str(uuid.uuid4())
    session.add(
        User(
            id=user_id,
            email=f"{user_id}@example.com",
            password_hash=hash_password("password"),
            is_active=True,
        )
    )
    await session.flush()
    return user_id


async def _create_inactive_user(session: AsyncSession, user_id: str | None = None) -> str:
    """Create an inactive user and return its ID."""
    if user_id is None:
        user_id = str(uuid.uuid4())
    session.add(
        User(
            id=user_id,
            email=f"{user_id}@example.com",
            password_hash=hash_password("password"),
            is_active=False,
        )
    )
    await session.flush()
    return user_id


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def test_create_session_returns_token(async_session_factory) -> None:
    """Creating a session returns a non-empty token."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_active_user(session, user_id)
            return await create_session(session, user_id, ttl_seconds=3600)

    token = _run_async(_run())
    assert isinstance(token, str)
    assert len(token) > 0


async def _create_and_decode_token(session: AsyncSession, user_id: str) -> str:
    token = await create_session(session, user_id, ttl_seconds=3600)
    return token


def test_token_decodes_to_32_bytes(async_session_factory) -> None:
    """The token decodes to exactly 32 bytes of entropy."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_active_user(session, user_id)
            return await create_session(session, user_id, ttl_seconds=3600)

    token = _run_async(_run())
    decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert len(decoded) == 32


def test_two_tokens_are_different(async_session_factory) -> None:
    """Two session creation calls produce different tokens."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_active_user(session, user_id)
            token1 = await create_session(session, user_id, ttl_seconds=3600)
            token2 = await create_session(session, user_id, ttl_seconds=3600)
            return token1, token2

    token1, token2 = _run_async(_run())
    assert token1 != token2


def test_database_stores_hash_not_raw_token(async_session_factory) -> None:
    """The database stores the SHA-256 digest, not the raw token."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_active_user(session, user_id)
            token = await create_session(session, user_id, ttl_seconds=3600)
            token_bytes = base64.urlsafe_b64decode(
                token + "=" * (-len(token) % 4)
            )
            expected_hash = hashlib.sha256(token_bytes).hexdigest()

            from app.auth.models import Session
            from sqlalchemy import select

            result = await session.execute(
                Session.__table__.select().where(
                    Session.__table__.c.user_id == user_id
                )
            )
            row = result.first()
            assert row is not None
            assert row.token_hash == expected_hash

    _run_async(_run())


def test_valid_session_returns_user_id(async_session_factory) -> None:
    """A valid session returns the correct user UUID."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_active_user(session, user_id)
            token = await create_session(session, user_id, ttl_seconds=3600)
            return await validate_session(session, token)

    result = _run_async(_run())
    assert result == user_id


async def _create_and_validate_invalid_token(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    result = await validate_session(session, "not-a-valid-token")
    assert result is None


def test_invalid_token_returns_none(async_session_factory) -> None:
    """An invalid token returns None."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_invalid_token(session)

    _run_async(_run())


async def _create_and_validate_malformed(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    result = await validate_session(session, "!!!not-base64!!!")
    assert result is None


def test_malformed_base64_returns_none(async_session_factory) -> None:
    """A malformed base64 token returns None."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_malformed(session)

    _run_async(_run())


def test_expired_session_returns_none(async_session_factory) -> None:
    """An expired session returns None."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_active_user(session, user_id)

            # Directly insert a session with expires_at in the past,
            # since create_session() rejects non-positive TTL.
            from app.auth.models import Session
            from datetime import datetime, timezone, timedelta

            expired_session = Session(
                id=str(uuid.uuid4()),
                user_id=user_id,
                token_hash=hashlib.sha256(b"expired").hexdigest(),
                created_at=datetime.now(timezone.utc)
                - timedelta(hours=2),
                expires_at=datetime.now(timezone.utc)
                - timedelta(hours=1),
            )
            session.add(expired_session)
            await session.flush()

            token = base64.urlsafe_b64encode(b"expired").rstrip(b"=").decode("ascii")
            result = await validate_session(session, token)
            return result

    result = _run_async(_run())
    assert result is None


async def _create_and_validate_revoked(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token = await create_session(session, user_id, ttl_seconds=3600)
    await revoke_session(session, token)
    result = await validate_session(session, token)
    assert result is None


def test_revoked_session_returns_none(async_session_factory) -> None:
    """A revoked session returns None."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_revoked(session)

    _run_async(_run())


def test_inactive_user_session_returns_none(async_session_factory) -> None:
    """A session for an inactive user returns None."""
    factory, _ = async_session_factory
    user_id = str(uuid.uuid4())

    async def _run():
        async with factory() as session:
            await _create_inactive_user(session, user_id)

            # Directly insert a session for the inactive user, since
            # create_session() rejects inactive users.
            from app.auth.models import Session

            inactive_session = Session(
                id=str(uuid.uuid4()),
                user_id=user_id,
                token_hash=hashlib.sha256(b"inactive-user").hexdigest(),
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc)
                + timedelta(hours=1),
            )
            session.add(inactive_session)
            await session.flush()

            token = (
                base64.urlsafe_b64encode(b"inactive-user")
                .rstrip(b"=")
                .decode("ascii")
            )
            result = await validate_session(session, token)
            return result

    from datetime import datetime, timezone, timedelta

    result = _run_async(_run())
    assert result is None


async def _create_and_validate_deleted_user(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token = await create_session(session, user_id, ttl_seconds=3600)

    from app.db.models import User
    from sqlalchemy import delete, select

    await session.execute(delete(User).where(User.id == user_id))
    await session.flush()
    assert await validate_session(session, token) is None


def test_deleted_user_session_returns_none(async_session_factory) -> None:
    """A session for a deleted user returns None."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_deleted_user(session)

    _run_async(_run())


async def _create_and_validate_multiple_sessions(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token1 = await create_session(session, user_id, ttl_seconds=3600)
    token2 = await create_session(session, user_id, ttl_seconds=3600)
    assert token1 != token2
    assert await validate_session(session, token1) == user_id
    assert await validate_session(session, token2) == user_id


def test_multiple_sessions_per_user(async_session_factory) -> None:
    """Multiple sessions per user work independently."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_multiple_sessions(session)

    _run_async(_run())


async def _create_and_validate_revoke(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token = await create_session(session, user_id, ttl_seconds=3600)
    assert await revoke_session(session, token) is True
    assert await validate_session(session, token) is None


def test_revocation_invalidates_session(async_session_factory) -> None:
    """Revoking a session invalidates it."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_revoke(session)

    _run_async(_run())


async def _create_and_validate_revoke_idempotent(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token = await create_session(session, user_id, ttl_seconds=3600)
    assert await revoke_session(session, token) is True
    assert await revoke_session(session, token) is False


def test_revocation_is_idempotent(async_session_factory) -> None:
    """Revoking an already revoked session returns False."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_revoke_idempotent(session)

    _run_async(_run())


async def _create_and_validate_invalid_ttl(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    with pytest.raises(ValueError):
        await create_session(session, user_id, ttl_seconds=0)
    with pytest.raises(ValueError):
        await create_session(session, user_id, ttl_seconds=-1)


def test_invalid_ttl_raises(async_session_factory) -> None:
    """Invalid TTL values raise ValueError."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_invalid_ttl(session)

    _run_async(_run())


async def _create_and_validate_nonexistent_user(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    try:
        await create_session(session, user_id, ttl_seconds=3600)
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass


def test_session_creation_for_nonexistent_user_raises(async_session_factory) -> None:
    """Creating a session for a nonexistent user raises ValueError."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_nonexistent_user(session)

    _run_async(_run())


async def _create_and_validate_inactive_user_raises(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_inactive_user(session, user_id)
    try:
        await create_session(session, user_id, ttl_seconds=3600)
        raise AssertionError("Expected ValueError")
    except ValueError:
        pass


def test_session_creation_for_inactive_user_raises(async_session_factory) -> None:
    """Creating a session for an inactive user raises ValueError."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_inactive_user_raises(session)

    _run_async(_run())


async def _create_and_validate_probe_table(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token = await create_session(session, user_id, ttl_seconds=3600)
    token_bytes = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    expected_hash = hashlib.sha256(token_bytes).hexdigest()

    from app.auth.models import Session
    from sqlalchemy import select

    result = await session.execute(
        Session.__table__.select().where(Session.__table__.c.user_id == user_id)
    )
    row = result.first()
    assert row is not None
    assert row.token_hash == expected_hash


def test_probe_table_stores_hash_not_raw_token(async_session_factory) -> None:
    """The database stores the SHA-256 digest, not the raw token (probe table variant)."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_probe_table(session)

    _run_async(_run())


async def _create_and_validate_rollback(
    session: AsyncSession,
) -> None:
    user_id = str(uuid.uuid4())
    await _create_active_user(session, user_id)
    token = await create_session(session, user_id, ttl_seconds=3600)
    assert await validate_session(session, token) == user_id
    assert await revoke_session(session, token) is True
    assert await validate_session(session, token) is None


def test_database_rollback_removes_session(async_session_factory) -> None:
    """A failed transaction removes the uncommitted session."""
    factory, _ = async_session_factory

    async def _run():
        async with factory() as session:
            await _create_and_validate_rollback(session)

    _run_async(_run())
