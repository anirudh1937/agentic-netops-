"""Live PostgreSQL 16 validation for the current Alembic head.

These tests are skipped unless ``NETOPS_TEST_DATABASE_URL`` is explicitly set
to a guarded disposable database. They do not read ``.env`` and do not fall
back to ``NETOPS_DATABASE_URL``.

The test intentionally leaves the disposable database upgraded after the
final verification so an operator can inspect it. The disposable container or
cluster must be destroyed separately.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from datetime import timedelta
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from .postgres_test_support import (
    TEST_DATABASE_URL_ENV,
    DisposablePostgresTarget,
    TestDatabaseSafetyError,
    assert_current_database_is_disposable,
    load_disposable_test_target,
    temporary_alembic_database_url,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_DATABASE_URL_ENV),
    reason=(
        f"{TEST_DATABASE_URL_ENV} is not set; live PostgreSQL 16 tests are "
        "skipped."
    ),
)

APP_TABLES = {"organizations", "users", "organization_memberships", "sessions"}
VERSION_TABLE = "alembic_version"

EXPECTED_COLUMNS = {
    "organizations": {
        "id",
        "name",
        "slug",
        "created_at",
        "updated_at",
    },
    "users": {
        "id",
        "email",
        "password_hash",
        "full_name",
        "is_active",
        "created_at",
        "updated_at",
    },
    "organization_memberships": {
        "id",
        "organization_id",
        "user_id",
        "role",
        "joined_at",
    },
    "sessions": {
        "id",
        "user_id",
        "token_hash",
        "created_at",
        "expires_at",
        "last_seen_at",
        "revoked_at",
    },
}

UUID_COLUMNS = {
    ("organizations", "id"),
    ("users", "id"),
    ("organization_memberships", "id"),
    ("organization_memberships", "organization_id"),
    ("organization_memberships", "user_id"),
    ("sessions", "id"),
    ("sessions", "user_id"),
}

TIMESTAMPTZ_COLUMNS = {
    ("organizations", "created_at"),
    ("organizations", "updated_at"),
    ("users", "created_at"),
    ("users", "updated_at"),
    ("organization_memberships", "joined_at"),
    ("sessions", "created_at"),
}

EXPECTED_CONSTRAINTS = {
    "ck_organization_memberships_role",
    "ck_sessions_ck_sessions_expires_after_created",
    "ck_sessions_ck_sessions_revoked_after_created",
    "uq_organization_memberships_organization_id_id",
    "uq_organization_memberships_organization_id_user_id",
    "uq_sessions_token_hash",
    "pk_organization_memberships",
    "pk_organizations",
    "pk_sessions",
    "pk_users",
    "fk_organization_memberships_organization_id_organizations",
    "fk_organization_memberships_user_id_users",
    "fk_sessions_user_id_users",
}

EXPECTED_INDEXES = {
    "ix_organizations_slug",
    "ix_organization_memberships_organization_id",
    "ix_organization_memberships_user_id",
    "ix_users_email",
    "ix_sessions_user_id",
    "pk_organizations",
    "pk_users",
    "pk_organization_memberships",
    "pk_sessions",
    "uq_organization_memberships_organization_id_id",
    "uq_organization_memberships_organization_id_user_id",
    "uq_sessions_token_hash",
}


def _assert_supported_postgres_version(identity: dict[str, Any]) -> None:
    """Fail closed unless the live server is PostgreSQL 16."""
    server_version_num = identity["server_version_num"]
    if not 160000 <= server_version_num < 170000:
        raise TestDatabaseSafetyError(
            "Refusing to migrate: live validation requires PostgreSQL 16; "
            f"got {identity['version']} "
            f"(server_version_num={server_version_num})."
        )
    if not identity["version"].startswith("PostgreSQL 16."):
        raise TestDatabaseSafetyError(
            "Refusing to migrate: server version does not identify "
            f"PostgreSQL 16: {identity['version']!r}."
        )


def _connect(target: DisposablePostgresTarget):
    return asyncpg.connect(**target.asyncpg_connect_kwargs())


async def _snapshot(target: DisposablePostgresTarget) -> dict[str, Any]:
    connection = await _connect(target)
    try:
        database = await assert_current_database_is_disposable(
            connection, target.database
        )
        identity = await connection.fetchrow(
            """
            SELECT current_database() AS database,
                   current_user AS current_user,
                   current_setting('server_version_num')::int
                        AS server_version_num,
                   version() AS version
            """
        )
        identity = dict(identity)
        _assert_supported_postgres_version(identity)
        tables = {
            row["tablename"]
            for row in await connection.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
        columns = {
            (row["table_name"], row["column_name"]): dict(row)
            for row in await connection.fetch(
                """
                SELECT table_name, column_name, data_type, udt_name,
                        is_nullable, column_default,
                        character_maximum_length
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
        }
        constraints = {
            row["conname"]: dict(row)
            for row in await connection.fetch(
                """
                SELECT c.conname, c.contype, rel.relname AS table_name,
                        pg_get_constraintdef(c.oid) AS definition
                FROM pg_constraint c
                JOIN pg_class rel ON rel.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = rel.relnamespace
                WHERE n.nspname = 'public'
                ORDER BY rel.relname, c.conname
                """
            )
        }
        indexes = {
            row["indexname"]: dict(row)
            for row in await connection.fetch(
                """
                SELECT tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                ORDER BY tablename, indexname
                """
            )
        }
        revision = None
        if VERSION_TABLE in tables:
            revision = await connection.fetchval(
                f"SELECT version_num FROM {VERSION_TABLE}"
            )
        return {
            "database": database,
            "identity": dict(identity),
            "tables": tables,
            "columns": columns,
            "constraints": constraints,
            "indexes": indexes,
            "revision": revision,
        }
    finally:
        await connection.close()


