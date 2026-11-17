"""Safety regression tests for the live PostgreSQL 16 harness.

These tests use fakes only. They do not connect to PostgreSQL, read ``.env``,
or invoke Docker.
"""

from __future__ import annotations

import pytest

from . import test_postgres_16_live as live
from .postgres_test_support import TEST_DATABASE_URL_ENV, TestDatabaseSafetyError

VALID_TEST_URL = (
    "postgresql+asyncpg://netops_test_user_1234:"
    "not-a-real-secret@127.0.0.1:55432/netops_test_0123456789abcdef"
)
NON_DISPOSABLE_URL = (
    "postgresql+asyncpg://netops_test_user_1234:"
    "not-a-real-secret@127.0.0.1:55432/netops"
)


class _RecordingCommand:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def upgrade(self, *args, **kwargs) -> None:
        self.calls.append("upgrade")

    def downgrade(self, *args, **kwargs) -> None:
        self.calls.append("downgrade")


class _FakeConnection:
    def __init__(self, server_version_num: int, version: str) -> None:
        self.server_version_num = server_version_num
        self.version = version
        self.fetch_calls = 0
        self.closed = False

    async def fetchval(self, query: str, *args):
        if "current_database()" in query:
            return "netops_test_0123456789abcdef"
        raise AssertionError(f"Unexpected fetchval query: {query}")

    async def fetchrow(self, query: str, *args):
        assert "server_version_num" in query
        return {
            "database": "netops_test_0123456789abcdef",
            "current_user": "netops_test_user_1234",
            "server_version_num": self.server_version_num,
            "version": self.version,
        }

    async def fetch(self, query: str, *args):
        self.fetch_calls += 1
        raise AssertionError(f"Catalog query reached after version gate: {query}")

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("server_version_num", "version"),
    [
        (150000, "PostgreSQL 15.15 (Debian 15.15-1.pgdg13+1)"),
        (170000, "PostgreSQL 17.0 (Debian 17.0-1.pgdg13+1)"),
        (180000, "PostgreSQL 18.0 (Debian 18.0-1.pgdg13+1)"),
    ],
)
def test_unsupported_postgres_versions_stop_before_migration(
    monkeypatch,
    server_version_num,
    version,
):
    """PG15 and PG17+ must be rejected before Alembic or catalog work."""
    fake_connection = _FakeConnection(server_version_num, version)
    fake_command = _RecordingCommand()

    async def fake_connect(_target):
        return fake_connection

    monkeypatch.setenv(TEST_DATABASE_URL_ENV, VALID_TEST_URL)
    monkeypatch.setattr(live, "_connect", fake_connect)
    monkeypatch.setattr(live, "command", fake_command)

    with pytest.raises(TestDatabaseSafetyError, match="PostgreSQL 16"):
        live.test_live_head_upgrade_downgrade_reupgrade()

    assert fake_command.calls == []
    assert fake_connection.fetch_calls == 0
    assert fake_connection.closed is True


def test_non_disposable_target_is_rejected_before_migration(monkeypatch):
    """A non-disposable DSN must fail before any connection or migration."""
    fake_command = _RecordingCommand()

    def fail_connect(_target):
        raise AssertionError("A non-disposable target must not be connected to")

    monkeypatch.setenv(TEST_DATABASE_URL_ENV, NON_DISPOSABLE_URL)
    monkeypatch.setattr(live, "_connect", fail_connect)
    monkeypatch.setattr(live, "command", fake_command)

    with pytest.raises(TestDatabaseSafetyError, match="Refusing to use database"):
        live.test_live_head_upgrade_downgrade_reupgrade()

    assert fake_command.calls == []
