# Milestone 8.1.9 — Composite Tenant-Key Strategy

**Status:** Design and review only  
**Repository:** `C:\Users\Aniru\agentic-netops-old`  
**Branch:** `feature/cisco-live-validation`  
**HEAD at review:** `2d5ef652809069527cef71ba6cfd9f67b3bcb00b`  
**Date:** 2026-11-09

No application code, database model, migration, test, requirement, or FastAPI lifecycle change was made for this milestone. This document is the only file created.

---

## Executive summary

The current schema has a sound tenant root (`organizations`) and a sound global-identity model (`users` plus `organization_memberships`). It does not yet contain tenant-owned operational tables, so the composite-key strategy can still be established without migrating operational data.

The recommended strategy is:

1. Keep a **globally unique UUID `id` primary key** on every entity.
2. Put **`organization_id UUID NOT NULL`** on every tenant-owned table.
3. Add **`UNIQUE (organization_id, id)`** to every tenant-owned table that is the target of a composite foreign key.
4. Have tenant-owned child tables reference parents through **composite foreign keys**:
   ```sql
   FOREIGN KEY (organization_id, parent_id)
       REFERENCES parent (organization_id, id)
   ```
5. Use **composite foreign keys as defense in depth alongside RLS**, not as a replacement for RLS.
6. Add **`UNIQUE (organization_id, id)` to `organization_memberships`** before 8.2 if future tables will reference memberships by surrogate ID.
7. Keep `users` global. Do not add `organization_id` to `users`; use membership records for tenant association.
8. Create tenant-owned tables in dependency order, with parent unique keys present before child foreign keys.

This preserves globally stable IDs, supports multiple organizations per user, prevents cross-tenant references even if an application query is wrong, and aligns with PostgreSQL composite-FK and RLS requirements.

---

## 1. Current schema assessment

### 1.1 Files inspected

- `app/db/models.py`
- `app/db/types.py`
- `app/db/session.py`
- `alembic/versions/0001_initial_tables.py`
- `alembic/env.py`
- `alembic.ini`
- `tests/db/test_models.py`
- `tests/db/test_migrations.py`
- `tests/db/test_role_constraints.py`
- `tests/db/test_schema_consistency.py`
- `tests/db/test_connection.py`
- `requirements.txt`
- FastAPI lifespan in `app/main.py`

### 1.2 Confirmed current tables

| Table | Current primary key | Tenant key | Current uniqueness | Current indexes | Assessment |
|---|---|---|---|---|---|
| `organizations` | `id UUID` | The row itself is the tenant boundary | `slug` unique index | `ix_organizations_slug` | Tenant root / platform registry. No `organization_id` column is needed. |
| `users` | `id UUID` | None | `email` unique index | `ix_users_email` | Global identity. A user can belong to multiple organizations. |
| `organization_memberships` | `id UUID` | `organization_id UUID NOT NULL` | `UNIQUE (organization_id, user_id)` | `ix_organization_memberships_organization_id`, `ix_organization_memberships_user_id` | Tenant-to-user association. RLS-scoped by `organization_id`. |
| `alembic_version` | Version string | None | Primary key | Alembic-managed | Platform schema metadata, not tenant data. |

Current relationships:

- `organization_memberships.organization_id` references `organizations.id` with `ON DELETE CASCADE`.
- `organization_memberships.user_id` references `users.id` with `ON DELETE CASCADE`.
- The membership role CHECK accepts only `org_admin` and `org_member`.
- Milestone 8.1.8 corrected the migration/model CHECK name drift. The current model and revision `0001` both produce `ck_organization_memberships_role`.

### 1.3 Confirmed current behavior

- The database layer uses async SQLAlchemy with `asyncpg` for PostgreSQL and `aiosqlite` for SQLite tests.
- `GUID` uses native PostgreSQL `UUID` and `CHAR(32)` on SQLite.
- `UTCDateTime` uses timezone-aware datetime behavior.
- Alembic revision `0001` contains the three current tables.
- SQLite migration, schema consistency, role enforcement, and connection tests exist.
- No tenant-owned collector, device, telemetry, diagnosis, alert, or audit table exists yet.
- No RLS policies, tenant GUC, database application role, or tenant context exists.
- The FastAPI lifespan conditionally initializes the database engine and session factory when `NETOPS_DATABASE_URL` is configured. Startup does not proactively connect or create tables, shutdown disposes the engine, and existing telemetry and MCP cleanup behavior is preserved. It does not provide a database session dependency.

### 1.4 Current schema gaps for the future

1. `organization_memberships` has `UNIQUE (organization_id, user_id)`, which is a valid composite foreign-key target for `(organization_id, user_id)`.
2. It does **not** have `UNIQUE (organization_id, id)`. A future composite foreign key targeting `(organization_id, id)` is therefore invalid, even though `id` is already the single-column primary key.
3. `organizations` has only `PRIMARY KEY (id)`. That is sufficient for ordinary `organization_id → organizations.id` references. It is not a composite target and does not need to be.
4. `users` has only `PRIMARY KEY (id)`. It should remain a global identity table; it should not become a tenant composite target by adding organization columns.
5. No tenant-owned operational table exists, so no current data migration is required to introduce `organization_id` on those tables.

---

## 2. Global versus tenant-owned table classification

The classifications below are proposed classifications for planned tables. Planned tables are **not** being implemented in 8.1.9.

