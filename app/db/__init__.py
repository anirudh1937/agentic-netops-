"""Database package for Agentic NetOps.

Provides SQLAlchemy async models, connection management, and Alembic
migration support for the multi-tenant SaaS backend.

Milestone 8.1 tables:
- organizations
- users
- organization_memberships

No tenant-owned business tables (devices, telemetry, etc.) are included
in this milestone; those are added in later milestones.
"""

from app.db.models import Organization, OrganizationMembership, User
from app.db.session import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
    create_db_and_tables,
    dispose_db,
    get_async_engine,
    get_async_session,
    get_async_session_factory,
    init_db,
)

__all__ = [
    "Organization",
    "User",
    "OrganizationMembership",
    "AsyncEngine",
    "AsyncSession",
    "async_sessionmaker",
    "create_async_engine",
    "create_db_and_tables",
    "dispose_db",
    "get_async_engine",
    "get_async_session",
    "get_async_session_factory",
    "init_db",
]
