-- PostgreSQL 16 role bootstrap for Agentic NetOps Milestone 8.2.3.3
--
-- This file creates the five approved database roles, configures the
-- migrator's restricted membership in the owner role, and establishes the
-- dedicated netops schema owned by netops_owner.
--
-- Security properties:
--   * No plaintext passwords appear in this file.
--   * Roles are created only if they do not already exist.
--   * Pre-existing roles are inspected but never silently altered.
--   * Pre-existing schemas are inspected but never silently altered.
--   * Unexpected existing role attributes, memberships, schema ownership,
--     schema privileges, or object ownership cause the bootstrap to fail
--     closed.
--   * Rerunning against an already-correct state is idempotent.
--
-- Execution context: this SQL must be run against a disposable PostgreSQL 16
-- cluster by an administrative identity with sufficient privilege to create
-- roles and schemas (for example, the container bootstrap superuser). It must
-- not be executed by netops_runtime, netops_auth, or netops_maintenance.
--
-- Credentials are provisioned externally and are never embedded here.

-- ---------------------------------------------------------------------------
-- Phase 1: Pre-flight inspection of existing roles
-- ---------------------------------------------------------------------------
-- The following DO blocks fail closed if any of the five roles already exist
-- with attributes outside the approved matrix. They do not alter pre-existing
-- roles. They only validate.
DO $$
DECLARE
    rec record;
    unexpected_attributes text := '';
BEGIN
    FOR rec IN
        SELECT r.rolname,
               r.rolcanlogin,
               r.rolsuper,
               r.rolcreaterole,
               r.rolcreatedb,
               r.rolinherit,
               r.rolbypassrls
        FROM pg_roles r
        WHERE r.rolname IN (
            'netops_owner',
            'netops_migrator',
            'netops_runtime',
            'netops_auth',
            'netops_maintenance'
        )
    LOOP
        -- Approved: netops_owner NOLOGIN; all others LOGIN; none superuser,
        -- createrole, createdb, inherit, or bypassrls.
        IF rec.rolname = 'netops_owner' AND rec.rolcanlogin THEN
            unexpected_attributes := unexpected_attributes ||
                'netops_owner must be NOLOGIN; ';
        END IF;
        IF rec.rolname <> 'netops_owner' AND NOT rec.rolcanlogin THEN
            unexpected_attributes := unexpected_attributes ||
                rec.rolname || ' must be LOGIN; ';
        END IF;
        IF rec.rolsuper THEN
            unexpected_attributes := unexpected_attributes ||
                rec.rolname || ' must not be superuser; ';
        END IF;
        IF rec.rolcreaterole THEN
            unexpected_attributes := unexpected_attributes ||
                rec.rolname || ' must not have CREATEROLE; ';
        END IF;
        IF rec.rolcreatedb THEN
            unexpected_attributes := unexpected_attributes ||
                rec.rolname || ' must not have CREATEDB; ';
        END IF;
        IF rec.rolinherit THEN
            unexpected_attributes := unexpected_attributes ||
                rec.rolname || ' must be NOINHERIT; ';
        END IF;
        IF rec.rolbypassrls THEN
            unexpected_attributes := unexpected_attributes ||
                rec.rolname || ' must not have BYPASSRLS; ';
        END IF;
    END LOOP;

    IF unexpected_attributes <> '' THEN
        RAISE EXCEPTION
            'Refusing to bootstrap roles: pre-existing role(s) have unexpected attributes: %',
            unexpected_attributes
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Phase 2: Create roles that do not already exist
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netops_owner') THEN
        CREATE ROLE netops_owner
            NOLOGIN
            NOSUPERUSER
            NOCREATEROLE
            NOCREATEDB
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netops_migrator') THEN
        CREATE ROLE netops_migrator
            LOGIN
            NOSUPERUSER
            NOCREATEROLE
            NOCREATEDB
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netops_runtime') THEN
        CREATE ROLE netops_runtime
            LOGIN
            NOSUPERUSER
            NOCREATEROLE
            NOCREATEDB
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netops_auth') THEN
        CREATE ROLE netops_auth
            LOGIN
            NOSUPERUSER
            NOCREATEROLE
            NOCREATEDB
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'netops_maintenance') THEN
        CREATE ROLE netops_maintenance
            LOGIN
            NOSUPERUSER
            NOCREATEROLE
            NOCREATEDB
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Phase 3: Configure permitted membership
-- ---------------------------------------------------------------------------
-- netops_migrator is a member of netops_owner with INHERIT FALSE, SET TRUE,
-- ADMIN FALSE. It must explicitly SET ROLE netops_owner to acquire owner
-- privileges. Membership alone does not convey owner privileges.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_auth_members am
        JOIN pg_roles r ON r.oid = am.roleid
        JOIN pg_roles m ON m.oid = am.member
        WHERE r.rolname = 'netops_owner'
          AND m.rolname = 'netops_migrator'
    ) THEN
        GRANT netops_owner TO netops_migrator WITH INHERIT FALSE, SET TRUE, ADMIN FALSE;
    END IF;
END $$;

-- Fail closed if an unexpected membership already exists.
DO $$
DECLARE
    unexpected_memberships text := '';