| Entity | Classification | `organization_id` required? | Composite FK target needed? | Rationale |
|---|---|---:|---:|---|
| `organizations` | Tenant root / platform registry | No, its own `id` is the tenant key | No | One row represents one tenant. Simple `organization_id → organizations.id` references are sufficient. |
| `users` | Global identity | No | No | A user can belong to multiple organizations. Tenant relationship belongs in memberships. |
| `organization_memberships` | Tenant-to-user association | Yes | Yes, depending on reference style | Tenant-scoped relationship. Existing `UNIQUE (organization_id, user_id)` supports `(organization_id, user_id)` targets. Add `UNIQUE (organization_id, id)` if future tables reference membership IDs. |
| `sessions` | Platform/auth operational table (assumption) | No, unless sessions are explicitly organization-scoped | No for tenant data | Authentication sessions are user-level. An optional `active_organization_id` is context, not ownership. If a session must be owned by one organization, redesign it as tenant-owned with `organization_id`. |
| `collectors` | Tenant-owned | Yes | Yes | A collector belongs to one organization and is a parent of devices. |
| `devices` | Tenant-owned | Yes | Yes | A device belongs to one organization and may belong to a collector in the same organization. |
| `telemetry_records` | Tenant-owned | Yes | Yes if referenced by diagnosis/alerts | Telemetry belongs to a tenant and a device in that tenant. High-volume time-series table. |
| `diagnosis_events` | Tenant-owned | Yes | Yes if referenced by alerts/audit | Diagnosis concerns a tenant's telemetry and device. |
| `alerts` | Tenant-owned | Yes | Yes if referenced by other tenant records | Alerts are tenant operational data and commonly reference devices, telemetry, or diagnoses. |
| `audit_events` | Tenant-owned for tenant actions | Yes | Usually no; avoid FK dependencies on mutable operational rows | Tenant audit records should survive deletion of the resources they describe. Platform-level events should use a separate platform audit table. |
| `alembic_version` | Platform operational | No | No | Schema migration metadata. |

### 2.1 Classification rules

- **Global table:** The row is not owned by a tenant and does not carry `organization_id`. `users` is the primary example. `organizations` is a tenant-root registry rather than a tenant-owned row.
- **Tenant-owned table:** A row belongs to exactly one organization; `organization_id` is mandatory; access is filtered by tenant; RLS applies.
- **Tenant-to-user association table:** A row connects a global user to a tenant. It carries `organization_id` and `user_id`; it is tenant-scoped for RLS but also references a global identity.
- **Platform-level operational table:** Platform-wide schema/auth/observability data with no tenant ownership. `alembic_version` is an example. A platform audit stream should be separate from tenant audit data.

### 2.2 Important distinction: global user identity versus tenant-owned actor data

`users` remains global. A tenant-owned record should not use a bare `user_id → users.id` foreign key as proof of tenant membership. If the record needs to prove that the actor belongs to the organization, use one of:

- a composite reference to `organization_memberships (organization_id, user_id)` if the design uses user IDs; or
- a composite reference to `organization_memberships (organization_id, id)` if the design uses membership IDs and the additional membership unique key is added.

For historical audit records, a hard foreign key to a membership may be inappropriate because memberships can be removed. In that case, store the tenant, actor user ID, and membership ID as historical references without a cascading FK, and document the retention behavior.

---

## 3. Composite key conventions

### 3.1 Primary keys remain globally unique UUIDs

Recommendation:

- Every table keeps `id UUID PRIMARY KEY` with a generated UUID.
- Do not replace the primary key with `(organization_id, id)` for tenant-owned tables.

Why:

- Existing and future APIs can address resources by one stable global ID.
- ORM identity maps and relationships remain simpler.
- A global UUID remains unique across tenants and deployment environments.
- Composite foreign keys can still enforce tenant ownership by referencing a separate unique tenant key.

The alternative — a composite primary key `(organization_id, id)` — would automatically satisfy composite foreign-key targets and make tenant identity part of the physical key. However, it changes resource identity semantics, complicates ORM relationships and API paths, and allows different tenants to reuse the same `id` value. That is a larger architectural change and is not recommended for the current codebase.

### 3.2 Tenant-owned tables always carry `organization_id`

For every tenant-owned table:

```sql
organization_id UUID NOT NULL
```

Also add a direct foreign key to the tenant root:

```sql
FOREIGN KEY (organization_id)
    REFERENCES organizations (id)
```

The direct tenant FK is useful for existence, cascade policy, and RLS clarity. It is not a substitute for composite parent references.

### 3.3 Add `UNIQUE (organization_id, id)` when the table is a composite FK target

PostgreSQL requires a unique or primary-key constraint on exactly the referenced column set. A single-column primary key on `id` does not make `(organization_id, id)` unique.

Add:

```sql
UNIQUE (organization_id, id)
```

to every tenant-owned table that is the target of a composite foreign key.

Recommended targets:

| Table | `UNIQUE (organization_id, id)` | Reason |
|---|---:|---|
| `organization_memberships` | Recommended if membership IDs will be referenced | Enables `(organization_id, membership_id)` composite FKs. Existing `(organization_id, user_id)` remains useful for user-based membership references. |
| `collectors` | Required | Devices reference collectors by `(organization_id, collector_id)`. |
| `devices` | Required | Telemetry references devices by `(organization_id, device_id)`. |
| `telemetry_records` | Required if diagnosis references telemetry | Diagnosis references telemetry by `(organization_id, telemetry_id)`, or a three-column key for same-device consistency. |
| `diagnosis_events` | Required if alerts/audit reference diagnosis | Alerts may reference `(organization_id, diagnosis_id)`. |
| `alerts` | Only if another table references alerts | Leaf references may not need it. |
| `audit_events` | Usually no | Audit should avoid hard dependencies on mutable records. |

