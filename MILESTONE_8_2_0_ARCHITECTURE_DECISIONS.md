# Milestone 8.2.0 — Multi-Tenant Isolation Architecture and Security Decisions

**Status:** Architecture and documentation only  
**Repository:** `C:\Users\Aniru\agentic-netops-old`  
**Branch:** `feature/cisco-live-validation`  
**Verified HEAD:** `991a4362b6298243277b01356e0c05d35087006d`  
**Date:** 2026-11-10

> This document is a design proposal. It does not implement authentication, RLS, tenant context, database roles, application routes, or migrations. It does not claim that live PostgreSQL validation has occurred. It is the only file created by this milestone.

---

## 1. Executive Summary

Milestone 8.1 delivered a small, internally consistent database foundation:

- Global UUID primary keys.
- Global users.
- Multi-organization memberships.
- An async SQLAlchemy engine and session factory.
- Conditional FastAPI lifespan initialization and disposal.
- Alembic migration and offline PostgreSQL SQL support.
- Database tests that pass against SQLite and offline DDL.

Milestone 8.1 did **not** deliver multi-tenant security. There is currently no authentication flow, no organization-selection flow, no tenant context, no RLS policy, no runtime database role, no migration role, and no live PostgreSQL validation.

The recommended architecture is:

> **Global users + tenant-scoped memberships + server-side authenticated sessions + explicit organization authorization + transaction-local tenant context + non-owner runtime roles + composite tenant keys + RLS defense in depth.**

The key decisions are:

| Area | Recommended decision |
|---|---|
| Users | Keep `users` global. Do not add `organization_id` to users. |
| Membership | Keep `organization_memberships` as the source of tenant membership and role. |
| Membership key | Add `UNIQUE (organization_id, id)` in the next migration, while retaining `UNIQUE (organization_id, user_id)`. |
| Authentication | Authenticate globally through a dedicated auth path; never let the tenant runtime role read arbitrary users. |
| Organization selection | Server-side session stores the active organization; validate membership before setting tenant context. |
| Tenant context | Set `app.current_organization_id` with `set_config(..., true)` inside the request transaction. |
| Transaction ownership | The request/unit-of-work layer owns begin, commit, and rollback. Repositories do not commit. |
| Runtime role | Non-owner, non-superuser, no `BYPASSRLS`, no DDL, no `TRUNCATE`. |
| Migration role | Separate owner/DDL role used only for migrations. |
| RLS | `ENABLE` and `FORCE ROW LEVEL SECURITY` on tenant-owned tables, with `USING` and `WITH CHECK`. |
| Composite keys | Mandatory `organization_id` on tenant-owned tables; `UNIQUE (organization_id, id)` on composite FK targets; composite tenant FKs. |
| PostgreSQL validation | Required before claiming compatibility; no live validation has been performed yet. |

**Readiness verdict:** the architecture is ready for review and approval. Milestone 8.2 implementation is not ready to claim multi-tenant security until the decisions in section 9 are approved and the validation plan in section 7 succeeds.

---

## 2. Verified Current Implementation

### 2.1 Repository and examined files

The design review was based on the committed tree at HEAD `991a4362b6298243277b01356e0c05d35087006d`, including:

- `app/db/models.py`
- `app/db/session.py`
- `app/db/types.py`
- `app/main.py`
- `alembic/env.py`
- `alembic/versions/0001_initial_tables.py`
- `MILESTONE_8_1_12_FINAL_DATABASE_AUDIT.md`
- `MILESTONE_8_1_9_TENANT_KEY_STRATEGY.md`
- `tests/db/test_app_lifespan.py`
- `tests/db/test_connection.py`
- `tests/db/test_migrations.py`
- `tests/db/test_models.py`
- `tests/db/test_role_constraints.py`
- `tests/db/test_schema_consistency.py`

No `.env` file was opened. No external service or database was contacted.

### 2.2 Current schema

Only three application tables exist:

| Table | Current key and tenant behavior |
|---|---|
| `organizations` | `id UUID` primary key. The row is the tenant root. |
| `users` | `id UUID` primary key. Global identity with no `organization_id`. |
| `organization_memberships` | `id UUID` primary key, `organization_id UUID NOT NULL`, `user_id UUID NOT NULL`, role CHECK, and `UNIQUE (organization_id, user_id)`. |

Current membership relationships:

```text
organization_memberships.organization_id
    -> organizations.id ON DELETE CASCADE

organization_memberships.user_id
    -> users.id ON DELETE CASCADE
```

The role constraint is exactly:

```sql
CONSTRAINT ck_organization_memberships_role
CHECK (role IN ('org_admin', 'org_member'))
```

The current schema does **not** have:

```sql
UNIQUE (organization_id, id)
```

on `organization_memberships`.

### 2.3 Current database lifecycle

- Engine and session factory are created lazily from `NETOPS_DATABASE_URL`.
- The engine uses async SQLAlchemy and `asyncpg` for PostgreSQL.
- SQLite is used for offline tests.
- The FastAPI lifespan conditionally calls `init_db()` when `NETOPS_DATABASE_URL` is configured.
- Startup does not create tables and does not proactively connect.
- Shutdown disposes the engine.
- Existing telemetry and MCP cleanup behavior is preserved.
- `get_async_session()` yields a request-scoped session, but no application route currently uses it.
- There is no tenant context setting, transaction unit-of-work, auth dependency, or organization selection dependency.

### 2.4 Current migration and validation state

- Alembic supports async online migration through `async_engine_from_config()`.
- Alembic supports offline SQL generation.
- Revision `0001` contains the three current tables.
- SQLite upgrade/downgrade and schema consistency tests exist.
- Offline PostgreSQL DDL checks exist.
- The exact role CHECK constraint is tested.
- No live PostgreSQL migration has been executed.
- No RLS behavior, runtime role, composite FK target, or tenant-context isolation has been tested.

At the final 8.1.12 audit, the database suite reported `46 passed, 2 warnings`, and the full suite reported `415 passed, 2 skipped, 1 xfailed, 17 warnings`. Those results validate the 8.1 implementation, not live PostgreSQL or RLS.

### 2.5 Gap summary

