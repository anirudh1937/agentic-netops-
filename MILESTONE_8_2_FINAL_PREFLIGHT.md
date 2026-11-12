# Milestone 8.2 — Final Architecture Preflight

**Status:** Read-only architecture preflight; design and validation plan only
**Repository:** `C:\Users\Aniru\agentic-netops-old`
**Branch:** `feature/cisco-live-validation`
**Verified HEAD:** `991a4362b6298243277b01356e0c05d35087006d`
**Date:** 2026-11-10

> This report does not implement authentication, organization onboarding, tenant context, database roles, RLS, migrations, or application routes. It is the only file created by this task. Milestone 8.2.1 has not started.

---

## 1. Approved Owner Decisions

The project owner has approved the following decisions:

| Area | Approved decision |
|---|---|
| Authentication | Server-side sessions. |
| PostgreSQL | PostgreSQL 16 minimum. |
| Organization onboarding | Hybrid self-service SaaS onboarding and enterprise/platform provisioning. |
| Email and slug normalization | Canonical lowercase and trimmed values. |
| Production database | PostgreSQL for SaaS and on-premises; SQLite for development and testing only. |
| Remaining architecture | The remaining recommended architecture and security baseline in `MILESTONE_8_2_0_ARCHITECTURE_DECISIONS.md` is approved. |

Approval of the architecture does not waive live PostgreSQL validation. No RLS, tenant-isolation, or PostgreSQL compatibility claim is justified until the validation plan in section 8 succeeds.

---

## 2. Verified Current Baseline

### 2.1 Repository checkpoint

At the start of this review:

- Branch: `feature/cisco-live-validation`
- HEAD: `991a4362b6298243277b01356e0c05d35087006d`
- No staged changes.
- No tracked working-tree modifications.
- Existing untracked files:
  - `MILESTONE_8_2_0_ARCHITECTURE_DECISIONS.md`
  - `PROJECT_STATUS.backup.md`
  - `dbg.txt`
  - `tatus --short`
  - `test_results.txt`
  - `tstest_integrations.py`

`MILESTONE_8_2_FINAL_PREFLIGHT.md` did not exist before this task.

### 2.2 Current schema and migration

Revision `0001_initial_tables.py` creates:

- `organizations`
  - UUID primary key
  - Unique `slug`
  - `created_at` and `updated_at` with timezone-aware timestamps
- `users`
  - UUID primary key
  - Unique `email`
  - `password_hash`, `full_name`, `is_active`
  - No `organization_id`
- `organization_memberships`
  - UUID primary key
  - `organization_id` and `user_id`, both non-null
  - Foreign keys to organizations and users with `ON DELETE CASCADE`
  - Role CHECK: `org_admin` or `org_member`
  - `UNIQUE (organization_id, user_id)`
  - No `UNIQUE (organization_id, id)`
  - Separate indexes on `organization_id` and `user_id`

The migration is the only revision in the current chain. It is valid for the current foundation and must not be rewritten to simplify 8.2 work.

### 2.3 Current session lifecycle

`app/db/session.py` currently provides:

- A lazily created async engine from `NETOPS_DATABASE_URL`.
- A lazily created async session factory.
- `init_db()` that creates the engine/factory without connecting or creating tables.
- `create_db_and_tables()` for development/testing only.
- `dispose_db()` that disposes the engine and resets module state.
- `get_async_session()` that yields an `AsyncSession` and closes it.

`get_async_session()` does not:

- Begin an explicit request transaction.
- Set tenant context.
- Commit or roll back on its own.
- Distinguish auth/bootstrap from tenant requests.
- Enforce organization membership.

FastAPI lifespan initializes database resources only when `NETOPS_DATABASE_URL` is configured. Startup does not connect or create tables; shutdown disposes the engine. Existing telemetry, poller, and MCP cleanup behavior is preserved.

### 2.4 Current tests

The existing tests cover:

- SQLite migration upgrade/downgrade.
- SQLite schema/model consistency.
- Role CHECK enforcement in SQLite.
- Engine and session-factory lifecycle.
- FastAPI lifespan initialization and disposal.
- Offline PostgreSQL SQL generation.
- PostgreSQL async engine construction without connecting.
- Current model constraints and relationships.

They do not cover:

- Live PostgreSQL execution.
- PostgreSQL RLS.
- Tenant context or pooled-connection isolation.
- Database role privileges.
- Authentication, sessions, or organization selection.
- Organization creation or bootstrap functions.
- Composite membership foreign keys.

SQLite tests remain useful for schema, model, and migration-shape validation. They must not be presented as proof of PostgreSQL RLS behavior.

### 2.5 Current gaps

The following remain unimplemented:

- Authentication and global sessions.
- Organization selection and switching.
- First-organization/onboarding flow.
- Tenant context.
- RLS policies.
- Migration, runtime, auth, and maintenance role separation.
- `UNIQUE (organization_id, id)` on memberships.
- Live PostgreSQL migration and privilege validation.

