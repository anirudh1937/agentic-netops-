"""Unit tests for the telemetry poller (Milestone 2).

All tests use in-memory fakes and mocks -- no live device, no network, and no
external service is contacted. The poller is tested in isolation with a fake
:class:`TelemetryStore` and fake :class:`SandboxConnector` instances.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import pytest

from app.cisco import CiscoTelemetryError
from app.connectors.base import SandboxConnector, SandboxConnectorError
from app.inventory import Vendor
from app.telemetry import NetworkTelemetry
from app.telemetry_poller import TelemetryPoller
from app.telemetry_store import TelemetryStore


# -------------------------------------------------------------------------- #
# Fakes
# -------------------------------------------------------------------------- #


class _FakeConnector(SandboxConnector):
    """Minimal connector stand-in for poller tests.

    ``vendor`` must be set (the ABC requires it as a class attribute).
    ``fetch_telemetry`` returns the configured ``NetworkTelemetry`` or raises
    the configured exception.
    """

    vendor = Vendor.CISCO

    def __init__(
        self,
        *,
        host="https://fake.device",
        telemetry=None,
        exc: Optional[Exception] = None,
    ) -> None:
        self._device = SimpleNamespace(host=host)
        self._telemetry = telemetry
        self._exc = exc
        self.calls = 0

    @property
    def host(self) -> str:
        return self._device.host

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used by poller tests")

    def fetch_telemetry(self, interface=None) -> NetworkTelemetry:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._telemetry


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


# -------------------------------------------------------------------------- #
# Configuration / disabled-by-default
# -------------------------------------------------------------------------- #


def test_poller_disabled_by_default():
    store = TelemetryStore()
    poller = TelemetryPoller(store=store)
    assert poller.enabled is False
    assert poller.is_running is False


def test_poller_enabled_only_when_explicit():
    store = TelemetryStore()
    poller = TelemetryPoller(store=store, enabled=True, interval_seconds=1.0)
    assert poller.enabled is True


def test_poller_rejects_non_positive_interval():
    store = TelemetryStore()
    with pytest.raises(ValueError):
        TelemetryPoller(store=store, interval_seconds=0)
    with pytest.raises(ValueError):
        TelemetryPoller(store=store, interval_seconds=-1.0)


def test_poller_no_connectors_poll_once_is_noop():
    store = TelemetryStore()
    poller = TelemetryPoller(store=store, enabled=True)
    asyncio.run(poller.poll_once())
    assert len(store) == 0


# -------------------------------------------------------------------------- #
# Successful poll
# -------------------------------------------------------------------------- #


def test_successful_poll_stores_telemetry_and_diagnosis():
    store = TelemetryStore()
    telemetry = _make_telemetry()
    connector = _FakeConnector(telemetry=telemetry)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
    )

    asyncio.run(poller.poll_once())

    assert connector.calls == 1
    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.vendor == "cisco"
    assert reading.device_id == "router-01"
    assert reading.interface == "GigabitEthernet0/1"
    assert reading.source == "cisco"
    assert reading.error is None
    assert reading.telemetry["cpu_percent"] == 50.0
    assert reading.diagnosis["severity"] == "normal"
    assert reading.fetched_at.endswith("+00:00")


# -------------------------------------------------------------------------- #
# Multiple targets
# -------------------------------------------------------------------------- #


def test_multiple_targets_all_attempted():
    store = TelemetryStore()
    tel_a = _make_telemetry(device_id="device-a", interface="GigabitEthernet0/1")
    tel_b = _make_telemetry(device_id="device-b", interface="GigabitEthernet0/2")
    conn_a = _FakeConnector(host="https://device-a", telemetry=tel_a)
    conn_b = _FakeConnector(host="https://device-b", telemetry=tel_b)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn_a, "arista": conn_b},
        enabled=True,
    )

    asyncio.run(poller.poll_once())

    assert conn_a.calls == 1
    assert conn_b.calls == 1
    assert store.get("cisco", "device-a", "GigabitEthernet0/1") is not None
    assert store.get("arista", "device-b", "GigabitEthernet0/2") is not None
    assert len(store) == 2


# -------------------------------------------------------------------------- #
# Failure isolation
# -------------------------------------------------------------------------- #


def test_failed_target_does_not_block_subsequent_targets():
    store = TelemetryStore()
    good = _make_telemetry(device_id="device-good", interface="GigabitEthernet0/1")
    conn_bad = _FakeConnector(
        host="https://bad.device",
        exc=SandboxConnectorError("Could not reach device at 'bad.device'."),
    )
    conn_good = _FakeConnector(host="https://good.device", telemetry=good)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn_bad, "arista": conn_good},
        enabled=True,
    )

    asyncio.run(poller.poll_once())

    assert conn_bad.calls == 1
    assert conn_good.calls == 1
    # Bad target (first failure, no verified identity): no store record is
    # created with a fabricated device ID.
    assert store.get("cisco", "cisco:https://bad.device", "unknown") is None
    # Good target still stored.
    assert store.get("arista", "device-good", "GigabitEthernet0/1") is not None
    assert len(store) == 1


def test_first_poll_failure_creates_no_store_record():
    """A first-poll failure with no verified identity must not fabricate a record."""
    store = TelemetryStore()
    conn = _FakeConnector(
        exc=SandboxConnectorError("Could not reach device at 'bad.device'.")
    )
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn},
        enabled=True,
    )

    asyncio.run(poller.poll_once())

    # The attempt was made.
    assert conn.calls == 1
    # But no store record with a fabricated device ID.
    assert len(store) == 0
    assert store.get("cisco", "cisco:https://fake.device", "unknown") is None
    assert store.get_all() == []


def test_failure_after_success_preserves_existing_record():
    """A failure after a successful poll records the error against the real
    identity while preserving last-success telemetry and timestamps."""
    store = TelemetryStore()
    first = _make_telemetry(device_id="device-1", interface="GigabitEthernet0/1")
    conn = _FakeConnector(telemetry=first)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn},
        enabled=True,
    )

    # First poll succeeds -> verified identity established.
    asyncio.run(poller.poll_once())
    reading = store.get("cisco", "device-1", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.telemetry["cpu_percent"] == 50.0
    assert reading.error is None
    assert reading.last_successful_timestamp == "2026-01-01T00:00:00+00:00"

    # Second poll fails: error recorded against the same real identity.
    conn._exc = SandboxConnectorError("Device rejected authentication.")
    asyncio.run(poller.poll_once())

    reading = store.get("cisco", "device-1", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is not None
    assert "authentication" in reading.error.lower()
    # Last-success telemetry and timestamps preserved.
    assert reading.telemetry["cpu_percent"] == 50.0
    assert reading.last_successful_timestamp == "2026-01-01T00:00:00+00:00"
    assert reading.last_poll_outcome == "failure"
    assert reading.consecutive_failures == 1


# -------------------------------------------------------------------------- #
# No overlapping polls
# -------------------------------------------------------------------------- #


def test_no_overlapping_poll_cycles():
    store = TelemetryStore()

    class _GuardedConnector(_FakeConnector):
        """Tracks concurrent fetch_telemetry invocations to detect overlap."""

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.active_fetches = 0
            self.max_active = 0

        def fetch_telemetry(self, interface=None):
            self.active_fetches += 1
            self.max_active = max(self.max_active, self.active_fetches)
            try:
                return super().fetch_telemetry(interface)
            finally:
                self.active_fetches -= 1

    telemetry = _make_telemetry()
    conn = _GuardedConnector(telemetry=telemetry)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn},
        enabled=True,
        interval_seconds=0.01,
    )

    async def run():
        # Run two poll_once() concurrently. The lock serializes them, so the
        # connector must never be re-entered while a fetch is in flight.
        await asyncio.gather(poller.poll_once(), poller.poll_once())

    asyncio.run(run())
    assert conn.max_active == 1
    assert conn.calls == 2


# -------------------------------------------------------------------------- #
# Start/stop lifecycle and cancellation
# -------------------------------------------------------------------------- #


def test_start_stop_lifecycle():
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": _FakeConnector(telemetry=_make_telemetry())},
        enabled=True,
        interval_seconds=0.01,
    )
    assert not poller.is_running

    async def run():
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert not poller.is_running

    asyncio.run(run())


def test_start_when_disabled_does_not_run():
    store = TelemetryStore()
    poller = TelemetryPoller(store=store, enabled=False)
    asyncio.run(poller.start())
    assert not poller.is_running


def test_stop_when_not_running_is_safe():
    store = TelemetryStore()
    poller = TelemetryPoller(store=store, enabled=True)
    asyncio.run(poller.stop())  # should not raise


# -------------------------------------------------------------------------- #
# No unintended network calls
# -------------------------------------------------------------------------- #


def test_poller_never_makes_real_network_calls(monkeypatch):
    """Verify the poller only touches injected connectors, never the network.

    We patch httpx.Client to raise if instantiated, confirming the poller
    never builds a real HTTP client.
    """
    import httpx

    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": _FakeConnector(telemetry=_make_telemetry())},
        enabled=True,
    )

    original_client = httpx.Client

    def _boom(*args, **kwargs):
        raise AssertionError("Poller must not instantiate httpx.Client")

    monkeypatch.setattr(httpx, "Client", _boom)
    try:
        asyncio.run(poller.poll_once())
    finally:
        monkeypatch.setattr(httpx, "Client", original_client)

    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None


def test_poller_does_not_log_credentials(caplog):
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={
            "cisco": _FakeConnector(
                host="https://sandbox.local",
                exc=SandboxConnectorError("Cisco device rejected authentication."),
            )
        },
        enabled=True,
    )
    with caplog.at_level("WARNING"):
        asyncio.run(poller.poll_once())
    # No passwords/auth tokens appear in log output.
    for record in caplog.records:
        assert "password" not in record.getMessage().lower()
        assert "authorization" not in record.getMessage().lower()
        assert "S3cr3t" not in record.getMessage()


# -------------------------------------------------------------------------- #
# from_config factory
# -------------------------------------------------------------------------- #


def test_from_config_builds_connectors_via_make_connector(monkeypatch):
    """from_config uses make_connector for each available vendor."""
    store = TelemetryStore()
    created = []

    def _fake_make_connector(vendor, environ=None):
        created.append(vendor.value)
        return _FakeConnector(host=f"https://{vendor.value}.device")

    monkeypatch.setattr(
        "app.telemetry_poller.make_connector", _fake_make_connector
    )
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco", "arista"],
    )

    poller = TelemetryPoller.from_config(store, enabled=False)

    assert created == ["cisco", "arista"]
    assert set(poller.connectors) == {"cisco", "arista"}
    assert not poller.enabled
    assert not poller.is_running


def test_from_config_skips_unconfigured_vendor(monkeypatch, caplog):
    """from_config skips vendors with missing configuration instead of pretending."""
    store = TelemetryStore()

    def _fake_make_connector(vendor, environ=None):
        if vendor == Vendor.CISCO:
            raise SandboxConnectorError(
                "Cisco device host is not configured (set CISCO_SANDBOX_HOST)."
            )
        return _FakeConnector(host=f"https://{vendor.value}.device")

    monkeypatch.setattr(
        "app.telemetry_poller.make_connector", _fake_make_connector
    )
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco", "arista"],
    )

    with caplog.at_level("WARNING"):
        poller = TelemetryPoller.from_config(store, enabled=False)

    assert "cisco" in poller.unconfigured_vendors
    assert "arista" in poller.connectors
    assert "cisco" not in poller.connectors
    # Credential-safe message logged, no secrets.
    for record in caplog.records:
        assert "password" not in record.getMessage().lower()


def test_from_config_skips_cisco_telemetry_error(monkeypatch):
    """Cisco's construction error is treated as missing configuration."""
    store = TelemetryStore()

    def _fake_make_connector(vendor, environ=None):
        if vendor == Vendor.CISCO:
            raise CiscoTelemetryError(
                "Cisco device host is not configured (set CISCO_SANDBOX_HOST)."
            )
        return _FakeConnector(host="https://arista.device")

    monkeypatch.setattr(
        "app.telemetry_poller.make_connector", _fake_make_connector
    )
    monkeypatch.setattr(
        "app.telemetry_poller.available_vendors",
        lambda: ["cisco", "arista"],
    )

    poller = TelemetryPoller.from_config(store, enabled=False)

    assert "cisco" not in poller.connectors
    assert "arista" in poller.connectors
    assert "cisco" in poller.unconfigured_vendors