This unique constraint is redundant for global uniqueness because `id` is already a primary key. It is required for the composite foreign-key target and is the cost of the global-UUID strategy.

### 3.4 Tenant-owned child references use composite foreign keys

The standard child pattern is:

```sql
FOREIGN KEY (organization_id, parent_id)
    REFERENCES parent (organization_id, id)
```

Both columns must be populated from the same tenant context. The presence of `organization_id` on the child means:

- RLS can filter the child table directly.
- A child row cannot reference a parent in another organization.
- Indexes can lead with `organization_id` for tenant-scoped queries.
- The application does not have to trust a bare parent ID.

Do not use only:

```sql
FOREIGN KEY (parent_id) REFERENCES parent (id)
```

for tenant-owned relationships. It enforces existence but not tenant ownership.

### 3.5 Same-parent-chain consistency

For a chain such as organization → device → telemetry → diagnosis, a two-column foreign key only proves that the child and parent are in the same organization. It does not always prove that the reference chain is internally consistent.

Example: a diagnosis row might contain both `device_id` and `telemetry_id`. If both are separately constrained to the organization, a bug could pair telemetry from device A with `device_id` for device B in the same organization.

For chains where this matters, add a three-column unique target:

```sql
UNIQUE (organization_id, device_id, id)
```

on `telemetry_records`, then use:

```sql
FOREIGN KEY (organization_id, device_id, telemetry_id)
    REFERENCES telemetry_records (organization_id, device_id, id)
```

This enforces both tenant and device consistency. Use it only where the same-device relationship is a real invariant.

### 3.6 Nullable composite foreign keys

PostgreSQL defaults to `MATCH SIMPLE` for multi-column foreign keys. If any referencing column is NULL, the constraint is not checked. Consequences:

- `organization_id` must be `NOT NULL` so a tenant record can never escape tenant enforcement through a NULL organization.
- A nullable optional parent ID such as `collector_id` may be NULL, and the composite FK will not be enforced for that row. That is acceptable only when the relationship is genuinely optional.
- Do not use `MATCH FULL` as a workaround when `organization_id` is always non-null; it would require every nullable parent ID to be non-null whenever organization is non-null.

### 3.7 `ON DELETE` behavior with composite foreign keys

`ON DELETE SET NULL` on a multi-column foreign key sets all referencing columns to NULL. If `organization_id` is `NOT NULL`, this will fail.

Therefore, for composite tenant foreign keys:

- Prefer `ON DELETE CASCADE` for wholly dependent operational data where deleting the parent should delete the child (for example, device → telemetry, if that is the retention policy).
- Prefer `ON DELETE RESTRICT` or `NO ACTION` where history must be preserved or where the child can be reassigned.
- Do not use unqualified `ON DELETE SET NULL` with a composite FK that includes a `NOT NULL organization_id`.
- If a deployment requires PostgreSQL's column-list form of `ON DELETE SET NULL`, verify the supported PostgreSQL version first; do not make it a baseline assumption.

### 3.8 Constraint and index naming

The current naming convention is:

```python
{
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

The existing `fk` convention uses only the first child column and the referenced table. It becomes ambiguous for multiple composite foreign keys from the same child table to the same parent table. For example, two collector references from `devices` would both start with `organization_id` and both refer to `collectors`.

Recommended naming rules for new constraints:

- Primary key: `pk_<table>`.
- Tenant target key: `uq_<table>_organization_id_id`.
- Tenant natural key: `uq_<table>_organization_id_<natural_key>`.
- Composite foreign key: explicit descriptive name such as:
  - `fk_devices_organization_id_collector_id_collectors`
  - `fk_telemetry_records_organization_id_device_id_devices`
  - `fk_diagnosis_events_org_device_telemetry`
- Index: `ix_<table>_<tenant_columns>_<query_columns>`.

Do not rely on the current `fk` convention to generate unique names for composite foreign keys. Use explicit names and verify them in offline PostgreSQL SQL. PostgreSQL truncates identifiers at 63 bytes, so names must be checked for length and uniqueness.

The current CHECK convention should continue to use the model-side pattern `name="role"` so the convention produces `ck_organization_memberships_role` exactly once.

### 3.9 Index strategy

A `UNIQUE (organization_id, id)` constraint creates a btree index with `organization_id` as its leading column. That can satisfy queries filtered only by `organization_id`, so a separate `ix_<table>_organization_id` is often redundant.

Recommended index rules:

1. Do not create a separate organization-only index when a unique `(organization_id, id)` index already exists and all tenant queries filter by organization first.
2. Add composite indexes for actual access patterns, for example:
   - `(organization_id, collector_id)` for device-by-collector queries.
   - `(organization_id, device_id, observed_at)` for telemetry time-series queries.
   - `(organization_id, created_at)` for diagnosis/alert history.
   - `(organization_id, status, created_at)` for alert queues.
   - `(organization_id, occurred_at)` for tenant audit streams.
3. Add an index on `organization_memberships.user_id` for “organizations for this user”; that index is not replaced by the tenant composite index.
4. PostgreSQL does not automatically index foreign-key referencing columns. Add indexes for common parent lookups and deletes.
5. Avoid duplicate indexes. The current `ix_organization_memberships_organization_id` is already largely redundant with `UNIQUE (organization_id, user_id)` and would become more redundant if `UNIQUE (organization_id, id)` is added. It can be removed in a later migration after confirming query plans.

### 3.10 Composite unique key for natural tenant data

Natural keys should generally be tenant-scoped:

- `UNIQUE (organization_id, serial_number)` for devices, if serial numbers are unique within a tenant.
- `UNIQUE (organization_id, name)` for collectors, if names are unique within a tenant.
- Global `organizations.slug` and `users.email` uniqueness can remain global.

Do not make operational natural keys globally unique unless the business rule truly requires it.

---

## 4. Recommended relationship diagram

```text
organizations (tenant root / registry)
    │
    ├──< organization_memberships >── users (global identity)
    │          │
    │          └── organization_id, user_id, role
    │
    ├──< collectors
    │          │
    │          └──< devices (collector_id optional)
    │                     │
    │                     └──< telemetry_records
    │                                │
    │                                └──< diagnosis_events
    │                                           │
    │                                           └──< alerts
    │
    ├──< alerts
    └──< audit_events (tenant actions)