---

## 3. Resolved Technical Design Decisions

### 3.1 Canonical tenant-context helper

#### Canonical helper

Use one canonical SQL function:

```sql
app.current_org_id()
```

The GUC key remains:

```text
app.current_organization_id
```

The function is owned by `netops_owner`, is `STABLE`, is `SECURITY INVOKER`, uses a fixed `search_path`, and is revoked from `PUBLIC`. It returns the UUID value of the transaction-local GUC or NULL.

Illustrative definition:

```sql
CREATE FUNCTION app.current_org_id()
RETURNS uuid
LANGUAGE sql
STABLE
SET search_path = pg_catalog
AS $$
    SELECT NULLIF(
        btrim(current_setting('app.current_organization_id', true)),
        ''
    )::uuid
$$;

REVOKE ALL ON FUNCTION app.current_org_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.current_org_id()
    TO netops_runtime, netops_auth, netops_maintenance;
```

The exact grant statement belongs in the environment grant bootstrap, after the roles and function exist.

A parallel helper may be used for trusted user context during authentication/bootstrap:

```sql
app.current_user_id()
```

It reads `app.current_user_id` with the same transactional semantics. It must not be used as a substitute for server-side session validation.

#### Setting context

Set context inside an explicit transaction, using a parameterized call:

```sql
SELECT set_config(
    'app.current_organization_id',
    :organization_id::text,
    true
);
```

Rules:

- The application must never use session-level `SET`.
- Context must be set after authentication and membership authorization.
- Context must be set before the first tenant query.
- Context must be set inside the same transaction that performs tenant reads/writes.
- No `COALESCE` or default organization may substitute for missing context.
- The auth/bootstrap path does not set tenant context and does not access tenant-owned tables.

#### Missing, empty, and malformed behavior

| Context state | Behavior |
|---|---|
| Missing | `current_setting(..., true)` returns NULL; helper returns NULL; RLS predicates are not true; reads return no tenant rows; `WITH CHECK` rejects writes. |
| Empty or whitespace-only | `btrim` produces an empty string; `NULLIF` returns NULL; reads return no rows; writes fail. |
| Malformed UUID text | The `::uuid` cast raises an error; the transaction fails closed. No tenant data is returned. |
| Syntactically valid but unauthorized organization | Membership authorization must reject before setting context. If a bug sets it anyway, RLS restricts access to that organization, but application authorization still must fail. |

A malformed value causing a query error is acceptable and preferred over silently falling back to another tenant.

#### Pool safety

The only safe lifecycle is:

```text
BEGIN
  authenticate/authorize
  set_config('app.current_organization_id', ..., true)
  tenant queries
  COMMIT or ROLLBACK
END
pooled connection released with no tenant context
```

Required tests:

- Missing context returns zero tenant rows.
- Empty context returns zero tenant rows.
- Malformed context fails closed.
- Context resets after commit.
- Context resets after rollback.
- Context resets after cancellation/connection release.
- `pool_size=1` sequential requests for A then B cannot see A's context.
- No session-level GUC remains after release.
- Auth/bootstrap requests cannot read tenant tables.

#### Tenant Context and the Runtime SQL Trust Boundary

Transaction-local tenant context is a query-scoping mechanism, not an authentication credential or an unforgeable authorization token. The trust boundary must be stated explicitly so that RLS is not described as stronger than it is.

**What transaction-local context protects against**

- `set_config('app.current_organization_id', ..., true)` sets the value only for the current transaction.
- Commit or rollback clears the setting, so a correctly used pooled connection does not carry the previous request's tenant into the next transaction.
- Missing context is designed to fail closed: tenant rows are not visible and writes fail their `WITH CHECK` predicates.
- It helps prevent accidental cross-tenant access caused by a missing tenant filter, an incorrectly scoped ORM query, or a newly written query that forgets the active organization.

**What it does not protect against**

A custom PostgreSQL setting is caller-settable. A role with permission to execute arbitrary SQL as `netops_runtime` can generally call `set_config()` and attempt to choose another tenant context value. Transaction-local scope limits persistence; it does not make the value trustworthy.

Therefore:

- Transaction-local context alone does not protect against arbitrary SQL execution under a compromised runtime credential.
- RLS policies based only on this caller-settable context must not be described as independently preventing a malicious runtime SQL caller from selecting another tenant.
- `FORCE ROW LEVEL SECURITY` does not make a caller-settable GUC trustworthy. It addresses owner bypass behavior, not the trustworthiness of the context value.
- RLS remains valuable for enforcing tenant scoping against accidental query mistakes and for limiting access when context is established through trusted application logic, but that trust assumption must be explicit.

**Required application controls**

- Use parameterized SQL and ORM-bound parameters.
- Never interpolate or concatenate untrusted input into SQL.
- Apply strict SQL-injection prevention and input validation.
- Do not provide arbitrary SQL or user-controlled query execution.
- Use one request-owned transaction and one commit boundary.
- Validate tenant membership before setting tenant context.
- Set transaction-local context only after authorization.
- Grant minimal runtime database privileges.
- Keep authentication, migration, runtime, and maintenance credentials separate.
- Test missing context, cross-tenant reads/writes, pooled-connection reuse, and SQL injection at the application boundary.