def test_from_config_does_not_start_polling_or_network():
    """Construction via from_config never starts the loop or makes calls."""
    store = TelemetryStore()
    poller = TelemetryPoller.from_config(store, enabled=True, vendors=[])
    assert poller.enabled
    assert not poller.is_running
    assert len(poller.connectors) == 0


# -------------------------------------------------------------------------- #
# Freshness through the poller
# -------------------------------------------------------------------------- #


def test_poller_failure_preserves_last_success_timestamps():
    """A failed poll via the poller preserves last-success timestamps."""
    store = TelemetryStore()
    first = _make_telemetry(device_id="device-1", interface="GigabitEthernet0/1")
    conn = _FakeConnector(telemetry=first)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn},
        enabled=True,
    )

    # First poll succeeds.
    asyncio.run(poller.poll_once())
    reading = store.get("cisco", "device-1", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.last_successful_timestamp == "2026-01-01T00:00:00+00:00"
    assert reading.last_successful_fetched_at.endswith("+00:00")
    assert reading.error is None

    # Second poll fails: success timestamps preserved.
    conn._exc = SandboxConnectorError("Could not reach device at 'bad.device'.")
    asyncio.run(poller.poll_once())

    reading = store.get("cisco", "device-1", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is not None
    assert reading.last_poll_outcome == "failure"
    assert reading.consecutive_failures == 1
    # Last-success timestamps preserved, not clobbered by failure.
    assert reading.last_successful_timestamp == "2026-01-01T00:00:00+00:00"
    assert reading.last_successful_fetched_at.endswith("+00:00")
    # Last-success telemetry preserved.
    assert reading.telemetry["cpu_percent"] == 50.0


def test_poller_recovery_clears_error_and_updates_success():
    """A successful recovery clears the error and updates success timestamps."""
    store = TelemetryStore()
    first = _make_telemetry(device_id="device-1", interface="GigabitEthernet0/1")
    conn = _FakeConnector(telemetry=first)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": conn},
        enabled=True,
    )

    asyncio.run(poller.poll_once())
    conn._exc = SandboxConnectorError("Device rejected authentication.")
    asyncio.run(poller.poll_once())

    reading = store.get("cisco", "device-1", "GigabitEthernet0/1")
    assert reading.error is not None

    # Recovery: clear the exception.
    conn._exc = None
    asyncio.run(poller.poll_once())

    reading = store.get("cisco", "device-1", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None
    assert reading.last_poll_outcome == "success"
    assert reading.consecutive_failures == 0
    assert reading.telemetry["cpu_percent"] == 50.0


# -------------------------------------------------------------------------- #
# Lifecycle edge cases
# -------------------------------------------------------------------------- #


def test_stop_idempotent_after_start():
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": _FakeConnector(telemetry=_make_telemetry())},
        enabled=True,
        interval_seconds=0.01,
    )

    async def run():
        await poller.start()
        await poller.stop()
        # Calling stop again must be safe.
        await poller.stop()
        assert not poller.is_running

    asyncio.run(run())