users
    └──< sessions (global auth session; optional active_organization_id)

alembic_version (platform schema metadata)
```

Legend:

- `──<` means one-to-many.
- Every tenant-owned table below `organizations` carries `organization_id NOT NULL`.
- Every arrow from a tenant-owned child to a tenant-owned parent is a composite foreign key on `(organization_id, parent_id)`.
- `sessions` is not tenant-owned under the recommended assumption; it is a global authentication session with an optional active organization reference.
- Platform-level audit events, if required, should be a separate table rather than rows with nullable `organization_id` mixed into tenant audit data.

---

## 5. Example SQL constraints

These are design examples only. They are not migrations to implement in 8.1.9.

### 5.1 Collectors

```sql
CREATE TABLE collectors (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    name TEXT NOT NULL,

    CONSTRAINT uq_collectors_organization_id_id
        UNIQUE (organization_id, id),

    CONSTRAINT fk_collectors_organization_id_organizations
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE
);
```

Why:

- `organization_id` establishes tenant ownership.
- `UNIQUE (organization_id, id)` is the composite target required by devices.
- The direct organization foreign key provides existence and tenant-root cascade.

### 5.2 Devices belonging to an organization and optionally a collector

```sql
CREATE TABLE devices (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    collector_id UUID,
    name TEXT NOT NULL,
    serial_number TEXT,

    CONSTRAINT uq_devices_organization_id_id
        UNIQUE (organization_id, id),

    CONSTRAINT uq_devices_organization_id_serial_number
        UNIQUE (organization_id, serial_number),

    CONSTRAINT fk_devices_organization_id_organizations
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_devices_organization_id_collector_id_collectors
        FOREIGN KEY (organization_id, collector_id)
        REFERENCES collectors (organization_id, id)
);
```

Why:

- The device cannot reference a collector from another organization.
- `collector_id` may be NULL under `MATCH SIMPLE`, representing an unassigned device.
- The organization foreign key is still mandatory.
- If a collector must be required, change `collector_id` to `NOT NULL`.
- Default `NO ACTION` prevents deleting a collector while assigned devices exist. Reassign or cascade deliberately.

### 5.3 Telemetry referencing a device in the same organization

```sql
CREATE TABLE telemetry_records (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    device_id UUID NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB,

    CONSTRAINT uq_telemetry_records_organization_id_id
        UNIQUE (organization_id, id),

    CONSTRAINT uq_telemetry_records_org_device_id
        UNIQUE (organization_id, device_id, id),

    CONSTRAINT fk_telemetry_records_organization_id_organizations
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_telemetry_records_organization_id_device_id_devices
        FOREIGN KEY (organization_id, device_id)
        REFERENCES devices (organization_id, id)
        ON DELETE CASCADE
);
```

Why:

- `(organization_id, device_id)` proves the telemetry and device share a tenant.
- `UNIQUE (organization_id, id)` supports generic tenant-aware references to telemetry.
- `UNIQUE (organization_id, device_id, id)` supports the stronger same-device diagnostic reference in section 5.4.
- `ON DELETE CASCADE` is appropriate only if telemetry should disappear with its device. If retention or legal hold applies, use `RESTRICT` and archive instead.

### 5.4 Diagnosis event referencing telemetry from the same organization and device

```sql
CREATE TABLE diagnosis_events (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    device_id UUID NOT NULL,
    telemetry_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    result JSONB,

    CONSTRAINT uq_diagnosis_events_organization_id_id
        UNIQUE (organization_id, id),

    CONSTRAINT fk_diagnosis_events_organization_id_organizations
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_diagnosis_events_org_device_telemetry
        FOREIGN KEY (organization_id, device_id, telemetry_id)
        REFERENCES telemetry_records (organization_id, device_id, id)
        ON DELETE RESTRICT
);
```

Why:

- The three-column foreign key proves that the diagnosis, telemetry, and device are all in the same organization **and** that the telemetry belongs to that device.
- A two-column `(organization_id, telemetry_id)` foreign key would prove only tenant consistency, not device consistency.
- `ON DELETE RESTRICT` preserves diagnosis history if telemetry is deleted. If diagnosis must be deleted with telemetry, use `CASCADE` deliberately.
- A direct `(organization_id, device_id) → devices (organization_id, id)` foreign key is optional because the telemetry reference already implies the device; add it if independent device validation is useful.

### 5.5 Alerts referencing tenant resources

```sql
CREATE TABLE alerts (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    device_id UUID NOT NULL,
    telemetry_id UUID,
    diagnosis_id UUID,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_alerts_organization_id_id
        UNIQUE (organization_id, id),

    CONSTRAINT fk_alerts_organization_id_organizations
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_alerts_organization_id_device_id_devices
        FOREIGN KEY (organization_id, device_id)
        REFERENCES devices (organization_id, id),

    CONSTRAINT fk_alerts_organization_id_telemetry_id_telemetry_records
        FOREIGN KEY (organization_id, telemetry_id)
        REFERENCES telemetry_records (organization_id, id),

    CONSTRAINT fk_alerts_organization_id_diagnosis_id_diagnosis_events
        FOREIGN KEY (organization_id, diagnosis_id)
        REFERENCES diagnosis_events (organization_id, id)
);
```

Why:

- Every optional reference is still tenant-constrained.
- `telemetry_id` and `diagnosis_id` are nullable optional references; under `MATCH SIMPLE`, a NULL optional reference is not checked.
- This example proves same-organization ownership, not same-device consistency across the optional references. If an alert must reference telemetry and diagnosis from the same device, use three-column unique targets and composite FKs, or derive the redundant device from one authoritative source.

### 5.6 Membership target keys

Current schema already has:

```sql
UNIQUE (organization_id, user_id)
```

This supports:

```sql
FOREIGN KEY (organization_id, user_id)
    REFERENCES organization_memberships (organization_id, user_id)
