"""Tests for the disposable PostgreSQL test-target safety guard."""

from __future__ import annotations

import pytest

from .postgres_test_support import (
    TEST_DATABASE_URL_ENV,
    TestDatabaseSafetyError,
    assert_disposable_database_name,
    load_disposable_test_target,
    parse_disposable_test_url,
)


VALID_URL = (
    "postgresql+asyncpg://netops_test_user_1234:"
    "not-a-real-secret@127.0.0.1:55432/netops_test_0123456789abcdef"
)


def test_valid_disposable_target_is_accepted():
    target = parse_disposable_test_url(VALID_URL)

    assert target.database == "netops_test_0123456789abcdef"
    assert target.username == "netops_test_user_1234"
    assert target.host == "127.0.0.1"
    assert target.port == 55432
    assert "not-a-real-secret" not in target.safe_dsn
    assert "***" in target.safe_dsn


@pytest.mark.parametrize(
    "database",
    [
        "netops",
        "netops_test",
        "netops_test_",
        "netops_test_short",
        "netops_test_0123456789ABCDEF",
        "netops_test_0123456789abcdefg",
        "prod_netops_test_0123456789abcdef",
        "netops_test_0123456789abcdef_extra",
        "",
    ],
)
def test_non_disposable_database_names_are_rejected(database):
    with pytest.raises(TestDatabaseSafetyError, match="Refusing to use database"):
        assert_disposable_database_name(database)


def test_missing_explicit_test_dsn_is_rejected_even_with_application_url(monkeypatch):
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(
        "NETOPS_DATABASE_URL",
        "postgresql+asyncpg://app:secret@127.0.0.1:5432/netops",
    )

    with pytest.raises(TestDatabaseSafetyError, match="NETOPS_TEST_DATABASE_URL"):
        load_disposable_test_target()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///netops_test_0123456789abcdef.db",
        "postgresql+psycopg://user:pass@127.0.0.1:5432/netops_test_0123456789abcdef",
        "postgresql+asyncpg://user:pass@db.example.com:5432/netops_test_0123456789abcdef",
        "postgresql+asyncpg://user:pass@127.0.0.1:5432/netops",
        "postgresql+asyncpg://user@127.0.0.1:5432/netops_test_0123456789abcdef",
        "not-a-url",
    ],
)
def test_unsafe_targets_are_rejected(url):
    with pytest.raises(TestDatabaseSafetyError):
        parse_disposable_test_url(url)


def test_test_dsn_is_loaded_only_from_explicit_environment(monkeypatch):
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, VALID_URL)

    target = load_disposable_test_target()

    assert target.database == "netops_test_0123456789abcdef"