| Concern | Current state | Consequence |
|---|---|---|
| Authentication | Not implemented | No trusted user identity yet. |
| Organization selection | Not implemented | No active tenant context can be established safely. |
| Membership authorization | Model exists only | No request-time membership check. |
| Tenant context | Not implemented | No request can be scoped to a tenant. |
| RLS | Not implemented | Database does not independently enforce tenant isolation. |
| Runtime role | Not implemented | No non-owner application identity is enforced. |
| Migration role | Not implemented | Migration/ownership separation is not established. |
| Membership composite key | `UNIQUE (organization_id, id)` absent | Membership-ID composite FKs are not yet possible. |
| Composite tenant FKs | No tenant-owned child tables yet | Future table design must apply them. |
| Live PostgreSQL | Not tested | PostgreSQL and RLS compatibility remain unproven. |

---

## 3. Architecture Decisions A–G

### A. Global users and tenant memberships

#### Recommended decision

Keep `users` as a global identity table and keep `organization_memberships` as the authoritative tenant-membership table.

Authentication should be handled through a dedicated auth path, not through general tenant queries. The recommended model is:

1. Authenticate the user globally using the existing credential/session design or a future external identity provider.
2. Resolve the authenticated user ID from a server-side session or verified token, never from a client-supplied user ID.
3. Store global sessions with `user_id` and an optional `active_organization_id`.
4. For an organization request, validate that the authenticated user has a matching `organization_memberships` row.
5. Only after that validation may the request set `app.current_organization_id`.
6. Before an organization is selected, permit only the bootstrap operations needed to read the user's own identity and permitted organizations.

The `users` table should not be directly readable by the tenant runtime role. Authentication and bootstrap should use a narrowly scoped auth role or security-definer function. If a tenant-facing feature needs user profile data, expose only the required fields through a function or view that validates membership.

#### Why this is recommended

- It preserves a single identity per person across organizations.
- It avoids duplicating user records per tenant.
- It makes membership revocation immediate at the membership row.
- It prevents a client from changing its identity by submitting a different user ID.
- It separates global authentication data from tenant-owned operational data.
- It leaves room for an external identity provider without changing the tenant schema.

#### Alternatives considered

1. **Put `organization_id` on `users`.**
   - Rejected. A user could belong to only one organization, or would need duplicate user rows, which breaks global identity and audit continuity.
2. **Allow the runtime tenant role to read all users.**
   - Rejected. Tenant queries could enumerate users outside the active organization.
3. **Use only application-level filtering for users and memberships.**
   - Rejected as the sole control. It does not protect against a missed query, injection, or future route bug.
4. **Use a JWT organization claim without server-side membership validation.**
   - Rejected as the sole control. A stale or forged claim must not bypass membership checks.

#### Consequences and trade-offs

- Authentication becomes a distinct security boundary with its own role and tests.
- Bootstrap functions or auth views add implementation complexity.
- Global user data must be protected by privilege design even though it is not tenant-RLS-scoped.
- Membership checks must be fast; an index on `(user_id, organization_id)` and the existing `(organization_id, user_id)` unique key support lookups.
- A tenant cannot be allowed to resolve arbitrary user IDs to email addresses or other profile fields.

#### Tests needed

- A user can belong to multiple organizations.
- A user can authenticate without an active organization.
- A user can see only their own memberships before selecting an organization.
- A user cannot retrieve another user's memberships.
- A user cannot retrieve another tenant's users through a tenant endpoint.
- A deactivated user cannot authenticate or retain an active tenant session.
- Removing a membership invalidates subsequent tenant access.

---

### B. Organization selection and switching

#### Recommended decision

Use a server-side authenticated session with an optional active organization. The active organization is a **server-side selection**, not an authorization claim supplied by the client.

Recommended flow:

1. Authenticate the user and establish a trusted user identity.
2. Read the user's permitted organizations through the bootstrap path.
3. When the client requests an organization switch, treat the submitted organization ID as untrusted input.
4. Validate `(authenticated_user_id, requested_organization_id)` against `organization_memberships`.
5. If membership is valid and active, update the server-side session's active organization.
6. For the current request, begin a database transaction and set `app.current_organization_id` transaction-locally.
7. At transaction end, the setting disappears.
8. If membership is invalid, clear or reject the switch and do not set tenant context.

The client may submit an organization ID in a path or body, but that ID is only a request. It must never directly set the database GUC.

#### Why this is recommended

- Membership is revalidated at the point of authorization.
- A stolen or forged organization ID cannot become an active tenant context.
- A server-side session can be revoked centrally.
- Switching does not leave a persistent database session setting.
- Concurrent requests cannot inherit another request's organization through a shared connection.

#### Alternatives considered

1. **Put the active organization in an unsigned client cookie or header.**
   - Rejected. The client could change the tenant identifier.
2. **Put the active organization in a JWT and never revalidate membership.**
   - Rejected. Revoked memberships and role changes would remain effective until token expiry.
3. **Use session-level `SET app.current_organization_id` during switching.**
   - Rejected. The setting can persist across pooled transactions and leak to another request.
4. **Use a separate database connection per organization.**
   - Not chosen. It increases operational complexity and does not remove the need for transaction-local context and RLS.

#### Consequences and trade-offs

- The session store becomes security-sensitive and must be protected.
- Organization switching has a small authorization query cost.
- If using JWTs rather than server-side sessions, switching requires token rotation or strict revalidation.
- A user with multiple concurrent active organizations needs each request to carry its own validated context; the database must not rely on a single session-level value.

#### Tests needed

- A valid member can select an organization.
- A non-member cannot select an organization even with a valid organization UUID.
- A forged organization ID is rejected before any tenant query.
- Switching from organization A to B does not make A's context visible in B's request.
- Reusing a pooled connection after a switch does not retain the old setting.
- Removing membership while a session is active denies the next request.
- Concurrent requests for different organizations cannot share context.

---

### C. Database roles and privileges

#### Recommended decision

Use separate database identities with least privilege:

| Role | Purpose | Ownership and privileges |
|---|---|---|
| `netops_owner` / schema owner | Owns schema objects or is the migration owner | DDL and schema management only; not used by the application. |
| `netops_migrator` | Runs Alembic migrations | DDL and migration metadata access; no interactive application use. |
| `netops_runtime` | Tenant application queries | Non-owner, non-superuser, no `BYPASSRLS`; only required `SELECT`/`INSERT`/`UPDATE`/`DELETE`; no `CREATE`, `ALTER`, `DROP`, or `TRUNCATE`. |
| `netops_auth` | Authentication, session lookup, and membership bootstrap | Limited access to users, sessions, and membership bootstrap functions; not a general tenant role. |
| `netops_maintenance` | Retention, archive, and maintenance jobs | Separate credentials; preferably no `BYPASSRLS`; explicit functions or per-tenant operations. |

