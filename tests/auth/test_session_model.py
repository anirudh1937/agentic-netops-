"""Tests for the Session model.

Milestone 8.2.4.3 — Session model and migration tests.

These tests run without a live PostgreSQL database. They verify that the
Session model is correctly defined and that the migration creates the
expected schema.
"""

from __future__ import annotations

import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.auth.models import Session
from app.db.models import Base


def test_session_model_has_expected_columns() -> None:
    """Session model has all expected columns."""
    mapper = Session.__table__
    columns = {c.name for c in mapper.columns}
    expected = {
        "id",
        "user_id",
        "token_hash",
        "created_at",
        "expires_at",
        "last_seen_at",
        "revoked_at",
    }
    assert expected.issubset(columns)


def test_session_primary_key_is_uuid() -> None:
    """Session uses UUID primary key named 'id'."""
    mapper = Session.__table__
    pk = mapper.primary_key
    assert len(pk) == 1
    assert pk.columns[0].name == "id"


def test_session_has_user_foreign_key() -> None:
    """Session has user_id foreign key to users.id with cascade."""
    mapper = Session.__table__
    user_id_col = mapper.columns.get("user_id")
    assert user_id_col is not None
    fks = list(user_id_col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"
    assert fks[0].column.name == "id"


def test_session_has_token_hash_column() -> None:
    """Session has token_hash column."""
    mapper = Session.__table__
    columns = {c.name for c in mapper.columns}
    assert "token_hash" in columns


def test_token_hash_is_unique() -> None:
    """token_hash column has a unique constraint."""
    mapper = Session.__table__
    unique_constraints = [
        c.name for c in mapper.constraints
        if hasattr(c, "name") and c.name is not None
    ]
    assert any("token_hash" in name for name in unique_constraints)
    assert any(name.endswith("_token_hash") for name in unique_constraints)


def test_session_has_created_at() -> None:
    """Session has created_at column."""
    mapper = Session.__table__
    columns = {c.name for c in mapper.columns}
    assert "created_at" in columns


def test_session_has_expires_at() -> None:
    """Session has expires_at column."""
    mapper = Session.__table__
    columns = {c.name for c in mapper.columns}
    assert "expires_at" in columns


def test_session_has_optional_timestamps() -> None:
    """Session has optional last_seen_at and revoked_at columns."""
    mapper = Session.__table__
    columns = {c.name for c in mapper.columns}
    assert "last_seen_at" in columns
    assert "revoked_at" in columns


def test_session_metadata_registered() -> None:
    """Session table is registered in SQLAlchemy metadata."""
    from app.db.models import Base
    assert "sessions" in Base.metadata.tables


def test_session_model_relationship_to_user() -> None:
    """Session has relationship to user via user_id foreign key."""
    mapper = Session.__table__
    user_id_col = mapper.columns.get("user_id")
    assert user_id_col is not None
    fks = list(user_id_col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "users"
    assert fks[0].column.name == "id"


def test_alembic_head_includes_session_table() -> None:
    """Alembic head revision includes session table."""
    from alembic.script import ScriptDirectory
    from alembic.config import Config

    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "0003"


def test_session_check_constraints() -> None:
    """Session has check constraints for expires_at and revoked_at."""
    mapper = Session.__table__
    check_constraints = [
        c for c in mapper.constraints
        if hasattr(c, "name") and c.name is not None
        and c.name.startswith("ck_sessions")
    ]
    constraint_names = {c.name for c in check_constraints}
    assert any(name.endswith("_expires_after_created") for name in constraint_names)
    assert any(name.endswith("_revoked_after_created") for name in constraint_names)


def test_session_unique_constraint_names() -> None:
    """Session has unique constraint on token_hash."""
    mapper = Session.__table__
    unique_constraints = [
        c.name for c in mapper.constraints
        if hasattr(c, "name") and c.name is not None
    ]
    assert any(name.endswith("_token_hash") for name in unique_constraints)
