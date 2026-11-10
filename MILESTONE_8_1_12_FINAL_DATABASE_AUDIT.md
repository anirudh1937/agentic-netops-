# Milestone 8.1.12 — Final Database Foundation Audit

**Status:** Read-only final audit  
**Repository:** `C:\Users\Aniru\agentic-netops-old`  
**Branch:** `feature/cisco-live-validation`  
**HEAD:** `2d5ef652809069527cef71ba6cfd9f67b3bcb00b`  
**Audit date:** 2026-11-10

> This report is the only file written by this documentation task. No application code, model, migration, test, dependency, `alembic.ini`, or existing documentation file was modified. No Git mutation, staging, commit, push, reset, checkout, stash, or deletion was performed. No `.env` credentials were accessed, and no external service was contacted.

---

## 1. Executive Summary and Verdict

**Verdict: conditionally ready for Milestone 8.2.**

The implemented Milestone 8.1 database foundation is coherent and passes its offline validation suite. It provides a sound starting point for tenant isolation and RLS work in 8.2:

- Lazy asynchronous SQLAlchemy engine creation.
- Async session factory and request-scoped session dependency.
- Async engine disposal and repeated lifecycle support.
- PostgreSQL-oriented `asyncpg` configuration.
- Platform-independent UUID and UTC datetime types.
- Organizations, global users, and organization memberships.
- Named primary-key, foreign-key, unique, index, and CHECK constraints.
- Exact `ck_organization_memberships_role` enforcement.
- SQLite migration and schema-consistency tests.
- Async Alembic online migrations and PostgreSQL offline SQL generation.
- Working Alembic console-script and module invocation.
- Preserved Alembic logging behavior.
- FastAPI startup/shutdown integration for database, telemetry, and MCP resources.

**No Critical or High actual defect was found in the implemented 8.1 foundation.** No existing schema or lifecycle failure was discovered.

The high-priority gaps are validation prerequisites and design decisions for 8.2, not hidden defects in the current code:

1. PostgreSQL has **not** been executed live. Only SQLite execution and PostgreSQL offline SQL generation were tested.
2. RLS, tenant context, runtime/migration roles, and organization-selection bootstrap are not implemented; they are intentionally scheduled for 8.2.
3. `organization_memberships` has `UNIQUE (organization_id, user_id)` but not `UNIQUE (organization_id, id)`. The latter is required only if future tables reference membership IDs through composite tenant foreign keys.
4. Global `users` under RLS and organization selection before tenant context exists remain unresolved design decisions.
5. Request transaction/commit ownership and connection-pool tenant-context isolation must be designed before RLS can be trusted.

**No claim of live PostgreSQL or RLS compatibility is made by this report.**

---

## 2. Scope and Architecture Reviewed

### 2.1 Database foundation

The audit reviewed the asynchronous SQLAlchemy foundation:

- `app/db/session.py`
- `app/db/models.py`
- `app/db/types.py`
- `app/db/__init__.py`
- `app/main.py`
- `alembic.ini`
- `alembic/env.py`
- `alembic/versions/0001_initial_tables.py`
- `requirements.txt`

### 2.2 Models and schema

The current implemented schema contains exactly three tables:

| Table | Purpose |
|---|---|
| `organizations` | Tenant root. |
| `users` | Global user identity. |
| `organization_memberships` | Associates global users with organizations and roles. |

The schema uses globally unique UUID primary keys. `organization_memberships` carries `organization_id` and `user_id`, both non-null, and enforces the role values `org_admin` and `org_member`.

### 2.3 Platform types

- `GUID` maps to native PostgreSQL `UUID` and SQLite `CHAR(32)`.
- `UTCDateTime` wraps `DateTime(timezone=True)` and normalizes naive values to UTC on bind/result.
- UUID primary keys are single-column across all current tables.

### 2.4 Alembic