The exact role names may change, but the separation must remain.

Requirements:

- Runtime must not own tenant tables.
- Runtime must not be a superuser.
- Runtime must not have `BYPASSRLS`.
- Tenant tables must use `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`.
- Migration/DDL privileges must never be granted to the runtime role.
- The application must not use `SET ROLE` to elevate itself.
- The auth and runtime roles should use separate connection credentials, not a single role with broad privileges.
- Maintenance must not be an interactive application login.

#### Why this is recommended

- A query bug cannot become a tenant bypass if runtime cannot bypass RLS.
- A compromised runtime credential cannot alter schema or grant itself privileges.
- A migration credential leak does not automatically grant application access.
- `FORCE ROW LEVEL SECURITY` protects against owner/superuser assumptions and on-premises divergence.
- A separate maintenance identity prevents retention work from becoming an application backdoor.

#### Alternatives considered

1. **Use one superuser or owner role for application and migrations.**
   - Rejected. It defeats RLS and creates excessive blast radius.
2. **Use a runtime role with `BYPASSRLS` for bootstrap.**
   - Rejected. Bootstrap must be narrow and function-scoped, not a broad bypass.
3. **Use a maintenance role with `BYPASSRLS` by default.**
   - Not recommended. Prefer per-tenant context; if unavoidable, isolate credentials and require audit and tests.
4. **Use `SET ROLE` from the runtime connection.**
   - Rejected. It creates privilege-escalation risk and complicates accountability.

#### Consequences and trade-offs

- More credentials and operational setup are required.
- Grants must be maintained as new tables and functions are added.
- The auth role adds a second database access path, but reduces tenant data exposure.
- Maintenance jobs may need redesign to operate per tenant rather than globally.

#### Tests needed

- Runtime can connect and execute allowed tenant queries.
- Runtime cannot create, alter, or drop schema objects.
- Runtime cannot truncate tables.
- Runtime cannot bypass RLS.
- Runtime cannot read another tenant without context.
- Runtime cannot assume the migration or auth role.
- Migration role can run migrations; runtime role cannot.
- Maintenance role behavior is tested and cannot be reached through the application.
- Owner/superuser behavior is explicitly understood and forced where required.

---

### D. Transaction-local tenant context

#### Recommended decision

Set tenant context inside each tenant transaction with:

```sql
SELECT set_config('app.current_organization_id', :organization_id, true);
```

The third argument `true` makes the setting transaction-local.

For bootstrap operations, use a separate transaction-local user context if needed:

```sql
SELECT set_config('app.current_user_id', :user_id, true);
```

Policies should read settings with:

```sql
current_setting('app.current_organization_id', true)
```

A missing setting must evaluate to NULL and therefore deny tenant rows. Do not use `COALESCE` to substitute a default organization.

#### Why this is recommended

- Transaction-local context automatically resets at transaction end.
- Pooled connections cannot retain the previous request's tenant.
- Context is set only after authentication and membership authorization.
- Parameterized `set_config` avoids injecting tenant values into SQL text.
- Missing context fails closed rather than selecting an arbitrary tenant.

#### Alternatives considered

1. **Session-level `SET app.current_organization_id = ...`**
   - Rejected. It persists on a pooled connection and can leak between requests.
2. **Application-only filtering without a database setting.**
   - Rejected as the sole enforcement. It cannot protect against missed filters.
3. **A default organization when context is missing.**
   - Rejected. Missing context must deny, not fall back.
4. **Store the active organization in a SQLAlchemy session object.**
   - Rejected. The database must enforce context; application memory is not a security boundary.

#### Consequences and trade-offs

- Every tenant query must execute inside the correct transaction.
- Queries issued outside a tenant transaction will return no tenant rows, by design.
- Connection-pool tests are required because the safety property depends on transaction boundaries.
- Bootstrap operations need a separate, explicit context model.

#### Tests needed

- Missing context returns no tenant rows.
- Empty or invalid context is rejected or denied.
- Context is set before the first tenant query.
- Context resets after commit and after rollback.
- Two sequential requests using the same pooled connection cannot see each other's tenant.
- A tenant cannot use a forged organization ID to set context.
- Bootstrap user context cannot read tenant tables.
- No session-level setting remains after a transaction.

---

### E. Request transaction ownership

#### Recommended decision

Use a request-scoped unit of work owned by a FastAPI dependency or service boundary:

1. Create an `AsyncSession`.
2. Begin exactly one transaction at the request boundary.
3. Set user and tenant context inside that transaction, after authorization.
4. Yield the session to the endpoint or service.
5. Commit when the request completes successfully.
6. Roll back on exceptions, authorization failures, and cancellation.
7. Close the session and return the connection to the pool.

Repositories and endpoint handlers must not independently call `begin()` or `commit()`. They may use savepoints only inside a clearly documented service-level unit of work.

The auth/bootstrap path is separate: it uses the auth role and does not set tenant context or access tenant-owned tables.

#### Why this is recommended

- There is one obvious commit boundary.
- Tenant context and tenant queries share the same transaction.
- Exceptions and cancellation cannot leave an uncommitted tenant operation.
- Nested transaction ownership ambiguity is removed.
- Pooled connections are returned only after the transaction ends and the context resets.

#### Alternatives considered

1. **Let each repository commit.**
   - Rejected. Multiple commits break atomicity and can commit before authorization is complete.
2. **Use autobegin and rely on implicit commit.**
   - Rejected. The commit boundary would be unclear.
3. **Set context in a middleware on a session-level connection.**
   - Rejected. Context could leak across requests.
4. **Use nested transactions for every service call.**
   - Rejected as a default. Savepoints should be exceptional and explicit.

#### Consequences and trade-offs

- Streaming or long-running responses need careful transaction handling.
- Endpoints must not commit manually.
- Background jobs need their own unit of work, not a request dependency.
- Tests must prove rollback, cancellation, and connection reuse.

#### Tests needed

- A successful write commits once.
- An exception rolls back all request writes.
- A failed authorization sets no tenant context.
- A cancelled request rolls back and releases the connection.
- A repository that attempts a nested commit is rejected or fails the design test.
- Two requests cannot share a transaction or tenant setting.
- A read-only request still uses a transaction-local context and does not leak it.

---

### F. Composite tenant constraints

#### Recommended decision

Preserve the 8.1.9 strategy:

- Globally unique UUID primary keys.
- `organization_id UUID NOT NULL` on every tenant-owned table.
- `UNIQUE (organization_id, id)` on every tenant-owned table that is a composite foreign-key target.
- Composite foreign keys:

```sql
FOREIGN KEY (organization_id, parent_id)
    REFERENCES parent (organization_id, id)
```

- Use `ON DELETE CASCADE`, `RESTRICT`, or `NO ACTION` deliberately.
- Do not use unqualified `ON DELETE SET NULL` on a composite FK containing `organization_id NOT NULL`.
- Keep `organizations` as the tenant root with no `organization_id` column.
- Keep `users` global with no `organization_id` column.
- Keep `sessions` global with an optional `active_organization_id` reference that is not ownership.

#### Membership decision

**Recommended: add `UNIQUE (organization_id, id)` to `organization_memberships` in a new migration.**

Retain the existing:

```sql
UNIQUE (organization_id, user_id)
```

Why:

- It enables uniform `(organization_id, membership_id)` composite references.
- It supports future records that need to prove an actor's tenant membership.
- It avoids special-case application-only membership checks.
- It matches the uniform composite-key pattern for future tenant tables.
- The current membership table is small, so the extra index cost is low.

The alternative is to require every membership reference to use `(organization_id, user_id)` or historical IDs without FKs. That is workable but less uniform and makes actor references harder to validate.

#### Future table rules

| Future table | Required design |
|---|---|
| `collectors` | `organization_id NOT NULL`, PK `id`, `UNIQUE (organization_id, id)`, organization FK. |
| `devices` | `organization_id NOT NULL`, PK `id`, `UNIQUE (organization_id, id)`, organization FK, optional composite collector FK. |
| `telemetry_records` | `organization_id NOT NULL`, PK `id`, `UNIQUE (organization_id, id)`, composite device FK, indexes for tenant/time queries. |
| `diagnosis_events` | `organization_id NOT NULL`, PK `id`, `UNIQUE (organization_id, id)`, composite telemetry/device FKs; same-device consistency key where required. |
| `alerts` | `organization_id NOT NULL`, PK `id`, `UNIQUE (organization_id, id)` if referenced, composite device/telemetry/diagnosis FKs as needed. |
| `audit_events` | `organization_id NOT NULL`; tenant-owned audit. Avoid cascading FKs to mutable operational rows. Store historical IDs. Add `UNIQUE (organization_id, id)` only if audit rows become FK targets. |
| `sessions` | Global auth session, not tenant-owned. `user_id` and optional `active_organization_id`; validate membership before using active organization. |
| `organizations` | Tenant root. PK `id`; no `organization_id`; no composite tenant target. |
| `users` | Global identity. PK `id`; no `organization_id`; no tenant composite target. |

#### Consequences and trade-offs

- Every composite FK target needs an extra unique index.
- Future table creation must occur in dependency order: parent unique key before child FK.
- Optional parent IDs and `MATCH SIMPLE` need explicit tests.
- Explicit constraint names are required; the existing name convention is ambiguous for multiple composite FKs.
- Cross-tenant references are prevented structurally even if RLS is missing or bypassed.

#### Tests needed

- `UNIQUE (organization_id, id)` exists on memberships after migration.
- A composite FK to memberships succeeds for the same tenant.
- A composite FK to memberships fails for a different tenant.
- Future parent/child composite FK tests reject cross-tenant references.
- `organization_id NOT NULL` prevents tenantless rows.
- `ON DELETE` behavior matches the documented retention policy.
- Constraint names and offline PostgreSQL SQL are exact.

---

### G. Row-Level Security

#### Recommended decision

Use RLS as an independent database enforcement layer for tenant-owned tables:

- Enable and force RLS.
- Use `USING` for visibility and existing-row modification.
- Use `WITH CHECK` for inserted and updated rows.
- Deny when tenant context is missing or invalid.
- Keep global users and auth bootstrap on a separate privileged path.
- Use composite FKs as defense in depth.

Conceptual policy:

```sql
ALTER TABLE tenant_table ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_table FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON tenant_table
    FOR ALL
    USING (
        organization_id =
        current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id =
        current_setting('app.current_organization_id', true)::uuid
    );
```

`organizations` requires a different predicate:

```sql
USING (id = current_setting('app.current_organization_id', true)::uuid)
WITH CHECK (id = current_setting('app.current_organization_id', true)::uuid)
```

`organization_memberships` uses `organization_id`.

`users` must not be protected by a simple tenant policy because the table is global. Use a restricted auth role or security-definer function for authentication and bootstrap. If tenant-facing user information is required, expose only the needed fields through a checked function or view.

#### Why this is recommended

- RLS protects against missing application filters.
- `FORCE` reduces owner/superuser bypass risk.
- `WITH CHECK` prevents cross-tenant inserts and updates.
- Composite FKs prevent structurally invalid cross-tenant references.
- Application authorization and RLS address different failure modes.

#### Alternatives considered

1. **Application filtering only.**
   - Rejected. One missing `WHERE organization_id` can expose data.
2. **Composite FKs only.**
   - Rejected. They do not protect reads or same-tenant unauthorized rows.
3. **RLS with a broad `BYPASSRLS` runtime role.**
   - Rejected. It makes RLS advisory.
4. **One global policy based on a client-supplied header.**
   - Rejected. The setting would not be trustworthy.
5. **Disable RLS for background jobs.**
   - Rejected as a default. Background jobs should iterate per tenant and set context.

#### Background jobs

Background jobs should:

- Use a dedicated worker identity.
- Fetch the tenant list from the platform registry.
- Open one transaction per organization.
- Set `app.current_organization_id` inside each transaction.
- Stop the tenant loop on authorization/context failure.
- Avoid long cross-tenant transactions.
- Never share a session-level tenant setting.

#### Maintenance paths

- Retention and archive jobs should use `netops_maintenance`, not the interactive runtime role.
- Prefer per-tenant operations with explicit context.
- Avoid `BYPASSRLS`; if a cross-tenant maintenance operation is unavoidable, scope it to a security-definer function with a narrow allowlist, separate credentials, audit logging, and tests.
- No maintenance function should be callable by tenant users.

#### Application authorization plus RLS

Both layers are required:

1. **Application authorization:** verifies authenticated user, active organization, membership, and role.
2. **Database RLS:** enforces tenant visibility and writes.
3. **Composite FKs:** prevent cross-tenant structural references.
4. **Privileges:** ensure the runtime role cannot bypass RLS or alter schema.

#### Tests needed

