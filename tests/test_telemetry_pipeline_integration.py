"""End-to-end telemetry pipeline integration tests (Milestone 3).

These tests deliberately cross the existing component boundaries:

    connector -> TelemetryPoller -> TelemetryStore -> /telemetry/latest
                                                   -> /telemetry/status

All device I/O is served by ``httpx.MockTransport`` or an in-memory connector.
No live Cisco, DGX, Gemini, or other external service is contacted.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cisco import CiscoClient, CiscoConnector, CiscoDevice
from app.connectors.base import SandboxConnector, SandboxConnectorError
from app.inventory import Vendor
from app.main import app
from app.telemetry import NetworkTelemetry
from app.telemetry_poller import TelemetryPoller
from app.telemetry_store import TelemetryStore


DEVICE_HOST = "https://sandbox.local"
USERNAME = "devnet"
PASSWORD = "not-a-real-secret"

CPU_BODY = {
    "Cisco-IOS-XE-process-cpu-oper:cpu-utilization": {
        "five-seconds": 92.5,
        "one-second": 91.0,
        "five-minute": 80.0,
        "one-minute": 78.0,
        "total": 92.5,
    }
}

MEMORY_BODY = {
    "Cisco-IOS-XE-memory-oper:memory-statistic": [
        {"total-memory": 4000000, "used-memory": 3600000},
    ]
}

INTERFACES_BODY = {
    "Cisco-IOS-XE-interfaces-oper:interfaces": {
        "interface": [
            {
                "name": "GigabitEthernet1",
                "oper-status": "if-oper-state-ready",
                "admin-status": "if-state-up",
            },
        ]
    }
}

HOSTNAME_BODY = {"Cisco-IOS-XE-native:hostname": "sandbox-iosxe"}


@pytest.fixture(autouse=True)
def _offline_defaults(monkeypatch):
    """Keep background polling and threshold overrides out of these tests."""

    monkeypatch.delenv("NETOPS_POLL_ENABLED", raising=False)
    monkeypatch.delenv("NETOPS_STALE_THRESHOLD_SECONDS", raising=False)


def _mock_transport() -> httpx.MockTransport:
    routes = {
        "process-cpu-oper": (200, CPU_BODY),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, INTERFACES_BODY),
        "native:native": (200, HOSTNAME_BODY),
    }

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for key, (status, body) in routes.items():
            if key in path:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"errors": [{"description": "not found"}]})

    return httpx.MockTransport(responder)


def _make_cisco_connector() -> CiscoConnector:
    """Build the real Cisco adapter over a mocked HTTP transport."""

    device = CiscoDevice(
        host=DEVICE_HOST,
        username=USERNAME,
        password=PASSWORD,
        api_root="/restconf",
    )
    http_client = httpx.Client(base_url=DEVICE_HOST, transport=_mock_transport())
    client = CiscoClient(device, client=http_client)
    return CiscoConnector(device=device, client=client)


def _make_normal_telemetry() -> NetworkTelemetry:
    return NetworkTelemetry(
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp="2026-01-01T00:00:00+00:00",
        cpu_percent=50.0,
        memory_percent=60.0,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status="up",
    )


class _ToggleConnector(SandboxConnector):
    """Deterministic connector whose next fetch can be made to fail."""

    vendor = Vendor.CISCO

    def __init__(self) -> None:
        self._device = SimpleNamespace(host="https://fake.device")
        self._telemetry = _make_normal_telemetry()
        self.fail = False
        self.calls = 0

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used by pipeline integration tests")

    def fetch_telemetry(self, interface=None) -> NetworkTelemetry:
        self.calls += 1
        if self.fail:
            raise SandboxConnectorError("Device rejected authentication.")
        return self._telemetry


@contextmanager
def _pipeline_client(store: TelemetryStore, poller: TelemetryPoller):
    """Serve the test-owned pipeline through the real FastAPI app."""

    with TestClient(app) as client:
        client.app.state.telemetry_store = store
        client.app.state.telemetry_poller = poller
        yield client


def test_cisco_connector_to_poller_store_and_api():
    """Real Cisco parsing -> real poller/store -> latest/status endpoints."""

    connector = _make_cisco_connector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
    )

    asyncio.run(poller.poll_once())

    with _pipeline_client(store, poller) as client:
        latest = client.get("/telemetry/latest")
        status = client.get("/telemetry/status")

    assert latest.status_code == 200
    body = latest.json()
    assert body["count"] == 1

    reading = body["readings"][0]
    assert reading["vendor"] == "cisco"
    assert reading["device_id"] == "sandbox-iosxe"
    assert reading["interface"] == "GigabitEthernet1"
    assert reading["telemetry"]["cpu_percent"] == 92.5
    assert reading["telemetry"]["memory_percent"] == 90.0
    assert reading["diagnosis"]["severity"] == "high"
    assert reading["diagnosis"]["anomalies"] == ["high_cpu", "high_memory"]
    assert reading["error"] is None
    assert reading["freshness"]["state"] == "HEALTHY"

    assert status.status_code == 200
    status_body = status.json()
    assert status_body["configured_vendors"] == ["cisco"]
    assert status_body["unconfigured_vendors"] == []
    assert status_body["last_cycle_outcome"] == "success"
    assert status_body["last_error"] is None


def test_poll_failure_preserves_last_known_then_recovers():
    """Success -> failure -> recovery is visible through both read endpoints."""

    store = TelemetryStore()
    connector = _ToggleConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
    )

    # Initial success establishes the verified identity and last-known data.
    asyncio.run(poller.poll_once())
    first = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert first is not None
    successful_fetched_at = first.last_successful_fetched_at

    # Failure for the same verified identity.
    connector.fail = True
    asyncio.run(poller.poll_once())

    with _pipeline_client(store, poller) as client:
        failed_latest = client.get("/telemetry/latest")
        failed_status = client.get("/telemetry/status")

    assert failed_latest.status_code == 200
    failed_body = failed_latest.json()
    assert failed_body["count"] == 1
    failed_reading = failed_body["readings"][0]
    assert failed_reading["error"] == "Device rejected authentication."
    assert failed_reading["last_poll_outcome"] == "failure"
    assert failed_reading["consecutive_failures"] == 1
    assert failed_reading["last_successful_fetched_at"] == successful_fetched_at
    assert failed_reading["telemetry"]["cpu_percent"] == 50.0
    assert failed_reading["diagnosis"]["severity"] == "normal"
    assert failed_reading["freshness"]["state"] == "POLLING_FAILED"

    assert failed_status.status_code == 200
    failed_status_body = failed_status.json()
    assert failed_status_body["last_cycle_outcome"] == "degraded"
    assert failed_status_body["last_error"] == "1 vendor poll failed."

    # Recovery: the same connector succeeds again.
    connector.fail = False
    asyncio.run(poller.poll_once())

    with _pipeline_client(store, poller) as client:
        recovered_latest = client.get("/telemetry/latest")
        recovered_status = client.get("/telemetry/status")

    assert recovered_latest.status_code == 200
    recovered_body = recovered_latest.json()
    assert recovered_body["count"] == 1
    recovered_reading = recovered_body["readings"][0]
    assert recovered_reading["error"] is None
    assert recovered_reading["last_poll_outcome"] == "success"
    assert recovered_reading["consecutive_failures"] == 0
    assert recovered_reading["telemetry"]["cpu_percent"] == 50.0
    assert recovered_reading["diagnosis"]["severity"] == "normal"
    assert recovered_reading["freshness"]["state"] == "HEALTHY"

    assert recovered_status.status_code == 200
    recovered_status_body = recovered_status.json()
    assert recovered_status_body["last_cycle_outcome"] == "success"
    assert recovered_status_body["last_error"] is None


def test_poller_produced_telemetry_becomes_stale(monkeypatch):
    """A poller-produced reading becomes STALE when the read clock advances."""

    store = TelemetryStore()
    connector = _ToggleConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
    )

    asyncio.run(poller.poll_once())
    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None

    future_now = datetime.now(timezone.utc) + timedelta(seconds=360)

    class _FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future_now if tz is not None else future_now.replace(tzinfo=None)

    # Only the read-side clock is advanced; no production clock abstraction.
    monkeypatch.setattr("app.main.datetime", _FutureDatetime)

    with _pipeline_client(store, poller) as client:
        response = client.get("/telemetry/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1

    stored = body["readings"][0]
    assert stored["freshness"]["state"] == "STALE"
    assert stored["freshness"]["age_seconds"] > stored["freshness"]["threshold_seconds"]
    assert stored["freshness"]["threshold_seconds"] == 300.0
    assert stored["last_poll_outcome"] == "success"
    # Last-known telemetry is preserved, not replaced by the freshness state.
    assert stored["telemetry"]["cpu_percent"] == 50.0
    assert stored["telemetry"]["memory_percent"] == 60.0
    assert stored["diagnosis"]["severity"] == "normal"