- `alembic/env.py` supports asynchronous online migrations through `async_engine_from_config()` and `connection.run_sync()`.
- Offline SQL generation is supported through `context.configure(url=...)`.
- `alembic.ini` uses `script_location = alembic`, `version_locations = alembic/versions`, `prepend_sys_path = .`, and `path_separator = os`.
- The database URL is read from `NETOPS_DATABASE_URL`; no credential value is stored in `alembic.ini`.

### 2.5 FastAPI lifespan integration

`app/main.py` integrates database initialization and disposal into the existing lifespan alongside telemetry and MCP resources. Database initialization is conditional on `NETOPS_DATABASE_URL` being set.

### 2.6 Tests and dependencies

Reviewed database tests:

- `tests/db/__init__.py`
- `tests/db/test_app_lifespan.py`
- `tests/db/test_connection.py`
- `tests/db/test_migrations.py`
- `tests/db/test_models.py`
- `tests/db/test_role_constraints.py`
- `tests/db/test_schema_consistency.py`

Reviewed dependency declarations in `requirements.txt` and Alembic configuration in `alembic.ini`.

### 2.7 Explicitly not implemented

The following are outside the implemented 8.1 foundation and were not claimed as present:

- Row-level security policies.
- Tenant context helpers or GUC setup.
- Runtime and migration database role separation.
- Authentication, authorization, sessions, or organization-selection flow.
- Collectors, devices, telemetry persistence, diagnosis, alerts, or audit tables.
- Composite tenant foreign keys beyond the current membership relationships.
- Database readiness probes that prove live connectivity.
- Live PostgreSQL migration or RLS execution.
- Any Milestone 8.2 implementation.

---

## 3. Schema and Model Findings

### 3.1 Global UUID primary keys

All current tables use single-column UUID primary keys:

```text
organizations.id            UUID PRIMARY KEY
users.id                    UUID PRIMARY KEY
organization_memberships.id UUID PRIMARY KEY
```

This preserves the 8.1.9 strategy of globally unique identifiers, avoid collisions across tenants, simplify future data movement, and support stable composite tenant-key design. **No defect found.**

### 3.2 Global users and multi-organization memberships

- `users` is a global identity table and has no `organization_id`.
- `organization_memberships` associates a global user with an organization.
- `UNIQUE (organization_id, user_id)` prevents duplicate membership of the same user in the same organization.
- Nothing limits a user to one organization.

This correctly supports multi-organization membership. **No defect found.**

### 3.3 Membership role constraint

The role constraint is named and enforced exactly as:

```sql
CONSTRAINT ck_organization_memberships_role
CHECK (role IN ('org_admin', 'org_member'))
```

The exact constraint name is present in both the model/migration intent and the PostgreSQL offline SQL output. The SQLite tests also verify valid and invalid role behavior and the exact CHECK name. **No defect found.**

### 3.4 Membership uniqueness

Implemented:

```sql
UNIQUE (organization_id, user_id)
```

Not implemented:

```sql
UNIQUE (organization_id, id)
```

`UNIQUE (organization_id, user_id)` is sufficient for future references expressed as:

```sql
FOREIGN KEY (organization_id, user_id)
    REFERENCES organization_memberships (organization_id, user_id)
```

It is not sufficient for a composite reference by membership surrogate ID:

```sql
FOREIGN KEY (organization_id, membership_id)
    REFERENCES organization_memberships (organization_id, id)
```

PostgreSQL requires a matching unique or primary-key constraint on the referenced columns. `PRIMARY KEY (id)` is not a composite target.

This is a **High-priority design decision**, not a current defect. Add `UNIQUE (organization_id, id)` before 8.2 if any planned table or audit record will reference membership IDs. If every membership reference uses `(organization_id, user_id)`, the key can be deferred and the convention documented.

### 3.5 Relationships and indexes

- `organization_memberships.organization_id` references `organizations.id` with `ON DELETE CASCADE`.
- `organization_memberships.user_id` references `users.id` with `ON DELETE CASCADE`.
- `organizations.slug` and `users.email` have unique indexes.
- `organization_memberships` has indexes on `organization_id` and `user_id`.

Lower-priority observations:

- `ix_organization_memberships_organization_id` duplicates the leading column of `UNIQUE (organization_id, user_id)` and may also be redundant with a future `(organization_id, id)` unique key. This is an extra-index concern, not a correctness defect.
- `email` and `slug` uniqueness are case-sensitive by default. Authentication and naming policy must decide whether case-insensitive uniqueness is required.
- `updated_at` is maintained by ORM `onupdate` only. Raw SQL updates will not refresh it without a database trigger.
- Model `Mapped[str]` annotations do not exactly describe the `uuid.UUID` values returned by `GUID.process_result_value()`. Runtime behavior and tests are consistent; this is an annotation concern.

None of these are Critical or High actual defects.

---

## 4. Database Lifecycle and Migration Findings

### 4.1 Milestone 8.1.10 Alembic CLI import fix

The console-script failure was caused by import-path resolution when Alembic was launched through `.venv\Scripts\alembic.exe`.

The fix added:

```ini
prepend_sys_path = .
path_separator = os
```

`prepend_sys_path = .` makes the repository root importable when Alembic is launched from the repository root. `path_separator = os` avoids the replaced `version_path_separator` configuration and is correct for Windows and POSIX paths.

Result: both the console script and `python -m alembic` resolve project imports and produce identical migration SQL.

### 4.2 Milestone 8.1.11 conditional database initialization

`app/main.py` now:

1. Creates the telemetry store and poller.
2. Reads the stale-telemetry threshold.
3. Checks whether `NETOPS_DATABASE_URL` is configured.
4. Calls `init_db()` only when configured.
5. Does **not** create tables at startup.
6. Does **not** proactively connect to the database at startup.
7. Starts the poller only when enabled.
8. Yields to the application.

This preserves the no-network startup requirement while making a configured engine and session factory available to database-backed code.

### 4.3 Shutdown and error precedence

Shutdown order:

1. Stop the poller if running.
2. Close the MCP toolset.
3. Dispose the database engine if a database URL was configured.
4. Re-raise the appropriate poller or database shutdown error.

Cleanup behavior:

- Startup failure after database initialization disposes the database.
- Poller startup failure cleans up the poller and the database.
- Shutdown continues through MCP and database cleanup even if poller shutdown fails.
- Cleanup errors do not replace the original startup exception.
- A normal-shutdown poller error retains precedence over a database disposal error.
- No database URL means no database initialization or disposal.
- No tables are created during application startup.

**Assessment:** the lifecycle integration meets the 8.1.11 requirements and preserves existing telemetry/MCP behavior.

### 4.4 Session lifecycle

- Engine creation is lazy.
- The session factory uses `async_sessionmaker(..., expire_on_commit=False)`.
- The request dependency creates one session per request and closes it when the generator completes.
- No global `AsyncSession` is shared.
- `dispose_db()` disposes the engine and resets the module-level globals.
- `init_db()` is idempotent and does not create tables.

Lower-priority lifecycle observations:

- The engine and session factory are module-level singletons shared by all FastAPI app instances in one process. Sequential lifespan cycles are tested. Concurrently live app instances with a database URL share one engine, so one shutdown disposes the other's engine. This is a Medium limitation for multi-app test fixtures or embedded use, not a single-process production failure.
- There is no explicit request transaction/commit policy yet. No database-backed route exists. Milestone 8.2 must define commit and rollback ownership rather than relying on autobegin behavior.
- There is no database connectivity check at startup. This is consistent with the current requirement, but `/health` does not prove database availability.

### 4.5 Alembic invocation

Both supported invocation methods work with the repository configuration:

- `.venv\Scripts\alembic.exe -c alembic.ini upgrade head --sql`
- `.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head --sql`

Both produce the same PostgreSQL offline SQL. No deprecation warning for the obsolete path-separator setting was emitted.

Lower-priority tooling observation: no automated pytest test invokes the installed console script; the console-script verification is currently manual. Also, no `alembic/script.py.mako` template is present in the tree. Applying revision `0001` does not require it, but future `alembic revision` generation will need a template or an explicitly hand-authored migration.

---

## 5. Security and Tenant-Isolation Readiness

### 5.1 Current state

