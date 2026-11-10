"""Custom SQLAlchemy types for the Agentic NetOps database.

Provides cross-dialect-compatible UUID and timezone-aware DateTime types.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import String, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import DateTime


class GUID(TypeDecorator):
    """Platform-independent GUID type.

    Uses PostgreSQL's native UUID type when available, otherwise falls back
    to CHAR(32) storing hex-encoded UUIDs.
    """

    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID())
        return dialect.type_descriptor(String(32))

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            # PostgreSQL's native UUID type accepts the canonical string form.
            # Other dialects store compact 32-character hex in CHAR(32).
            if dialect.name == "postgresql":
                return str(value)
            return value.hex
        return str(value)

    def process_result_value(self, value: Any, dialect):
        if value is None:
            return value
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value


class UTCDateTime(TypeDecorator):
    """Timezone-aware DateTime type that stores and returns UTC datetimes.

    On PostgreSQL, uses TIMESTAMP WITH TIME ZONE. On other dialects, uses
    DateTime(timezone=True).
    """

    impl = DateTime
    cache_ok = True

    def __init__(self, **kwargs):
        super().__init__(timezone=True, **kwargs)

    def process_bind_param(self, value: Any, dialect):
        if value is None:
            return value
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value
        return value

    def process_result_value(self, value: Any, dialect):
        if value is None:
            return value
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
        return value


__all__ = ["GUID", "UTCDateTime"]