- Tenant A can read only A's rows.
- Tenant A cannot insert, update, or delete B's rows.
- Cross-tenant `UPDATE ... WHERE` affects zero rows or is denied.
- Missing context returns no rows and rejects writes.
- Invalid context is rejected.
- Bootstrap cannot enumerate other users or organizations.
- Background jobs operate per tenant without leaking context.
- Maintenance operations are not reachable through the runtime role.
- Owner and superuser behavior is explicitly tested and documented.

---

## 4. Proposed Database Role and Transaction Model

### 4.1 Role and privilege matrix

| Capability | `netops_owner` / schema owner | `netops_migrator` | `netops_runtime` | `netops_auth` | `netops_maintenance` |
|---|---:|---:|---:|---:|---:|
| Own schema objects | Yes | Optional | No | No | No |
| Run Alembic DDL | Yes | Yes | No | No | No |
| Create/alter/drop tables | Yes | Yes | No | No | No |
| Read tenant tables | Policy-dependent | Yes for migration | Yes, under RLS | No broad access | Per-job, explicit |
| Write tenant tables | Policy-dependent | Yes | Only required DML under RLS | No | Per-job, explicit |
| `TRUNCATE` | Yes | Yes | No | No | No default |
| `BYPASSRLS` | Possibly | Only if operationally required | No | No | No by default |
| Read `users` | Yes | Yes | No | Limited | No default |
| Read sessions | Yes | Yes | No | Yes | No default |
| Application login | No | No | Yes | Auth path only | Maintenance only |

### 4.2 Ownership and grants

- Create tables as the migration/schema owner, not as runtime.
- Apply `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`.
- Grant runtime only the DML it needs on tables it may use.
- Grant `USAGE` on the schema.
- Do not grant `CREATE` on the schema to runtime.
- Do not grant `TRUNCATE`.
- Do not grant membership in migration/auth roles to runtime.
- Use separate connection strings/secrets per role. Do not use `SET ROLE` for ordinary request handling.
- Keep migration credentials out of the application process.

### 4.3 Request flow

For a tenant-scoped request:

```text
1. Authenticate server-side session.
2. Resolve trusted user identity.
3. Read/validate active organization from the server-side session.
4. Validate membership for (user_id, organization_id).
5. Open a runtime-role transaction.
6. Set app.current_user_id locally.
7. Set app.current_organization_id locally.
8. Execute tenant queries.
9. Commit or roll back.
10. Context disappears at transaction end.
```

For a bootstrap request:

```text
1. Authenticate server-side session.
2. Use the auth role/function path.
3. Return only this user's identity and permitted organizations.
4. Do not set tenant context.
5. Do not access tenant-owned tables.
```

For an organization switch:

```text
1. Read the untrusted requested organization ID.
2. Validate membership for the authenticated user.
3. Update the server-side session's active organization.
4. Set transaction-local context only for the current authorized request.
5. Reject and clear the switch if membership is invalid.
```

### 4.4 Transaction lifecycle

The dependency/unit-of-work should own:

- Session creation.
- Transaction begin.
- Tenant context setup.
- Successful commit.
- Exception rollback.
- Cancellation rollback.
- Session close/connection release.

Repositories should:

- Accept an existing `AsyncSession`.
- Use `flush()` when an ID is needed before commit.
- Never call `commit()` or `begin()`.
- Never create a second engine or connection for the same request.
- Never set tenant context themselves.

### 4.5 Explicit non-goals for this design

This document does not implement:

- Session or token storage.
- Password verification.
- Organization-selection endpoints.
- RLS SQL.
- Alembic migrations.
- Database roles.
- Application authorization dependencies.
- Background worker plumbing.
- Retention jobs.

Those are future milestones.

---

## 5. Tenant Isolation and RLS Strategy

### 5.1 Defense in depth

Tenant isolation must use all layers:

1. **Authentication:** trusted user identity.
2. **Membership authorization:** user belongs to the selected organization.
3. **Tenant context:** transaction-local organization ID.
4. **RLS:** database-enforced row visibility and writes.
5. **Composite tenant FKs:** structural prevention of cross-tenant references.
6. **Least-privilege roles:** no runtime schema ownership or bypass.
7. **Validation:** live PostgreSQL tests with multiple tenants.

No single layer is sufficient.

### 5.2 Policy matrix

| Table | Tenant predicate | Notes |
|---|---|---|
| `organizations` | `id = current_org_id()` | Tenant root; creation/administration may use a separate privileged path. |
| `organization_memberships` | `organization_id = current_org_id()` | Tenant-scoped membership rows. Bootstrap access must not use this policy. |
| `users` | No tenant policy | Global identity; restricted auth path only. |
| `sessions` | No tenant policy | Global auth session; active organization is context, not ownership. |
| Future tenant-owned tables | `organization_id = current_org_id()` | Add `USING` and `WITH CHECK`, force RLS, composite FKs. |
| Platform audit/registry | Separate privileged policy | Never mix platform and tenant audit semantics. |

### 5.3 Missing or invalid context

Use a helper or direct predicate that yields NULL when no setting exists. Do not use a default organization.

Policy behavior must be:

- Missing setting: no tenant rows visible; inserts/updates fail.
- Empty setting: no tenant rows visible; writes fail.
- Malformed setting: query fails closed; no data returned.
- Setting for an organization the user does not belong to: request rejected before setting context; if set by a bug, RLS still limits to that organization but application authorization must also fail.

### 5.4 Bootstrap access path

The bootstrap path should be narrower than the tenant runtime path:

- Auth role can verify a user and read that user's sessions.
- A security-definer function returns only the authenticated user's permitted organizations.
- The function uses a fixed search path and a trusted user ID from the session.
- The function does not accept an arbitrary user ID from the client.
- The application does not receive broad `SELECT` on `users` or all memberships.

### 5.5 Connection-pool safety

Required properties:

- Set context with `set_config(..., true)` after `BEGIN`.
- Never use session-level `SET`.
- Commit or roll back before releasing the connection.
- Test with `pool_size=1` and two sequential requests to force reuse.
- Test after rollback and after cancellation.
- Test a request with no tenant context followed by a tenant request.

### 5.6 Maintenance and retention

- Separate maintenance identity.
- Per-tenant operations by default.
- Explicit audit logging for cross-tenant maintenance.
- No app-callable bypass function.
- RLS remains enabled; any exception is deliberate, documented, and tested.

---

## 6. Threat Model

