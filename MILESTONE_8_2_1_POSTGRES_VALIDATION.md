# Milestone 8.2.1 — PostgreSQL 16 Validation

**Status:** Completed; subject to owner review
**Repository:** `C:\Users\Aniru\agentic-netops-old`
**Branch:** `feature/cisco-live-validation`
**Verified HEAD:** `991a4362b6298243277b01356e0c05d35087006d`
**Preflight report SHA-256:** `2cc5e547f7f6595ff42f835d80ec5b5d22142d4a3ca371da4def897c4f3ac47a`
**Date:** 2026-11-10
**Review follow-up:** B-1, W-2, W-5, and W-6 corrections applied. Offline/unit tests passed; no live PostgreSQL rerun was performed after the corrections.

> This milestone validates the existing revision `0001` against a disposable PostgreSQL 16 database. It does not implement or validate authentication, server-side sessions, tenant context, database role separation, RLS, organization onboarding, revision `0002`, or any later milestone.

## 1. Scope and boundaries

Validated:

- Docker-provided PostgreSQL 16.
- An explicit-DSN test harness with a disposable-database-name guard.
- Live `alembic upgrade head` for revision `0001`.
- Live `alembic downgrade base`.
- Live second `alembic upgrade head`.
- PostgreSQL UUID, timestamptz, defaults, primary keys, unique constraints, indexes, foreign keys, CHECK constraints, and cascade behavior.
- Existing SQLite regression tests.
- Offline PostgreSQL SQL generation.

Not implemented or validated:

- Authentication or session management.
- Tenant context or request unit of work.
- Database role separation or privilege model.
- RLS policies or tenant isolation.
- Organization onboarding or security-definer functions.
- Revision `0002` or later.
- Production or existing database access.

No `.env` file was read. No production credentials were used. No existing or production database was contacted. No existing container or Kubernetes resource was modified.

## 2. PostgreSQL image and environment

### Image

```text
Tag:            postgres:16
Image ID:       sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657
RepoDigest:     postgres@sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657
Architecture:   amd64
OS:             linux
PG_MAJOR:       16
PG_VERSION:     16.15-1.pgdg13+2
```

The image was pulled once from Docker Hub with the explicitly authorized command:

```bash
docker pull postgres:16
```

It was retained after cleanup for future authorized local testing.

### Disposable database

```text
Container:      netops-pg16-821-82920c68894617c9
Volume:         netops_pg16_821_82920c68894617c9
Database:       netops_test_82920c68894617c9
Database user:  netops_test_user_82920c68894617c9
Host binding:   127.0.0.1
Host port:      62462
```

The password was generated with `secrets.token_hex(24)`. It was passed through a temporary mode-`0600` environment file, never printed, and the file was deleted.

Server identity recorded from the live connection:

```text
PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) on x86_64-pc-linux-gnu,
compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit
server_version_num = 160015
```

The official container bootstrap user was used as the temporary migration credential. It is not the eventual `netops_owner`, `netops_migrator`, or runtime role model. Role provisioning and privilege testing remain out of scope for 8.2.1.

## 3. Test harness

Created files:

- `tests/db/postgres_test_support.py`
- `tests/db/test_postgres_test_support.py`
- `tests/db/test_postgres_16_live.py`
- `tests/db/test_postgres_16_safety.py`

### Safety guard

The live harness:

- Reads only `NETOPS_TEST_DATABASE_URL`.
- Never falls back to `NETOPS_DATABASE_URL`.
- Never reads `.env`.
- Requires the `postgresql+asyncpg` driver.
- Requires an explicit username and password.
- Requires a local host: `127.0.0.1`, `localhost`, or `::1`.
- Requires the database name to match:

```text
^netops_test_[a-f0-9]{8,32}$
```

- Requires `160000 <= server_version_num < 170000` immediately after the initial identity query and before any Alembic upgrade or downgrade.
- Rejects PostgreSQL 15 and PostgreSQL 17-or-later before migration execution.
- Verifies `current_database()` through a live connection before migration or other destructive operations.
- Rejects a connected database that differs from the validated target.
- Temporarily exposes the validated DSN to Alembic and restores the prior `NETOPS_DATABASE_URL` value afterward.
- Skips live tests when `NETOPS_TEST_DATABASE_URL` is absent.
- Asserts exact application table, column, constraint, and index sets in addition to the expected definitions.

The guard unit suite includes rejection of non-disposable names, non-PostgreSQL drivers, remote hosts, missing passwords, and the case where only `NETOPS_DATABASE_URL` is present.

Result:

```text
tests/db/test_postgres_test_support.py
18 passed
```

## 4. Live validation sequence

