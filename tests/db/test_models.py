"""Unit tests for the Milestone 8.1 database models.

These tests verify that the SQLAlchemy model definitions have the correct
columns, types, defaults, constraints, and relationships. They do not
require a live database connection.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, String, inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Organization, OrganizationMembership, User
from app.db.types import GUID, UTCDateTime


def test_organization_model_columns():
    """Organization table has expected columns and constraints."""
    mapper = inspect(Organization)
    columns = {c.name: c for c in mapper.columns}

    assert "id" in columns
    assert columns["id"].primary_key
    assert columns["id"].type.__class__.__name__ == "GUID"

    assert "name" in columns
    assert not columns["name"].nullable

    assert "slug" in columns
    assert not columns["slug"].nullable
    assert columns["slug"].unique

    assert "created_at" in columns
    assert not columns["created_at"].nullable

    assert "updated_at" in columns
    assert not columns["updated_at"].nullable


def test_user_model_columns():
    """User table has expected columns and constraints."""
    mapper = inspect(User)
    columns = {c.name: c for c in mapper.columns}

    assert "id" in columns
    assert columns["id"].primary_key
    assert columns["id"].type.__class__.__name__ == "GUID"

    assert "email" in columns
    assert not columns["email"].nullable
    assert columns["email"].unique

    assert "password_hash" in columns
    assert not columns["password_hash"].nullable

    assert "full_name" in columns
    assert columns["full_name"].nullable

    assert "is_active" in columns
    assert not columns["is_active"].nullable
    assert columns["is_active"].default is not None

    assert "created_at" in columns
    assert not columns["created_at"].nullable

    assert "updated_at" in columns
    assert not columns["updated_at"].nullable


def test_organization_membership_model_columns():
    """OrganizationMembership table has expected columns and constraints."""
    mapper = inspect(OrganizationMembership)
    columns = {c.name: c for c in mapper.columns}

    assert "id" in columns
    assert columns["id"].primary_key
    assert columns["id"].type.__class__.__name__ == "GUID"

    assert "organization_id" in columns
    assert not columns["organization_id"].nullable

    assert "user_id" in columns
    assert not columns["user_id"].nullable

    assert "role" in columns
    assert not columns["role"].nullable

    assert "joined_at" in columns
    assert not columns["joined_at"].nullable

    # Check composite unique constraint exists
    unique_constraints = [
        c
        for c in mapper.tables[0].constraints
        if hasattr(c, "columns")
        and len(c.columns) == 2
        and {c.name for c in c.columns} == {"organization_id", "user_id"}
    ]
    assert len(unique_constraints) == 1

    composite_key_constraints = [
        c
        for c in mapper.tables[0].constraints
        if getattr(c, "name", None)
        == "uq_organization_memberships_organization_id_id"
    ]
    assert len(composite_key_constraints) == 1
    assert [
        column.name for column in composite_key_constraints[0].columns
    ] == ["organization_id", "id"]


def test_organization_relationships():
    """Organization has relationship to memberships."""
    mapper = inspect(Organization)
    assert "memberships" in mapper.relationships
    rel = mapper.relationships["memberships"]
    assert rel.argument == "OrganizationMembership"


def test_user_relationships():
    """User has relationship to memberships."""
    mapper = inspect(User)
    assert "memberships" in mapper.relationships
    rel = mapper.relationships["memberships"]
    assert rel.argument == "OrganizationMembership"


def test_organization_membership_relationships():
    """OrganizationMembership has relationships to organization and user."""
    mapper = inspect(OrganizationMembership)
    assert "organization" in mapper.relationships
    assert "user" in mapper.relationships


def test_guid_type_is_uuid():
    """GUID type is a String-based type."""
    from app.db.types import GUID

    guid_type = GUID()
    # The impl should be a string-based type
    assert isinstance(guid_type.impl, String)


def test_utc_datetime_type_is_datetime():
    """UTCDateTime type wraps DateTime."""
    from app.db.types import UTCDateTime

    utc_type = UTCDateTime()
    assert utc_type.impl is not None


def test_organization_defaults():
    """Organization model creates with expected defaults."""
    org = Organization(name="Acme Corp", slug="acme-corp")
    assert org.name == "Acme Corp"
    assert org.slug == "acme-corp"


def test_user_defaults():
    """User model creates with expected defaults."""
    user = User(
        email="alice@example.com",
        password_hash="hashed-password",
        full_name="Alice Example",
    )
    assert user.email == "alice@example.com"
    assert user.password_hash == "hashed-password"
    assert user.full_name == "Alice Example"


def test_organization_membership_defaults():
    """OrganizationMembership model creates with expected defaults."""
    import uuid

    membership = OrganizationMembership(
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="org_admin",
    )
    assert membership.role == "org_admin"


def test_role_check_constraint_metadata():
    """Role is a non-null String(32) with a named CHECK constraint."""
    mapper = inspect(OrganizationMembership)
    role_col = {c.name: c for c in mapper.columns}["role"]
    assert role_col.nullable is False
    assert role_col.type.length == 32

    role_checks = [
        constraint
        for constraint in OrganizationMembership.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_organization_memberships_role"
    ]
    assert len(role_checks) == 1

    sqltext = str(role_checks[0].sqltext)
    assert "org_admin" in sqltext
    assert "org_member" in sqltext


def test_uuid_primary_keys():
    """All three tables use UUID primary keys."""
    for model in [Organization, User, OrganizationMembership]:
        mapper = inspect(model)
        pk_cols = mapper.primary_key
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"


def test_timezone_aware_timestamps():
    """All datetime columns are timezone-aware."""
    for model in [Organization, User, OrganizationMembership]:
        mapper = inspect(model)
        for col in mapper.columns:
            if col.name in ("created_at", "updated_at", "joined_at"):
                assert col.type.timezone is True


def test_async_engine_creation_without_url():
    """Creating engine without DATABASE_URL raises clear error."""
    from app.db.session import get_async_engine

    # Temporarily unset the env var
    import os

    old = os.environ.pop("NETOPS_DATABASE_URL", None)
    try:
        # Reset the cached engine
        import app.db.session as session_mod

        session_mod._async_engine = None
        session_mod._async_session_factory = None

        with pytest.raises(RuntimeError, match="NETOPS_DATABASE_URL"):
            session_mod.get_async_engine()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        # Reset again for other tests
        import app.db.session as session_mod

        session_mod._async_engine = None
        session_mod._async_session_factory = None


def test_async_session_factory_creation_without_url():
    """Session factory creation without DATABASE_URL raises clear error."""
    import app.db.session as session_mod

    session_mod._async_engine = None
    session_mod._async_session_factory = None

    import os

    old = os.environ.pop("NETOPS_DATABASE_URL", None)
    try:
        with pytest.raises(RuntimeError, match="NETOPS_DATABASE_URL"):
            session_mod.get_async_session_factory()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        session_mod._async_engine = None
        session_mod._async_session_factory = None