| Threat | Attack path | Impact | Mitigation | Required test |
|---|---|---|---|---|
| Cross-tenant read | A query omits or misuses `organization_id`, or a stored ID belongs to another tenant. | Tenant data disclosure. | RLS `USING`, app membership checks, tenant context, least privilege. | A user in A cannot read B's rows through any endpoint or direct query. |
| Cross-tenant write | A client submits another tenant's ID on insert/update. | Data corruption or unauthorized modification. | `WITH CHECK`, composite FKs, server-derived tenant context, untrusted-input validation. | Cross-tenant INSERT/UPDATE/DELETE denied. |
| Forged organization ID | Client submits an organization UUID in a header, path, or body. | Attempt to switch tenant context. | Validate membership server-side; never set GUC from client input. | Non-member org ID rejected; no tenant query occurs. |
| Missing tenant context | A code path forgets to set context. | RLS may hide data or writes fail, or a poorly written policy may allow access. | Fail-closed policies; tests; helper required for tenant dependency. | Missing context returns zero rows and rejects writes. |
| Connection-pool context leakage | Session-level setting persists on a reused connection. | One request sees another tenant's data. | Transaction-local `set_config`; no `SET`; pool isolation tests. | Pool size 1, sequential tenants, no leakage. |
| Runtime role escalation | Runtime role owns tables, has `BYPASSRLS`, or can `SET ROLE`. | RLS bypass and full data compromise. | Separate non-owner runtime, no bypass, no DDL, no role membership. | Runtime cannot bypass, create, alter, drop, or truncate. |
| Bootstrap endpoint leakage | User listing reveals other users or organizations. | Tenant/organization enumeration and PII leakage. | Auth-only functions, current-user filter, no broad role access. | User sees only own memberships; cannot enumerate others. |
| Cross-tenant FK reference | Child row references a parent UUID from another tenant. | Structurally invalid or cross-tenant data access. | `UNIQUE (organization_id, id)` and composite FKs. | Composite FK rejects same-org lookup across tenants. |
| Unauthorized background/maintenance operation | Worker uses broad credentials or disables RLS. | Mass data access or deletion. | Separate worker/maintenance roles; per-tenant loops; explicit context; audit. | Worker cannot access other tenants outside its loop; maintenance is not app-callable. |
| Stale session after membership removal | A user retains an active org after removal. | Continued access to tenant data. | Revalidate membership per request or on sensitive actions; revoke sessions. | Removed member receives denial on next request. |
| Stale token with old org claim | JWT remains valid after switch or revocation. | Access to previous organization. | Revalidate membership; prefer server-side sessions; short expiry if JWT. | Old token cannot access removed/old org. |
| SQL injection in tenant context | Dynamic SQL interpolation of organization ID. | Arbitrary SQL or context manipulation. | Parameterized `set_config`; no dynamic tenant SQL; validated UUID. | Injection strings rejected or treated as invalid UUIDs. |

---

## 7. PostgreSQL Validation Plan

This plan is for future implementation. No live PostgreSQL validation has been performed for this document.

### 7.1 Preconditions

- Use a disposable local PostgreSQL instance, such as a temporary container or isolated cluster.
- Never point tests at an existing development, staging, or production database.
- Guard test setup by requiring a clearly disposable database name and an explicit test DSN.
- Do not read `.env`; pass test credentials explicitly from the test environment.
- Record `SELECT version()` and the server major version.
- Confirm the supported PostgreSQL version before implementation. The repository does not currently declare a minimum version; this is an open approval item.

### 7.2 Environment setup

1. Start a disposable PostgreSQL server.
2. Create a disposable database named with a test-only prefix.
3. Create:
   - schema owner/migration role,
   - `netops_runtime`,
   - `netops_auth`,
   - `netops_maintenance`.
4. Set explicit passwords only in the disposable environment; do not print them.
5. Apply the migration role credentials to Alembic.
6. Apply runtime/auth credentials to the test application path.
7. Keep the migration role out of the runtime connection.

### 7.3 Migration checks

- Run `alembic upgrade head` against the disposable database.
- Run `alembic downgrade base` and upgrade again.
- Verify the Alembic version table.
- Verify native PostgreSQL `UUID` columns.
- Verify `TIMESTAMP WITH TIME ZONE` columns and UTC behavior.
- Verify `ck_organization_memberships_role` exists and rejects invalid roles.
- Verify `UNIQUE (organization_id, user_id)`.
- Verify `UNIQUE (organization_id, id)` after its migration.
- Verify composite FK creation and failure behavior.
- Verify offline SQL and online schema agree.

### 7.4 RLS isolation tests

Seed at least two organizations and multiple users. Use real runtime credentials, not an owner or superuser.

Required tests:

1. Missing context:
   - `SELECT` on tenant tables returns zero rows.
   - `INSERT` fails.
   - `UPDATE` and `DELETE` affect no rows or fail.
2. Valid context for organization A:
   - A can read A's rows.
   - A cannot read B's rows.
3. Cross-tenant write:
   - A cannot insert B's `organization_id`.
   - A cannot update a row to B's `organization_id`.
   - A cannot delete B's rows.
4. Organization root:
   - A sees only organization A.
   - A cannot modify organization B.
5. Membership:
   - A sees only memberships in A.
   - Bootstrap returns only the authenticated user's memberships.
6. Composite FK:
   - A child row cannot reference B's parent ID through a composite FK.
   - Same-tenant reference succeeds.
7. Pool context:
   - Set A, commit.
   - Reuse the same pooled connection for B.
   - Verify B sees only B.
   - Verify no context remains after rollback and cancellation.
8. Runtime role:
   - Cannot `CREATE`, `ALTER`, or `DROP`.
   - Cannot `TRUNCATE`.
   - Cannot read `users` directly.
   - Cannot execute maintenance-only functions.
   - Cannot set `BYPASSRLS` or assume another role.
9. Maintenance:
   - Per-tenant retention works under explicit context.
   - No application role can invoke cross-tenant maintenance functions.

### 7.5 Acceptance evidence

Record:

- PostgreSQL version and configuration.
- Migration revision IDs.
- Actual constraint/index names.
- Actual role names and grants.
- Test command and output.
- RLS policy definitions.
- Pool test output.
- Any deviations or skipped tests.

Do not claim PostgreSQL or RLS compatibility until all required tests pass against the disposable server.

---

## 8. 8.2.x Implementation Roadmap

These milestones are proposals. None are implemented by this document.

### 8.2.1 — Validation harness and decision lock

**Objective:** Establish a safe, repeatable disposable PostgreSQL environment and freeze the decisions in this document.