**Stronger threat model**

If the product requirement is to protect against an attacker who already has arbitrary SQL execution under the runtime credential, the current caller-settable GUC design is insufficient by itself. A database-verifiable authorization mechanism must be evaluated before making that security claim. Possible directions include narrowly scoped database functions and a context-establishment mechanism that the runtime role cannot forge.

No such stronger mechanism is implemented or approved by this report. This remains a security boundary and a future design decision; it does not authorize a redesign in this task.

**Accurate customer-facing security language**

The planned RLS design provides database-enforced tenant filtering under the trusted application-context model. Transaction-local context helps prevent context leakage between pooled requests. Protection against arbitrary SQL execution using compromised runtime credentials requires additional controls and cannot be claimed from the current design alone.

Tenant isolation is not yet implemented or validated, and this limitation does not change the approved 8.2.1 scope or the milestone sequence.

---

### 3.2 Safe first-organization creation

#### Least-privilege onboarding path

`netops_runtime` must not receive broad `INSERT`, `UPDATE`, or `DELETE` privileges on `organizations`. Self-service organization creation and first-administrator creation should be performed by a narrowly scoped security-definer function invoked through the authenticated bootstrap path.

Illustrative function:

```sql
app.create_organization_for_current_user(
    p_name text,
    p_slug text
)
```

Properties:

- Owned by `netops_owner`.
- `SECURITY DEFINER`.
- Fixed `search_path` using schema-qualified objects.
- Execute granted only to `netops_auth`.
- Revoked from `PUBLIC` and not executable by `netops_runtime`.
- Accepts only a display name and canonical slug.
- Accepts no user ID, role, or organization ID from the client.
- Reads the trusted user ID from transaction-local `app.current_user_id()`.
- Verifies the user exists and is active.
- Generates the organization UUID server-side.
- Inserts the organization and the first `org_admin` membership atomically.
- Forces the initial membership role to `org_admin`.
- Returns the created organization and membership IDs.
- Rolls back entirely if either insert fails.

The function must set the new organization ID as transaction-local context before inserting into `organizations` and `organization_memberships`, so the RLS `WITH CHECK` policies are satisfied. It must not disable RLS or use `BYPASSRLS`.

The function is owned by `netops_owner`, which is non-login. Because tenant tables are owned by `netops_owner` and use `FORCE ROW LEVEL SECURITY`, the function's inserts are subject to the applicable policies. Its temporary tenant context is limited to the transaction and disappears at commit/rollback.

#### Required checks

- Slug is canonical lowercase and trimmed before insertion.
- Slug uniqueness is enforced by the database, including case-insensitive uniqueness if approved.
- The initial role is always `org_admin`, never client-selected.
- A user cannot create the first membership for another user.
- The organization and membership are created in one transaction.
- Duplicate or unavailable slugs produce a controlled application error.
- Self-service rate limits and abuse controls are applied in the application before calling the function.
- Enterprise/platform provisioning uses an operator/admin path outside the runtime role; it does not share runtime credentials.
- No direct organization-table write privilege is granted to `netops_runtime`.

#### Why this is least privilege

- Runtime can read organization rows under RLS but cannot create or modify them directly.
- Auth has only function execution for onboarding, not broad organization DML.
- The function forces the initial administrator role.
- The owner role is non-login.
- The operation is atomic and auditable.

#### Later organization administration

Organization name/slug updates, deletion, ownership transfer, and membership management should use separate controlled functions after the first-organization flow is validated. They must not become broad table grants to runtime.

---

### 3.3 Database roles, ownership, and privileges

#### Role model

| Role | Login | Ownership and privileges |
|---|---:|---|
| `netops_owner` | No | Non-login owner of application schema objects, tables, functions, and policies. No superuser, no `BYPASSRLS`, no `CREATEROLE`, no `CREATEDB`. |
| `netops_migrator` | Yes | Login role used only by Alembic/deployment migrations. Member of `netops_owner` only. No app connection, no superuser, no `BYPASSRLS`, no `CREATEROLE`, no `CREATEDB`. |
| `netops_runtime` | Yes | Application tenant role. Non-owner, no superuser, no `BYPASSRLS`, no DDL, no `TRUNCATE`, no membership in owner/auth/migrator/maintenance. |
| `netops_auth` | Yes | Narrow authentication/session/bootstrap role. Non-owner, no superuser, no `BYPASSRLS`, no DDL, no tenant operational-table access. |
| `netops_maintenance` | Yes, job use only | Separate retention/maintenance role. Non-owner, no superuser, no `BYPASSRLS` by default, no interactive application login, no broad tenant access. |

#### `netops_owner`

