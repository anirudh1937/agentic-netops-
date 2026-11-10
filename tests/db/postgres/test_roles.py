"""PostgreSQL 16 security tests for Milestone 8.2.3.2 role bootstrap.

These tests verify role attributes, membership options, schema ownership,
schema privileges, and SET ROLE behavior against a disposable PostgreSQL 16
container. They use NETOPS_TEST_DATABASE_URL only and never fall back to
NETOPS_DATABASE_URL.

Tests requiring role creation or schema ownership connect through an
administrative test identity (the container bootstrap superuser). Tests
asserting restrictions connect as netops_runtime or netops_migrator.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import asyncpg
import pytest

from ..postgres_test_support import (
    TEST_DATABASE_URL_ENV,
    DisposablePostgresTarget,
    TestDatabaseSafetyError,
    load_disposable_test_target,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(TEST_DATABASE_URL_ENV),
    reason=(
        f"{TEST_DATABASE_URL_ENV} is not set; PostgreSQL 16 role tests are "
        "skipped."
    ),
)

APPROVED_ROLES = [
    "netops_owner",
    "netops_migrator",
    "netops_runtime",
    "netops_auth",
    "netops_maintenance",
]


def _connect_kwargs(target: DisposablePostgresTarget) -> dict[str, object]:
    return target.asyncpg_connect_kwargs()


def _connect_kwargs_as_migrator(target: DisposablePostgresTarget) -> dict[str, object]:
    kwargs = target.asyncpg_connect_kwargs()
    kwargs["user"] = "netops_migrator"
    kwargs["password"] = getattr(target, "migrator_password", None)
    return kwargs


def _connect_kwargs_as_runtime(target: DisposablePostgresTarget) -> dict[str, object]:
    kwargs = target.asyncpg_connect_kwargs()
    kwargs["user"] = "netops_runtime"
    kwargs["password"] = getattr(target, "runtime_password", None)
    return kwargs


async def _fetch_role_attribute(
    connection: asyncpg.Connection, role_name: str, column: str
) -> Any:
    return await connection.fetchval(
        f"SELECT {column} FROM pg_roles WHERE rolname = $1", role_name
    )


_MEMBERSHIP_COLUMNS = {"inherit_option", "set_option", "admin_option"}


async def _fetch_membership_option(
    connection: asyncpg.Connection,
    parent_role: str,
    member_role: str,
    column: str,
) -> Any:
    if column not in _MEMBERSHIP_COLUMNS:
        raise ValueError(f"Invalid membership column: {column}")
    return await connection.fetchval(
        f"""
        SELECT am.{column}
        FROM pg_auth_members am
        JOIN pg_roles r ON r.oid = am.roleid
        JOIN pg_roles m ON m.oid = am.member
        WHERE r.rolname = $1 AND m.rolname = $2
        """,
        parent_role,
        member_role,
    )


def _run_async(coro) -> Any:
    return asyncio.run(coro)


def test_approved_role_names_are_defined() -> None:
    """The approved role set matches the architecture document."""
    assert APPROVED_ROLES == [
        "netops_owner",
        "netops_migrator",
        "netops_runtime",
        "netops_auth",
        "netops_maintenance",
    ]


def test_role_attributes_in_pg_roles(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """Each role has the approved pg_roles attributes."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            for role in APPROVED_ROLES:
                login = await _fetch_role_attribute(connection, role, "rolcanlogin")
                superuser = await _fetch_role_attribute(connection, role, "rolsuper")
                createrole = await _fetch_role_attribute(connection, role, "rolcreaterole")
                createdb = await _fetch_role_attribute(connection, role, "rolcreatedb")
                inherit = await _fetch_role_attribute(connection, role, "rolinherit")
                bypassrls = await _fetch_role_attribute(connection, role, "rolbypassrls")

                assert superuser is False, f"{role} must not be superuser"
                assert createrole is False, f"{role} must not have CREATEROLE"
                assert createdb is False, f"{role} must not have CREATEDB"
                assert inherit is False, f"{role} must be NOINHERIT"
                assert bypassrls is False, f"{role} must not have BYPASSRLS"

                if role == "netops_owner":
                    assert login is False, "netops_owner must be NOLOGIN"
                else:
                    assert login is True, f"{role} must be LOGIN"
        finally:
            await connection.close()

    _run_async(_run())