**Scope:**
- Add a test-only PostgreSQL setup procedure.
- Add a disposable-database guard.
- Record the supported PostgreSQL version.
- Add test role definitions.
- Do not add application RLS or tenant routes yet.

**Expected files:**
- `tests/db/postgres/` test support.
- `tests/db/postgres/conftest.py` or equivalent.
- Documentation for the disposable test database.
- No production migrations.

**Required tests:**
- Environment refuses non-disposable database names.
- Migration role can connect.
- Runtime role can connect only with its intended grants.
- Existing revision `0001` upgrades and downgrades live.
- Existing 8.1 tests still pass.

**Acceptance criteria:**
- A fresh disposable PostgreSQL database can be created and destroyed.
- Revision `0001` works online.
- No application code or production database is modified.

**Migration required:** No.

**Security risks addressed:** Prevents accidental testing against real databases and establishes the role baseline.

---

### 8.2.2 — Membership composite key migration

**Objective:** Add the membership tenant target key.

**Scope:**
- Add `UNIQUE (organization_id, id)` to `organization_memberships`.
- Retain `UNIQUE (organization_id, user_id)`.
- Do not modify revision `0001`.
- Add exact offline/online SQL and schema tests.

**Expected files:**
- New Alembic revision, for example `alembic/versions/0002_membership_tenant_key.py`.
- `app/db/models.py` update for the new unique constraint.
- `tests/db/test_memberships_composite_key.py` or equivalent.
- Schema consistency/offline SQL tests updated.

**Required tests:**
- Constraint exists in model and migration.
- PostgreSQL offline SQL has the exact name and columns.
- SQLite migration shape is valid.
- Same-tenant membership references succeed.
- Cross-tenant membership references fail.
- Downgrade removes the key.

**Acceptance criteria:**
- Migration is reversible.
- No existing table data is lost.
- The existing `(organization_id, user_id)` unique key remains.

**Migration required:** Yes.

**Security risks addressed:** Enables future composite membership references and uniform tenant-key defense.

---

### 8.2.3 — Database roles and privileges

**Objective:** Establish migration, runtime, auth, and maintenance role separation.

**Scope:**
- Add role bootstrap SQL/scripts for a disposable environment.
- Grant runtime only required DML and schema usage.
- Deny DDL and `TRUNCATE`.
- Ensure runtime is non-owner and no `BYPASSRLS`.
- Document credential separation.

**Expected files:**
- `scripts/db/roles.sql` or equivalent.
- `tests/db/postgres/test_roles.py`.
- Operations documentation.
- No tenant routes.

**Required tests:**
- Runtime cannot create/alter/drop/truncate.
- Runtime cannot bypass RLS.
- Runtime cannot assume auth/migration/maintenance roles.
- Migration role can run migrations.
- Auth role has only its narrow access.
- Maintenance role is not app-callable.

**Acceptance criteria:**
- Role matrix from section 4 is enforced.
- Credentials are not shared between roles.
- No changes to application request behavior yet.

**Migration required:** No Alembic schema migration; environment bootstrap SQL/script is required.

**Security risks addressed:** Runtime escalation, bypass, and excessive privilege.

---

### 8.2.4 — Tenant context and request unit of work

**Objective:** Implement transaction-local context and one request transaction boundary.

**Scope:**
- Add a request-scoped session/unit-of-work dependency.
- Add `set_config(..., true)` helper.
- Set user and organization context after authorization.
- Commit/rollback at the boundary.
- Do not add tenant-owned routes yet.
- Do not implement RLS policies yet.

**Expected files:**
- `app/db/session.py` or new `app/db/tenant.py`.
- `app/db/unit_of_work.py` if warranted.
- `tests/db/test_tenant_context.py`.
- `tests/db/test_app_lifespan.py` adjustments if needed.

**Required tests:**
- Context set inside a transaction.
- Missing context fails closed.
- Commit/rollback/cancellation behavior.
- Pool reuse does not leak.
- No repository-level commits.
- No session-level `SET`.

**Acceptance criteria:**
- A request can use an explicit tenant transaction.
- No route can accidentally query tenant data without context.
- Existing telemetry/MCP behavior is unchanged.

**Migration required:** No.

**Security risks addressed:** Context leakage, nested commits, unowned transactions.

---

### 8.2.5 — Authentication session and organization selection

**Objective:** Implement global authentication sessions and authorized organization selection.

**Scope:**
- Add a global sessions table if approved.
- Add auth role/function access for user/session lookup.
- Add bootstrap endpoint(s) for the current user's permitted organizations.
- Add organization selection/switching with membership validation.
- Store active organization server-side.
- Do not expose tenant operational tables yet.

**Expected files:**
- New Alembic revision for global sessions if approved.
- `app/db/models.py` for sessions.
- `app/auth/` or equivalent auth service.
- `app/api/auth.py` or equivalent routes.
- `tests/auth/` and `tests/db/test_auth_session.py`.

**Required tests:**
- Login/logout behavior.
- User sees only own memberships.
- Invalid organization switch rejected.
- Non-member cannot become active.
- Membership removal invalidates access.
- No tenant context before selection.
- Auth path cannot enumerate users.

**Acceptance criteria:**
- A trusted user identity and active organization can be established.
- Bootstrap does not leak tenant data.
- Session storage and revocation behavior are documented.

**Migration required:** Yes if a sessions table is added (recommended).

**Security risks addressed:** Forged identity, forged organization IDs, bootstrap enumeration.

---

### 8.2.6 — RLS policies for the existing foundation

**Objective:** Add RLS to the current tenant-scoped tables and the users/bootstrap boundary.

**Scope:**
- Add policies for `organizations` and `organization_memberships`.
- Define global-user access through the approved auth path.
- Enable and force RLS.
- Add `USING` and `WITH CHECK`.
- Do not add collectors/devices/telemetry yet.
- Do not grant bypass to runtime.

**Expected files:**
- New Alembic revision for RLS policies.
- `tests/db/postgres/test_rls_existing.py`.
- Policy SQL assertions.
- Role/auth integration tests.

**Required tests:**
- Missing/invalid context denial.
- Cross-tenant read/write denial.
- Bootstrap access limited to current user.
- Runtime role behavior under `FORCE RLS`.
- Owner/superuser behavior documented and tested.
- Pool isolation.

**Acceptance criteria:**
- Runtime cannot access another tenant's rows.
- Bootstrap cannot enumerate other tenants.
- Policies are present in live PostgreSQL and offline SQL.
- No app route bypasses the tenant dependency.