- Created as `NOLOGIN`.
- Owns the application schema and objects.
- Does not have a password or interactive connection.
- Is not a superuser.
- Is not granted `BYPASSRLS`.
- Can own objects even though application roles cannot log in as it.
- Its ownership does not create an application bypass because RLS is forced and runtime is not a member.

#### `netops_migrator`

- Login is used only by the deployment/migration pipeline.
- Membership in `netops_owner` is the only privileged membership it receives.
- Migration connections set the owner role for DDL creation so objects are owned by `netops_owner`, for example through migration connection bootstrap or `SET ROLE netops_owner` in the migration context.
- It has no application runtime use.
- It must not be a member of `netops_runtime`, `netops_auth`, or `netops_maintenance`.
- It may read migration metadata and perform DDL; it does not need broad tenant-data access for schema migrations.
- It should not have `BYPASSRLS` by default.

#### `netops_runtime`

- Login used by tenant application requests.
- Non-owner, non-superuser, no `BYPASSRLS`, no `CREATEROLE`, no `CREATEDB`, no replication.
- `USAGE` on the application schema(s).
- `EXECUTE` on tenant helper functions such as `app.current_org_id()`.
- `SELECT` on `organizations`, subject to RLS.
- `SELECT`, `INSERT`, `UPDATE`, `DELETE` on `organization_memberships` only when membership-management routes are implemented, subject to RLS and application authorization.
- No direct `INSERT`, `UPDATE`, or `DELETE` on `organizations`.
- No `SELECT` on `users` or `sessions`.
- No execute on the onboarding security-definer function.
- No `TRUNCATE`.
- No DDL.
- Not a table owner.

The final grant set should be minimal for current routes. If no membership-management route exists yet, do not grant membership DML merely because it may be needed later.

#### `netops_auth`

Permitted operations:

- `SELECT` only the required columns from `users` for authentication:
  - `id`
  - `email`
  - `password_hash`
  - `is_active`
  - timestamps needed for account policy
- `INSERT`, `SELECT`, `UPDATE`, and `DELETE` on `sessions` for creation, validation, expiry, and revocation.
- `SELECT` on organization memberships only through a controlled bootstrap function; avoid direct broad membership reads if practical.
- `EXECUTE` on:
  - `app.current_user_id()`
  - the session/auth functions needed by the auth service
  - `app.create_organization_for_current_user(...)`
- `USAGE` on the application schema.
- No tenant operational-table access.
- No direct `organizations` DML.
- No DDL, `TRUNCATE`, superuser, or `BYPASSRLS`.
- No `netops_owner` membership.

#### `netops_maintenance`

- Login only for scheduled jobs, never interactive application sessions.
- No superuser, no `BYPASSRLS` by default, no DDL, no `TRUNCATE`.
- `USAGE` on the application schema.
- `SELECT`/`UPDATE`/`DELETE` only on tables required by approved retention/archive jobs.
- `EXECUTE` only on approved maintenance functions.
- Per-tenant context by default.
- No `users` or `sessions` access unless a separately approved maintenance function requires a narrow field.
- No app-callable maintenance function.
- Cross-tenant maintenance, if ever required, must use a separately approved security-definer function with narrow inputs, separate credentials, and audit logging.

#### `PUBLIC` and default privileges

- Revoke `CREATE` on application schemas from `PUBLIC`.
- Revoke execute on security-definer functions from `PUBLIC`.
- Do not grant broad default privileges to runtime.
- Future tables must be explicitly granted and reviewed; they must not be automatically exposed through broad default privileges.
- `alembic_version` is migration metadata and is not granted to runtime.
- `SET ROLE` is forbidden for application request handling; it is permitted only in the tightly controlled migration/bootstrap context.

#### Role provisioning by environment

| Environment | Provisioning approach |
|---|---|
| Local development | Disposable PostgreSQL 16 instance or container. Create ephemeral roles with generated local passwords. Never commit passwords. Drop or recreate the environment freely. |
| CI | Ephemeral PostgreSQL 16 service/container per job. Provision roles and grants through an idempotent test/bootstrap script. Credentials come from CI secret storage, never repository files. |
| Production | Roles provisioned by the deployment/DBA pipeline. Secrets are stored in the managed secret store and rotated. Runtime, auth, migration, and maintenance use separate connection strings. `netops_owner` remains non-login. Migration credentials never reach the application process. |

Production credentials must never be committed in `.env` files, SQL files, documentation, test fixtures, or repository configuration.

---

### 3.4 Global authentication and session design

#### Authentication path

1. User submits credentials or completes the future external identity flow.
2. Auth service looks up the canonical user by normalized email or identity-provider subject.
3. Auth service verifies the password/credential and checks `is_active`.
4. On success, create a session and issue an opaque token in a secure cookie.
5. Store only a hash of the session token, never the raw token.
6. On each request, hash the presented token, look up the session, and verify:
   - session exists,
   - not revoked,
   - not expired,
   - associated user is active,
   - active organization, if present, is still authorized by membership.
