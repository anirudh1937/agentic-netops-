"""Full offline application smoke test (Milestone 7.3).

Exercises the real application lifespan, telemetry poller, telemetry store,
FastAPI endpoints, and deterministic diagnosis engine against a simulated
vendor connector. Only the external device boundary is replaced; the store,
poller, endpoints, and diagnosis engine are the production implementations.

No live device, credential, model, or external service is contacted. Polling
is enabled through controlled environment configuration and shut down by the
application lifespan at the end of each test.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.connectors.base import SandboxConnector
from app.diagnosis import diagnose_network_issue
from app.inventory import Vendor
from app.main import app
from app.telemetry import NetworkTelemetry
from app.telemetry_poller import TelemetryPoller
from app.telemetry_store import TelemetryStore


LIVE_CONFIGURATION_ENV_VARS = (
    "CISCO_SANDBOX_HOST",
    "CISCO_SANDBOX_USERNAME",
    "CISCO_SANDBOX_PASSWORD",
    "JUNIPER_SANDBOX_HOST",
    "JUNIPER_SANDBOX_USERNAME",
    "JUNIPER_SANDBOX_PASSWORD",
    "ARISTA_SANDBOX_HOST",
    "ARISTA_SANDBOX_USERNAME",
    "ARISTA_SANDBOX_PASSWORD",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture(autouse=True)
def _offline_environment(monkeypatch):
    """Enable polling while proving no live configuration is required."""

    monkeypatch.setenv("NETOPS_POLL_ENABLED", "1")
    monkeypatch.delenv("NETOPS_STALE_THRESHOLD_SECONDS", raising=False)
    for name in LIVE_CONFIGURATION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _make_telemetry(
    *,
    device_id="router-01",
    interface="GigabitEthernet0/1",
    cpu_percent=40.0,
    memory_percent=60.0,
    packet_loss_percent=0.1,
    latency_ms=20.0,
    interface_status="up",
):
    return NetworkTelemetry(
        device_id=device_id,
        interface=interface,
        timestamp="2026-01-01T00:00:00+00:00",
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        bandwidth_mbps=500.0,
        packet_loss_percent=packet_loss_percent,
        latency_ms=latency_ms,
        interface_status=interface_status,
    )


class _SimulatedConnector(SandboxConnector):
    """Deterministic in-memory connector at the external device boundary."""

    vendor = Vendor.CISCO

    def __init__(self, telemetry: NetworkTelemetry) -> None:
        self._device = SimpleNamespace(host="https://simulated.device")
        self._telemetry = telemetry
        self.fetch_calls = 0
        self.closed = False

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("inventory is not used by this smoke test")

    def fetch_telemetry(self, interface=None) -> NetworkTelemetry:
        self.fetch_calls += 1
        return self._telemetry

    def close(self) -> None:
        self.closed = True


@contextmanager
def _application_with_connector(connector: _SimulatedConnector):
    """Start the real app with polling enabled and the simulated connector."""

    def _make_connector(vendor, environ=None):
        return connector

    with patch(
        "app.telemetry_poller.available_vendors", lambda: ["cisco"]
    ), patch(
        "app.telemetry_poller.make_connector", _make_connector
    ):
        with TestClient(app) as client:
            yield client


def _exercise_smoke(
    connector: _SimulatedConnector,
    *,
    expected_severity: str,
    expected_anomalies: list[str],
):
    """Run one real poll through the app and verify the full read path."""

    with _application_with_connector(connector) as client:
        poller = client.app.state.telemetry_poller
        store = client.app.state.telemetry_store

        # The lifespan created the real production store and poller and
        # started the poller because polling was configured as enabled.
        assert isinstance(store, TelemetryStore)
        assert isinstance(poller, TelemetryPoller)
        assert poller.enabled is True
        assert poller.is_running is True
        assert len(store) == 0

        # Run exactly one real poll cycle on the application's event loop.
        # This is deterministic: it neither waits for the 60-second interval
        # nor relies on arbitrary sleeps.
        client.portal.call(poller.poll_once)

        # The real poller wrote the simulated connector's telemetry into the
        # actual lifespan-owned store.
        assert connector.fetch_calls == 1
        assert len(store) == 1
        stored = store.get_all()[0]
        assert stored.vendor == "cisco"
        assert stored.device_id == connector._telemetry.device_id
        assert stored.interface == connector._telemetry.interface
        assert stored.telemetry == connector._telemetry.model_dump()
        assert stored.error is None
        assert stored.last_poll_outcome == "success"

        # GET /telemetry/latest exposes the stored reading.
        latest = client.get("/telemetry/latest")
        assert latest.status_code == 200
        body = latest.json()
        assert body["count"] == 1
        reading = body["readings"][0]
        assert reading["device_id"] == stored.device_id
        assert reading["interface"] == stored.interface
        assert reading["telemetry"] == stored.telemetry
        assert reading["error"] is None
        assert reading["last_poll_outcome"] == "success"

        # GET /telemetry/status reports the expected poller/store state.
        status = client.get("/telemetry/status")
        assert status.status_code == 200
        status_body = status.json()
        assert status_body["enabled"] is True
        assert status_body["running"] is True
        assert status_body["configured_vendors"] == ["cisco"]
        assert status_body["unconfigured_vendors"] == []
        assert status_body["last_cycle_outcome"] == "success"
        assert status_body["last_cycle_started_at"] is not None
        assert status_body["last_cycle_finished_at"] is not None
        assert status_body["last_error"] is None

        # Pass the telemetry returned by the API through the real
        # deterministic diagnosis engine (no fake) and confirm it agrees with
        # the diagnosis the poller stored at fetch time.
        diagnosis = diagnose_network_issue(
            NetworkTelemetry(**reading["telemetry"])
        )
        assert diagnosis == reading["diagnosis"]
        assert diagnosis == stored.diagnosis
        assert diagnosis["severity"] == expected_severity
        assert diagnosis["anomalies"] == expected_anomalies
        assert diagnosis["device_id"] == stored.device_id
        assert diagnosis["interface"] == stored.interface

    # The lifespan shut the poller down cleanly and closed the owned
    # connector exactly once.
    assert poller.is_running is False
    assert poller.status().running is False
    assert connector.closed is True


def test_full_app_smoke_healthy_telemetry():
    connector = _SimulatedConnector(_make_telemetry())

    _exercise_smoke(
        connector,
        expected_severity="normal",
        expected_anomalies=[],
    )


def test_full_app_smoke_high_cpu_telemetry():
    connector = _SimulatedConnector(_make_telemetry(cpu_percent=95.0))

    _exercise_smoke(
        connector,
        expected_severity="medium",
        expected_anomalies=["high_cpu"],
    )
