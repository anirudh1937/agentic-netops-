"""SQLAlchemy async models for Agentic NetOps authentication.

Milestone 8.2.4.3 — Database foundation for server-side session authentication.

Defines the Session model used for server-side session authentication.
Authentication endpoints, token issuance, and session management services
are implemented in later milestones.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.models import Base
from app.db.types import GUID, UTCDateTime


class Session(Base):
    """A server-side authentication session.

    A session represents an authenticated user's active login state.
    The session token is never stored in plaintext; only its SHA-256
    digest is stored in ``token_hash``.

    Multiple sessions per user are allowed. Each session has an absolute
    expiration and an optional revocation timestamp.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[str] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    revoked_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "expires_at > created_at",
            name="ck_sessions_expires_after_created",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_sessions_revoked_after_created",
        ),
        UniqueConstraint(
            "token_hash",
            name="uq_sessions_token_hash",
        ),
    )

    def __repr__(self) -> str:
        return f"<Session(id={self.id!r}, user_id={self.user_id!r})>"