7. Before an organization is selected, no tenant context is set.
8. On organization selection, validate membership and update the server-side active organization.
9. Set tenant context only for the authorized tenant transaction.

#### Session lifecycle

- **Create:** after successful authentication; cryptographically random token; token hash stored; expiry set; secure cookie issued.
- **Validate:** hash lookup; expiry check; revocation check; user-active check; membership check if an active organization is present.
- **Expire:** absolute expiration and, if configured, idle expiration; expired sessions are rejected and cleaned up.
- **Revoke:** logout, password change, account deactivation, membership removal, suspected compromise, and administrative action.
- **Rotate:** rotate session token after authentication or privilege changes if the session design requires it.
- **Switch organization:** validate membership first and update the server-side active organization; never trust a client-provided organization as authorization.

#### Session table

A global `sessions` table is recommended and approved through the baseline architecture:

- `id` UUID primary key.
- `user_id` UUID, non-null, references `users.id`.
- `token_hash` unique, non-null; no raw token.
- `active_organization_id` nullable, references `organizations.id`; context, not ownership.
- `created_at`, `last_seen_at`, `expires_at`, `revoked_at`.
- Optional minimal metadata for abuse detection, not unnecessary personal data.
- Sessions are not tenant-owned and do not receive a tenant RLS policy.
- The active organization reference does not grant tenant access by itself.

#### Global-user restrictions

`netops_runtime` cannot:

- `SELECT` from `users`.
- `SELECT` from `sessions`.
- Enumerate users through auth functions.
- Execute broad bootstrap functions.

`netops_auth` is the only application path that can read the required user and session fields. It must expose no user-list endpoint and must return generic authentication errors to avoid account enumeration. Rate limiting and secure error handling are required.

---

### 3.5 Migration and schema-change order

The existing `0001` revision must not be rewritten. New work uses forward revisions.

#### Proposed revision sequence

| Revision | Purpose | Milestone | Notes |
|---|---|---|---|
| `0002` | Add `UNIQUE (organization_id, id)` to `organization_memberships`; retain `(organization_id, user_id)`. | 8.2.2 | Reversible; no existing data loss. |
| `0003` | Create application schema and canonical helper functions, including `app.current_org_id()` and `app.current_user_id()`. | 8.2.4 | Functions are schema objects; grants are environment bootstrap. |
| `0004` | Create global `sessions` table and indexes. | 8.2.5 | No tenant RLS; auth-owned path. |
| `0005` | Enable/force RLS and create policies for `organizations` and `organization_memberships`. | 8.2.6 | Policies reference the canonical helper. |
| `0006` | Create security-definer authentication/onboarding functions, including first-organization creation. | 8.2.6 | Only after policies are defined; grants via bootstrap. |

The exact revision numbers may be finalized when implementation begins, but the dependency order must not change:

```text
0001 foundation
  -> membership composite key
  -> tenant helper functions
  -> sessions table
  -> RLS policies
  -> security-definer auth/onboarding functions
```

#### Downgrade order

Reverse dependency order:

1. Drop or revoke security-definer functions.
2. Drop RLS policies; disable RLS where required before table changes.
3. Drop sessions.
4. Drop helper functions and application schema if safe.
5. Drop the membership composite key.
6. Leave `0001` unchanged.

Role creation and credentials are outside Alembic, so downgrade must not attempt to drop roles automatically.

#### Role/bootstrap operations outside Alembic

These are environment operations, not schema migrations:

- Database creation.
- Role creation and attributes.
- Role memberships.
- Passwords and credentials.
- Grants and revokes.
- Default privileges.
- `SET ROLE` configuration for migration connections.
- `pg_hba.conf` and connection security.
- Secret-store integration and rotation.

These operations may be scripted idempotently, but production credentials must not be committed. Schema objects, constraints, functions, indexes, and RLS policies belong in Alembic revisions.

#### Initial migration integrity

- Do not modify `0001_initial_tables.py`.
- Do not squash revisions to simplify the sequence.
- Do not change the existing role CHECK or UUID behavior.
- Preserve SQLite migration-shape tests and add PostgreSQL live tests separately.
- Keep offline PostgreSQL SQL generation in the validation loop.

---

### 3.6 PostgreSQL 16 validation design

#### Disposable environment

Use a disposable local PostgreSQL 16 instance, such as a temporary container or isolated local cluster.

Requirements:

- Explicit test connection DSN; do not use application or production database credentials.
- Database name must be clearly disposable, for example `netops_test_<random>`.
- Test harness must refuse to run against a database name without the approved test prefix.
- Do not read `.env`; pass test configuration explicitly.
- Roles and passwords are ephemeral; do not print them.
- Destroy the database/container after validation.
- No existing PostgreSQL database may be used.

#### Required validation sequence

