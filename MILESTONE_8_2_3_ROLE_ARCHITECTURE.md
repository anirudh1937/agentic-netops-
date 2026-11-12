# Milestone 8.2.3 — Database Role Architecture Decision Document

**Status:** Approved architecture decision; read-only planning  
**Repository:** `C:\Users\Aniru\agentic-netops-old`  
**Branch:** `feature/cisco-live-validation`  
**Verified HEAD:** `991a4362b6298243277b01356e0c05d35087006d`  
**Date:** 2026-11-10

> This document records approved architecture decisions for Milestone 8.2.3. It does not implement PostgreSQL roles, bootstrap SQL, application code changes, migration changes, or database tests. It must not be treated as an implementation artifact.

---

## 1. Approved role names

| Role | LOGIN/NOLOGIN |
|---|---|
| `netops_owner` | NOLOGIN |
| `netops_migrator` | LOGIN |
| `netops_runtime` | LOGIN |
| `netops_auth` | LOGIN |
| `netops_maintenance` | LOGIN |

No role receives `SUPERUSER`, `CREATEROLE`, `CREATEDB`, or `BYPASSRLS` unless a separately documented and explicitly approved requirement emerges.

---

## 2. Application schema

Use a dedicated `netops` schema instead of the default `public` schema for application objects. Schema selection, creation, and privilege grants are part of implementation planning and are not implemented by this document.

---

## 3. Role matrix

| Role | Ownership | Memberships | BYPASSRLS |
|---|---|---|---|
| `netops_owner` | Owns application database objects | None required | NO |
| `netops_migrator` | No direct ownership | `netops_owner` (`INHERIT FALSE`, `SET TRUE`, `ADMIN FALSE`) | NO |
| `netops_runtime` | None | None | NO |
| `netops_auth` | None | None | NO |
| `netops_maintenance` | None | None | NO |

---

## 4. Migration ownership model — Alternative A

- `netops_owner` is NOLOGIN and owns application database objects.
- `netops_migrator` is LOGIN and is used exclusively by the migration/deployment process.
- `netops_migrator` is a member of `netops_owner` with `INHERIT FALSE`, `SET TRUE`, `ADMIN FALSE`.
- Alembic connects as `netops_migrator`.
- Alembic must explicitly execute `SET ROLE netops_owner` before creating the Alembic version table or any application objects.
- Newly created objects are owned by `netops_owner`.
- Membership alone does not automatically make Alembic operate as the owner.
- Application runtime must never receive migrator credentials.

---

## 5. Membership semantics (PostgreSQL 16)

| Concept | Meaning in this design |
|---|---|
| `INHERIT FALSE` | Membership-specific option. `netops_migrator` does not automatically inherit `netops_owner` privileges. |
| `SET TRUE` | Permits `netops_migrator` to explicitly execute `SET ROLE netops_owner`. |
| `ADMIN FALSE` | Prevents `netops_migrator` from granting or revoking `netops_owner` membership to/from other roles. |
| `ADMIN OPTION` | Not granted. No separate role-admin role is created at this stage. |

---

## 6. Credential separation

Separate credentials are required for:

- Migration process (`netops_migrator`)
- Application runtime (`netops_runtime`)
- Authentication service (`netops_auth`)
- Maintenance jobs (`netops_maintenance`)

Migration credentials must not be available to the normal application runtime. Actual credentials are never printed, inspected, copied, or stored in this document. `.env` is not read.

---

## 7. RLS validation boundary

- RLS is **not** implemented in Milestone 8.2.3.
- No production tenant-table DML is granted to `netops_runtime` before RLS is implemented and validated.
- `rolbypassrls` must be validated through administrative catalog inspection of `pg_roles`; `SET ROLE` does not change this attribute.
- Actual tenant isolation validation is deferred to Milestone 8.2.6.

---

## 8. Default privileges and Alembic

Default privileges must be configured for the role that actually creates the objects. Under Alternative A, that role is `netops_owner`, because Alembic must explicitly `SET ROLE netops_owner` before DDL. `ALTER DEFAULT PRIVILEGES FOR ROLE netops_owner` applies to future objects created by `netops_owner`. Runtime access is granted explicitly per table only after RLS readiness.

---

## 9. Test isolation

PostgreSQL roles are cluster-global, not database-local. Future role tests must use a dedicated disposable PostgreSQL 16 container or cluster, or uniquely named test roles with reliable cleanup. Existing test harness safety requirements must be preserved:

- Use `NETOPS_TEST_DATABASE_URL` only.
- Never fall back to `NETOPS_DATABASE_URL`.
- Preserve existing database-name safety validation.
- Never run destructive tests against a production or development database.

---

## 10. Decisions now locked

1. Five role names: `netops_owner`, `netops_migrator`, `netops_runtime`, `netops_auth`, `netops_maintenance`.
2. Dedicated `netops` schema.
3. Migration credentials exclusively in the deployment/migration process.
4. Dedicated disposable PostgreSQL 16 container for role tests.
5. `BYPASSRLS` disabled for all five roles.
6. `ADMIN FALSE` on `netops_owner` membership granted to `netops_migrator`.
7. No separate role-admin role at this stage.
8. Alembic executes `SET ROLE netops_owner` before creating the Alembic version table or application objects.

---

## 11. Decisions still requiring approval

- Bootstrap SQL location and execution mechanism.
- Exact default-privilege grant statements for future objects.
- Role test cleanup and failure-resource handling procedures.
- Credential sourcing mechanism for deployment (environment, secrets manager, protected file).

---

## 12. Implementation boundaries

**In scope for Milestone 8.2.3 (future implementation only):**
- Role creation and privilege-boundary validation.
- Idempotent, secret-free role bootstrap SQL (not implemented by this document).
- Role migration/ownership model documentation.
- Disposable PostgreSQL 16 role tests.

**Out of scope — deferred to later milestones:**
- Milestone 8.2.4: Tenant context helper, `set_config(..., true)`, transaction-local context, request unit of work.
- Milestone 8.2.5: Authentication, server-side sessions, `sessions` table, auth role/function access, organization-selection endpoints.
- Milestone 8.2.6: RLS policies, runtime tenant-table DML grants, RLS isolation tests.

---

## 13. Repository state at document creation

- Branch: `feature/cisco-live-validation`
- HEAD: `991a4362b6298243277b01356e0c05d35087006d`
- No staged changes.
- Existing tracked modifications preserved:
  - `app/db/models.py`
  - `tests/db/test_migrations.py`
  - `tests/db/test_models.py`
  - `tests/db/test_role_constraints.py`
  - `tests/db/test_schema_consistency.py`
- Existing untracked files preserved.

---

**STOP.** This document is architecture only. No implementation has been performed. Await next authorization before Milestone 8.2.3 implementation.