The current foundation has **no RLS and no tenant context**. It is a schema and connection foundation, not a completed tenant-isolation implementation.

Not implemented:

- Row-level security policies on any table.
- Transaction-local tenant context.
- Runtime role separate from the table owner.
- Migration/DDL role separate from the runtime role.
- `BYPASSRLS` restrictions or `FORCE ROW LEVEL SECURITY` verification.
- Global-user authentication/bootstrap access path under RLS.
- Organization selection before tenant context exists.
- Request transaction/commit policy.
- Pooled-connection tenant-context isolation tests.

### 5.2 Preserved 8.1.9 tenant-key strategy

The 8.1.9 strategy remains authoritative:

- Globally unique UUID primary keys.
- Mandatory future `organization_id` on tenant-owned tables.
- `UNIQUE (organization_id, id)` on composite foreign-key target tables.
- Composite tenant foreign keys.
- RLS as defense in depth, not as the only isolation mechanism.

The current schema conforms for the tables that exist. Future tenant-owned tables must apply the remaining requirements as they are created.

### 5.3 Runtime and migration roles

No database roles or grants are implemented. Before RLS can be trusted:

- Create a non-owner runtime role without `BYPASSRLS`.
- Create a separate migration/DDL role.
- Grant only required runtime `SELECT`/DML privileges.
- Enable and force RLS on tenant-owned tables.
- Verify that table owners and superusers cannot silently bypass RLS in on-premises deployments.

This is a **High-priority 8.2 prerequisite**.

### 5.4 Global-user RLS and bootstrap

`users` has no `organization_id` and correctly remains a global identity table. A simple organization-scoped policy cannot directly govern the whole table.

Unresolved choices:

- Use a separate authentication role or security-definer function for user lookup and login.
- Apply a membership-aware policy using `EXISTS` against `organization_memberships`.
- Keep global user access behind application-level restrictions and apply tenant RLS only to tenant-owned tables.

Any solution must prevent a tenant from enumerating other tenants' users. This is a **High-priority 8.2 design decision**.

### 5.5 Organization selection before tenant context

An active organization cannot be required before the user has selected one. The 8.2 bootstrap path must:

1. Authenticate the user.
2. Establish a trusted user identity context.
3. Allow the user to read their own memberships and permitted organizations.
4. Select an active organization.
5. Set `app.current_organization_id` transaction-locally.
6. Apply strict tenant policies.

No authentication, session, or membership-selection flow exists yet. This is intentionally outside 8.1 and is a **High-priority design decision**.

### 5.6 Transaction-local tenant context

The intended direction, pending approval, is:

```sql
SELECT set_config('app.current_organization_id', :organization_id, true);
```

The `true` argument makes the setting transaction-local. It must be set inside the same transaction as the tenant queries.

Do not use session-level `SET`, and do not set tenant context on a pooled connection outside a transaction. Session-level context can leak to the next request that reuses the connection.

This is a **High-priority 8.2 design requirement**.

### 5.7 Request transaction and commit ownership

The current session dependency closes the session but does not define a commit boundary. Before database-backed routes are added, decide whether commits are owned by:

- the endpoint,
- a service/unit-of-work layer,
- or an explicit transaction dependency.

The decision must be documented and tested. Do not rely on implicit autobegin or on a context manager silently rolling back work that should have been committed.

### 5.8 Connection-pool context isolation

The current pool configuration is conventional but has no tenant context to isolate. RLS tests must verify that:

- Tenant context is set only inside a transaction.
- Two sequential requests that reuse the same pooled connection cannot see each other's organization context.
- Missing or invalid context denies access rather than falling through to unrestricted access.
- Reset behavior is correct after commit and rollback.

This is a **High-priority 8.2 test prerequisite**.

### 5.9 RLS defense in depth

RLS is not a substitute for correct tenant keys. The intended layering is:

1. Application authorization.
2. Composite tenant keys and foreign keys.
3. RLS policies on tenant-owned tables.
4. Non-owner runtime roles without bypass privileges.
5. PostgreSQL integration tests that prove cross-tenant denial.