1. Start PostgreSQL 16.
2. Record `SELECT version()` and `server_version_num`.
3. Create the disposable database.
4. Create ephemeral test migration/owner credentials sufficient to apply `0001`.
5. Run `alembic upgrade head` against a fresh database.
6. Inspect the schema and constraints.
7. Run `alembic downgrade base`.
8. Confirm all application tables are removed.
9. Run `alembic upgrade head` again.
10. Verify offline SQL and online schema agree.
11. Destroy the environment.

#### Baseline live checks for 8.2.1

- Native PostgreSQL `UUID` columns.
- `TIMESTAMP WITH TIME ZONE` columns and UTC behavior.
- `now()` server defaults.
- Exact `ck_organization_memberships_role` CHECK name and rejection of invalid roles.
- Foreign keys and `ON DELETE CASCADE`.
- Existing `UNIQUE (organization_id, user_id)`.
- Current indexes and primary keys.
- Migration upgrade/downgrade reversibility.
- No RLS, tenant context, or 8.2 tables yet.

#### Later security validation

After later milestones, add tests for:

- Membership composite key and same-tenant/cross-tenant composite FK behavior.
- Role attributes and privileges:
  - `netops_owner` is `NOLOGIN`.
  - `netops_runtime` is not owner, not superuser, not `BYPASSRLS`.
  - Runtime cannot create/alter/drop/truncate.
  - Runtime cannot read `users` or `sessions`.
  - Runtime cannot execute onboarding/maintenance functions not granted.
  - Migrator can run migrations; runtime cannot.
- Tenant context:
  - Missing, empty, malformed, and unauthorized context.
  - Context resets after commit, rollback, and cancellation.
  - Pooled connection reuse with `pool_size=1`.
- Cross-tenant behavior:
  - Read denial.
  - Insert denial.
  - Update denial.
  - Delete denial.
  - `UPDATE ... WHERE` affecting zero rows or failing.
- Auth/session behavior:
  - Session creation, validation, expiry, revocation.
  - Membership removal invalidates access.
  - No tenant context before organization selection.
  - Bootstrap returns only the authenticated user's permitted organizations.
- Organization onboarding:
  - First organization and `org_admin` membership created atomically.
  - Duplicate slug rejected.
  - Client cannot choose role or user.
  - Runtime direct organization write denied.
  - Function cannot be executed by runtime or `PUBLIC`.
- RLS:
  - `ENABLE` and `FORCE ROW LEVEL SECURITY`.
  - `USING` and `WITH CHECK` exist.
  - Owner/superuser/BYPASSRLS behavior is documented and tested.
  - Missing context fails closed.

#### SQLite policy

SQLite tests remain for:

- Model metadata.
- Migration Python validity.
- SQLite schema shape.
- Role CHECK enforcement.
- Session lifecycle without a database connection.
- Offline test behavior.

SQLite does not prove PostgreSQL RLS, role attributes, `set_config`, or pooled-connection behavior. No SQLite result may be used to claim PostgreSQL tenant isolation.

---

## 4. Proposed Implementation Sequence

| Order | Milestone | Scope | Required decisions | Acceptance |
|---:|---|---|---|---|
| 1 | **8.2.1** | Disposable PostgreSQL 16 harness and live validation of existing `0001`. | PostgreSQL 16, branch/HEAD safety, test DSN policy. | Fresh PG16 database; `0001` upgrade/downgrade/upgrade; UUID/timestamptz/CHECK verified; no schema changes; SQLite tests preserved. |
| 2 | **8.2.2** | Membership composite key migration. | Membership unique key approval. | Reversible `0002`; `UNIQUE (organization_id, id)` present; existing membership uniqueness retained; live and offline SQL agree. |
| 3 | **8.2.3** | Role provisioning and grants. | Role ownership model, no shared credentials, maintenance no-bypass default. | Five roles exist with intended attributes; runtime denied DDL/BYPASSRLS; migrator works; auth/maintenance narrow; local/CI/prod scripts documented without secrets. |
| 4 | **8.2.4** | Tenant context and request unit of work. | Canonical helper; transaction ownership. | `app.current_org_id()` behavior proven; missing/empty/malformed deny; commit/rollback/cancellation; pool isolation; no repository commits. |
| 5 | **8.2.5** | Sessions, authentication, organization selection. | Server-side sessions, session storage, global-user access path, normalization. | Login/session create/validate/expire/revoke; bootstrap only own organizations; no tenant context before selection; runtime cannot enumerate users. |
| 6 | **8.2.6** | RLS and onboarding functions. | RLS policy strategy, onboarding path, role semantics. | RLS on organizations/memberships; first org/admin atomic; runtime cannot write organizations; cross-tenant and missing-context denial live. |
| 7 | **8.2.7** | Full live PostgreSQL security suite and CI gate. | All prior decisions implemented. | Role, RLS, context, pool, session, onboarding, composite-key, and migration tests pass in one disposable environment. |
| 8 | **8.2.8** | Future tenant-table guardrails, audit, and SaaS/on-prem parity. | Audit/membership references, future table scope, on-prem packaging. | Guardrail tests; audit retention decision documented; no unapproved operational tables; deployment guidance. |

