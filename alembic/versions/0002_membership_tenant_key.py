"""Add organization-scoped membership composite key.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 00:00:00.000000

Adds ``UNIQUE (organization_id, id)`` to ``organization_memberships`` without
recreating the table or modifying existing membership rows on PostgreSQL.
SQLite uses Alembic batch mode because SQLite cannot add table constraints in
place.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "uq_organization_memberships_organization_id_id"
TABLE_NAME = "organization_memberships"
COLUMNS = ["organization_id", "id"]


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.create_unique_constraint(CONSTRAINT_NAME, COLUMNS)
    else:
        op.create_unique_constraint(CONSTRAINT_NAME, TABLE_NAME, COLUMNS)


def downgrade() -> None:
    if _is_sqlite():
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_constraint(CONSTRAINT_NAME, type_="unique")
    else:
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="unique")