def _assert_require_column(snapshot: dict[str, Any], table: str, column: str) -> dict[str, Any]:
    return snapshot["columns"][(table, column)]


def _print_schema_evidence(snapshot: dict[str, Any], phase: str) -> None:
    identity = snapshot["identity"]
    print(f"[{phase}] database={snapshot['database']}")
    print(f"[{phase}] postgres_version={identity['version']}")
    print(f"[{phase}] server_version_num={identity['server_version_num']}")
    print(f"[{phase}] revision={snapshot['revision']!r}")
    print(f"[{phase}] tables={sorted(snapshot['tables'])}")
    print(f"[{phase}] constraints={sorted(snapshot['constraints'])}")
    print(f"[{phase}] indexes={sorted(snapshot['indexes'])}")


def _assert_expected_schema(snapshot: dict[str, Any]) -> None:
    identity = snapshot["identity"]
    _assert_supported_postgres_version(identity)

    assert snapshot["tables"] == APP_TABLES | {VERSION_TABLE}
    assert snapshot["revision"] == "0003"

    actual_app_columns = {
        (table, column)
        for table, column in snapshot["columns"]
        if table in APP_TABLES
    }
    expected_app_columns = {
        (table, column)
        for table, columns in EXPECTED_COLUMNS.items()
        for column in columns
    }
    assert actual_app_columns == expected_app_columns, (
        "Unexpected application column set: "
        f"missing={sorted(expected_app_columns - actual_app_columns)}, "
        f"extra={sorted(actual_app_columns - expected_app_columns)}"
    )

    for table, column in UUID_COLUMNS:
        details = _assert_require_column(snapshot, table, column)
        assert details["data_type"] == "uuid", (table, column, details)
        assert details["udt_name"] == "uuid", (table, column, details)
        assert details["is_nullable"] == "NO", (table, column, details)

    for table, column in TIMESTAMPTZ_COLUMNS:
        details = _assert_require_column(snapshot, table, column)
        assert details["data_type"] == "timestamp with time zone", (
            table,
            column,
            details,
        )
        assert details["is_nullable"] == "NO", (table, column, details)
        assert "now()" in (details["column_default"] or "").lower(), (
            table,
            column,
            details,
        )

    expected_lengths = {
        ("organizations", "name"): 255,
        ("organizations", "slug"): 255,
        ("users", "email"): 255,
        ("users", "full_name"): 255,
        ("organization_memberships", "role"): 32,
    }
    for (table, column), length in expected_lengths.items():
        assert _assert_require_column(snapshot, table, column)[
            "character_maximum_length"
        ] == length

    assert _assert_require_column(snapshot, "users", "password_hash")["data_type"] == "text"
    is_active = _assert_require_column(snapshot, "users", "is_active")
    assert is_active["data_type"] == "boolean"
    assert is_active["is_nullable"] == "NO"
    assert (is_active["column_default"] or "").lower() == "true"

    constraints = snapshot["constraints"]
    app_constraints = {
        name: constraint
        for name, constraint in constraints.items()
        if constraint["table_name"] in APP_TABLES
    }
    assert set(app_constraints) == EXPECTED_CONSTRAINTS, (
        "Unexpected application constraint set: "
        f"missing={sorted(EXPECTED_CONSTRAINTS - set(app_constraints))}, "
        f"extra={sorted(set(app_constraints) - EXPECTED_CONSTRAINTS)}"
    )

    for name in ("pk_organizations", "pk_users", "pk_organization_memberships"):
        assert "PRIMARY KEY (id)" in constraints[name]["definition"]

    composite_unique = constraints[
        "uq_organization_memberships_organization_id_user_id"
    ]["definition"]
    assert "UNIQUE (organization_id, user_id)" in composite_unique

    composite_key = constraints[
        "uq_organization_memberships_organization_id_id"
    ]["definition"]
    assert "UNIQUE (organization_id, id)" in composite_key

    for name in (
        "fk_organization_memberships_organization_id_organizations",
        "fk_organization_memberships_user_id_users",
    ):
        definition = constraints[name]["definition"]
        assert "FOREIGN KEY" in definition
        assert "ON DELETE CASCADE" in definition

    role_check = constraints["ck_organization_memberships_role"]["definition"]
    assert "CHECK" in role_check
    assert "org_admin" in role_check
    assert "org_member" in role_check

    indexes = snapshot["indexes"]
    app_indexes = {
        name: index
        for name, index in indexes.items()
        if index["tablename"] in APP_TABLES
    }
    assert set(app_indexes) == EXPECTED_INDEXES, (
        "Unexpected application index set: "
        f"missing={sorted(EXPECTED_INDEXES - set(app_indexes))}, "
        f"extra={sorted(set(app_indexes) - EXPECTED_INDEXES)}"
    )

    assert "CREATE UNIQUE INDEX" in indexes["ix_organizations_slug"]["indexdef"]
    assert "CREATE UNIQUE INDEX" in indexes["ix_users_email"]["indexdef"]
    assert (
        "CREATE UNIQUE INDEX"
        in indexes["uq_organization_memberships_organization_id_user_id"]["indexdef"]
    )
    assert (
        "CREATE UNIQUE INDEX"
        in indexes["uq_organization_memberships_organization_id_id"]["indexdef"]
    )
    assert "UNIQUE" not in indexes["ix_organization_memberships_organization_id"]["indexdef"]
    assert "UNIQUE" not in indexes["ix_organization_memberships_user_id"]["indexdef"]