### Dependencies

- 8.2.1 must finish before any later live PostgreSQL claim.
- 8.2.2 depends on 8.2.1 and the approved membership-key decision.
- 8.2.3 depends on approved role model and 8.2.1 harness.
- 8.2.4 depends on 8.2.3 for grants and on the canonical helper design.
- 8.2.5 depends on 8.2.3 and 8.2.4.
- 8.2.6 depends on 8.2.4, 8.2.5, and 8.2.3.
- 8.2.7 depends on 8.2.6 and must not be reduced to SQLite-only tests.
- 8.2.8 depends on the earlier foundation and owner decisions about audit/retention and on-premises packaging.

---

## 5. 8.2.1 Scope and Acceptance Criteria

### 5.1 Scope

8.2.1 is limited to:

- A disposable PostgreSQL 16 test environment.
- A guard that rejects non-disposable database names.
- Explicit test DSN configuration without `.env` access.
- Live validation of the existing `0001` migration foundation.
- Live upgrade, downgrade, and re-upgrade.
- UUID, timestamptz, CHECK, foreign-key, unique, and index verification.
- Capture of PostgreSQL version and actual schema evidence.
- Preservation and continued execution of existing SQLite tests.

### 5.2 Explicitly out of scope

8.2.1 must not include:

- Changing revision `0001`.
- Adding revision `0002` or later.
- Membership composite-key changes.
- Production role provisioning or grants.
- Tenant context helpers.
- Application transaction/unit-of-work code.
- Sessions, authentication, or organization selection.
- RLS policies.
- Organization onboarding functions.
- Application route changes.
- Any claim that RLS or multi-tenant isolation is implemented.

A minimal ephemeral database/migration credential may be created solely to run `0001` in the disposable environment. Full five-role provisioning and privilege testing belong to 8.2.3.

### 5.3 Acceptance criteria

8.2.1 is accepted only when:

1. Branch and HEAD match the approved checkpoint.
2. The six existing untracked artifacts remain untouched.
3. A fresh disposable PostgreSQL 16 database can be created and destroyed.
4. The harness refuses to target a non-disposable database name.
5. `alembic upgrade head` succeeds live.
6. `alembic downgrade base` succeeds live.
7. A second `alembic upgrade head` succeeds live.
8. Native UUID and timestamptz behavior is verified.
9. The exact role CHECK, foreign keys, unique keys, and indexes are verified.
10. Offline PostgreSQL SQL remains consistent with the live schema.
11. Existing SQLite baseline tests still pass.
12. No application code, model, existing test, migration, dependency, or existing documentation is modified.
13. No production or existing PostgreSQL database is contacted.
14. No `.env` credentials are accessed or printed.
15. Test output records PostgreSQL version, database identity, and any skipped checks.

8.2.1 success establishes only that the existing foundation runs on PostgreSQL 16. It does not establish RLS readiness.

---

## 6. Remaining Risks and Owner Input

### 6.1 Risks accepted until later validation

| Risk | Current status | Required treatment |
|---|---|---|
| No live PostgreSQL validation | Unvalidated | 8.2.1 live upgrade/downgrade and schema checks. |
| No RLS implementation | Not implemented | 8.2.6 policies and 8.2.7 live isolation suite. |
| Runtime role not yet provisioned | Not implemented | 8.2.3 role/privilege tests. |
| `FORCE ROW LEVEL SECURITY` does not stop superusers or `BYPASSRLS` roles | Design constraint | Keep runtime non-owner, non-superuser, no `BYPASSRLS`; test attributes. |
| Security-definer functions can bypass RLS if misconfigured | Design risk | Fixed search path, schema-qualified objects, owner non-login, revoke `PUBLIC`, narrow grants, live tests. |
| Malformed tenant context causes query failure | Intended fail-closed behavior | Test and normalize error handling. |
| Pooled connection context leakage | Not yet tested | `set_config(..., true)`, transaction ownership, pool-size-1 tests. |
| Session cookie/CSRF/session fixation | Not yet implemented | Secure cookie settings, CSRF protection, rotation, revocation. |
| Organization onboarding abuse/slug squatting | Not yet implemented | Rate limits, normalization, reserved slugs, duplicate handling. |
| Case-insensitive uniqueness requires indexes/normalization | Not yet implemented | Decide exact normalization and add database-enforced uniqueness. |
| Maintenance without bypass may complicate retention | Design constraint | Per-tenant jobs; any exception requires owner approval and audit. |
| SQLite cannot prove RLS | Known limitation | Keep SQLite for shape tests; require PostgreSQL for isolation claims. |
| Migration downgrade may not unwind role grants | Design constraint | Keep roles/grants outside Alembic; downgrade schema only. |
| Existing low-risk observations remain | Low priority | UUID annotations vs runtime UUID values; redundant membership organization index; weak missing-URL migration test. |

### 6.2 Decisions requiring owner/product input

