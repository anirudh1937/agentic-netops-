"""Database-level tests for organization membership role enforcement.

These tests apply the current Alembic head to a temporary SQLite database and
verify both accepted and rejected role values through SQL, not through
application-level validation.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

ROLE_CONSTRAINT = "ck_organization_memberships_role"


def _upgrade_database(db_path) -> None:
    old = os.environ.get("NETOPS_DATABASE_URL")
    os.environ["NETOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)


@pytest.fixture()
def migrated_engine(tmp_path):
    db_path = tmp_path / "role_constraint.db"
    _upgrade_database(db_path)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        yield engine
    finally:
        engine.dispose()


def _insert_organization(connection, slug: str) -> str:
    organization_id = uuid.uuid4().hex
    connection.execute(
        text(
            "INSERT INTO organizations (id, name, slug) "
            "VALUES (:id, :name, :slug)"
        ),
        {"id": organization_id, "name": f"Org {slug}", "slug": slug},
    )
    return organization_id


def _insert_user(connection, email: str) -> str:
    user_id = uuid.uuid4().hex
    connection.execute(
        text(
            "INSERT INTO users (id, email, password_hash) "
            "VALUES (:id, :email, :password_hash)"
        ),
        {"id": user_id, "email": email, "password_hash": "hash"},
    )
    return user_id


def _insert_membership(connection, organization_id: str, user_id: str, role: str) -> None:
    connection.execute(
        text(
            "INSERT INTO organization_memberships "
            "(id, organization_id, user_id, role) "
            "VALUES (:id, :organization_id, :user_id, :role)"
        ),
        {
            "id": uuid.uuid4().hex,
            "organization_id": organization_id,
            "user_id": user_id,
            "role": role,
        },
    )


def test_migration_declares_named_role_check(migrated_engine):
    with migrated_engine.connect() as connection:
        ddl = connection.execute(
            text(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'organization_memberships'"
            )
        ).scalar_one()

    check_names = re.findall(
        r"CONSTRAINT\s+([A-Za-z_][A-Za-z0-9_]*)\s+CHECK",
        ddl,
        flags=re.IGNORECASE,
    )
    assert check_names == [ROLE_CONSTRAINT]
    assert "org_admin" in ddl
    assert "org_member" in ddl


def test_valid_roles_are_accepted_by_database(migrated_engine):
    with migrated_engine.begin() as connection:
        organization_id = _insert_organization(connection, "valid-roles")
        admin_id = _insert_user(connection, "admin@example.com")
        member_id = _insert_user(connection, "member@example.com")
        _insert_membership(connection, organization_id, admin_id, "org_admin")
        _insert_membership(connection, organization_id, member_id, "org_member")

    with migrated_engine.connect() as connection:
        roles = {
            row[0]
            for row in connection.execute(
                text("SELECT role FROM organization_memberships")
            )
        }

    assert roles == {"org_admin", "org_member"}


@pytest.mark.parametrize("role", ["owner", "org-admin", "ORG_ADMIN", ""])
def test_invalid_roles_are_rejected_by_database(migrated_engine, role):
    with pytest.raises(IntegrityError) as exc_info:
        with migrated_engine.begin() as connection:
            organization_id = _insert_organization(
                connection, f"invalid-{uuid.uuid4().hex}"
            )
            user_id = _insert_user(
                connection, f"user-{uuid.uuid4().hex}@example.com"
            )
            _insert_membership(connection, organization_id, user_id, role)

    message = str(exc_info.value)
    matches = re.findall(
        r"CHECK constraint failed:\s*([A-Za-z_][A-Za-z0-9_]*)",
        message,
    )
    assert matches == [ROLE_CONSTRAINT]