```

If future tables reference membership rows by surrogate ID, add:

```sql
ALTER TABLE organization_memberships
    ADD CONSTRAINT uq_organization_memberships_organization_id_id
    UNIQUE (organization_id, id);
```

Then this is valid:

```sql
FOREIGN KEY (organization_id, membership_id)
    REFERENCES organization_memberships (organization_id, id)
```

Trade-offs:

- **Add the key:** uniform `(organization_id, id)` references work for memberships, devices, collectors, telemetry, and diagnosis; one membership record can be referenced by ID.
- **Do not add the key:** the schema stays smaller, but membership references must use `(organization_id, user_id)` or rely on application checks.
- The additional unique index is redundant for global uniqueness, but it is required for a composite FK target. It is recommended before 8.2 if the design will use membership IDs.

### 5.7 Sessions

Recommended default interpretation:

```sql
CREATE TABLE sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL,
    active_organization_id UUID,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT fk_sessions_user_id_users
        FOREIGN KEY (user_id) REFERENCES users (id)
        ON DELETE CASCADE,

    CONSTRAINT fk_sessions_active_organization_id_organizations
        FOREIGN KEY (active_organization_id) REFERENCES organizations (id)
);
```

This is a global authentication session, not tenant-owned data. Validate that the user is a member of `active_organization_id` before using it as tenant context.

If the product instead requires sessions to be owned by one organization, add `organization_id NOT NULL`, a composite membership/organization reference, and RLS. That is a different design and requires approval.

### 5.8 Audit events

Recommended interpretation:

```sql
CREATE TABLE audit_events (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL,
    actor_user_id UUID,
    actor_membership_id UUID,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    details JSONB,

    CONSTRAINT fk_audit_events_organization_id_organizations
        FOREIGN KEY (organization_id)
        REFERENCES organizations (id)
);
```

Audit events describing tenant actions are tenant-owned and RLS-scoped. They should usually avoid cascading foreign keys to mutable operational rows, because deleting a device or membership must not delete or invalidate audit history. Store historical identifiers and enforce tenant access through `organization_id` and RLS.

Platform-level events should use a separate platform audit table. A nullable `organization_id` in a tenant audit table makes RLS, indexing, and retention semantics ambiguous.

---

## 6. RLS integration strategy

RLS is **not** implemented in 8.1.9. The following is the target design for 8.2.

### 6.1 RLS prerequisites

- Every tenant-owned table has `organization_id NOT NULL`.
- `organizations` is filtered by `id`; it does not need an `organization_id` column.
- `organization_memberships` is filtered by its `organization_id`.
- `users` and authentication sessions are handled by a separate auth access path because they are global/user-level.
- The application connects as a non-owner role with no `BYPASSRLS`.
- Tables use `FORCE ROW LEVEL SECURITY` where the application role could otherwise be the owner.
- Migrations run under a separate migration role.
- The application does not grant DDL or unnecessary `TRUNCATE` privileges to the runtime role.

### 6.2 Transaction-local tenant context

Set tenant context inside the transaction, not on a pooled session:

```sql
SELECT set_config('app.current_organization_id', :organization_id, true);
```

The `true` argument makes the setting transaction-local. It resets at transaction end and does not leak to the next request using the same pooled connection.

Do not use:

```sql
SET app.current_organization_id = ...;
```

for a pooled application connection. Session-level settings persist across transactions and can leak tenant context.

The application should derive the organization ID from authenticated membership, not from an untrusted path/query/header parameter. It should validate that the user is a member of that organization before setting context.

### 6.3 Policy shape

For tenant-owned tables:

```sql
ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE devices FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON devices
    USING (
        organization_id =
        current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id =
        current_setting('app.current_organization_id', true)::uuid
    );
```

Policy semantics:

- `USING` controls which existing rows are visible to `SELECT`, `UPDATE`, and `DELETE`.
- `WITH CHECK` controls rows inserted or updated.
- Both are needed for tenant-owned tables where writes are allowed.
- Policy names should be unique and descriptive; `tenant_isolation` per table is acceptable in PostgreSQL because policy names are scoped to a table.

For `organizations`:

```sql
CREATE POLICY tenant_isolation ON organizations
    USING (id = current_setting('app.current_organization_id', true)::uuid)
    WITH CHECK (id = current_setting('app.current_organization_id', true)::uuid);
```

For `organization_memberships`:

```sql
CREATE POLICY tenant_isolation ON organization_memberships
    USING (
        organization_id =
        current_setting('app.current_organization_id', true)::uuid
    )
    WITH CHECK (
        organization_id =
        current_setting('app.current_organization_id', true)::uuid
    );