> The live results in this section are from the completed PostgreSQL 16.15 run performed before the review corrections. The corrected harness was not rerun against a live database during the safety-correction task.

The database was created fresh. The live test:

1. Verified the guarded database identity and PostgreSQL 16 version.
2. Confirmed the fresh database had no tables.
3. Ran `alembic upgrade head`.
4. Inspected version, tables, columns, constraints, and indexes.
5. Exercised runtime constraint behavior.
6. Generated offline PostgreSQL SQL and checked that expected objects were present.
7. Ran `alembic downgrade base`.
8. Confirmed application tables, constraints, and indexes were removed.
9. Ran `alembic upgrade head` again.
10. Repeated the schema assertions.

Command:

```bash
NETOPS_TEST_DATABASE_URL="postgresql+asyncpg://<user>:<secret>@127.0.0.1:<port>/<netops_test_database>" \
  .venv/Scripts/python.exe -m pytest tests/db/test_postgres_16_live.py -q -s
```

Result:

```text
1 passed
```

### Fresh database

```text
revision = None
tables = []
constraints = []
indexes = []
```

### First upgrade

```text
revision = 0001
tables = alembic_version, organization_memberships, organizations, users
```

### Downgrade

```text
revision = None
tables = alembic_version
application tables removed = yes
application constraints removed = yes
application indexes removed = yes
```

### Re-upgrade

```text
revision = 0001
tables = alembic_version, organization_memberships, organizations, users
schema assertions repeated = passed
```

## 5. Actual schema evidence

### Tables

```text
alembic_version
organization_memberships
organizations
users
```

### UUID columns

All were live PostgreSQL native `uuid` columns, non-null:

```text
organizations.id
users.id
organization_memberships.id
organization_memberships.organization_id
organization_memberships.user_id
```

### Timezone-aware timestamp columns

All were `timestamp with time zone`, non-null, with `now()` as the server default:

```text
organizations.created_at
organizations.updated_at
users.created_at
users.updated_at
organization_memberships.joined_at
```

A live inserted row returned timezone-aware values with a UTC offset. A native UUID value was returned as a `uuid.UUID` instance.

### Column shapes

```text
organizations.name                     varchar(255)  NOT NULL
organizations.slug                     varchar(255)  NOT NULL
users.email                            varchar(255)  NOT NULL
users.password_hash                    text          NOT NULL
users.full_name                        varchar(255)  NULL
users.is_active                        boolean       NOT NULL  DEFAULT true
organization_memberships.role          varchar(32)   NOT NULL
```

### Constraints

Actual definitions from `pg_get_constraintdef`:

```text
organizations
  pk_organizations
    PRIMARY KEY (id)

users
  pk_users
    PRIMARY KEY (id)

organization_memberships
  pk_organization_memberships
    PRIMARY KEY (id)

  uq_organization_memberships_organization_id_user_id
    UNIQUE (organization_id, user_id)

  fk_organization_memberships_organization_id_organizations
    FOREIGN KEY (organization_id) REFERENCES organizations(id) ON DELETE CASCADE

  fk_organization_memberships_user_id_users
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE

  ck_organization_memberships_role
    CHECK (((role)::text = ANY ((ARRAY['org_admin'::character varying,
    'org_member'::character varying])::text[])))
```

### Indexes

```text
ix_organizations_slug
  CREATE UNIQUE INDEX ... (slug)

ix_users_email
  CREATE UNIQUE INDEX ... (email)

ix_organization_memberships_organization_id
  CREATE INDEX ... (organization_id)

ix_organization_memberships_user_id
  CREATE INDEX ... (user_id)

uq_organization_memberships_organization_id_user_id
  CREATE UNIQUE INDEX ... (organization_id, user_id)

pk_organizations
pk_users
pk_organization_memberships
  PRIMARY KEY backing unique indexes
```

## 6. Live behavior assertions

The live test confirmed:

- Both `org_admin` and `org_member` were accepted by the database.
- `owner` was rejected by `ck_organization_memberships_role`.
- Duplicate organization slug was rejected by the unique index.
- Duplicate user email was rejected by the unique index.
- Duplicate `(organization_id, user_id)` membership was rejected by the composite unique constraint.
- Deleting an organization cascaded to its memberships while preserving the user.
- Deleting a user cascaded to its memberships while preserving the organization.
- UUID values were native PostgreSQL UUID values.
- Timestamps returned with timezone information and UTC offset.

No migration defect was found.

## 7. Offline PostgreSQL SQL and SQLite regressions

### Existing SQLite and database tests

Command, with no live test DSN set:

