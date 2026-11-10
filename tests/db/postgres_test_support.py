"""Explicit-DSN support for disposable PostgreSQL 16 tests.

The live PostgreSQL harness deliberately reads only
``NETOPS_TEST_DATABASE_URL``. It never falls back to the application's
``NETOPS_DATABASE_URL`` and never reads ``.env``.

A test target is accepted only when it is a PostgreSQL URL whose database
name is ``netops_test_<random-suffix>``. The suffix must be 8-32 lowercase
hexadecimal characters. Destructive migration tests must validate the
connected database name through :func:`assert_current_database_is_disposable`
before running.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from sqlalchemy.engine import URL, make_url

TEST_DATABASE_URL_ENV = "NETOPS_TEST_DATABASE_URL"
DISPOSABLE_DATABASE_PREFIX = "netops_test_"
DISPOSABLE_DATABASE_PATTERN = re.compile(
    r"^netops_test_[a-f0-9]{8,32}$"
)
LOCAL_POSTGRES_HOSTS = {"127.0.0.1", "localhost", "::1"}


class TestDatabaseSafetyError(ValueError):
    """The supplied test database target is not safe to use."""

    __test__ = False


@dataclass(frozen=True)
class DisposablePostgresTarget:
    """Validated, explicit target for a disposable PostgreSQL 16 database."""

    url: URL
    database: str
    username: str
    host: str
    port: int

    @property
    def password(self) -> str:
        """Return the ephemeral password without logging it."""
        if self.url.password is None:
            raise TestDatabaseSafetyError(
                "The test database URL must include an ephemeral password."
            )
        return self.url.password

    @property
    def alembic_url(self) -> str:
        """Return the SQLAlchemy URL used only for Alembic subprocess state."""
        return self.url.render_as_string(hide_password=False)

    @property
    def safe_dsn(self) -> str:
        """Return a display-safe DSN with the password redacted."""
        return self.url.render_as_string(hide_password=True)

    def asyncpg_connect_kwargs(self) -> dict[str, object]:
        """Return connection arguments for asyncpg without a DSN string."""
        return {
            "user": self.username,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "server_settings": {
                "application_name": "netops-8.2.1-postgres-validation",
            },
        }


def assert_disposable_database_name(database: str) -> None:
    """Reject database names outside the approved disposable namespace."""
    if not isinstance(database, str) or not DISPOSABLE_DATABASE_PATTERN.fullmatch(
        database
    ):
        raise TestDatabaseSafetyError(
            "Refusing to use database "
            f"{database!r}; expected a disposable name matching "
            f"{DISPOSABLE_DATABASE_PREFIX}<8-32 lowercase hex characters>."
        )


def parse_disposable_test_url(raw_url: str) -> DisposablePostgresTarget:
    """Parse and validate an explicit disposable PostgreSQL test URL."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} must contain an explicit PostgreSQL DSN."
        )

    try:
        url = make_url(raw_url)
    except Exception as exc:  # SQLAlchemy raises several parser exception types.
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} is not a valid SQLAlchemy URL."
        ) from exc

    if url.drivername != "postgresql+asyncpg":
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} must use the postgresql+asyncpg driver."
        )

    if url.database is None:
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} must name an explicit database."
        )
    assert_disposable_database_name(url.database)

    if url.username is None or not url.username:
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} must include an explicit test user."
        )
    if url.password is None or not url.password:
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} must include an ephemeral password."
        )

    if url.host not in LOCAL_POSTGRES_HOSTS:
        raise TestDatabaseSafetyError(
            "Refusing non-local PostgreSQL host "
            f"{url.host!r}; expected one of {sorted(LOCAL_POSTGRES_HOSTS)}."
        )

    port = url.port if url.port is not None else 5432
    return DisposablePostgresTarget(
        url=url,
        database=url.database,
        username=url.username,
        host=url.host,
        port=port,
    )


def load_disposable_test_target() -> DisposablePostgresTarget:
    """Load the explicit test target, never the application database URL."""
    raw_url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not raw_url:
        raise TestDatabaseSafetyError(
            f"{TEST_DATABASE_URL_ENV} is not set. Live PostgreSQL tests require "
            "an explicit disposable DSN; NETOPS_DATABASE_URL is never used as "
            "a fallback."
        )
    return parse_disposable_test_url(raw_url)


@contextmanager
def temporary_alembic_database_url(
    target: DisposablePostgresTarget,
) -> Iterator[None]:
    """Expose the validated test DSN to Alembic only for this call."""
    previous: Optional[str] = os.environ.get("NETOPS_DATABASE_URL")
    os.environ["NETOPS_DATABASE_URL"] = target.alembic_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("NETOPS_DATABASE_URL", None)
        else:
            os.environ["NETOPS_DATABASE_URL"] = previous


async def assert_current_database_is_disposable(
    connection: object,
    expected_database: Optional[str] = None,
) -> str:
    """Verify the live connection points at the guarded disposable database."""
    actual = await connection.fetchval("SELECT current_database()")
    assert_disposable_database_name(actual)
    if expected_database is not None and actual != expected_database:
        raise TestDatabaseSafetyError(
            "Connected database "
            f"{actual!r} does not match the explicit test target "
            f"{expected_database!r}."
        )
    return actual