def test_migrator_membership_options(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """netops_migrator membership in netops_owner has correct options."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            inherit_option = await _fetch_membership_option(
                connection, "netops_owner", "netops_migrator", "inherit_option"
            )
            set_option = await _fetch_membership_option(
                connection, "netops_owner", "netops_migrator", "set_option"
            )
            admin_option = await _fetch_membership_option(
                connection, "netops_owner", "netops_migrator", "admin_option"
            )

            assert inherit_option is False, "inherit_option must be FALSE"
            assert set_option is True, "set_option must be TRUE"
            assert admin_option is False, "admin_option must be FALSE"
        finally:
            await connection.close()

    _run_async(_run())


def test_no_unexpected_memberships_among_netops_roles(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """Only netops_owner -> netops_migrator membership may exist."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            rows = await connection.fetch(
                """
                SELECT r.rolname AS parent_role, m.rolname AS member_role
                FROM pg_auth_members am
                JOIN pg_roles r ON r.oid = am.roleid
                JOIN pg_roles m ON m.oid = am.member
                WHERE r.rolname = ANY($1::text[])
                  AND m.rolname = ANY($1::text[])
                ORDER BY r.rolname, m.rolname
                """,
                APPROVED_ROLES,
            )
            memberships = {
                (row["parent_role"], row["member_role"]) for row in rows
            }
            assert memberships == {
                ("netops_owner", "netops_migrator")
            }, f"Unexpected memberships: {memberships}"
        finally:
            await connection.close()

    _run_async(_run())


def test_migrator_can_set_role_owner(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """netops_migrator can execute SET ROLE netops_owner."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs_as_migrator(disposable_postgres_cluster))
        try:
            await connection.execute("SET ROLE netops_owner")
            current_user = await connection.fetchval("SELECT current_user")
            session_user = await connection.fetchval("SELECT session_user")
            assert current_user == "netops_owner", (
                f"current_user after SET ROLE: {current_user}"
            )
            assert session_user == "netops_migrator", (
                f"session_user after SET ROLE: {session_user}"
            )
        finally:
            await connection.execute("RESET ROLE")
            await connection.close()

    _run_async(_run())


def test_reset_role_returns_to_migrator(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """RESET ROLE returns current_user to netops_migrator."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs_as_migrator(disposable_postgres_cluster))
        try:
            await connection.execute("SET ROLE netops_owner")
            await connection.execute("RESET ROLE")
            current_user = await connection.fetchval("SELECT current_user")
            assert current_user == "netops_migrator", (
                f"current_user after RESET ROLE: {current_user}"
            )
        finally:
            await connection.close()

    _run_async(_run())


def test_runtime_cannot_set_role_owner(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """netops_runtime cannot execute SET ROLE netops_owner."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            can_set_role = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members am
                    JOIN pg_roles r ON r.oid = am.roleid
                    JOIN pg_roles m ON m.oid = am.member
                    WHERE r.rolname = 'netops_owner'
                      AND m.rolname = 'netops_runtime'
                      AND am.set_option = TRUE
                )
                """
            )
            assert can_set_role is False, (
                "netops_runtime must not have SET option on netops_owner"
            )
        finally:
            await connection.close()

    _run_async(_run())


def test_schema_ownership_and_privileges(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """netops schema is owned by netops_owner; PUBLIC has no privileges."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            owner = await connection.fetchval(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'netops'"
            )
            assert owner == "netops_owner", f"Schema owner: {owner}"

            # Verify PUBLIC has no privileges on netops schema via pg_namespace ACL.
            public_privs = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_namespace n
                CROSS JOIN LATERAL unnest(n.nspacl) AS acl(aclitem)
                WHERE n.nspname = 'netops'
                  AND (aclitem::text LIKE 'PUBLIC=%')
                """
            )
            assert public_privs == 0, (
                f"PUBLIC privileges on netops schema: {public_privs}"
            )

            # Verify netops_migrator has USAGE via pg_has_privilege.
            migrator_usage = await connection.fetchval(
                "SELECT has_schema_privilege('netops_migrator', 'netops', 'USAGE')"
            )
            assert migrator_usage is True, (
                f"netops_migrator USAGE on netops schema: {migrator_usage}"
            )
        finally:
            await connection.close()

    _run_async(_run())


def test_migrator_cannot_ddl_before_set_role(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """netops_migrator cannot drop owner-owned tables without SET ROLE."""

    async def _run() -> None:
        # Create a table owned by netops_owner in the netops schema.
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            await admin.execute("SET ROLE netops_owner")
            await admin.execute("DROP TABLE IF EXISTS netops.role_ddl_probe")
            await admin.execute(
                "CREATE TABLE netops.role_ddl_probe (id uuid PRIMARY KEY)"
            )
            await admin.execute("RESET ROLE")
        finally:
            await admin.close()

        migrator = await asyncpg.connect(**_connect_kwargs_as_migrator(disposable_postgres_cluster))
        try:
            # Without SET ROLE netops_owner, migrator cannot drop the table.
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await migrator.execute("DROP TABLE netops.role_ddl_probe")
        finally:
            # Clean up: drop the probe table as owner.
            admin2 = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
            try:
                await admin2.execute("SET ROLE netops_owner")
                await admin2.execute("DROP TABLE IF EXISTS netops.role_ddl_probe")
                await admin2.execute("RESET ROLE")
            finally:
                await admin2.close()
            await migrator.close()

    _run_async(_run())


def test_bootstrap_idempotency(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """Re-running role attribute checks against an already-correct cluster is safe."""

    async def _run() -> None:
        connection = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            for role in APPROVED_ROLES:
                exists = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = $1)", role
                )
                assert exists is True, f"{role} must exist"

            schema_exists = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = 'netops')"
            )
            assert schema_exists is True, "netops schema must exist"
        finally:
            await connection.close()

    _run_async(_run())


