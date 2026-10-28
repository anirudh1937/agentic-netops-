"""Tests for GET /telemetry/latest and application lifespan (Milestone 2,
Phase 3).

All tests use in-memory fakes and mocks -- no live device, no network, and no
external service is contacted. Polling is disabled by default; the
``NETOPS_POLL_ENABLED`` opt-in flag is never set in these tests unless a test
explicitly enables it with a mocked connector.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

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
    """Minimal connector stand-in for lifespan tests.

    Not a SandboxConnector subclass -- it is injected by patching
    ``make_connector`` so the poller never builds a real HTTP client.
    """

    def __init__(self, host="https://fake.device"):
        self._device = SimpleNamespace(host=host)

    @property
    def host(self):
        return self._device.host

    def connectivity_check(self):
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used by lifespan tests")

    def fetch_telemetry(self, interface=None):
        return _make_telemetry()


class _FailingConnector(_FakeConnector):
    """Connector that always fails without touching the network."""

    def fetch_telemetry(self, interface=None):
        raise SandboxConnectorError("Device rejected authentication.")


def _put_reading(store, vendor, device_id, interface, telemetry, diagnosis, error=None):
    """Helper to persist a reading into the app-owned store."""
    store.put(
        vendor=vendor,
        device_id=device_id,
        interface=interface,
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        source=vendor,
        telemetry=telemetry,
        diagnosis=diagnosis,
        error=error,
    )


# -------------------------------------------------------------------------- #
# A. Empty store
# -------------------------------------------------------------------------- #


def test_latest_telemetry_empty_store():
    """GET /telemetry/latest returns an empty response with HTTP 200."""
    with TestClient(app) as client:
        response = client.get("/telemetry/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["readings"] == []
    assert body["count"] == 0
    assert "generated_at" in body


# -------------------------------------------------------------------------- #
# B. Stored successful reading
# -------------------------------------------------------------------------- #


def test_latest_telemetry_returns_stored_reading():
    telemetry = _make_telemetry()
    diagnosis = {
        "device_id": "router-01",
        "interface": "GigabitEthernet0/1",
        "severity": "normal",
        "diagnosis": "No significant network anomaly detected.",
        "anomalies": [],
        "evidence": [],
        "recommendation": "Continue monitoring.",
    }

    with TestClient(app) as client:
        store: TelemetryStore = app.state.telemetry_store
        _put_reading(
            store,
            vendor="cisco",
            device_id="router-01",
            interface="GigabitEthernet0/1",
            telemetry=telemetry.model_dump(),
            diagnosis=diagnosis,
        )

        response = client.get("/telemetry/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    reading = body["readings"][0]
    assert reading["vendor"] == "cisco"
    assert reading["device_id"] == "router-01"
    assert reading["interface"] == "GigabitEthernet0/1"
    assert reading["source"] == "cisco"
    assert reading["error"] is None
    assert reading["last_poll_outcome"] == "success"
    assert reading["consecutive_failures"] == 0
    assert reading["telemetry"]["cpu_percent"] == 50.0
    assert reading["diagnosis"]["severity"] == "normal"
    assert reading["last_successful_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert reading["last_successful_fetched_at"].endswith("+00:00")


# -------------------------------------------------------------------------- #
# C. Failed polling after success
# -------------------------------------------------------------------------- #


def test_latest_telemetry_failure_preserves_last_success():
    telemetry = _make_telemetry()
    diagnosis = {
        "device_id": "router-01",
        "interface": "GigabitEthernet0/1",
        "severity": "normal",
        "diagnosis": "No significant network anomaly detected.",
        "anomalies": [],
        "evidence": [],
        "recommendation": "Continue monitoring.",
    }

    with TestClient(app) as client:
        store: TelemetryStore = app.state.telemetry_store

        # First, a successful reading.
        _put_reading(
            store,
            vendor="cisco",
            device_id="router-01",
            interface="GigabitEthernet0/1",
            telemetry=telemetry.model_dump(),
            diagnosis=diagnosis,
        )

        # Then a failed poll against the same verified identity.
        store.put(
            vendor="cisco",
            device_id="router-01",
            interface="GigabitEthernet0/1",
            timestamp="2026-01-01T00:00:00+00:00",
            fetched_at="2026-01-01T00:01:00+00:00",
            source="cisco",
            telemetry={},
            diagnosis={},
            error="Device rejected authentication.",
        )

        response = client.get("/telemetry/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    reading = body["readings"][0]
    assert reading["error"] == "Device rejected authentication."
    assert reading["last_poll_outcome"] == "failure"
    assert reading["consecutive_failures"] == 1
    # Last-success telemetry and timestamps preserved, not clobbered.
    assert reading["telemetry"]["cpu_percent"] == 50.0
    assert reading["diagnosis"]["severity"] == "normal"
    assert reading["last_successful_timestamp"] == "2026-01-01T00:00:00+00:00"
    assert reading["last_successful_fetched_at"].endswith("+00:00")


# -------------------------------------------------------------------------- #
# D. Recovery
# -------------------------------------------------------------------------- #


def test_latest_telemetry_recovery_clears_error():
    telemetry = _make_telemetry()
    diagnosis = {
        "device_id": "router-01",
        "interface": "GigabitEthernet0/1",
        "severity": "normal",
        "diagnosis": "No significant network anomaly detected.",
        "anomalies": [],
        "evidence": [],
        "recommendation": "Continue monitoring.",
    }

    with TestClient(app) as client:
        store: TelemetryStore = app.state.telemetry_store

        # Success, then failure.
        _put_reading(
            store,
            vendor="cisco",
            device_id="router-01",
            interface="GigabitEthernet0/1",
            telemetry=telemetry.model_dump(),
            diagnosis=diagnosis,
        )
        store.put(
            vendor="cisco",
            device_id="router-01",
            interface="GigabitEthernet0/1",
            timestamp="2026-01-01T00:00:00+00:00",
            fetched_at="2026-01-01T00:01:00+00:00",
            source="cisco",
            telemetry={},
            diagnosis={},
            error="Device rejected authentication.",
        )

        # Recovery: successful poll again.
        recovery_diagnosis = dict(diagnosis, severity="medium")
        _put_reading(
            store,
            vendor="cisco",
            device_id="router-01",
            interface="GigabitEthernet0/1",
            telemetry=telemetry.model_dump(),
            diagnosis=recovery_diagnosis,
        )

        response = client.get("/telemetry/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    reading = body["readings"][0]
    assert reading["error"] is None
    assert reading["last_poll_outcome"] == "success"
    assert reading["consecutive_failures"] == 0


# -------------------------------------------------------------------------- #
# E. First poll failure without known identity
# -------------------------------------------------------------------------- #


def test_latest_telemetry_no_fabricated_record_on_first_failure():
    """A first-poll failure creates no store record with a fabricated device ID."""
    with TestClient(app) as client:
        store: TelemetryStore = client.app.state.telemetry_store
        poller = TelemetryPoller(
            store=store,
            connectors={"cisco": _FailingConnector()},
            enabled=True,
        )

        asyncio.run(poller.poll_once())

        assert len(store) == 0
        response = client.get("/telemetry/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["readings"] == []
    assert body["count"] == 0


# -------------------------------------------------------------------------- #
# F. Read-only behavior
# -------------------------------------------------------------------------- #


def test_latest_telemetry_read_only_no_poll():
    """Calling the endpoint does not invoke poll_once or any connector."""
    poll_calls = []

    def _fake_poll_once(self):
        poll_calls.append(True)

    with patch.object(TelemetryPoller, "poll_once", _fake_poll_once):
        with TestClient(app) as client:
            store: TelemetryStore = client.app.state.telemetry_store
            store.put(
                vendor="cisco",
                device_id="router-01",
                interface="GigabitEthernet0/1",
                timestamp="2026-01-01T00:00:00+00:00",
                fetched_at="2026-01-01T00:00:05+00:00",
                source="cisco",
                telemetry={"cpu_percent": 50.0},
                diagnosis={"severity": "normal"},
            )
            response = client.get("/telemetry/latest")

    assert response.status_code == 200
    assert poll_calls == []
    body = response.json()
    assert body["count"] == 1
    assert body["readings"][0]["device_id"] == "router-01"


# -------------------------------------------------------------------------- #
# G. Lifespan
# -------------------------------------------------------------------------- #


def test_lifespan_creates_store_and_poller_disabled_by_default(monkeypatch):
    """Application startup creates store + poller; polling disabled by default."""
    monkeypatch.delenv("CISCO_SANDBOX_HOST", raising=False)

    with TestClient(app) as client:
        assert hasattr(client.app.state, "telemetry_store")
        assert isinstance(client.app.state.telemetry_store, TelemetryStore)
        assert hasattr(client.app.state, "telemetry_poller")
        assert isinstance(client.app.state.telemetry_poller, TelemetryPoller)
        assert client.app.state.telemetry_poller.enabled is False
        assert not client.app.state.telemetry_poller.is_running


def test_lifespan_stops_poller_on_shutdown(monkeypatch):
    """When polling is enabled, shutdown stops the poller cleanly."""
    monkeypatch.setenv("NETOPS_POLL_ENABLED", "1")

    fake_connector = _FakeConnector(host="https://fake.device")

    def _fake_make_connector(vendor, environ=None):
        return fake_connector

    with patch(
        "app.telemetry_poller.make_connector", _fake_make_connector
    ), patch(
        "app.telemetry_poller.available_vendors", lambda: ["cisco"]
    ):
        with TestClient(app) as client:
            poller = client.app.state.telemetry_poller
            assert poller.enabled is True
            assert poller.is_running is True

        # After shutdown.
        assert poller.is_running is False


# -------------------------------------------------------------------------- #
# H. Isolation
# -------------------------------------------------------------------------- #


def test_lifespan_separate_app_instances_have_separate_stores():
    """Two application instances do not share a telemetry store."""
    from fastapi import FastAPI

    from app.main import app_lifespan

    app_a = FastAPI(title="Agentic NetOps A", lifespan=app_lifespan)
    app_b = FastAPI(title="Agentic NetOps B", lifespan=app_lifespan)

    with patch(
        "app.telemetry_poller.available_vendors", lambda: []
    ), TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        store_a = client_a.app.state.telemetry_store
        store_b = client_b.app.state.telemetry_store
        assert store_a is not store_b
        assert len(store_a) == 0
        assert len(store_b) == 0

        # Inject into A only; B must remain unaffected.
        store_a.put(
            vendor="cisco",
            device_id="device-a",
            interface="GigabitEthernet0/1",
            timestamp="2026-01-01T00:00:00+00:00",
            fetched_at="2026-01-01T00:00:05+00:00",
            source="cisco",
            telemetry={"cpu_percent": 50.0},
            diagnosis={"severity": "normal"},
        )

        assert len(store_a) == 1
        assert len(store_b) == 0


# -------------------------------------------------------------------------- #
# I. Regression: existing routes still work
# -------------------------------------------------------------------------- #


def test_existing_routes_unaffected():
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/dashboard").status_code == 200
        assert client.get("/vendors").status_code == 200
