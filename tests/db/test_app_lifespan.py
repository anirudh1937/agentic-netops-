"""Offline tests for FastAPI database lifecycle integration.

These tests exercise ``app.main.app_lifespan`` with no live database. A
configured database is represented by an in-memory SQLite async URL, which
validates engine/session-factory initialization and disposal without opening a
PostgreSQL connection.
"""

from __future__ import annotations

import asyncio

import pytest

import app.db.session as session_mod
import app.main as main
from app.telemetry_poller import TelemetryPoller
from app.telemetry_store import TelemetryStore


class _FakeMCPToolset:
    def __init__(self, close_error: Exception | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakePoller:
    def __init__(
        self,
        *,
        enabled: bool = True,
        running: bool = False,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
    ) -> None:
        self.enabled = enabled
        self._running = running
        self.start_error = start_error
        self.stop_error = stop_error
        self.stop_calls = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self._running = False


@pytest.fixture(autouse=True)
def _clean_database_state(monkeypatch):
    """Keep each test isolated from the module-level engine and factory."""

    if session_mod._async_engine is not None:
        asyncio.run(session_mod.dispose_db())
    session_mod._async_engine = None
    session_mod._async_session_factory = None

    monkeypatch.delenv("NETOPS_DATABASE_URL", raising=False)
    monkeypatch.delenv("NETOPS_POLL_ENABLED", raising=False)

    yield

    if session_mod._async_engine is not None:
        asyncio.run(session_mod.dispose_db())
    session_mod._async_engine = None
    session_mod._async_session_factory = None


@pytest.fixture
def fake_mcp(monkeypatch):
    fake = _FakeMCPToolset()
    monkeypatch.setattr(main, "mcp_toolset", fake)
    return fake


def _install_fake_poller(monkeypatch, poller: _FakePoller) -> None:
    class _FakePollerFactory:
        @classmethod
        def from_config(cls, store, enabled):
            return poller

    monkeypatch.setattr(main, "TelemetryPoller", _FakePollerFactory)


def test_startup_without_database_url_does_not_initialize_database(
    fake_mcp,
):
    """Application startup remains database-free when no URL is configured."""

    async def _scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            assert session_mod._async_engine is None
            assert session_mod._async_session_factory is None

    asyncio.run(_scenario())

    assert session_mod._async_engine is None
    assert session_mod._async_session_factory is None
    assert fake_mcp.close_calls == 1


def test_startup_with_database_url_initializes_and_disposes_engine(
    monkeypatch,
    fake_mcp,
):
    """A configured URL initializes the engine and shutdown disposes it."""

    monkeypatch.setenv(
        "NETOPS_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )

    async def _scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            engine = session_mod._async_engine
            assert engine is not None
            assert session_mod._async_session_factory is not None
            assert engine.url.drivername == "sqlite+aiosqlite"

        assert session_mod._async_engine is None
        assert session_mod._async_session_factory is None

    asyncio.run(_scenario())
    assert fake_mcp.close_calls == 1


def test_startup_failure_disposes_database_and_preserves_original_error(
    monkeypatch,
    fake_mcp,
):
    """Database initialization failure still runs cleanup and is not hidden."""

    monkeypatch.setenv(
        "NETOPS_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )

    calls: list[str] = []
    real_init_db = main.init_db
    real_dispose_db = main.dispose_db

    async def _failing_init() -> None:
        await real_init_db()
        calls.append("init")
        raise RuntimeError("database startup failed")

    async def _tracked_dispose() -> None:
        calls.append("dispose")
        await real_dispose_db()

    monkeypatch.setattr(main, "init_db", _failing_init)
    monkeypatch.setattr(main, "dispose_db", _tracked_dispose)

    async def _scenario() -> None:
        with pytest.raises(RuntimeError, match="database startup failed"):
            async with main.app.router.lifespan_context(main.app):
                pytest.fail("lifespan must not start after database failure")

    asyncio.run(_scenario())

    assert calls == ["init", "dispose"]
    assert session_mod._async_engine is None
    assert session_mod._async_session_factory is None
    assert fake_mcp.close_calls == 1


def test_poller_startup_failure_cleans_up_database(
    monkeypatch,
    fake_mcp,
):
    """A poller startup failure still disposes the initialized database."""

    monkeypatch.setenv(
        "NETOPS_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )

    poller = _FakePoller(
        enabled=True,
        start_error=RuntimeError("poller startup failed"),
    )
    _install_fake_poller(monkeypatch, poller)

    async def _scenario() -> None:
        with pytest.raises(RuntimeError, match="poller startup failed"):
            async with main.app.router.lifespan_context(main.app):
                pytest.fail("lifespan must not start after poller failure")

    asyncio.run(_scenario())

    assert poller.stop_calls == 1
    assert session_mod._async_engine is None
    assert session_mod._async_session_factory is None
    assert fake_mcp.close_calls == 1


def test_shutdown_cleanup_continues_after_poller_and_mcp_failures(
    monkeypatch,
):
    """Poller, MCP, and database cleanup are independent on shutdown."""

    monkeypatch.setenv(
        "NETOPS_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )

    poller = _FakePoller(
        enabled=True,
        running=True,
        stop_error=RuntimeError("poller shutdown failed"),
    )
    _install_fake_poller(monkeypatch, poller)

    fake_mcp = _FakeMCPToolset(
        close_error=RuntimeError("MCP shutdown failed"),
    )
    monkeypatch.setattr(main, "mcp_toolset", fake_mcp)

    async def _scenario() -> None:
        with pytest.raises(RuntimeError, match="poller shutdown failed"):
            async with main.app.router.lifespan_context(main.app):
                pass

    asyncio.run(_scenario())

    assert poller.stop_calls == 1
    assert fake_mcp.close_calls == 1
    assert session_mod._async_engine is None
    assert session_mod._async_session_factory is None


def test_lifespan_preserves_telemetry_store_and_poller(fake_mcp):
    """Database integration does not replace existing telemetry lifecycle."""

    async def _scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            assert isinstance(main.app.state.telemetry_store, TelemetryStore)
            assert isinstance(
                main.app.state.telemetry_poller,
                TelemetryPoller,
            )
            assert main.app.state.telemetry_poller.enabled is False
            assert main.app.state.telemetry_poller.is_running is False

    asyncio.run(_scenario())
    assert fake_mcp.close_calls == 1


def test_repeated_lifespans_do_not_leak_database_engines(
    monkeypatch,
    fake_mcp,
):
    """Repeated startup/shutdown cycles reset engine and factory state."""

    monkeypatch.setenv(
        "NETOPS_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )

    engines = []

    async def _scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            engine = session_mod._async_engine
            assert engine is not None
            assert session_mod._async_session_factory is not None
            engines.append(engine)

        assert session_mod._async_engine is None
        assert session_mod._async_session_factory is None

    for _ in range(3):
        asyncio.run(_scenario())

    assert len({id(engine) for engine in engines}) == 3
    assert fake_mcp.close_calls == 3