def test_bootstrap_sql_file_exists() -> None:
    """The roles.sql bootstrap file is present."""
    import os as _os

    path = _os.path.join(
        _os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "scripts",
        "db",
        "roles.sql",
    )
    assert _os.path.isfile(path), "scripts/db/roles.sql must exist"


def test_preexisting_role_unexpected_attributes_causes_failure(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """A pre-existing role with unexpected attributes causes bootstrap failure."""

    async def _run() -> None:
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Make netops_migrator a superuser (unexpected attribute).
            await admin.execute("ALTER ROLE netops_migrator WITH SUPERUSER")

            # Snapshot state before failure.
            superuser_before = await admin.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = 'netops_migrator'"
            )
            assert superuser_before is True

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError(
                    "Bootstrap should have failed for pre-existing superuser role"
                )
            except asyncpg.exceptions.PostgresError:
                # Expected: bootstrap fails closed.
                pass

            # State must be unchanged: netops_migrator is still superuser.
            superuser_after = await admin.fetchval(
                "SELECT rolsuper FROM pg_roles WHERE rolname = 'netops_migrator'"
            )
            assert superuser_after is True

            # Clean up the bad state.
            await admin.execute(
                "ALTER ROLE netops_migrator WITH NOSUPERUSER"
            )
        finally:
            await admin.close()

    _run_async(_run())


def test_preexisting_unexpected_membership_causes_failure(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """An unexpected pre-existing membership causes bootstrap failure."""

    async def _run() -> None:
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Grant netops_runtime membership in netops_owner (unexpected).
            await admin.execute("GRANT netops_owner TO netops_runtime")

            # Snapshot membership before failure.
            membership_before = await admin.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members am
                    JOIN pg_roles r ON r.oid = am.roleid
                    JOIN pg_roles m ON m.oid = am.member
                    WHERE r.rolname = 'netops_owner'
                      AND m.rolname = 'netops_runtime'
                )
                """
            )
            assert membership_before is True

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError(
                    "Bootstrap should have failed for unexpected membership"
                )
            except asyncpg.exceptions.PostgresError:
                pass

            # State must be unchanged: unexpected membership still exists.
            membership_after = await admin.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members am
                    JOIN pg_roles r ON r.oid = am.roleid
                    JOIN pg_roles m ON m.oid = am.member
                    WHERE r.rolname = 'netops_owner'
                      AND m.rolname = 'netops_runtime'
                )
                """
            )
            assert membership_after is True

            # Clean up the bad state.
            await admin.execute("REVOKE netops_owner FROM netops_runtime")
        finally:
            await admin.close()

    _run_async(_run())


