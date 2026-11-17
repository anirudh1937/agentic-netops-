"""Tenant-context helpers for Agentic NetOps.

Provides transaction-scoped organization context using PostgreSQL
``set_config(..., true)`` and ``current_setting(..., true)``.

Security properties:
- Tenant context is transaction-local: ``set_config(..., true)`` resets
  automatically at COMMIT or ROLLBACK, so pooled connections cannot leak
  context between requests.
- Context is never derived from an untrusted client value without
  authorization; callers must validate membership before setting context.
- Missing context fails closed: ``current_setting(..., true)`` returns NULL,
  which must not match any tenant row.
- Only parameterized SQL is used; tenant values are never interpolated into
  SQL text.
- Session-level ``SET`` is never used.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

GUC_KEY = "app.current_organization_id"


async def set_organization_context(
    session: AsyncSession,
    organization_id: uuid.UUID,
) -> None:
    """Set transaction-local organization context.

    Args:
        session: Async SQLAlchemy session within an active transaction.
        organization_id: UUID of the organization to set as context.

    Raises:
        ValueError: If ``organization_id`` is not a valid UUID.
    """
    if not isinstance(organization_id, uuid.UUID):
        raise ValueError("organization_id must be a uuid.UUID")

    await session.execute(
        text(
            "SELECT set_config(:key, :value, true)"
        ),
        {"key": GUC_KEY, "value": str(organization_id)},
    )


async def clear_organization_context(
    session: AsyncSession,
) -> None:
    """Clear transaction-local organization context.

    Sets the GUC to NULL so subsequent reads fail closed.
    """
    await session.execute(
        text(
            "SELECT set_config(:key, NULL, true)"
        ),
        {"key": GUC_KEY},
    )


async def get_organization_context(
    session: AsyncSession,
) -> Optional[uuid.UUID]:
    """Read transaction-local organization context.

    Returns:
        The organization UUID if context is set, otherwise None.

    Raises:
        ValueError: If the stored value is not a valid UUID (malformed
            context fails closed).
    """
    result = await session.execute(
        text(
            "SELECT current_setting(:key, true)"
        ),
        {"key": GUC_KEY},
    )
    value = result.scalar()
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        # Malformed context fails closed: reject rather than fallback.
        raise ValueError(
            f"Malformed organization context value: {value!r}"
        )


async def current_organization_id(
    session: AsyncSession,
) -> Optional[uuid.UUID]:
    """Return the current transaction-local organization context.

    Alias for :func:`get_organization_context`, matching the canonical
    ``app.current_organization_id()`` helper described in the architecture.
    """
    return await get_organization_context(session)