These are not implementation blockers for 8.2.1 but must be settled before the relevant later milestone:

1. **Self-service onboarding controls**
   - Maximum organizations per user.
   - Email verification requirement.
   - Rate limits and abuse handling.
   - Reserved slugs and slug-claim policy.
   - Whether enterprise tenant creation is operator-only or admin-service mediated.

2. **Session policy**
   - Session absolute and idle lifetimes.
   - Cookie `SameSite` policy and CSRF strategy.
   - Session rotation rules.
   - MFA and external identity-provider timing.
   - Password-reset and forced-logout behavior.

3. **Organization administration**
   - Can `org_admin` rename or change the organization slug?
   - Can an organization be deleted, archived, or transferred?
   - Who can invite/remove members or change roles?
   - Whether a member can leave voluntarily.

4. **Email canonicalization details**
   - Lowercase the entire address versus only the domain.
   - Unicode normalization and alias handling.
   - Duplicate-email response policy and support workflow.

5. **Audit and retention**
   - Legal/retention requirements.
   - Whether audit records need membership references.
   - Whether platform audit is required separately.
   - Cross-tenant maintenance exception, if any.

6. **On-premises packaging**
   - Customer-managed PostgreSQL 16 support boundary.
   - Backup/restore and maintenance role provisioning.
   - Whether any SQLite deployment is supported at all; if so, it must not be represented as RLS-equivalent.

7. **Production operations**
   - Secret manager and rotation ownership.
   - CI PostgreSQL 16 availability and credential storage.
   - Migration pipeline ownership and approval process.

---

## 7. Resolved Contradictions and Dependencies

The preflight resolves the following issues identified in the architecture report:

1. **Canonical tenant helper selected.**
   The report mixed `current_org_id()` and direct `current_setting(...)`. The canonical design is `app.current_org_id()`, with fixed missing/empty/malformed behavior.

2. **Owner/migrator ambiguity resolved.**
   `netops_owner` is a non-login owner role. `netops_migrator` is a login role with only the required membership in `netops_owner` for migrations. Runtime is never a member of either.

3. **`FORCE RLS` limitation made explicit.**
   `FORCE` makes owners subject to RLS; it does not stop superusers or `BYPASSRLS` roles. Runtime must be non-owner, non-superuser, and no `BYPASSRLS`.

4. **Global users are not handled by tenant RLS.**
   `users` remains global. Authentication/bootstrap uses `netops_auth` and controlled functions, not a normal tenant policy.

5. **Organization creation is not a runtime table grant.**
   It is a controlled security-definer function executable only by the auth path. Runtime gets no broad organization-table writes.

6. **8.2.1 scope is narrowed.**
   8.2.1 is only the disposable PostgreSQL 16 harness and live validation of `0001`. Membership, roles, context, sessions, and RLS are separate reviewable milestones.

7. **Migration order is explicit.**
   New revisions are forward-only from `0001`; role/bootstrap operations remain outside Alembic; downgrade reverses schema objects but does not manage roles.

8. **SQLite validation boundary is explicit.**
   SQLite remains for model/migration-shape tests. PostgreSQL RLS claims require live PostgreSQL 16 tests.

### Pre-existing low-risk observations

These are not 8.2 blockers but should remain visible:

- `app/db/models.py` annotates IDs as `Mapped[str]` while `GUID` returns `uuid.UUID` at runtime. This is a low-priority type-contract issue.
- `ix_organization_memberships_organization_id` duplicates the leading column of the existing membership unique key and may become more redundant after adding `(organization_id, id)`. Do not remove it without measured query-plan review.
- Email and slug uniqueness are currently case-sensitive. The approved normalization decision requires explicit lowercase/canonical enforcement, likely through normalized values and functional/or case-insensitive unique indexes.
- The current missing-URL migration test is weak and should be strengthened during the live PostgreSQL work.

---

## 8. Validation and Evidence Requirements

For every later implementation milestone, preserve evidence of:

- PostgreSQL server version.
- Database name and disposable-environment guard result.
- Revision IDs applied and rolled back.
- Actual role names, attributes, and relevant grants.
- Actual policy definitions and RLS state.
- Test command and result summary.
- Any skipped tests and why.
- Confirmation that no production or existing database was contacted.
- Confirmation that no credentials were printed or committed.

No PostgreSQL or RLS compatibility claim may be made from SQLite results alone.

---

## 9. Implementation Status

**No implementation was performed in this task.**

Confirmed:

- No application source file was modified.
- No model was modified.
- No test was modified.
- No migration was created or modified.
- No database role or grant was created.
- No RLS policy or tenant-context function was created.
- No authentication or session code was created.
- No PostgreSQL, Cisco, Gemini, DGX, or other external service was contacted.
- No `.env` file was opened or printed.
- No file was staged, committed, or pushed.
- No branch, HEAD, reset, rebase, merge, or pull operation was performed.
- Milestone 8.2.1 has not started.

**Awaiting approval to begin 8.2.1 only.**