second_org_id = str(uuid.uuid4())
second_membership_id = str(uuid.uuid4())

async def _exercise_runtime_constraints(target: DisposablePostgresTarget) -> None:
    connection = await _connect(target)
    try:
        await assert_current_database_is_disposable(connection, target.database)

        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        admin_membership_id = uuid.uuid4()
        member_membership_id = uuid.uuid4()
        member_user_id = uuid.uuid4()
        suffix = uuid.uuid4().hex
        slug = f"live-{suffix}"
        email = f"live-{suffix}@example.com"
        probe_table = f"membership_reference_probe_{suffix}"

        async with connection.transaction():
            await connection.execute(
                "INSERT INTO organizations (id, name, slug) VALUES ($1, $2, $3)",
                org_id,
                "Live Validation Org",
                slug,
            )
            await connection.execute(
                "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
                user_id,
                email,
                "not-a-real-hash",
            )
            await connection.execute(
                """
                INSERT INTO organization_memberships
                    (id, organization_id, user_id, role)
                VALUES ($1, $2, $3, $4)
                """,
                admin_membership_id,
                org_id,
                user_id,
                "org_admin",
            )

        cross_org_id = uuid.uuid4()
        async with connection.transaction():
            await connection.execute(
                "INSERT INTO organizations (id, name, slug) VALUES ($1, $2, $3)",
                cross_org_id,
                "Cross Tenant Probe Org",
                f"cross-{suffix}",
            )

        # PostgreSQL temporary tables cannot reference permanent tables.
        # Use a uniquely named regular table and drop it in finally.
        probe_created = False
        try:
            async with connection.transaction():
                await connection.execute(
                    f"""
                    CREATE TABLE {probe_table} (
                        id UUID PRIMARY KEY,
                        organization_id UUID NOT NULL,
                        membership_id UUID NOT NULL,
                        FOREIGN KEY (organization_id, membership_id)
                            REFERENCES organization_memberships
                                (organization_id, id)
                    )
                    """
                )
            probe_created = True

            async with connection.transaction():
                await connection.execute(
                    f"""
                    INSERT INTO {probe_table}
                        (id, organization_id, membership_id)
                    VALUES ($1, $2, $3)
                    """,
                    uuid.uuid4(),
                    org_id,
                    admin_membership_id,
                )

            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        f"""
                        INSERT INTO {probe_table}
                            (id, organization_id, membership_id)
                        VALUES ($1, $2, $3)
                        """,
                        uuid.uuid4(),
                        cross_org_id,
                        admin_membership_id,
                    )

            with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
                async with connection.transaction():
                    await connection.execute(
                        f"""
                        INSERT INTO {probe_table}
                            (id, organization_id, membership_id)
                        VALUES ($1, $2, $3)
                        """,
                        uuid.uuid4(),
                        org_id,
                        uuid.uuid4(),
                    )
        finally:
            if probe_created:
                await connection.execute(
                    f"DROP TABLE IF EXISTS {probe_table}"
                )

        row = await connection.fetchrow(
            """
            SELECT o.id AS organization_id, o.created_at, o.updated_at,
                   u.id AS user_id, u.created_at AS user_created_at,
                   m.joined_at
            FROM organizations o
            JOIN organization_memberships m ON m.organization_id = o.id
            JOIN users u ON u.id = m.user_id
            WHERE o.id = $1
            """,
            org_id,
        )
        assert isinstance(row["organization_id"], uuid.UUID)
        assert isinstance(row["user_id"], uuid.UUID)
        assert row["created_at"].tzinfo is not None
        assert row["created_at"].utcoffset() == timedelta(0)
        assert row["updated_at"].tzinfo is not None
        assert row["updated_at"].utcoffset() == timedelta(0)
        assert row["user_created_at"].tzinfo is not None
        assert row["joined_at"].tzinfo is not None

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO organization_memberships
                        (id, organization_id, user_id, role)
                    VALUES ($1, $2, $3, $4)
                    """,
                    uuid.uuid4(),
                    org_id,
                    user_id,
                    "owner",
                )

        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO organization_memberships
                        (id, organization_id, user_id, role)
                    VALUES ($1, $2, $3, $4)
                    """,
                    uuid.uuid4(),
                    org_id,
                    user_id,
                    "org_member",
                )

        async with connection.transaction():
            await connection.execute(
                "INSERT INTO organizations (id, name, slug) VALUES ($1, $2, $3)",
                second_org_id,
                "Second Live Org",
                f"second-{suffix}",
            )
            await connection.execute(
                """
                INSERT INTO organization_memberships
                    (id, organization_id, user_id, role)
                VALUES ($1, $2, $3, $4)
                """,
                second_membership_id,
                second_org_id,
                user_id,
                "org_admin",
            )
            await connection.execute("DELETE FROM users WHERE id = $1", user_id)

        assert (
            await connection.fetchval(
                "SELECT count(*) FROM organization_memberships WHERE id = $1",
                second_membership_id,
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM organizations WHERE id = $1",
                second_org_id,
            )
            == 1
        )
    finally:
        await connection.close()


