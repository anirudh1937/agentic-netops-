"""Tests for GET /telemetry/status.

All tests use in-memory fakes and monkeypatched connector factories. No live
device, network, or external service is contacted, and polling remains
disabled unless a test explicitly enables it with a fake connector.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.connectors import SandboxConnectorError
from app.main import app
from app.telemetry import NetworkTelemetry
from app.telemetry_poller import TelemetryPoller
from app.telemetry_store import TelemetryStore


@pytest.fixture(autouse=True)
def _ensure_polling_disabled(monkeypatch):
    """Keep background polling opt-in for every test in this module."""

    monkeypatch.delenv("NETOPS_POLL_ENABLED", raising=False)


def _make_telemetry(*, device_id="router-01", interface="GigabitEthernet0/1"):
    return NetworkTelemetry(
        device_id=device_id,
        interface=interface,
        timestamp="2026-01-01T00:00:00+00:00",
        cpu_percent=50.0,
        memory_percent=60.0,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status="up",
    )


class _FakeConnector:
    """Minimal connector stand-in that records any device-access attempt."""

    def __init__(self, host="https://fake.device"):
        self._device = SimpleNamespace(host=host)
        self.fetch_telemetry_calls = 0
        self.fetch_inventory_calls = 0
        self.connectivity_check_calls = 0

    @property
    def host(self):
        return self._device.host

    def connectivity_check(self):
        self.connectivity_check_calls += 1
        return True

    def fetch_inventory(self, interface=None):
        self.fetch_inventory_calls += 1
        raise NotImplementedError("not used by status tests")

    def fetch_telemetry(self, interface=None):
        self.fetch_telemetry_calls += 1
        return _make_telemetry()


STATUS_FIELDS = {
    "enabled",
    "running",
    "interval_seconds",
    "configured_vendors",
    "unconfigured_vendors",
    "last_cycle_started_at",
    "last_cycle_finished_at",
    "last_cycle_outcome",
    "last_error",
}


def test_status_endpoint_disabled_by_default(monkeypatch):
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: [],
    )

    with TestClient(app) as client:
        response = client.get("/telemetry/status")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == STATUS_FIELDS
    assert body["enabled"] is False
    assert body["running"] is False
    assert body["configured_vendors"] == []
    assert body["unconfigured_vendors"] == []
    assert body["last_cycle_started_at"] is None
    assert body["last_cycle_finished_at"] is None
    assert body["last_cycle_outcome"] is None
    assert body["last_error"] is None


def test_status_endpoint_reports_configured_vendors(monkeypatch):
    connector = _FakeConnector()
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco"],
    )
    monkeypatch.setattr(
        "app.telemetry_poller.make_connector",
        lambda vendor, environ=None: connector,
    )

    with TestClient(app) as client:
        response = client.get("/telemetry/status")

    assert response.status_code == 200
    body = response.json()
    assert body["configured_vendors"] == ["cisco"]
    assert body["unconfigured_vendors"] == []
    assert connector.fetch_telemetry_calls == 0
    assert connector.fetch_inventory_calls == 0
    assert connector.connectivity_check_calls == 0


def test_status_endpoint_reports_unconfigured_vendors(monkeypatch):
    def _missing_connector(vendor, environ=None):
        raise SandboxConnectorError(
            "Cisco device host is not configured (set CISCO_SANDBOX_HOST)."
        )

    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco"],
    )
    monkeypatch.setattr(
        "app.telemetry_poller.make_connector",
        _missing_connector,
    )

    with TestClient(app) as client:
        response = client.get("/telemetry/status")

    assert response.status_code == 200
    body = response.json()
    assert body["configured_vendors"] == []
    assert body["unconfigured_vendors"] == ["cisco"]


def test_status_endpoint_never_polls(monkeypatch):
    poll_calls = []

    async def _fake_poll_once(self):
        poll_calls.append(True)

    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: [],
    )
    monkeypatch.setattr(TelemetryPoller, "poll_once", _fake_poll_once)

    with TestClient(app) as client:
        response = client.get("/telemetry/status")

    assert response.status_code == 200
    assert poll_calls == []


def test_status_endpoint_after_shutdown(monkeypatch):
    monkeypatch.setenv("NETOPS_POLL_ENABLED", "1")
    connector = _FakeConnector()
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco"],
    )
    monkeypatch.setattr(
        "app.telemetry_poller.make_connector",
        lambda vendor, environ=None: connector,
    )

    with TestClient(app) as client:
        poller = client.app.state.telemetry_poller
        response = client.get("/telemetry/status")
        assert response.status_code == 200
        assert response.json()["running"] is True

    assert poller.status().running is False

    # The endpoint remains readable after shutdown and reports the stopped state.
    after_shutdown = TestClient(app)
    try:
        response = after_shutdown.get("/telemetry/status")
    finally:
        after_shutdown.close()

    assert response.status_code == 200
    assert response.json()["running"] is False


def test_status_endpoint_returns_503_when_poller_unavailable(monkeypatch):
    monkeypatch.delattr(app.state, "telemetry_poller", raising=False)

    client = TestClient(app)
    try:
        response = client.get("/telemetry/status")
    finally:
        client.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "Telemetry poller is unavailable."


def test_status_endpoint_error_is_credential_safe(monkeypatch):
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": _FakeConnector()},
        enabled=True,
        interval_seconds=0.01,
    )

    async def _boom_poll_all():
        raise RuntimeError("password=S3cr3t")

    monkeypatch.setattr(poller, "_poll_all", _boom_poll_all)

    async def run():
        await poller.start()
        try:
            for _ in range(300):
                if poller.status().last_cycle_outcome == "error":
                    return
                await asyncio.sleep(0.01)
            pytest.fail("Poller never recorded an unexpected cycle error")
        finally:
            await poller.stop()

    asyncio.run(run())
    assert poller.status().last_error == "RuntimeError"

    with TestClient(app) as client:
        client.app.state.telemetry_poller = poller
        response = client.get("/telemetry/status")

    assert response.status_code == 200
    assert response.json()["last_error"] == "RuntimeError"
    assert "S3cr3t" not in response.text


def test_status_endpoint_does_not_contact_devices(monkeypatch):
    connector = _FakeConnector()
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco"],
    )
    monkeypatch.setattr(
        "app.telemetry_poller.make_connector",
        lambda vendor, environ=None: connector,
    )

    with TestClient(app) as client:
        response = client.get("/telemetry/status")

    assert response.status_code == 200
    assert connector.fetch_telemetry_calls == 0
    assert connector.fetch_inventory_calls == 0
    assert connector.connectivity_check_calls == 0


def test_status_endpoint_preserves_latest_contract(monkeypatch):
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: [],
    )

    with TestClient(app) as client:
        latest = client.get("/telemetry/latest")
        status = client.get("/telemetry/status")

    assert latest.status_code == 200
    assert set(latest.json()) == {"readings", "count", "generated_at"}
    assert latest.json()["readings"] == []
    assert status.status_code == 200