No part of this layering beyond the base schema exists today.

---

## 6. Validation Evidence

### 6.1 Alembic console script

Command:

```text
.venv\Scripts\alembic.exe -c alembic.ini upgrade head --sql
```

Result:

```text
Exit code: 0
SQL lines: 54
Status: passed
```

### 6.2 Alembic module invocation

Command:

```text
.venv\Scripts\python.exe -m alembic -c alembic.ini upgrade head --sql
```

Result:

```text
Exit code: 0
SQL lines: 54
Status: passed
```

### 6.3 Output comparison

```text
console_status=0
module_status=0
diff_status=0
```

The two outputs were identical.

### 6.4 PostgreSQL offline CHECK constraint

The PostgreSQL offline SQL includes the exact role constraint:

```sql
CONSTRAINT ck_organization_memberships_role
CHECK (role IN ('org_admin', 'org_member'))
```

### 6.5 Database tests

Command:

```text
pytest tests/db/ -q
```

Exact result:

```text
46 passed, 2 warnings in 6.84s
```

This includes the seven focused FastAPI database-lifecycle tests in `tests/db/test_app_lifespan.py`.

### 6.6 Full test suite

Command:

```text
pytest -q
```

Exact result:

```text
415 passed, 2 skipped, 1 xfailed, 17 warnings in 156.07s
```

The two skips are the opt-in live Gemini and Cisco tests. The one xfail is the documented pre-existing MCP lifecycle case. The warnings are existing Google ADK/MCP deprecation or experimental warnings, not database failures.

### 6.7 Whitespace check

Command:

```text
git diff --check
```

Result:

```text
Exit code: 0
Output: none
```

This checks tracked modifications in the working tree. The audit report is untracked and was not part of the tracked diff when the check was performed.

### 6.8 Dependency check

Command:

```text
pip check
```

Result:

```text
No broken requirements found.
```

### 6.9 Test coverage observed

The database test suite covers:

- Engine creation without a URL raises a clear error.
- Session factory creation without a URL raises a clear error.
- SQLite async engine and session factory creation.
- Request session creation and closure.
- Engine disposal and pool cleanup.
- Optional development table creation.
- Alembic configuration loading, head resolution, upgrade, downgrade, and column presence.
- PostgreSQL async engine construction without connecting.
- PostgreSQL offline SQL with the exact role CHECK name.
- Exact SQLite DDL CHECK name.
- Valid and invalid role enforcement at database level.
- Model/migration schema consistency.
- FastAPI startup with and without a configured SQLite URL.
- Database disposal on shutdown.
- Database cleanup after startup failure.
- Poller startup-failure cleanup.
- Independent poller/MCP/database shutdown cleanup.
- Preservation of telemetry store and poller behavior.
- Repeated lifespan execution without leaked engine/factory state.

---

## 7. PostgreSQL Verification Boundaries

| Validation area | Status | Notes |
|---|---|---|
| SQLite migration and constraint execution | **TESTED** | Revision `0001` upgrades/downgrades; valid/invalid roles and exact CHECK name are exercised. |
| SQLite schema consistency | **TESTED** | Alembic-generated schema is compared with `Base.metadata`, excluding the documented SQLite UUID reflection difference. |
| PostgreSQL offline SQL generation | **TESTED** | `upgrade head --sql` emits PostgreSQL DDL for all current tables, constraints, and indexes. |
| asyncpg engine construction | **TESTED without connecting** | The PostgreSQL async engine can be constructed; no server connection was made. |
| PostgreSQL live migration execution | **NOT TESTED** | No live server was contacted. |
| PostgreSQL live CHECK enforcement | **NOT TESTED** | Only offline SQL and SQLite behavior were verified. |
| PostgreSQL native UUID and timestamptz behavior | **NOT TESTED** | Offline DDL exists; live behavior is unproven. |
| asyncpg pooling, pre-ping, recycle, timeout, disposal | **NOT TESTED** | Configuration exists; live behavior is unproven. |
| RLS policies and roles | **NOT TESTED** | Not implemented. |
| Tenant-context and pool-isolation behavior | **NOT TESTED** | Not implemented. |
| Composite tenant foreign keys | **NOT TESTED** | Not implemented. |
| Backup/restore, locking, migration concurrency | **NOT TESTED** | Out of scope for this audit. |