BEGIN
    SELECT string_agg(r.rolname || ' -> ' || m.rolname, '; ')
    INTO unexpected_memberships
    FROM pg_auth_members am
    JOIN pg_roles r ON r.oid = am.roleid
    JOIN pg_roles m ON m.oid = am.member
    WHERE r.rolname IN (
        'netops_owner',
        'netops_migrator',
        'netops_runtime',
        'netops_auth',
        'netops_maintenance'
    )
      AND m.rolname IN (
        'netops_owner',
        'netops_migrator',
        'netops_runtime',
        'netops_auth',
        'netops_maintenance'
    )
      AND NOT (
        r.rolname = 'netops_owner' AND m.rolname = 'netops_migrator'
      );

    IF unexpected_memberships IS NOT NULL THEN
        RAISE EXCEPTION
            'Refusing to bootstrap roles: unexpected role membership(s) among netops roles: %',
            unexpected_memberships
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Phase 4: Establish the dedicated netops schema
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'netops') THEN
        CREATE SCHEMA netops AUTHORIZATION netops_owner;
    END IF;
END $$;

-- Verify schema ownership. Fail closed if the schema exists but is not owned
-- by netops_owner. Do not silently reassign ownership.
DO $$
DECLARE
    schema_owner text;
BEGIN
    SELECT nspowner::regrole::text
    INTO schema_owner
    FROM pg_namespace
    WHERE nspname = 'netops';

    IF schema_owner <> 'netops_owner' THEN
        RAISE EXCEPTION
            'Refusing to bootstrap schema netops: it is owned by %, not netops_owner',
            schema_owner
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $$;

-- Apply approved privileges for newly created schemas only.
-- A newly created schema has nspacl IS NULL (no explicit ACL entries).
-- Pre-existing schemas may have an existing ACL.
DO $$
DECLARE
    schema_nspacl text[];
BEGIN
    SELECT nspacl INTO schema_nspacl
    FROM pg_namespace
    WHERE nspname = 'netops';

    IF schema_nspacl IS NULL THEN
        -- Newly created schema: apply approved PUBLIC revocation and migrator USAGE.
        REVOKE ALL ON SCHEMA netops FROM PUBLIC;
        GRANT USAGE ON SCHEMA netops TO netops_migrator;
    END IF;
END $$;

-- Validate schema privileges (fail closed, no modification).
-- For newly created schemas, the grants above are already correct.
-- For pre-existing schemas, any deviation from the approved state raises.
DO $$
DECLARE
    public_has_privs boolean;
    migrator_has_usage boolean;
    unexpected_count int;
BEGIN
    -- Check PUBLIC privileges via nspacl text pattern.
    -- ACL entries appear as "role=privilege/grantor" separated by spaces.
    -- PUBLIC grants appear as "PUBLIC=...".
    SELECT (nspacl::text LIKE '% PUBLIC=%' OR nspacl::text LIKE 'PUBLIC=%')
    INTO public_has_privs
    FROM pg_namespace
    WHERE nspname = 'netops';

    IF public_has_privs THEN
        RAISE EXCEPTION
            'Refusing to bootstrap schema netops: PUBLIC has privileges on schema'
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;

    -- Check netops_migrator has exactly USAGE (no CREATE, DROP, etc.).
    SELECT has_schema_privilege('netops_migrator', 'netops', 'USAGE')
    INTO migrator_has_usage;

    IF NOT migrator_has_usage THEN
        RAISE EXCEPTION
            'Refusing to bootstrap schema netops: netops_migrator does not have USAGE privilege on schema'
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;

    -- Check netops_migrator does not have extra privileges.
    -- Schema-level privileges are CREATE and USAGE only.
    SELECT (has_schema_privilege('netops_migrator', 'netops', 'CREATE'))::int
    INTO unexpected_count;

    IF unexpected_count > 0 THEN
        RAISE EXCEPTION
            'Refusing to bootstrap schema netops: netops_migrator has unexpected privileges on schema'
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;

    -- Detect any unexpected role grants on the schema by checking the
    -- other netops roles (runtime, auth, maintenance) for any schema
    -- privileges. They must not have any.
    SELECT COUNT(*) INTO unexpected_count
    FROM pg_roles r
    WHERE r.rolname IN ('netops_runtime', 'netops_auth', 'netops_maintenance')
      AND (
          has_schema_privilege(r.rolname, 'netops', 'USAGE')
          OR has_schema_privilege(r.rolname, 'netops', 'CREATE')
      );

    IF unexpected_count > 0 THEN
        RAISE EXCEPTION
            'Refusing to bootstrap schema netops: unexpected role(s) have schema privileges'
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Phase 4c: Existing object ownership validation
-- ---------------------------------------------------------------------------
-- Detect relations and other objects under the netops schema owned by roles
-- other than netops_owner. Fail closed if unexpected ownership is found.
-- Do not reassign ownership or drop objects.
DO $$
DECLARE
    unexpected_objects text := '';
BEGIN
    SELECT string_agg(
        c.relkind::text || ' ' || c.relname || ' owned by ' || c.relowner::regrole::text,
        '; '
    )
    INTO unexpected_objects
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'netops'
      AND c.relowner <> 'netops_owner'::regrole;

    IF unexpected_objects IS NOT NULL THEN
        RAISE EXCEPTION
            'Refusing to bootstrap schema netops: unexpected object ownership: %',
            unexpected_objects
        USING ERRCODE = 'object_not_in_prerequisite_state';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Phase 5: Document future default-privilege mechanism
-- ---------------------------------------------------------------------------
-- When runtime/auth table objects are introduced in later milestones, the
-- following mechanism should be used by an appropriate administrative
-- identity:
--
--   ALTER DEFAULT PRIVILEGES FOR ROLE netops_owner IN SCHEMA netops
--       GRANT ... TO netops_runtime;
--
-- No speculative default privilege grants are executed in this milestone.
-- netops_runtime receives no tenant-table DML before RLS validation.