```bash
unset NETOPS_TEST_DATABASE_URL
unset NETOPS_DATABASE_URL
.venv/Scripts/python.exe -m pytest tests/db -q
```

Result:

```text
64 passed, 1 skipped
```

The one skipped test was the new live PostgreSQL test because `NETOPS_TEST_DATABASE_URL` was intentionally absent in that run. The live test was executed separately and passed.

### Review follow-up offline/unit validation

After the review corrections, the following offline/unit tests were run:

```bash
unset NETOPS_TEST_DATABASE_URL
unset NETOPS_DATABASE_URL
.venv/Scripts/python.exe -m pytest \
  tests/db/test_postgres_test_support.py \
  tests/db/test_postgres_16_safety.py -q
```

Result:

```text
22 passed
```

The full database/offline suite was then rerun:

```bash
unset NETOPS_TEST_DATABASE_URL
unset NETOPS_DATABASE_URL
.venv/Scripts/python.exe -m pytest tests/db -q
```

Result:

```text
68 passed, 1 skipped
```

The skipped test remained the live PostgreSQL test because no live test DSN was set. No live PostgreSQL validation was performed in the correction task.

### Targeted SQLite, schema consistency, and offline PostgreSQL checks

```bash
.venv/Scripts/python.exe -m pytest \
  tests/db/test_migrations.py::test_alembic_offline_sql_with_asyncpg_url \
  tests/db/test_migrations.py::test_alembic_upgrade_downgrade_sqlite \
  tests/db/test_schema_consistency.py \
  tests/db/test_role_constraints.py -v
```

Result:

```text
9 passed
```

The offline SQL-generation check succeeded and confirmed:

- `organizations`, `users`, and `organization_memberships` are created.
- Native UUID SQL is emitted.
- `TIMESTAMP WITH TIME ZONE` is emitted.
- The named role CHECK is emitted.
- `ON DELETE CASCADE` is emitted.
- Expected constraint and index names are emitted.

The live test also compared the expected live constraints and indexes against generated offline SQL. No mismatch was found.

SQLite remains a schema-shape and offline test environment only. It does not prove PostgreSQL RLS or tenant isolation.

## 8. Reproducible setup and cleanup

### Setup outline

Use a newly generated suffix and ephemeral password. Do not commit or print the password.

```bash
SUFFIX=$(python -c "import secrets; print(secrets.token_hex(8))")
DB="netops_test_$SUFFIX"
DBUSER="netops_test_user_$SUFFIX"
CONTAINER="netops-pg16-821-$SUFFIX"
VOLUME="netops_pg16_821_$SUFFIX"
PASSWORD=$(python -c "import secrets; print(secrets.token_hex(24))")

# Write POSTGRES_USER, POSTGRES_PASSWORD, and POSTGRES_DB to a mode-0600 file.
# Do not place the password in the repository or print it.

docker volume create "$VOLUME"
docker run -d --name "$CONTAINER" --restart=no \
  --env-file <protected-env-file> \
  -p 127.0.0.1::5432 \
  -v "$VOLUME:/var/lib/postgresql/data" \
  --health-cmd "pg_isready -U $DBUSER -d $DB" \
  postgres@sha256:f9a7b9ebf6f56201f949db745e47bc423c0f0f8294bafe991cc10bf74b029657

PORT=$(docker port "$CONTAINER" 5432/tcp | sed -E 's/.*:([0-9]+)$/\1/')
export NETOPS_TEST_DATABASE_URL="postgresql+asyncpg://$DBUSER:$PASSWORD@127.0.0.1:$PORT/$DB"
```

Then run:

```bash
.venv/Scripts/python.exe -m pytest tests/db/test_postgres_16_live.py -q -s
```

The live test requires a fresh disposable database with no application tables.

### Cleanup outline

```bash
docker rm -f "$CONTAINER"
docker volume rm "$VOLUME"
# Remove the temporary protected environment/DSN directory.
```

The `postgres:16` image was intentionally retained.

### Cleanup verification

```text
container_removed = yes
volume_removed = yes
temporary_credentials_directory_removed = yes
postgres:16 image retained = yes
```

No unrelated container or Kubernetes resource was stopped, removed, or modified.

## 9. Files and integrity

Created during the milestone:

```text
MILESTONE_8_2_1_POSTGRES_VALIDATION.md
tests/db/postgres_test_support.py
tests/db/test_postgres_test_support.py
tests/db/test_postgres_16_live.py
tests/db/test_postgres_16_safety.py
```

The review corrections modified the two existing 8.2.1 test files:

- `tests/db/test_postgres_16_live.py`
- `tests/db/test_postgres_test_support.py`

No application source, model, migration, dependency, or existing documentation was modified.