**No live PostgreSQL execution was performed. No RLS compatibility claim is made. No live PostgreSQL, Cisco, Gemini, DGX, or other external service was contacted. No `.env` credential value was accessed or exposed.**

---

## 8. Findings and Approval Decisions

### 8.1 Actual defects

**No Critical or High actual defect was found in the implemented 8.1 foundation.**

| ID | Severity/type | Finding | Impact |
|---|---|---|---|
| A-01 | Medium actual limitation | Module-level engine/session factory are shared across FastAPI app instances in one process. | Concurrent app instances share lifecycle; one shutdown disposes another's engine. Single-process production use is unaffected. |
| A-02 | Low actual defect | Model IDs are annotated `Mapped[str]` while `GUID` returns `uuid.UUID`. | Type contract mismatch; runtime tests pass. |
| A-03 | Low actual defect | `ix_organization_memberships_organization_id` duplicates the leading column of the membership unique constraint. | Extra index maintenance; no correctness impact. |
| A-04 | Low actual defect | `updated_at` is ORM-only with no database trigger. | Raw SQL writes do not refresh the timestamp. |
| A-05 | Low actual defect | Email and organization slug uniqueness are case-sensitive. | Authentication and naming policy must decide whether normalization or case-insensitive indexes are required. |
| A-06 | Low test gap | Missing-URL migration test only asserts the environment variable is absent; it does not exercise the migration error path. | Weak negative test. |
| A-07 | Low test gap | No automated test invokes the `.venv\Scripts\alembic.exe` console script. | Console-script validation is manual. |
| A-08 | Low tooling gap | No `alembic/script.py.mako` template is present. | Revision `0001` applies, but future `alembic revision` generation will need a template or hand-authored migrations. |
| A-09 | Low dependency issue | `httpx` is unpinned and `anyio` is a direct test import not declared in `requirements.txt`. | Reproducibility risk; current environment passes `pip check`. |
| A-10 | Low documentation drift | `MILESTONE_8_1_9_TENANT_KEY_STRATEGY.md` still says the lifespan does not integrate database initialization/disposal. | Documentation is stale after 8.1.11; no code impact. |

### 8.2 High-priority prerequisites and design decisions

These are **not current defects**. They are required before PostgreSQL/RLS readiness can be claimed.

| ID | Type | Prerequisite or decision | Required outcome |
|---|---|---|---|
| P-01 | Validation prerequisite | Disposable live PostgreSQL validation | Run revision `0001` online, verify native UUID/timestamptz/CHECK behavior, and capture actual constraints and privileges before claiming compatibility. |
| D-01 | High design decision | `UNIQUE (organization_id, id)` on memberships | Add it if membership IDs will be referenced by composite foreign keys; otherwise document the `(organization_id, user_id)` convention. |
| D-02 | High design decision | Global-user RLS/bootstrap | Choose a narrow auth/security-definer path or membership-aware policy; test cross-tenant user enumeration denial. |
| D-03 | High design decision | Organization selection before tenant context | Authenticate, establish trusted user identity, read permitted memberships, select an organization, then set tenant context transaction-locally. |
| P-02 | High prerequisite | Separate non-owner runtime and migration roles | Create a runtime role without `BYPASSRLS`; keep migration/DDL privileges separate; verify forced RLS and owner behavior. |
| D-04 | High design decision | Request transaction/commit ownership | Define whether endpoint, service/unit-of-work, or middleware owns commit/rollback; test the chosen boundary. |
| D-05 | High design requirement | Transaction-local tenant context | Use `set_config('app.current_organization_id', ..., true)` inside the request transaction; never use session-level context on pooled connections. |
| P-03 | High test prerequisite | Connection-pool context isolation | Prove sequential requests reusing a pooled connection cannot leak tenant context. |
| D-06 | Medium design decision | Sessions and audit classification | Keep sessions global with an optional active organization; keep tenant audit separate from platform audit. |
| D-07 | Medium design decision | On-premises RLS parity | Require PostgreSQL for RLS guarantees; test non-owner and forced-RLS behavior in the deployment model. |