def _assert_application_tables_removed(snapshot: dict[str, Any]) -> None:
    """Verify no application tables remain after downgrade to base.

    The Alembic version table may still exist (it tracks revision None),
    but all application tables must be absent.
    """
    application_tables = snapshot["tables"] - {VERSION_TABLE}
    assert application_tables == set(), (
        f"Unexpected tables remain after downgrade to base: "
        f"{sorted(application_tables)}"
    )


def test_live_head_upgrade_downgrade_reupgrade():
    target = load_disposable_test_target()
    config = Config("alembic.ini")

    print(f"[live] guarded target={target.safe_dsn}")

    fresh = asyncio.run(_snapshot(target))
    _print_schema_evidence(fresh, "fresh")
    assert fresh["tables"] == set()
    assert fresh["revision"] is None

    second_org_id = str(uuid.uuid4())

    with temporary_alembic_database_url(target):
        command.upgrade(config, "head")

        upgraded = asyncio.run(_snapshot(target))
        _print_schema_evidence(upgraded, "upgrade-1")
        _assert_expected_schema(upgraded)

        offline_sql = _offline_sql(target)
        assert "CREATE TABLE organizations" in offline_sql
        assert "CREATE TABLE users" in offline_sql
        assert "CREATE TABLE organization_memberships" in offline_sql
        assert "UUID" in offline_sql
        assert "TIMESTAMP WITH TIME ZONE" in offline_sql
        assert "ON DELETE CASCADE" in offline_sql
        assert "ck_organization_memberships_role" in offline_sql
        assert "org_admin" in offline_sql
        assert "org_member" in offline_sql
        for constraint_name in EXPECTED_CONSTRAINTS:
            assert constraint_name in offline_sql, constraint_name
        for index_name in EXPECTED_INDEXES:
            assert index_name in offline_sql, index_name

        asyncio.run(_exercise_runtime_constraints(target))

        with temporary_alembic_database_url(target):
            command.downgrade(config, "base")

            downgraded = asyncio.run(_snapshot(target))
            _print_schema_evidence(downgraded, "downgrade")
            _assert_application_tables_removed(downgraded)
            assert downgraded["revision"] is None

        with temporary_alembic_database_url(target):
            command.upgrade(config, "head")

            reupgraded = asyncio.run(_snapshot(target))
            _print_schema_evidence(reupgraded, "upgrade-2")
            _assert_expected_schema(reupgraded)

    print("[live] head upgrade/downgrade/re-upgrade validation passed")


def _offline_sql(target: DisposablePostgresTarget) -> str:
    config = Config("alembic.ini")
    output = io.StringIO()
    config.output_buffer = output
    with temporary_alembic_database_url(target):
        command.upgrade(config, "head", sql=True)
    return output.getvalue()