```

### 6.4 Missing or invalid tenant context

Use `current_setting('app.current_organization_id', true)` so a missing setting returns NULL rather than raising an unhelpful error. A comparison against NULL does not authorize rows. Do not use `COALESCE` to substitute a default organization.

Required tests:

- No tenant setting: tenant-owned SELECT returns no rows; INSERT/UPDATE is denied.
- Invalid tenant setting: malformed UUID is rejected; no data is exposed.
- Cross-tenant SELECT/UPDATE/DELETE: denied.
- Cross-tenant INSERT: denied by `WITH CHECK`.
- A user cannot switch to an organization they do not belong to.
- The application does not leak one tenant's context to another request on a pooled connection.

### 6.5 Bootstrap problem

An active organization cannot be required before the user selects an organization.

The authentication/bootstrap path needs a deliberate policy:

- Authenticate the user and establish a user identity context.
- Allow the user to read their own memberships and the organizations they belong to.
- After selecting an active organization, set `app.current_organization_id` and apply strict tenant policies.
- Do not grant broad bypass privileges to the application role merely to solve bootstrap.
- Consider a security-definer function or a dedicated auth role for the narrow bootstrap operation.

`users` is global, so a simple organization RLS policy cannot protect it directly. Decide whether to:

- keep user access behind a separate authentication path and application-level restrictions; or
- apply a membership-aware RLS policy such as an `EXISTS` check against `organization_memberships`.

Whichever approach is chosen, test that a tenant cannot enumerate other tenants' users.

### 6.6 Connection-pool isolation

The tenant setting must be scoped to the transaction, not the connection. Recommended pattern:

1. Begin a transaction.
2. Set `app.current_organization_id` with `set_config(..., true)`.
3. Execute all tenant queries.
4. Commit or roll back.
5. The setting resets automatically at transaction end.

The current `get_async_session()` dependency yields an `AsyncSession` but does not set or reset tenant context. Milestone 8.2 must add this explicitly and test it against a pooled PostgreSQL connection.

### 6.7 Composite foreign keys as defense in depth

RLS and composite FKs solve different problems:

- RLS prevents a query from seeing or modifying another tenant's rows after context is set.
- Composite FKs prevent a row from structurally referencing a parent in another tenant, even if RLS is misconfigured, bypassed by an owner, or omitted from a new table.
- Neither replaces application authorization or correct tenant-context handling.

Use both. Add RLS to every tenant-owned table, and use composite FKs for every tenant-owned parent-child relationship where cross-tenant reference is possible.

### 6.8 SaaS and on-premises parity

The same schema should serve SaaS and on-premises deployments. However:

- On-premises administrators may run as the table owner or superuser, which can bypass RLS unless `FORCE ROW LEVEL SECURITY` and a non-owner application role are enforced.
- If an on-premises deployment permits SQLite, RLS is unavailable. Tenant isolation must then be enforced in the application, and the deployment must not be presented as equivalent to PostgreSQL RLS.
- Migration roles, backup/restore roles, and operational roles must be tested for both deployment modes.
- Do not vary the tenant key schema between SaaS and on-premises; only roles, privileges, and deployment operations should vary.

---

## 7. Migration considerations

### 7.1 Existing schema changes that may eventually be required

1. **Add `UNIQUE (organization_id, id)` to `organization_memberships`** if future tables will reference memberships by surrogate ID. This is recommended before 8.2 for a uniform composite-key pattern.
2. **Add tenant-owned tables** in dependency order: collectors, devices, telemetry, diagnosis, alerts, and audit.
3. **Add composite unique targets before composite foreign keys.** PostgreSQL requires the target unique constraint to exist when the child FK is created.
4. **Add RLS policies and roles** in a separate migration after table and key creation.
5. **Do not modify revision `0001` for planned tables.** Create new revisions. Revision `0001` is the current baseline and should be treated as frozen after 8.1.8.

No schema change is made in 8.1.9.

### 7.2 Foreign-key dependency order

Creation order:

```text
organizations
users
organization_memberships
collectors
devices
telemetry_records
diagnosis_events
alerts
audit_events
sessions (global auth; can be created after users/organizations)
```

Rules:

- Create `organizations` and `users` before memberships.
- Create `collectors` before devices.
- Create `devices` before telemetry.
- Create `telemetry` before diagnosis.
- Create diagnosis before alerts if alerts reference diagnosis.
- Audit can reference organizations only, or store historical identifiers without cascading operational FKs.
- Downgrade in reverse order.
- Drop RLS policies or disable RLS before dropping tables where policy dependencies exist.

### 7.3 Unique constraint and index ordering

- Create `UNIQUE (organization_id, id)` on each parent before creating children that reference it.
- Create natural-key unique constraints before any FK that targets them.
- Create indexes after table creation; name them explicitly.
- For PostgreSQL, `CREATE UNIQUE INDEX` can satisfy a foreign-key target only if it is non-partial and unique; prefer a named unique constraint for clarity.
- For SQLite migration tests, use Alembic batch operations for alter-table constraint changes where necessary.

### 7.4 Existing data and backfills

There are currently no tenant-owned operational tables, so no operational backfill is needed for 8.1.9. When those tables are introduced:

- Add `organization_id` as `NOT NULL`.
- Backfill only if tables already contain data; new tables should be created with the column mandatory.
- Validate that every row’s `organization_id` matches its parent.
- Add composite FKs after backfill and validation.
- Add unique targets before FKs.

### 7.5 Index and locking considerations

- Adding `UNIQUE (organization_id, id)` to a large existing table creates a new index and can lock writes. The current membership table is small, but future tables may be large.
- Adding `NOT NULL` to an existing populated table requires a safe backfill and constraint validation sequence.
- Creating indexes on high-volume telemetry should be planned with concurrent index creation where deployment policy permits.
- Avoid adding redundant organization-only indexes when a composite unique index already leads with `organization_id`.

### 7.6 Delete and retention semantics

Composite FKs require explicit delete rules:

- `CASCADE` is appropriate for wholly dependent ephemeral data only.
- `RESTRICT`/`NO ACTION` protects history and forces an explicit reassignment or archive.
- `SET NULL` is unsafe on composite FKs with a non-null `organization_id`.
- Audit and diagnosis history should generally not cascade from operational records.
- Define retention before implementing telemetry and alert deletion.

### 7.7 SaaS versus on-premises migration behavior

- Keep the same tables and constraints in both deployments.
- On-premises may run migrations as a privileged administrator; verify that the runtime role still behaves as a non-owner subject to RLS.
- Do not rely on deployment-specific database owners to enforce tenant isolation.
- SQLite is acceptable for development and migration-shape tests but not a substitute for PostgreSQL RLS validation.

---

## 8. Risks and unresolved decisions

### 8.1 Confirmed repository facts

- The current schema contains only `organizations`, `users`, and `organization_memberships`.
- `organization_memberships` has `UNIQUE (organization_id, user_id)`.
- `organization_memberships` does not have `UNIQUE (organization_id, id)`.
- `organizations` and `users` have single-column UUID primary keys.
- `users` is intentionally global and has no `organization_id`.
- The membership role CHECK permits only `org_admin` and `org_member`.
- No RLS, tenant context, application database role, collectors, devices, telemetry, diagnosis, alerts, or audit tables exist.
- The FastAPI lifespan conditionally initializes the database engine and session factory when `NETOPS_DATABASE_URL` is configured, does not proactively connect or create tables at startup, and disposes the engine during shutdown. Existing telemetry and MCP cleanup behavior is preserved.
- SQLite tests and offline PostgreSQL SQL tests exist; no live PostgreSQL validation has been performed.
- Milestone 8.1.8 corrected the CHECK constraint name to `ck_organization_memberships_role`.

### 8.2 Recommendations

- Keep globally unique UUID primary keys.
- Add `organization_id NOT NULL` to every tenant-owned table.
- Add `UNIQUE (organization_id, id)` to every tenant-owned table used as a composite FK target.
- Use composite foreign keys for tenant-owned parent-child relationships.
- Add a three-column unique target where same-device chain consistency matters.
- Add `UNIQUE (organization_id, id)` to `organization_memberships` if membership IDs will be referenced.
- Use explicit, length-checked names for composite constraints and indexes.
- Do not use `ON DELETE SET NULL` on composite FKs containing `organization_id NOT NULL`.
- Use RLS and composite FKs together.
- Keep global users and tenant memberships separate.
- Use separate platform and tenant audit streams if both are required.

### 8.3 Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Single-column PK assumed to satisfy composite FK targets | Migration failure when child tables are created | Add `UNIQUE (organization_id, id)` on every composite FK target. |
| Missing `organization_id` on a tenant table | RLS cannot filter it directly; cross-tenant references possible | Require `organization_id NOT NULL` and direct organization FK. |
| RLS-only isolation | Application/owner bypass or missing policy can expose data | Composite FKs plus RLS plus non-owner role. |
| Global `users` table exposed through tenant joins | Cross-tenant PII leakage | Membership-aware access, auth path, and tests. |
| Bootstrap policy hides organization list | User cannot select an organization | Explicit bootstrap policy or restricted auth path. |
| Connection-pool tenant leakage | One request sees another tenant's data | Transaction-local `set_config` and pool tests. |
| `ON DELETE SET NULL` on composite FK | Attempts to null `organization_id`, violating NOT NULL | Use `CASCADE`/`RESTRICT`/`NO ACTION` or a version-specific column-list strategy. |
| Multiple FKs with current naming convention | Duplicate or ambiguous names, PostgreSQL truncation | Explicit composite names, length checks, offline SQL assertions. |
| Redundant indexes | Write/storage overhead | Use `(organization_id, id)` indexes and remove redundant organization-only indexes where safe. |
| Audit records cascade away | Loss of security/audit history | No cascading operational FKs for audit; archive/retention design. |
| On-premises owner/superuser bypasses RLS | SaaS/on-prem behavior divergence | `FORCE ROW LEVEL SECURITY`, non-owner app role, deployment validation. |
| SQLite deployed on-premises | RLS unavailable; false isolation assumption | Restrict RLS guarantees to PostgreSQL; enforce app checks for SQLite. |
| Telemetry volume and retention | Index bloat, slow deletes, lock contention | Time-series indexing, partitioning/retention strategy before implementation. |
| Membership deletion vs historical actor references | Broken FKs or lost audit context | Historical IDs without cascading FKs, or documented retention. |
| Global email/slug uniqueness | Cross-tenant enumeration or collision policy | Decide case sensitivity and disclosure behavior before auth. |

### 8.4 Assumptions requiring approval

1. `sessions` means global authentication sessions with an optional `active_organization_id`, not tenant-owned sessions.
2. `audit_events` means tenant-owned audit records for tenant actions; platform-level events use a separate table.
3. A collector can serve multiple devices, and a device may have zero or one collector (`collector_id` nullable).
4. One device belongs to exactly one organization; one collector belongs to exactly one organization.
5. Telemetry and diagnosis may have different retention rules; delete/archive policy will be decided before implementation.
6. Alerts may reference a device, telemetry, and/or diagnosis; same-device consistency for optional combinations will be decided explicitly.
7. `users` remain global identities across SaaS and on-premises deployments.
8. Adding `UNIQUE (organization_id, id)` to `organization_memberships` before 8.2 is approved.
9. PostgreSQL is the production database for both SaaS and on-premises; SQLite remains development/test only.
10. The runtime database role is a non-owner, non-superuser role without `BYPASSRLS`; migrations use a separate role.

If any of these assumptions are wrong, the table classifications and composite-key design must be revisited before implementation.

---

## 9. Recommended implementation sequence for Milestone 8.2

Milestone 8.2 should implement tenant isolation and RLS for the **existing** foundation first. It should not create collectors, devices, telemetry, or other planned tables unless those are explicitly moved into scope.

1. **Approve the assumptions and key conventions**
   - Confirm global UUID PKs.
   - Confirm the `organization_memberships` unique-key decision.
   - Confirm sessions and audit classifications.
   - Confirm delete/retention semantics for future tables.

2. **Establish a disposable PostgreSQL validation environment**
   - Create a test database.
   - Create a migration role and a separate non-owner runtime role.
   - Capture the exact schema and constraint names.
   - Do not claim compatibility until this runs.

3. **Add the membership composite target key**
   - Create a new migration adding `UNIQUE (organization_id, id)` to `organization_memberships` if approved.
   - Keep `UNIQUE (organization_id, user_id)`.
   - Add exact offline SQL and schema tests.
   - Do not change revision `0001`.

4. **Add tenant context and session plumbing**
   - Wire conditional database initialization and disposal into the FastAPI lifespan.
   - Add a request-scoped session dependency.
   - Add a transaction-local tenant-context helper using `set_config(..., true)`.
   - Ensure no tenant context is set globally or on a pooled connection.

5. **Add application roles and privileges**
   - Runtime role: non-owner, no `BYPASSRLS`, limited DML.
   - Migration role: DDL and migration ownership.
   - Test role behavior explicitly.

6. **Implement RLS on existing tables**
   - `organizations`: policy on `id`.
   - `organization_memberships`: policy on `organization_id`.
   - Decide and implement the `users`/bootstrap access path.
   - Use `ENABLE` and `FORCE ROW LEVEL SECURITY`.
   - Add `USING` and `WITH CHECK` policies.

7. **Add RLS tests**
   - Missing context denies access.
   - Invalid context denies access.
   - Cross-tenant SELECT/INSERT/UPDATE/DELETE denied.
   - Pooled connection context does not leak.
   - Table owner/superuser behavior is understood and forced where required.
   - Organization selection/bootstrap works without broad bypass.

8. **Validate SaaS and on-premises parity**
   - Same schema and constraints.
   - Different roles/privileges are documented.
   - On-premises owner/superuser cannot silently bypass RLS.
   - SQLite limitations are documented if supported at all.

9. **Defer planned tenant tables**
   - Add collectors, devices, telemetry, diagnosis, alerts, and audit in later milestones.
   - For each, create `organization_id NOT NULL`, parent unique keys, composite FKs, indexes, and RLS policies.
   - Do not create them without the key strategy in this document.

---

## 10. Recommendation

Adopt the following composite tenant-key strategy for Agentic NetOps:

> **Global UUID primary keys + mandatory `organization_id` on tenant-owned tables + `UNIQUE (organization_id, id)` on composite FK targets + composite tenant foreign keys + RLS defense in depth.**

This is appropriate because:

- It preserves stable global resource IDs while adding tenant-safe reference paths.
- It supports users belonging to multiple organizations without duplicating global identity.
- It prevents cross-tenant references structurally, even when RLS is bypassed or a query is wrong.
- It aligns with PostgreSQL's requirement that composite foreign keys target a matching unique or primary-key constraint.
- It allows tenant-scoped indexes and RLS policies to share the `organization_id` leading column.
- It works for both hosted SaaS and on-premises deployments without divergent schemas.
- It avoids a disruptive composite-primary-key redesign of the current UUID-based foundation.

The main schema decision to resolve before 8.2 is whether to add `UNIQUE (organization_id, id)` to `organization_memberships`. The recommendation is **yes**, if future tenant tables or audit records will reference memberships by membership ID. If all references use `(organization_id, user_id)`, the key can be deferred, but the uniform composite-key strategy is simpler and more future-proof.

Do not proceed to Milestone 8.2 until the assumptions in section 8.4 are approved and the 8.2 sequence is accepted.

---

## Appendix A — Files inspected and modification status

### Inspected

- `app/db/models.py`
- `app/db/types.py`
- `app/db/session.py`
- `alembic/versions/0001_initial_tables.py`
- `alembic/env.py`
- `alembic.ini`
- `tests/db/test_models.py`
- `tests/db/test_migrations.py`
- `tests/db/test_role_constraints.py`
- `tests/db/test_schema_consistency.py`
- `tests/db/test_connection.py`
- `requirements.txt`
- FastAPI lifespan in `app/main.py`

### Modified

- None. This milestone created only this report.
- No application code, model, migration, test, requirement, or FastAPI lifecycle file was changed.

### Repository state at completion

- Branch: `feature/cisco-live-validation`
- HEAD: `2d5ef652809069527cef71ba6cfd9f67b3bcb00b`
- The report itself is the only new file.
- `requirements.txt` remains tracked and modified from the earlier Milestone 8.1.6 work; it was not changed by this milestone.
- Existing untracked files remain untouched.
- No staging, commit, push, reset, checkout, stash, or deletion was performed.