### 8.3 Why these are not current defects

- RLS, tenant context, roles, authentication, and organization selection were explicitly outside the 8.1 foundation.
- The current schema is internally consistent for the three implemented tables.
- The offline PostgreSQL DDL is complete for the current tables and constraints.
- The lifecycle correctly avoids table creation and proactive connection.
- The absence of live PostgreSQL execution is a validation boundary, not evidence that the offline DDL is wrong.

### 8.4 Deferred lower-priority improvements

- Remove redundant organization-only indexes after query-plan review.
- Consider case-insensitive email/slug indexes or normalization.
- Add database-side `updated_at` triggers if non-ORM writes become possible.
- Pin `httpx` and declare direct `anyio` test usage.
- Add an automated console-script invocation test.
- Add `alembic/script.py.mako` before generating the next revision.
- Document database URL format, migration commands, and role model.
- Add database readiness/observability without forcing startup network calls.
- Add PostgreSQL integration tests to CI when a disposable server is available.
- Partition or archive high-volume telemetry in a later milestone.

---

## 9. Final Repository State and Recommendation

### 9.1 Branch and HEAD

```text
Branch: feature/cisco-live-validation
HEAD:   2d5ef652809069527cef71ba6cfd9f67b3bcb00b
```

### 9.2 Complete Git status observed before this report update

```text
 M app/main.py
 M requirements.txt
?? MILESTONE_8_1_12_FINAL_DATABASE_AUDIT.md
?? MILESTONE_8_1_9_TENANT_KEY_STRATEGY.md
?? PROJECT_STATUS.backup.md
?? alembic.ini
?? alembic/env.py
?? alembic/versions/0001_initial_tables.py
?? alembic/versions/__init__.py
?? app/db/__init__.py
?? app/db/models.py
?? app/db/session.py
?? app/db/types.py
?? dbg.txt
?? "tatus --short"
?? test_results.txt
?? tests/db/__init__.py
?? tests/db/test_app_lifespan.py
?? tests/db/test_connection.py
?? tests/db/test_migrations.py
?? tests/db/test_models.py
?? tests/db/test_role_constraints.py
?? tests/db/test_schema_consistency.py
?? tstest_integrations.py
```

### 9.3 Working-tree interpretation

- **Existing tracked modifications:** `app/main.py` and `requirements.txt`.
  - `app/main.py` contains the Milestone 8.1.11 conditional database lifecycle integration.
  - `requirements.txt` contains the earlier DB dependency updates.
- **Intended Milestone 8.1 untracked files:** the Alembic configuration and migration, `app/db/` files, `tests/db/` tests, the 8.1.9 strategy document, and this audit report.
- **Unrelated untracked artifacts preserved but not part of this documentation task:**
  - `PROJECT_STATUS.backup.md`
  - `dbg.txt`
  - `"tatus --short"`
  - `test_results.txt`
  - `tstest_integrations.py`
- **No staged changes.**
- **No credentials, `.env` contents, or generated secrets are included in this report or in the intended documentation task.** No `.env` file was opened or read.

At the start of this documentation attempt, `MILESTONE_8_1_12_FINAL_DATABASE_AUDIT.md` was already present from the earlier interrupted write. It was completed in place. No application code, model, migration, test, dependency, `alembic.ini`, or existing documentation file was created or modified by this task. No Git mutation was performed.

### 9.4 Recommendation

**Conditionally proceed to pre-commit review** only after confirming this report exists and reviewing the intended Milestone 8.1 file set.

Before Milestone 8.2 implementation begins, approve or complete the high-priority prerequisites and decisions in section 8.2. Do not claim PostgreSQL or RLS readiness until a disposable live PostgreSQL validation pass succeeds.

**Milestone 8.2 has NOT started.**
