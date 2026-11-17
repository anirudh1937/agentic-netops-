# Milestone 8.2.4 — Tenant Context & Request Unit of Work

**Repository:** `C:\Users\Aniru\agentic-netops-old`
**Branch:** `feature/cisco-live-validation`
**HEAD:** `9bcc47c0ae93235ca1de24f703b2260afb9b1fa4`
**Date:** 2026-11-16

---

## 1. Implementation Overview

Milestone 8.2.4 establishes a secure, transaction-scoped tenant context and a
reliable database transaction lifecycle for application requests. It builds on
the existing Milestone 8.2.x foundation (session management, membership
composite key, role bootstrap) without duplicating or modifying completed work.

Two new modules were added:

- **`app/db/tenant.py`** — Transaction-scoped organization context helpers
  using PostgreSQL `set_config(..., true)` and `current_setting(..., true)`.
- **`app/db/unit_of_work.py`** — Request-scoped asynchronous Unit of Work that
  owns the SQLAlchemy `AsyncSession` lifecycle and transaction boundaries.

Two new test modules were added:

- **`tests/db/test_tenant_context.py`** — Offline tests for tenant context
  (mocked DB calls since `set_config` is PostgreSQL-only).
- **`tests/db/test_unit_of_work.py`** — Offline tests for Unit of Work
  lifecycle using SQLite.

---

## 2. Architecture Decisions Followed

Per `MILESTONE_8_2_0_ARCHITECTURE_DECISIONS.md` and `MILESTONE_8_2_FINAL_PREFLIGHT.md`:

- **Transaction-local tenant context:** `set_config('app.current_organization_id', value, true)` — the third argument `true` makes the setting reset automatically at COMMIT/ROLLBACK, preventing pooled-connection context leakage.
- **Canonical GUC key:** `app.current_organization_id` (matching the architecture's `app.current_org_id()` helper design).
- **Missing context fails closed:** `current_setting(..., true)` returns NULL, which must not match any tenant row.
- **No session-level `SET`:** Only transaction-local `set_config(..., true)` is used.
- **Parameterized SQL:** Tenant values are never interpolated into SQL text.
- **UUID validation:** Context values are validated as UUIDs before being sent to the database.
- **Unit of Work owns transaction boundaries:** Repositories do not independently commit.
- **No JWT, no auth endpoints:** Per milestone scope restrictions.

---

## 3. Tenant-Context Implementation

### File: `app/db/tenant.py`

| Function | Purpose |
|----------|---------|
| `set_organization_context(session, organization_id)` | Sets transaction-local org context via parameterized `set_config(:key, :value, true)`. Validates UUID before sending to DB. |
| `clear_organization_context(session)` | Clears context by setting the GUC to NULL. |
| `get_organization_context(session)` | Reads context via parameterized `current_setting(:key, true)`. Returns `None` if unset; raises `ValueError` on malformed values (fail closed). |
| `current_organization_id(session)` | Alias for `get_organization_context`. |

### Security properties

- **No raw token/session exposure:** Only organization UUIDs are used as context values.
- **No hardcoded secrets:** No credentials appear in this module.
- **UUID validation:** Non-UUID values are rejected before reaching the database.
- **Malformed context fails closed:** Non-UUID stored values raise `ValueError`.
- **Parameterized SQL:** Uses SQLAlchemy `text()` with bind parameters (`:key`, `:value`).

---

## 4. Unit-of-Work Implementation

### File: `app/db/unit_of_work.py`

| Method | Purpose |
|--------|---------|
| `async with UnitOfWork() as uow:` | Context manager; creates session on enter, closes on exit. |
| `await uow.begin()` | Begins an explicit transaction. Raises if already begun. |
| `await uow.commit()` | Commits the transaction, clears tenant context, closes session. |
| `await uow.rollback()` | Rolls back the transaction, clears context, closes session. |
| `await uow.cancel()` | Rolls back and closes (for cancellation/failure paths). |
| `await uow.set_organization_context(org_id)` | Sets tenant context (requires active transaction). |
| `await uow.get_organization_context()` | Reads current tenant context. |
| `uow.session` | Access to the `AsyncSession` for repository work. |

### Lifecycle guarantees

- Transactions are begun explicitly via `begin()`.
- Successful transactions are committed via `commit()`.
- Failed transactions are rolled back (via `__aexit__` on exception, or `rollback()`).
- Cancellation triggers cleanup via `cancel()` or `__aexit__`.
- Sessions are always closed (in `__aexit__`, `commit()`, `rollback()`, `cancel()`).
- Repositories cannot independently commit — they receive the session but the UoW owns `commit()`/`rollback()`.
- Nested `begin()`/`commit()` raises `InvalidRequestError`.
- Tenant context is cleared on both commit and rollback.

---

## 5. Commit and Rollback Behavior

| Scenario | Behavior |
|----------|----------|
| Successful request | `begin()` → work → `commit()` → context cleared → session closed |
| Exception in request | `__aexit__` rolls back → context cleared → session closed |
| Cancellation | `cancel()` rolls back → session closed |
| Double `begin()` | `InvalidRequestError` |
| Double `commit()` | `RuntimeError` ("Unit of work is not active") since session is closed after first commit |
| Commit after rollback | `InvalidRequestError` ("Transaction is already resolved") |

---

## 6. Security Considerations

- **No JWT implementation** — per scope restriction.
- **No raw token logging** — session tokens are not involved in tenant context.
- **No hardcoded secrets** — no credentials in tenant or UoW modules.
- **Tenant context is caller-settable** — per the architecture's candid assessment, transaction-local context limits persistence but does not make the value trustworthy against arbitrary SQL execution. Application authorization must validate membership before setting context.
- **Context is transaction-local** — `set_config(..., true)` resets at COMMIT/ROLLBACK, preventing pooled-connection leakage.
- **Missing context fails closed** — `current_setting(..., true)` returns NULL; no tenant rows match.

---

## 7. Files Changed

### New files (4)

| File | Purpose |
|------|---------|
| `app/db/tenant.py` | Tenant context helpers (set/get/clear organization context) |
| `app/db/unit_of_work.py` | Request-scoped async Unit of Work |
| `tests/db/test_tenant_context.py` | Offline tenant-context tests (mocked DB) |
| `tests/db/test_unit_of_work.py` | Offline UoW lifecycle tests (SQLite) |

### Modified files

None — no existing files were modified.

### Files NOT modified (intentionally)

- `app/db/session.py` — existing engine/session-factory lifecycle preserved
- `app/db/models.py` — no model changes needed
- `app/main.py` — no endpoint changes needed
- `alembic/env.py` — no metadata changes needed (Session already registered)
- `alembic/versions/0003_sessions.py` — already committed, no changes

---

## 8. Tests Executed and Exact Results

### New tests (18 total, all passing)

| Test file | Tests | Result |
|-----------|-------|--------|
| `tests/db/test_tenant_context.py` | 7 | 7 passed |
| `tests/db/test_unit_of_work.py` | 11 | 11 passed |

### Full offline regression suite (136 total, all passing)

| Test group | Count | Result |
|------------|-------|--------|
| `tests/auth/` | 45 | passed |
| `tests/db/test_migrations.py` | — | passed |
| `tests/db/test_models.py` | — | passed |
| `tests/db/test_schema_consistency.py` | — | passed |
| `tests/db/test_role_constraints.py` | — | passed |
| `tests/db/test_memberships_composite_key.py` | — | passed |
| `tests/db/test_postgres_test_support.py` | — | passed |
| `tests/db/test_postgres_16_safety.py` | — | passed |
| `tests/db/test_app_lifespan.py` | — | passed |
| `tests/db/test_connection.py` | — | passed |
| `tests/db/test_tenant_context.py` | 7 | passed |
| `tests/db/test_unit_of_work.py` | 11 | passed |
| **Total** | **136** | **136 passed, 0 failed** |

### Compilation checks

| File | Result |
|------|--------|
| `app/db/tenant.py` | py_compile OK |
| `app/db/unit_of_work.py` | py_compile OK |
| `tests/db/test_tenant_context.py` | py_compile OK |
| `tests/db/test_unit_of_work.py` | py_compile OK |

### Lint checks

| Check | Result |
|-------|--------|
| `git diff --check` | Exit code 0 (no whitespace errors) |

---

## 9. PostgreSQL Validation Status

**NOT YET VALIDATED against live PostgreSQL.**

The following require live PostgreSQL 16 validation and are documented as future work:

| Item | Reason |
|------|--------|
| `set_config('app.current_organization_id', value, true)` | `set_config` is a PostgreSQL function; not available in SQLite. |
| `current_setting('app.current_organization_id', true)` | Same — PostgreSQL-only. |
| Transaction-locality (context resets at COMMIT/ROLLBACK) | SQLite does not implement GUC transaction scoping. |
| Connection-pool isolation (no context leakage across pooled connections) | Requires live PostgreSQL with pooled connections. |
| `app.current_organization_id()` SQL function | Should be created via Alembic migration (planned as a future migration, not yet implemented). |

The tenant-context module uses parameterized SQL that is correct for PostgreSQL, but the actual runtime behavior can only be confirmed against a disposable PostgreSQL 16 instance (per Milestone 8.2.1's disposable-DB harness).

---

## 10. Known Limitations

1. **No live PostgreSQL validation** — Tenant context behavior (transaction-locality, pool isolation) is unvalidated against PostgreSQL.
2. **No Alembic migration for `app.current_organization_id()` function** — The architecture roadmap (section 3.5 of `MILESTONE_8_2_0_ARCHITECTURE_DECISIONS.md`) places canonical helper functions in a future migration (revision `0003` in the proposed sequence, but `0003` is already used by sessions). A new migration revision would be needed.
3. **`UnitOfWork` uses the module-level session factory** — Tests configure `NETOPS_DATABASE_URL` to SQLite. In production, this connects to the configured PostgreSQL URL.
4. **SQLite tests do not prove PostgreSQL tenant isolation** — Explicitly documented per project policy.

---

## 11. Recommended Next Milestone

**Milestone 8.2.5 — Authentication Session and Organization Selection**

Per the architecture roadmap (section 4 of `MILESTONE_8_2_0_ARCHITECTURE_DECISIONS.md`):

- Global authentication sessions
- Auth role/function access for user/session lookup
- Bootstrap endpoint(s) for the current user's permitted organizations
- Organization selection/switching with membership validation
- Store active organization server-side

This builds directly on the tenant context and Unit of Work implemented in this milestone.

---

**STOP.** Implementation complete. Awaiting review before any Git operations.