**Migration required:** Yes.

**Security risks addressed:** Cross-tenant reads/writes, RLS bypass, bootstrap leakage.

---

### 8.2.7 — Live PostgreSQL security suite and CI gate

**Objective:** Make tenant-isolation tests a required, repeatable validation gate.

**Scope:**
- Consolidate disposable PostgreSQL tests.
- Add role, RLS, pool, composite-key, and bootstrap tests.
- Define skip behavior when no disposable server is available.
- Add CI workflow or documented local command.
- Do not weaken offline tests.

**Expected files:**
- `tests/db/postgres/` suite.
- CI configuration if approved.
- Test documentation.

**Required tests:**
- All tests from section 7.
- Cross-tenant read/write matrix.
- Missing-context matrix.
- Runtime privilege matrix.
- Pool reuse and cancellation.
- Migration up/down.

**Acceptance criteria:**
- A single command runs the security suite against a disposable database.
- Test output records PostgreSQL version and role configuration.
- No production/existing database is contacted.

**Migration required:** No.

**Security risks addressed:** Regressions in isolation, role, and context behavior.

---

### 8.2.8 — Future tenant-table guardrails and parity

**Objective:** Prepare the pattern for collectors, devices, telemetry, diagnosis, alerts, and audit without creating them prematurely.

**Scope:**
- Add reusable constraint/index conventions.
- Add schema tests that enforce `organization_id NOT NULL`, `UNIQUE (organization_id, id)`, and composite FK patterns.
- Document SaaS/on-premises parity and SQLite limitations.
- Do not create operational tables unless separately approved.

**Expected files:**
- `docs/` or milestone documentation.
- Test helpers for tenant-table schema assertions.
- No operational table migration unless scoped separately.

**Required tests:**
- A sample tenant table template has the required tenant key, unique target, and RLS prerequisites.
- Cross-tenant composite FK rejection pattern is tested.
- On-premises/owner behavior is documented and tested.

**Acceptance criteria:**
- Future tables cannot be merged without the tenant-key guardrails.
- No unapproved operational tables are created.

**Migration required:** No.

**Security risks addressed:** Future cross-tenant references, missing tenant columns, accidental RLS omission.

---

## 9. Open Questions and Decisions Requiring Approval

The following must be approved before implementation proceeds.

1. **Membership composite key**
   - Approve adding `UNIQUE (organization_id, id)` to `organization_memberships`.
   - Recommendation: **yes**, while retaining `UNIQUE (organization_id, user_id)`.

2. **Authentication mechanism**
   - Choose server-side sessions, signed tokens, or an external identity provider.
   - Recommendation: server-side sessions for the first implementation because revocation and organization switching are simpler.

3. **Session storage**
   - Approve a global `sessions` table with `user_id` and optional `active_organization_id`.
   - Confirm sessions are global, not tenant-owned.

4. **Global-user access path**
   - Approve a dedicated `netops_auth` role and/or security-definer functions.
   - Approve that `users` is not directly readable by `netops_runtime`.

5. **Runtime and migration role names**
   - Approve `netops_runtime`, `netops_migrator`, `netops_auth`, and `netops_maintenance` or substitute names.
   - Confirm no shared credentials.

6. **Maintenance bypass**
   - Approve that maintenance defaults to per-tenant context and no `BYPASSRLS`.
   - If cross-tenant maintenance is required, define the narrow function and audit path.

7. **Database version**
   - Select and document the supported PostgreSQL major version.
   - The repository does not currently declare one.

8. **Transaction boundary**
   - Approve the request/unit-of-work dependency as the only commit boundary.
   - Confirm repositories and endpoints will not call `commit()`.

9. **Organization creation and first administrator**
   - Define how an organization and its first administrator are created.
   - Decide whether this uses a platform role, a security-definer function, or an administrative workflow.

10. **Email and slug normalization**
    - Decide whether email and organization slug uniqueness are case-insensitive.
    - Define disclosure behavior for duplicate email/slug attempts.

11. **Audit and membership references**
    - Confirm tenant audit events store historical actor IDs without cascading FKs to mutable operational rows.
    - Confirm whether any audit records need composite membership references.

12. **Future table scope**
    - Confirm 8.2 is limited to existing foundation, roles, context, auth/session, and RLS.
    - Confirm collectors, devices, telemetry, diagnoses, alerts, and audit remain later milestones.

13. **On-premises and SQLite**
    - Confirm PostgreSQL is the production database and SQLite remains development/test only.
    - Confirm on-premises owner/superuser behavior must be tested with `FORCE RLS`.

14. **Role semantics**
    - Confirm `org_admin` and `org_member` are sufficient for 8.2.
    - Defer additional roles until authorized.

---

## 10. Final Readiness Verdict

**Design verdict: approved-pending-decisions.**

The recommended architecture is coherent with the existing 8.1 schema and strategy:

- Global users remain global.
- Memberships authorize tenant access.
- Server-side sessions and organization selection are explicit.
- Tenant context is transaction-local.
- Runtime and migration roles are separated.
- Composite tenant keys and FKs prevent structural cross-tenant references.
- RLS adds database-level defense in depth.

**Implementation readiness: not yet.**

The following remain true at the time of this document:

- No authentication flow exists.
- No organization-selection flow exists.
- No tenant context is implemented.
- No RLS policy exists.
- No runtime or migration role exists in the repository.
- No live PostgreSQL migration has been run.
- No live RLS or pool-isolation test has been run.
- No claim of multi-tenant security is justified yet.

No Critical or High actual defect was found in the existing 8.1 foundation. The high-priority items are the architectural prerequisites and future validation work described above.

**Next action:** approve or amend the decisions in section 9. Then begin Milestone 8.2.1 only. Do not begin Milestone 8.2.1 in this task.

---

## Repository State at Document Creation

- Branch: `feature/cisco-live-validation`
- HEAD: `991a4362b6298243277b01356e0c05d35087006d`
- This report is the only file created by this milestone.
- No application code, test, migration, requirement, or existing documentation file was modified.
- The five existing scratch artifacts were preserved:
  - `PROJECT_STATUS.backup.md`
  - `dbg.txt`
  - `tatus --short`
  - `test_results.txt`
  - `tstest_integrations.py`
- No `.env` file was accessed.
- No stage, commit, or push was performed.
- No PostgreSQL, Cisco, Gemini, or DGX service was contacted.

**Milestone 8.2.1 has not started.**