Important hashes:

```text
alembic/versions/0001_initial_tables.py
d9ac73c579cbc5cbcfdc78d6e021718a65fa6f7420d3c3376fa6e0571bc2f592

app/db/models.py
4a8ba5ec8b80e4833df3985c4d378d264b87a4fdcea8a79b3e70f7dc4d54cd5e

app/db/session.py
be108201bb63cc2a27021ccd5ba5006937d9aafcde0589e3cd440c69e1581e97

tests/db/postgres_test_support.py
799fa1c58620feb54b5b53e2aab236e666ba0fbe815f5e379eef031aa8c7e481

tests/db/test_postgres_test_support.py
653077012c94a2a086759c2629e84d851f3d4c3bb60590428995b4ee4a0e0c6f

tests/db/test_postgres_16_live.py
540e4a3b3dad30e3e8a3312e120cb58fbefffae55a2c6d66be7926fdf34b14f0

tests/db/test_postgres_16_safety.py
f24e898de4a85d74d403ce3500e0e7aac1faa5bfc61668935bba577a44966d98
```

Migration `0001` is unchanged.

## 10. Limitations and skipped checks

- Validation used PostgreSQL `16.15`; it does not prove compatibility with every PostgreSQL 16 minor release.
- Only revision `0001` was validated. Revisions `0002` and later were not created or tested.
- The container bootstrap user was a temporary superuser credential. Production role separation, least privilege, `NOLOGIN` owner behavior, migrator/runtime separation, and `BYPASSRLS` restrictions were not tested.
- Authentication, server-side sessions, tenant context, request unit of work, onboarding, RLS, and security-definer functions remain unimplemented.
- No PostgreSQL RLS or tenant-isolation claim is made.
- Offline PostgreSQL SQL was generated and checked; it was not executed against a separate offline database.
- SQLite results validate migration shape and model consistency only.
- The live test covers one successful upgrade, one downgrade, and one re-upgrade sequence on a fresh disposable database.
- No production or existing database was contacted.
- No `.env` credentials were accessed.
- No credentials were printed or committed.

## 11. Review follow-up safety corrections

The read-only review identified one blocker and several warnings. The following corrections were applied without modifying revision `0001` or application source.

### B-1 — version gate before migration

- `_snapshot()` now calls `_assert_supported_postgres_version()` immediately after the initial identity query.
- The gate requires `160000 <= server_version_num < 170000`.
- The same gate runs after every snapshot and in `_assert_expected_schema()`.
- Unsupported versions raise `TestDatabaseSafetyError` before catalog work or any Alembic upgrade/downgrade.
- Regression tests cover PostgreSQL 15, 17, and 18 identities and assert that no Alembic command was invoked.

### W-2 — exact schema sets

Live assertions now require exact application sets for:

- Tables.
- Columns per application table.
- Constraints.
- Indexes.

Definitions for UUID, timestamptz, defaults, primary keys, unique constraints, foreign keys, CHECK constraints, and index uniqueness remain verified.

### W-5 — invalid target protection

Added isolated mock-based regression coverage proving that:

- A non-disposable DSN is rejected before any database connection or Alembic command.
- Unsupported PostgreSQL versions are rejected before migration.

### W-6 — fixture placeholder

The fake credential-shaped fixture was changed to `not-a-real-secret`. No real credential is stored.

### Validation after corrections

```text
Targeted safety and guard tests: 22 passed
Full database/offline suite:     68 passed, 1 skipped
```

The skipped test was the live PostgreSQL test because `NETOPS_TEST_DATABASE_URL` was not set. The corrected harness was not rerun live.

### Remaining warnings

- Container and volume cleanup remain manual; a failed test can leave disposable resources running.
- The live test still requires a fresh database and is not rerunnable against the same upgraded database.
- A missing live DSN still yields a skipped live test; CI must explicitly require the PostgreSQL gate.
- Live evidence remains from the earlier PostgreSQL 16.15 run; the corrected version gate and exact-set assertions have not been exercised against a live server yet.

## 12. Conclusion

Milestone 8.2.1 is complete within its approved scope.

The existing revision `0001` successfully applied to a fresh PostgreSQL 16 database, downgraded cleanly, and applied again. The expected native UUID, timestamptz, defaults, primary keys, unique constraints, indexes, foreign keys, CHECK constraint, and cascade behavior were verified live. Existing SQLite and offline PostgreSQL checks passed. The disposable container, database, volume, and temporary credentials were removed, while the `postgres:16` image was retained.

This establishes PostgreSQL 16 migration compatibility for revision `0001` only. It does not establish authentication, tenant isolation, database role separation, or RLS readiness.
