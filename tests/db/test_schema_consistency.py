"""Schema consistency between SQLAlchemy metadata and the current Alembic head.

The test applies all migrations to a temporary SQLite database and compares
the resulting schema against ``Base.metadata`` using Alembic's autogenerate
comparison. It fails on any difference that is not an explicitly documented
SQLite UUID reflection artifact.
"""

from __future__ import annotations

import os

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import String, create_engine

from app.db.models import Base
from app.db.types import GUID

# Import auth models so that Base.metadata includes session tables
from app.auth.models import Session  # noqa: F401

UUID_COLUMNS = {
    ("organizations", "id"),
    ("users", "id"),
    ("organization_memberships", "id"),
    ("organization_memberships", "organization_id"),
    ("organization_memberships", "user_id"),
    ("sessions", "id"),
    ("sessions", "user_id"),
}


def _is_known_sqlite_uuid_reflection_diff(diff) -> bool:
    """Return whether *diff* is the expected GUID/CHAR(32) SQLite artifact.

    The models use the custom ``GUID`` type so PostgreSQL gets a native UUID
    column. SQLite has no native UUID type, so reflection reports the stored
    ``CHAR(32)`` representation instead of ``GUID()``. On PostgreSQL both
    sides are UUID and this difference does not exist.
    """
    if isinstance(diff, list):
        if len(diff) != 1:
            return False
        diff = diff[0]
    if not isinstance(diff, tuple) or len(diff) != 7:
        return False

    kind, _schema, table, column, _kw, existing_type, metadata_type = diff
    if kind != "modify_type":
        return False
    if (table, column) not in UUID_COLUMNS:
        return False
    if not isinstance(metadata_type, GUID):
        return False
    if not isinstance(existing_type, String):
        return False

    return existing_type.length == 32


def test_alembic_schema_matches_model_metadata(tmp_path):
    """Revision 0001 and the SQLAlchemy models define the same schema."""
    db_path = tmp_path / "schema_consistency.db"
    async_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    sync_url = f"sqlite:///{db_path.as_posix()}"

    old = os.environ.get("NETOPS_DATABASE_URL")
    os.environ["NETOPS_DATABASE_URL"] = async_url

    try:
        command.upgrade(Config("alembic.ini"), "head")

        engine = create_engine(sync_url)
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection,
                    opts={
                        "compare_type": True,
                        "compare_server_default": True,
                    },
                )
                differences = compare_metadata(context, Base.metadata)
        finally:
            engine.dispose()
    finally:
        if old is not None:
            os.environ["NETOPS_DATABASE_URL"] = old
        else:
            os.environ.pop("NETOPS_DATABASE_URL", None)

    unexpected = [
        diff
        for diff in differences
        if not _is_known_sqlite_uuid_reflection_diff(diff)
    ]

    assert unexpected == [], (
        "Alembic revision 0001 and Base.metadata disagree on schema objects: "
        f"{unexpected!r}"
    )
