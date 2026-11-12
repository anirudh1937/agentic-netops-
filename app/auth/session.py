"""Server-side session management for Agentic NetOps.

Milestone 8.2.4.4 — Session creation, validation, and revocation.

Provides token generation, SHA-256 digest storage, and database-backed
session lifecycle management. Authentication endpoints, cookie handling,
and logout routes are implemented in later milestones.

Security properties:
- Token generated with secrets.token_bytes(32) (256 bits of entropy).
- Only SHA-256 digest stored in database; raw token never persisted.
- Raw token returned exactly once to the caller.
- Expiration enforced via expires_at column.
- Revocation enforced via revoked_at column.
- Multiple sessions per user allowed.
- Inactive user sessions rejected.
- Timezone-aware UTC timestamps.
- No JWT tokens.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Session
from app.db.models import User


async def create_session(
    db: AsyncSession,
    user_id: str,
    ttl_seconds: int = 43200,
) -> str:
    """Create a new session for a user.

    Generates a cryptographically secure random token, stores its SHA-256
    digest in the database, and returns the raw token once.

    Args:
        db: Async SQLAlchemy session.
        user_id: UUID of the user owning the session.
        ttl_seconds: Session lifetime in seconds (default 12 hours).

    Returns:
        Raw session token string to return to the client once.

    Raises:
        ValueError: If ttl_seconds is not positive, user_id is not a
            valid UUID string, user does not exist, or user is inactive.
    """
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")

    # Verify user exists and is active.
    user = await db.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} does not exist")
    if not user.is_active:
        raise ValueError(f"User {user_id} is not active")

    # Generate a single 32-byte random value. Derive both the URL-safe
    # token returned to the caller and the SHA-256 digest stored in the
    # database from these exact bytes.
    token_bytes = secrets.token_bytes(32)
    token = base64.urlsafe_b64encode(token_bytes).rstrip(b"=").decode("ascii")
    token_hash = hashlib.sha256(token_bytes).hexdigest()

    session = Session(
        id=str(uuid.uuid4()),
        user_id=user_id,
        token_hash=token_hash,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
    )
    db.add(session)
    await db.flush()
    return token


async def validate_session(
    db: AsyncSession,
    token: str,
) -> str | None:
    """Validate a session token and return the associated user ID.

    Args:
        db: Async SQLAlchemy session.
        token: Session token string from the client.

    Returns:
        User UUID if the session is valid, otherwise None.

    Raises:
        ValueError: If token is missing, empty, or not valid base64url.
    """
    if not token or not isinstance(token, str):
        return None

    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return None

    if len(decoded) != 32:
        return None

    token_hash = hashlib.sha256(decoded).hexdigest()

    result = await db.execute(
        Session.__table__.select().where(
            Session.__table__.c.token_hash == token_hash
        )
    )
    row = result.first()
    if row is None:
        return None

    session_row = row._mapping
    now = datetime.now(timezone.utc)
    if session_row.revoked_at is not None:
        return None
    if session_row.expires_at.tzinfo is None:
        # Defensive: stored value should be timezone-aware.
        expires_at = session_row.expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = session_row.expires_at
    if expires_at <= now:
        return None

    # Verify user is active.
    user = await db.get(User, str(session_row.user_id))
    if user is None or not user.is_active:
        return None

    return str(session_row.user_id)


async def revoke_session(
    db: AsyncSession,
    token: str,
) -> bool:
    """Revoke a session by token.

    Args:
        db: Async SQLAlchemy session.
        token: Session token string.

    Returns:
        True if the session was found and revoked, False if not found.
    """
    if not token or not isinstance(token, str):
        return False

    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return False

    if len(decoded) != 32:
        return False

    token_hash = hashlib.sha256(decoded).hexdigest()

    result = await db.execute(
        Session.__table__.select().where(
            Session.__table__.c.token_hash == token_hash
        )
    )
    row = result.first()
    if row is None:
        return False

    session_row = row._mapping
    if session_row.revoked_at is not None:
        return False

    await db.execute(
        Session.__table__.update()
        .where(Session.__table__.c.id == str(session_row.id))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.flush()
    return True
