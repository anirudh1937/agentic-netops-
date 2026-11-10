"""SQLAlchemy async models for the Agentic NetOps SaaS backend.

Milestone 8.1 initial schema:
- organizations
- users
- organization_memberships

All tables use UUID primary keys and timezone-aware timestamps.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.types import GUID, UTCDateTime

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Organization(Base):
    """A customer organization (tenant).

    Organizations are the top-level tenant boundary. All device, telemetry,
    and alert data is scoped to an organization.
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        "OrganizationMembership",
        back_populates="organization",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Organization(id={self.id!r}, name={self.name!r}, slug={self.slug!r})>"


class User(Base):
    """A global user identity.

    Users are not tenant-scoped; they can belong to multiple organizations
    through organization_memberships.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    memberships: Mapped[list["OrganizationMembership"]] = relationship(
        "OrganizationMembership",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id!r}, email={self.email!r})>"


class OrganizationMembership(Base):
    """Membership linking a user to an organization with a role.

    A user can belong to multiple organizations, each with their own role.
    """

    __tablename__ = "organization_memberships"

    id: Mapped[str] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[str] = mapped_column(
        GUID(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    joined_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    organization: Mapped["Organization"] = relationship("Organization", back_populates="memberships")
    user: Mapped["User"] = relationship("User", back_populates="memberships")

    __table_args__ = (
        CheckConstraint(
            "role IN ('org_admin', 'org_member')",
            # The naming convention resolves this to
            # ck_organization_memberships_role.
            name="role",
        ),
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_organization_memberships_organization_id_user_id",
        ),
    )

    def __repr__(self) -> str:
        return f"<OrganizationMembership(id={self.id!r}, organization_id={self.organization_id!r}, user_id={self.user_id!r}, role={self.role!r})>"
