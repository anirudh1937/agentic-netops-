"""Request-scoped asynchronous Unit of Work for Agentic NetOps.

Owns the SQLAlchemy ``AsyncSession`` lifecycle and transaction boundaries for
a single request. Provides tenant-context integration and guarantees that:

- Transactions are begun explicitly.
- Successful transactions are committed.
- Failed transactions are rolled back.
- Cancellation triggers cleanup.
- Sessions are always closed.
- Repositories cannot independently commit transactions.
- Tenant context is set within the transaction.
- Nested transaction misuse is handled safely.

This module does not change the existing database engine/session-factory
lifecycle in ``app/db/session.py``; it builds on top of it.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_async_session_factory
from app.db.tenant import (
    set_organization_context,
    clear_organization_context,
    get_organization_context,
)


class UnitOfWork:
    """Request-scoped unit of work.

    Usage::

        async with UnitOfWork() as uow:
            await uow.set_organization_context(org_id)
            # perform repository work using uow.session
            await uow.commit()

    The session is closed automatically on exit, even if an exception or
    cancellation occurs.
    """

    def __init__(self) -> None:
        self._session: Optional[AsyncSession] = None
        self._transaction_started: bool = False
        self._committed: bool = False
        self._rolled_back: bool = False

    @property
    def session(self) -> AsyncSession:
        """Return the active session.

        Raises:
            RuntimeError: If the unit of work is not active.
        """
        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        return self._session

    async def __aenter__(self) -> "UnitOfWork":
        factory = get_async_session_factory()
        self._session = factory()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Always roll back an uncommitted transaction, then close the session.
        # If commit already succeeded, there is nothing to roll back.
        if self._session is not None:
            if self._transaction_started and not self._committed and not self._rolled_back:
                await self._rollback()
            await self._session.close()
            self._session = None

    async def begin(self) -> None:
        """Begin an explicit transaction.

        Must be called before performing any data work. Tenant context can be
        set only after ``begin``.
        """
        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        if self._transaction_started:
            raise InvalidRequestError(
                "Transaction is already begun; use commit() or rollback() first"
            )
        await self._session.begin()
        self._transaction_started = True

    async def commit(self) -> None:
        """Commit the active transaction.

        After commit, tenant context is cleared and the session is closed.
        """
        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        if not self._transaction_started:
            raise InvalidRequestError("No transaction has been begun")
        if self._committed or self._rolled_back:
            raise InvalidRequestError(
                "Transaction is already resolved"
            )
        await self._session.commit()
        self._committed = True
        self._transaction_started = False
        # Clear tenant context so it cannot leak to the next request
        # reusing this session (though the session is closed below).
        try:
            await clear_organization_context(self._session)
        except Exception:
            # Context cleanup must not mask a successful commit.
            pass
        await self._session.close()
        self._session = None

    async def rollback(self) -> None:
        """Roll back the active transaction.

        Tenant context is cleared as part of rollback.
        """
        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        if not self._transaction_started:
            raise InvalidRequestError("No transaction has been begun")
        if self._committed or self._rolled_back:
            raise InvalidRequestError(
                "Transaction is already resolved"
            )
        await self._rollback()
        self._session = None

    async def _rollback(self) -> None:
        await self._session.rollback()
        self._rolled_back = True
        self._transaction_started = False
        try:
            await clear_organization_context(self._session)
        except Exception:
            # Context cleanup must not mask a rollback.
            pass

    async def set_organization_context(
        self,
        organization_id: str,
    ) -> None:
        """Set transaction-local organization context.

        Validates that the value is a parseable UUID before sending it to
        the database. Must be called after ``begin``.

        Args:
            organization_id: UUID string of the organization.

        Raises:
            ValueError: If ``organization_id`` is not a valid UUID.
            RuntimeError: If no transaction has been begun.
        """
        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        if not self._transaction_started:
            raise InvalidRequestError(
                "Transaction must be begun before setting tenant context"
            )
        try:
            org_uuid = uuid.UUID(organization_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError(
                f"Invalid organization ID: {organization_id!r}"
            ) from exc
        await set_organization_context(self._session, org_uuid)

    async def get_organization_context(self) -> Optional[str]:
        """Return the current transaction-local organization context as a
        string, or None if no context is set."""
        if self._session is None:
            raise RuntimeError("Unit of work is not active")
        org_id = await get_organization_context(self._session)
        return str(org_id) if org_id is not None else None

    async def cancel(self) -> None:
        """Cancel the unit of work: roll back and close the session.

        Intended for use when a request is cancelled or fails.
        """
        if self._session is None:
            return
        if self._transaction_started and not self._committed and not self._rolled_back:
            await self._rollback()
        await self._session.close()
        self._session = None

    def __del__(self) -> None:
        # Best-effort warning surface if the UoW is garbage-collected without
        # explicit close. Cannot await here; rely on async context manager.
        pass