def test_preexisting_schema_wrong_owner_causes_failure(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """A pre-existing schema with the wrong owner causes bootstrap failure."""

    async def _run() -> None:
        # Connect as admin (bootstrap superuser) for schema operations.
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Drop existing schema and recreate with wrong owner (netops_migrator).
            await admin.execute("DROP SCHEMA IF EXISTS netops")
            await admin.execute(
                "CREATE SCHEMA netops AUTHORIZATION netops_migrator"
            )

            # Snapshot owner before failure.
            owner_before = await admin.fetchval(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'netops'"
            )
            assert owner_before == "netops_migrator"

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError(
                    "Bootstrap should have failed for wrong schema owner"
                )
            except asyncpg.exceptions.PostgresError:
                pass

            # State must be unchanged: schema still owned by netops_migrator.
            owner_after = await admin.fetchval(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'netops'"
            )
            assert owner_after == "netops_migrator"

            # Clean up: drop and recreate schema with correct owner.
            await admin.execute("DROP SCHEMA IF EXISTS netops")
            await admin.execute(
                "CREATE SCHEMA netops AUTHORIZATION netops_owner"
            )
            await admin.execute("REVOKE ALL ON SCHEMA netops FROM PUBLIC")
            await admin.execute("GRANT USAGE ON SCHEMA netops TO netops_migrator")
        finally:
            await admin.close()

    _run_async(_run())


def test_preexisting_schema_public_privileges_causes_failure(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """A pre-existing schema with unexpected PUBLIC privileges causes failure."""

    async def _run() -> None:
        # Connect as admin (bootstrap superuser) to manage schema privileges.
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Grant USAGE on netops schema to PUBLIC (unexpected).
            await admin.execute("GRANT USAGE ON SCHEMA netops TO PUBLIC")

            # Snapshot PUBLIC privileges before failure.
            public_privs_before = await admin.fetchval(
                """
                SELECT COUNT(*) > 0
                FROM aclexplode((SELECT nspacl FROM pg_namespace WHERE nspname = 'netops')) AS a
                WHERE a.grantee = 0  -- PUBLIC's OID
                """
            )
            assert public_privs_before is True

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError(
                    "Bootstrap should have failed for PUBLIC schema privileges"
                )
            except asyncpg.exceptions.PostgresError:
                pass

            # State must be unchanged: PUBLIC still has privileges.
            public_privs_after = await admin.fetchval(
                """
                SELECT COUNT(*) > 0
                FROM aclexplode((SELECT nspacl FROM pg_namespace WHERE nspname = 'netops')) AS a
                WHERE a.grantee = 0  -- PUBLIC's OID
                """
            )
            assert public_privs_after is True

            # Clean up the bad state.
            await admin.execute("REVOKE USAGE ON SCHEMA netops FROM PUBLIC")
        finally:
            await admin.close()

    _run_async(_run())


def test_preexisting_schema_unexpected_role_grant_causes_failure(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """A pre-existing schema with unexpected role grants causes failure."""

    async def _run() -> None:
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Grant CREATE on netops schema to netops_runtime (unexpected).
            await admin.execute("GRANT CREATE ON SCHEMA netops TO netops_runtime")

            # Snapshot unexpected grant before failure.
            unexpected_before = await admin.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_namespace n
                CROSS JOIN LATERAL unnest(n.nspacl) AS acl(aclitem)
                WHERE n.nspname = 'netops'
                  AND (aclitem::text NOT LIKE 'PUBLIC=%')
                  AND NOT (
                      aclitem::text LIKE 'netops_migrator=%'
                      AND aclitem::text LIKE '%USAGE%'
                  )
                """
            )
            assert unexpected_before > 0

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError(
                    "Bootstrap should have failed for unexpected schema grants"
                )
            except asyncpg.exceptions.PostgresError:
                pass

            # State must be unchanged: unexpected grant still exists.
            unexpected_after = await admin.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_namespace n
                CROSS JOIN LATERAL unnest(n.nspacl) AS acl(aclitem)
                WHERE n.nspname = 'netops'
                  AND (aclitem::text NOT LIKE 'PUBLIC=%')
                  AND NOT (
                      aclitem::text LIKE 'netops_migrator=%'
                      AND aclitem::text LIKE '%USAGE%'
                  )
                """
            )
            assert unexpected_after > 0

            # Clean up the bad state.
            await admin.execute("REVOKE CREATE ON SCHEMA netops FROM netops_runtime")
        finally:
            await admin.close()

    _run_async(_run())


def test_preexisting_object_unexpected_ownership_causes_failure(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """An object with unexpected ownership inside netops causes failure."""

    async def _run() -> None:
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Create a table owned by netops_migrator in netops schema.
            await admin.execute("DROP TABLE IF EXISTS netops.unexpected_owner_probe")
            await admin.execute(
                "CREATE TABLE netops.unexpected_owner_probe (id uuid PRIMARY KEY)"
            )
            await admin.execute(
                "ALTER TABLE netops.unexpected_owner_probe OWNER TO netops_migrator"
            )

            # Snapshot unexpected ownership before failure.
            unexpected_before = await admin.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'netops'
                  AND c.relowner <> 'netops_owner'::regrole
                """
            )
            assert unexpected_before > 0

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError(
                    "Bootstrap should have failed for unexpected object ownership"
                )
            except asyncpg.exceptions.PostgresError:
                pass

            # State must be unchanged: unexpected ownership still exists.
            unexpected_after = await admin.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'netops'
                  AND c.relowner <> 'netops_owner'::regrole
                """
            )
            assert unexpected_after > 0

            # Clean up the bad state.
            await admin.execute("DROP TABLE IF EXISTS netops.unexpected_owner_probe")
        finally:
            await admin.close()

    _run_async(_run())


def test_bootstrap_failure_atomicity(
    disposable_postgres_cluster: DisposablePostgresTarget,
) -> None:
    """A failed bootstrap leaves role, membership, and schema state unchanged."""

    async def _run() -> None:
        admin = await asyncpg.connect(**_connect_kwargs(disposable_postgres_cluster))
        try:
            # Introduce a failure condition: make netops_migrator a superuser.
            await admin.execute("ALTER ROLE netops_migrator WITH SUPERUSER")

            # Snapshot full state after introducing the failure condition.
            role_attrs_before = {}
            for role in APPROVED_ROLES:
                row = await admin.fetchrow(
                    """
                    SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
                           rolinherit, rolbypassrls
                    FROM pg_roles WHERE rolname = $1
                    """,
                    role,
                )
                role_attrs_before[role] = dict(row)

            membership_before = await admin.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members am
                    JOIN pg_roles r ON r.oid = am.roleid
                    JOIN pg_roles m ON m.oid = am.member
                    WHERE r.rolname = 'netops_owner'
                      AND m.rolname = 'netops_migrator'
                )
                """
            )
            assert membership_before is True

            schema_owner_before = await admin.fetchval(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'netops'"
            )
            assert schema_owner_before == "netops_owner"

            public_privs_before = await admin.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_namespace n
                CROSS JOIN LATERAL unnest(n.nspacl) AS acl(aclitem)
                WHERE n.nspname = 'netops'
                  AND (aclitem::text LIKE 'PUBLIC=%')
                """
            )
            assert public_privs_before == 0

            with open(
                "scripts/db/roles.sql", "r", encoding="utf-8"
            ) as f:
                sql = f.read()

            try:
                await admin.execute(sql)
                raise AssertionError("Bootstrap should have failed")
            except asyncpg.exceptions.PostgresError:
                pass

            # Verify state is unchanged from the post-failure-condition snapshot.
            for role in APPROVED_ROLES:
                row = await admin.fetchrow(
                    """
                    SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb,
                           rolinherit, rolbypassrls
                    FROM pg_roles WHERE rolname = $1
                    """,
                    role,
                )
                row_dict = dict(row)
                expected = role_attrs_before[role]
                for key in expected:
                    assert row_dict[key] == expected[key], (
                        f"{role}.{key} changed after failed bootstrap: "
                        f"before={expected[key]}, after={row_dict[key]}"
                    )

            # Membership and schema state unchanged.
            membership_after = await admin.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_auth_members am
                    JOIN pg_roles r ON r.oid = am.roleid
                    JOIN pg_roles m ON m.oid = am.member
                    WHERE r.rolname = 'netops_owner'
                      AND m.rolname = 'netops_migrator'
                )
                """
            )
            assert membership_after is True

            schema_owner_after = await admin.fetchval(
                "SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = 'netops'"
            )
            assert schema_owner_after == "netops_owner"

            public_privs_after = await admin.fetchval(
                """
                SELECT COUNT(*)
                FROM pg_namespace n
                CROSS JOIN LATERAL unnest(n.nspacl) AS acl(aclitem)
                WHERE n.nspname = 'netops'
                  AND (aclitem::text LIKE 'PUBLIC=%')
                """
            )
            assert public_privs_after == 0

            # Clean up: netops_migrator is still superuser from the failure setup.
            await admin.execute("ALTER ROLE netops_migrator WITH NOSUPERUSER")
        finally:
            await admin.close()

    _run_async(_run())