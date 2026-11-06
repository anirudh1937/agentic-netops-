"""Tests for Milestone 6 timeout configuration primitives.

Offline-only: no live devices, no network calls, no credentials touched.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from app.connectors.base import SandboxConnector
from app.inventory import Vendor
from app.telemetry import NetworkTelemetry
from app.telemetry_store import TelemetryStore
from app.telemetry_poller import (
    DEFAULT_CYCLE_TIMEOUT_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    DEFAULT_VENDOR_TIMEOUT_SECONDS,
    TelemetryPoller,
    _positive_timeout,
    _timeout_from_env,
)
from tests.test_telemetry_poller import _FakeConnector


async def _wait_for_flag(flag_getter, *, timeout: float = 2.0) -> bool:
    """Poll a flag getter from async context without blocking the event loop."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while not flag_getter():
        if _time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True



class TestPositiveTimeout:
    def test_accepts_float_and_int(self):
        assert _positive_timeout(45, name="vendor_timeout_seconds") == 45.0
        assert _positive_timeout(45.0, name="vendor_timeout_seconds") == 45.0

    def test_raises_on_non_numeric(self):
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout("not-a-number", name="vendor_timeout_seconds")
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout(None, name="vendor_timeout_seconds")

    def test_raises_on_nan(self):
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout(float("nan"), name="vendor_timeout_seconds")

    def test_raises_on_positive_infinity(self):
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout(float("inf"), name="vendor_timeout_seconds")

    def test_raises_on_negative_infinity(self):
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout(float("-inf"), name="vendor_timeout_seconds")

    def test_raises_on_zero(self):
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout(0, name="vendor_timeout_seconds")

    def test_raises_on_negative(self):
        with pytest.raises(ValueError, match="vendor_timeout_seconds"):
            _positive_timeout(-1.0, name="vendor_timeout_seconds")


class TestTimeoutFromEnv:
    def test_missing_uses_default(self):
        assert _timeout_from_env({}, "FOO_SECONDS", 45.0) == 45.0

    def test_empty_uses_default_without_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert _timeout_from_env({"FOO_SECONDS": ""}, "FOO_SECONDS", 45.0) == 45.0
        assert "FOO_SECONDS" not in caplog.text

    def test_whitespace_uses_default_without_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert (
                _timeout_from_env({"FOO_SECONDS": "   "}, "FOO_SECONDS", 45.0) == 45.0
            )
        assert "FOO_SECONDS" not in caplog.text

    def test_valid_override(self):
        assert (
            _timeout_from_env({"FOO_SECONDS": "10"}, "FOO_SECONDS", 45.0) == 10.0
        )

    def test_invalid_value_uses_default_and_warns(self, caplog):
        with caplog.at_level("WARNING"):
            value = _timeout_from_env(
                {"FOO_SECONDS": "abc"}, "FOO_SECONDS", 45.0
            )
        assert value == 45.0
        assert "FOO_SECONDS" in caplog.text

    def test_warning_does_not_echo_value(self, caplog):
        with caplog.at_level("WARNING"):
            _timeout_from_env(
                {"FOO_SECONDS": "S3cr3t-value"}, "FOO_SECONDS", 45.0
            )
        assert "S3cr3t-value" not in caplog.text

    def test_non_finite_uses_default_and_warns(self, caplog):
        with caplog.at_level("WARNING"):
            value = _timeout_from_env(
                {"FOO_SECONDS": "inf"}, "FOO_SECONDS", 45.0
            )
        assert value == 45.0
        assert "FOO_SECONDS" in caplog.text


class TestTimeoutDefaults:
    def test_defaults(self):
        assert DEFAULT_VENDOR_TIMEOUT_SECONDS == 45.0
        assert DEFAULT_CYCLE_TIMEOUT_SECONDS == 150.0
        assert DEFAULT_SHUTDOWN_TIMEOUT_SECONDS == 5.0

    def test_poller_uses_defaults(self):
        poller = TelemetryPoller(store=TelemetryStore())
        assert poller._vendor_timeout_seconds == 45.0
        assert poller._cycle_timeout_seconds == 150.0
        assert poller._shutdown_timeout_seconds == 5.0


class TestTimeoutOverrides:
    def test_constructor_overrides(self):
        store = TelemetryStore()
        poller = TelemetryPoller(
            store=store,
            vendor_timeout_seconds=10.0,
            cycle_timeout_seconds=30.0,
            shutdown_timeout_seconds=2.0,
        )
        assert poller._vendor_timeout_seconds == 10.0
        assert poller._cycle_timeout_seconds == 30.0
        assert poller._shutdown_timeout_seconds == 2.0

    def test_from_config_env_overrides(self):
        store = TelemetryStore()
        environ = {
            "NETOPS_POLL_VENDOR_TIMEOUT_SECONDS": "12",
            "NETOPS_POLL_CYCLE_TIMEOUT_SECONDS": "36",
            "NETOPS_POLL_SHUTDOWN_TIMEOUT_SECONDS": "3",
        }
        poller = TelemetryPoller.from_config(store, enabled=False, environ=environ)
        assert poller._vendor_timeout_seconds == 12.0
        assert poller._cycle_timeout_seconds == 36.0
        assert poller._shutdown_timeout_seconds == 3.0

    def test_from_config_invalid_env_uses_defaults_and_warns(self, caplog):
        store = TelemetryStore()
        environ = {
            "NETOPS_POLL_VENDOR_TIMEOUT_SECONDS": "bad",
        }
        with caplog.at_level("WARNING"):
            poller = TelemetryPoller.from_config(store, enabled=False, environ=environ)
        assert poller._vendor_timeout_seconds == 45.0
        assert "NETOPS_POLL_VENDOR_TIMEOUT_SECONDS" in caplog.text

    def test_cycle_shorter_than_vendor_warns_and_preserves_values(self, caplog):
        store = TelemetryStore()
        with caplog.at_level("WARNING"):
            poller = TelemetryPoller(
                store=store,
                vendor_timeout_seconds=60.0,
                cycle_timeout_seconds=10.0,
            )
        assert "vendor_timeout_seconds" not in caplog.text  # no poller param echo required
        assert poller._vendor_timeout_seconds == 60.0
        assert poller._cycle_timeout_seconds == 10.0
        assert any("Cycle timeout" in record.getMessage() for record in caplog.records)
        assert "shorter than vendor timeout" in caplog.text

    def test_constructor_rejects_invalid_interval_unchanged(self):
        from app.telemetry_store import TelemetryStore

        with pytest.raises(ValueError, match="interval_seconds must be positive"):
            TelemetryPoller(store=TelemetryStore(), interval_seconds=0)


def test_enabled_behavior_unchanged():
    from app.telemetry_store import TelemetryStore

    poller = TelemetryPoller(store=TelemetryStore(), enabled=False)
    assert poller.enabled is False
    assert not poller.is_running


# -------------------------------------------------------------------------- #
# Step 2: vendor-timeout enforcement tests
# -------------------------------------------------------------------------- #


def _blocking_connector_with_event(
    store, *, vendor_timeout: float, block_seconds: float = 0.2
):
    """Build a poller with one connector whose fetch blocks on an Event.

    The event is released in the caller's finally block so no worker thread
    remains blocked after the test returns.
    """

    import threading
    from types import SimpleNamespace

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry import NetworkTelemetry

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://blocked.device")
            self.calls = 0
            self.calls = 0
            self.released = False

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=block_seconds):
                raise TimeoutError("simulated fetch did not release in time")
            self.released = True
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

    connector = _BlockingConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        vendor_timeout_seconds=vendor_timeout,
    )
    return poller, connector, release


def test_vendor_timeout_enforced_on_blocking_fetch():
    store = TelemetryStore()
    poller, connector, release = _blocking_connector_with_event(
        store, vendor_timeout=0.05, block_seconds=0.2
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()
    assert connector.calls == 1
    # No telemetry written: the late completion must not resurrect the vendor.
    assert len(store) == 0
    status = poller.status()
    assert status.last_cycle_outcome == "degraded"
    assert status.last_error == "1 vendor poll failed."


def test_vendor_timeout_reported_in_status():
    from app.connectors.base import SandboxConnectorError

    store = TelemetryStore()
    poller, connector, release = _blocking_connector_with_event(
        store, vendor_timeout=0.05, block_seconds=0.2
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()
    status = poller.status()
    assert status.last_cycle_outcome == "degraded"
    assert status.last_error == "1 vendor poll failed."
    assert status.last_cycle_finished_at is not None


def test_other_vendor_still_polled_after_timeout():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry import NetworkTelemetry
    from app.telemetry_store import TelemetryStore

    release_slow = threading.Event()
    tel_good = NetworkTelemetry(
        device_id="router-02",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        cpu_percent=10.0,
        memory_percent=20.0,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status="up",
    )

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release_slow.wait(timeout=0.2):
                raise TimeoutError("simulated fetch did not release in time")
            return tel_good

    slow = _SlowConnector()
    from tests.test_telemetry_poller import _FakeConnector

    good = _FakeConnector(host="https://good.device", telemetry=tel_good)

    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": slow, "arista": good},
        enabled=True,
        vendor_timeout_seconds=0.05,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release_slow.set()

    # Slow vendor timed out, good vendor still polled and stored.
    assert store.get("arista", "router-02", "GigabitEthernet0/2") is not None
    assert len(store) == 1
    status = poller.status()
    assert status.last_cycle_outcome == "degraded"
    assert status.last_error == "1 vendor poll failed."


def test_successful_vendor_within_timeout_unchanged():
    from app.telemetry_store import TelemetryStore

    store = TelemetryStore()
    telemetry = NetworkTelemetry(
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
    from tests.test_telemetry_poller import _FakeConnector

    connector = _FakeConnector(telemetry=telemetry)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        vendor_timeout_seconds=45.0,
    )
    asyncio.run(poller.poll_once())
    assert connector.calls == 1
    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None
    assert reading.telemetry["cpu_percent"] == 50.0


def test_vendor_timeout_uses_configured_value():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry import NetworkTelemetry

    release = threading.Event()
    tel = NetworkTelemetry(
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

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://blocked.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=1.0):
                raise TimeoutError("simulated fetch did not release in time")
            return tel

    connector = _BlockingConnector()
    from app.telemetry_store import TelemetryStore

    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        vendor_timeout_seconds=0.05,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()
    # A 50 ms configured timeout abandoned a fetch that blocks for 1 s.
    assert connector.calls == 1
    status = poller.status()
    assert status.last_cycle_outcome == "degraded"
    assert status.last_error == "1 vendor poll failed."


# -------------------------------------------------------------------------- #
# Step 3: cycle-deadline enforcement tests
# -------------------------------------------------------------------------- #


def test_cycle_completes_within_deadline():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()
    tel = NetworkTelemetry(
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

    class _QuickConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://quick.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=1.0):
                raise TimeoutError("simulated fetch did not release in time")
            return tel

    connector = _QuickConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=10.0,
        vendor_timeout_seconds=10.0,
    )
    try:
        release.set()
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    status = poller.status()
    assert status.last_cycle_outcome == "success"
    assert status.last_error is None
    assert status.last_cycle_finished_at is not None
    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None


def test_cycle_exceeds_deadline_is_reported():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    status = poller.status()
    assert status.last_cycle_outcome == "error"
    assert status.last_error == "Poll cycle timed out."
    assert status.last_cycle_finished_at is not None
    # No telemetry was written: the cycle was truncated before completion.
    assert len(store) == 0


def test_cycle_timeout_logged_without_sensitive_values(caplog):
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://secret-host.example")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
    )
    try:
        with caplog.at_level("WARNING"):
            asyncio.run(poller.poll_once())
    finally:
        release.set()

    assert "Poll cycle timed out" in caplog.text
    assert "secret-host.example" not in caplog.text


def test_poller_operational_after_timed_out_cycle():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    slow_release = threading.Event()
    tel = NetworkTelemetry(
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

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not slow_release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return tel

    slow = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": slow},
        enabled=True,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        slow_release.set()

    # First cycle timed out; status reflects the error.
    assert poller.status().last_cycle_outcome == "error"

    # A second cycle with a fast connector must still run and succeed.
    fast = _FakeConnector(host="https://fast.device", telemetry=tel)
    poller.add_connector("arista", fast)
    asyncio.run(poller.poll_once())

    status = poller.status()
    assert status.last_cycle_outcome == "success"
    reading = store.get("arista", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None


def test_vendor_timeout_still_functions_independently():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()
    tel = NetworkTelemetry(
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

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://blocked.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return tel

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=10.0,
        vendor_timeout_seconds=0.05,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    status = poller.status()
    assert status.last_cycle_outcome == "degraded"
    assert status.last_error == "1 vendor poll failed."
    assert len(store) == 0


def test_slow_vendor_cannot_exceed_cycle_deadline():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    status = poller.status()
    assert status.last_cycle_outcome == "error"
    assert len(store) == 0


def test_no_overlapping_cycles():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()
    concurrent = 0
    max_concurrent = 0

    class _GuardedConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://guarded.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            nonlocal concurrent, max_concurrent
            self.calls += 1
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            try:
                if not release.wait(timeout=2.0):
                    raise TimeoutError("simulated fetch did not release in time")
            finally:
                concurrent -= 1
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

    connector = _GuardedConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=2.0,
        vendor_timeout_seconds=2.0,
    )

    # Run cycles one at a time: they must not overlap.
    try:
        release.set()
        asyncio.run(poller.poll_once())
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    assert max_concurrent == 1
    assert connector.calls == 2
    assert poller.status().last_cycle_outcome == "success"


def test_no_late_telemetry_after_timed_out_cycle():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    # The late worker result must not be written after the cycle timed out.
    assert len(store) == 0
    assert poller.status().last_cycle_outcome == "error"
    assert poller.status().last_cycle_finished_at is not None


def test_existing_success_and_failure_behavior_unchanged():
    store = TelemetryStore()
    tel = NetworkTelemetry(
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
    connector = _FakeConnector(telemetry=tel)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=45.0,
    )
    asyncio.run(poller.poll_once())
    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None
    assert poller.status().last_cycle_outcome == "success"


def test_configured_cycle_timeout_actually_used():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry_store import TelemetryStore

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
    )
    try:
        asyncio.run(poller.poll_once())
    finally:
        release.set()

    # A 0.05 s configured cycle timeout must truncate a 2 s fetch.
    assert poller.status().last_cycle_outcome == "error"
    assert poller.status().last_error == "Poll cycle timed out."


# -------------------------------------------------------------------------- #
# Step 4: graceful shutdown enforcement tests
# -------------------------------------------------------------------------- #


async def _run_poller_cycle(poller):
    """Helper to run poll_once and return its status."""
    await poller.poll_once()
    return poller.status()


def test_shutdown_before_start():
    store = TelemetryStore()
    poller = TelemetryPoller(store=store, enabled=True)
    assert not poller.is_running
    asyncio.run(poller.stop())
    assert not poller.is_running
    assert poller._shutdown_completed


def test_shutdown_while_idle():
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store, connectors={"cisco": _FakeConnector()}, enabled=True
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert not poller.is_running
        assert poller._task is None
        assert poller._shutdown_completed

    asyncio.run(run())


def test_shutdown_during_successful_cycle():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    fetch_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            fetch_started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        # Wait until a cycle has started and the vendor fetch is blocked.
        assert await _wait_for_flag(lambda: fetch_started.is_set())
        await poller.stop()
        assert not poller.is_running
        assert poller._task is None
        assert poller._shutdown_completed

    asyncio.run(run())
    try:
        release.set()
    finally:
        pass


def test_shutdown_during_slow_vendor_operation():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    fetch_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            fetch_started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.2,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: fetch_started.is_set())
        await poller.stop()
        assert not poller.is_running
        assert poller._task is None
        assert poller._shutdown_completed

    asyncio.run(run())
    try:
        release.set()
    finally:
        pass


def test_shutdown_during_cycle_timeout():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    release = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://blocked.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _BlockingConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=0.05,
        vendor_timeout_seconds=2.0,
        shutdown_timeout_seconds=1.0,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        # Let one cycle time out.
        for _ in range(200):
            if poller.status().last_cycle_outcome == "error":
                break
            await asyncio.sleep(0.01)
        assert poller.status().last_cycle_outcome == "error"
        await poller.stop()
        assert not poller.is_running
        assert poller._task is None

    asyncio.run(run())
    try:
        release.set()
    finally:
        pass


def test_shutdown_deadline_respected():
    import threading
    import time

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    fetch_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            fetch_started.set()
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.1,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: fetch_started.is_set())
        start = time.monotonic()
        await poller.stop()
        elapsed = time.monotonic() - start
        assert not poller.is_running
        assert poller._task is None
        return elapsed

    elapsed = asyncio.run(run())

    # Shutdown must return within roughly the deadline (plus small overhead),
    # not block indefinitely on the worker thread. With a cancellable poll
    # loop the task finishes cleanly within the deadline.
    assert elapsed < 2.0
    assert poller._shutdown_completed
    assert not poller.is_running
    assert poller._task is None
    try:
        release.set()
    finally:
        pass


def test_repeated_shutdown_calls():
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store, connectors={"cisco": _FakeConnector()}, enabled=True
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert not poller.is_running
        # Second stop is idempotent.
        await poller.stop()
        assert not poller.is_running
        # Third stop.
        await poller.stop()
        assert poller._shutdown_completed

    asyncio.run(run())


def test_no_new_cycles_after_shutdown_begins():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    fetch_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            fetch_started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: fetch_started.is_set())
        calls_before = connector.calls
        await poller.stop()
        assert not poller.is_running
        return calls_before

    calls_before = asyncio.run(run())

    # After shutdown, the connector must not be polled again.
    assert connector.calls == calls_before
    try:
        release.set()
    finally:
        pass


def test_injected_connector_worker_does_not_block_shutdown():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    fetch_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            fetch_started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.2,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: fetch_started.is_set())
        await poller.stop()
        assert poller._shutdown_completed

    asyncio.run(run())
    # No close method exists in this step; the connector is intentionally not
    # closed while the worker may still use it. Verify shutdown still stopped
    # the loop cleanly.
    assert not poller.is_running
    try:
        release.set()
    finally:
        pass


def test_no_unobserved_task_exceptions():
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store, connectors={"cisco": _FakeConnector()}, enabled=True
    )

    async def run() -> None:
        await poller.start()
        await poller.stop()
        assert poller._task is None

    asyncio.run(run())
    # No exception should be pending on the task.


def test_configured_shutdown_timeout_actually_used():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.close_calls = 0
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        shutdown_timeout_seconds=0.05,
    )

    async def run() -> None:
        await poller.start()
        await poller.stop()
        assert poller._task is None
        assert poller._shutdown_completed

    asyncio.run(run())
    # The configured 0.05s shutdown timeout was used (returned quickly).
    assert not poller.is_running
    try:
        release.set()
    finally:
        pass


def test_existing_polling_behavior_unchanged():
    store = TelemetryStore()
    tel = NetworkTelemetry(
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
    connector = _FakeConnector(telemetry=tel)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=45.0,
        shutdown_timeout_seconds=5.0,
    )
    asyncio.run(poller.poll_once())
    reading = store.get("cisco", "router-01", "GigabitEthernet0/1")
    assert reading is not None
    assert reading.error is None
    assert poller.status().last_cycle_outcome == "success"


# -------------------------------------------------------------------------- #
# Step 4.1 shutdown correctness tests
# -------------------------------------------------------------------------- #


class _UncancellablePoller(TelemetryPoller):
    """Poller whose loop ignores task cancellation (for bounded-shutdown tests).

    The loop suppresses ``asyncio.CancelledError`` so ``stop()`` cannot
    cancel it. A separate ``_stop_loop`` event is provided only so the test
    can let the loop exit cleanly for ``asyncio.run()`` teardown -- it is not
    used by ``stop()``.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import threading

        self._stop_loop = threading.Event()

    async def _run_loop(self) -> None:
        while not self._stop_loop.is_set():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Cancellation suppressed: keep looping.
                continue


def test_shutdown_with_cancellation_suppressing_poll_loop():
    """stop() must return within the deadline even if cancellation is
    suppressed by the poll loop, and must record incomplete shutdown."""

    import time

    store = TelemetryStore()
    poller = _UncancellablePoller(
        store=store,
        enabled=True,
        shutdown_timeout_seconds=0.1,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        # Let the loop task actually start running (reach its first await)
        # so that cancelling it tests a live, suppressing loop rather than a
        # not-yet-started task.
        await asyncio.sleep(0.05)
        assert poller.is_running
        start = time.monotonic()
        await poller.stop()
        elapsed = time.monotonic() - start
        # stop() returns within the deadline, does not block indefinitely.
        assert elapsed < 2.0
        # Shutdown finished (completed/abandoned) but the abandoned task is
        # still running, so is_running() stays truthful and no duplicate
        # loop can be started.
        assert poller._shutdown_completed
        assert poller._shutdown_abandoned_workers
        assert poller.is_running
        # Restart is a no-op while the abandoned loop task is still alive.
        await poller.start()
        assert poller.is_running
        # Let the loop exit cleanly for asyncio.run() teardown.
        poller._stop_loop.set()

    asyncio.run(run())


def test_worker_remains_active_after_coroutine_cancellation():
    """When a coroutine is abandoned (timeout/cancel), the worker thread
    keeps running and the vendor must remain marked active in
    _active_vendors."""

    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    thread_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            thread_started.set()
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
    )

    async def run() -> None:
        # Submit the tracked fetch directly; the worker thread starts and
        # signals thread_started.
        task = asyncio.create_task(
            asyncio.to_thread(poller._fetch_tracked, "cisco", connector)
        )
        assert await _wait_for_flag(lambda: thread_started.is_set())
        # The worker thread is running: the vendor is marked active.
        assert "cisco" in poller._active_vendors
        # Abandon the coroutine (simulate a timeout cancelling the await).
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Coroutine cancelled, but the worker thread is still running: the
        # vendor remains marked active.
        assert "cisco" in poller._active_vendors
        # Let the worker thread finish; tracking must clear on actual thread
        # completion, not coroutine cancellation.
        release.set()
        assert await _wait_for_flag(
            lambda: "cisco" not in poller._active_vendors
        )
        return task

    abandoned_task = asyncio.run(run())
    # The abandoned task object is done (cancelled); no unobserved exception.
    assert abandoned_task.done()


def test_concurrent_stop_calls():
    """Concurrent stop() calls are bounded and only one performs shutdown."""

    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    fetch_started = threading.Event()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            fetch_started.set()
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: fetch_started.is_set())
        # Two concurrent stop() calls: both return bounded.
        results = await asyncio.gather(poller.stop(), poller.stop())
        assert all(r is None for r in results)
        assert poller._shutdown_completed
        assert not poller.is_running

    asyncio.run(run())
    try:
        release.set()
    finally:
        pass


def test_restart_after_shutdown():
    """After a clean shutdown, start() creates exactly one new loop task
    and polling works."""

    store = TelemetryStore()
    connector = _FakeConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        shutdown_timeout_seconds=1.0,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        first_task = poller._task
        await poller.stop()
        assert not poller.is_running
        assert poller._task is None
        # Restart creates a fresh task (not the old one).
        await poller.start()
        assert poller.is_running
        assert poller._task is not first_task
        assert poller._task.done() is False
        await poller.stop()
        assert not poller.is_running

    asyncio.run(run())


def test_no_late_telemetry_write_after_shutdown():
    """A worker whose cycle is abandoned at shutdown must not write to the
    store after the thread eventually completes."""

    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor
    from app.telemetry import NetworkTelemetry

    release = threading.Event()
    tel = NetworkTelemetry(
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

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return tel

    connector = _SlowConnector()
    store = TelemetryStore()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.1,
    )

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        # Shutdown while the fetch is blocked; shutdown completes bounded by
        # the deadline.
        await poller.stop()
        assert poller._shutdown_completed
        assert not poller.is_running

    asyncio.run(run())

    # The worker thread is still blocked; release it. Even though the fetch
    # eventually succeeds, its result must NOT be written to the store
    # because the cycle was abandoned at shutdown.
    release.set()

    # Give the worker thread a moment to complete.
    import time as _time

    _time.sleep(0.5)
    # No telemetry write may happen after the cycle was abandoned.
    assert len(store) == 0
    try:
        release.set()
    finally:
        pass


# -------------------------------------------------------------------------- #
# Step 4.1 active-worker tracking overlap tests
# -------------------------------------------------------------------------- #


def test_overlapping_workers_same_vendor_remain_active():
    """A vendor stays active while any worker thread for it is running,
    even after an earlier worker's coroutine was abandoned."""

    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    vendor = "cisco"
    poller = TelemetryPoller(store=TelemetryStore())

    start_a = threading.Event()
    release_a = threading.Event()
    start_b = threading.Event()
    release_b = threading.Event()

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self, started: threading.Event, release: threading.Event) -> None:
            self._device = SimpleNamespace(host="https://device")
            self._started = started
            self._release = release

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self._started.set()
            if not self._release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    conn_a = _BlockingConnector(start_a, release_a)
    conn_b = _BlockingConnector(start_b, release_b)

    async def run() -> None:
        # Worker A starts.
        task_a = asyncio.create_task(
            asyncio.to_thread(poller._fetch_tracked, vendor, conn_a)
        )
        assert await _wait_for_flag(lambda: start_a.is_set())
        assert poller._active_vendors == {vendor: 1}

        # Worker B starts while A is still running (overlapping).
        task_b = asyncio.create_task(
            asyncio.to_thread(poller._fetch_tracked, vendor, conn_b)
        )
        assert await _wait_for_flag(lambda: start_b.is_set())
        assert poller._active_vendors == {vendor: 2}

        # Worker A finishes while B is still running: vendor must stay active.
        release_a.set()
        await task_a
        assert poller._active_vendors == {vendor: 1}

        # Worker B finishes: vendor is no longer active.
        release_b.set()
        await task_b
        assert poller._active_vendors == {}

    asyncio.run(run())


def test_overlapping_workers_counter_returns_to_empty():
    """After all overlapping workers finish, no vendor remains tracked."""

    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    poller = TelemetryPoller(store=TelemetryStore())
    vendor = "cisco"
    releases: list[threading.Event] = []
    started_events: list[threading.Event] = []
    connectors: list[_BlockingConnector] = []

    class _BlockingConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self, started: threading.Event, release: threading.Event) -> None:
            self._device = SimpleNamespace(host="https://device")
            self._started = started
            self._release = release

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self._started.set()
            if not self._release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

    for _ in range(3):
        started = threading.Event()
        release = threading.Event()
        connectors.append(_BlockingConnector(started, release))
        started_events.append(started)
        releases.append(release)

    async def run() -> None:
        # Start all three workers inside the running event loop.
        tasks = [
            asyncio.create_task(asyncio.to_thread(poller._fetch_tracked, vendor, conn))
            for conn in connectors
        ]
        # Wait for all three workers to start.
        for started in started_events:
            assert await _wait_for_flag(lambda: started.is_set())
        assert poller._active_vendors == {vendor: 3}

        # Finish the first two while the third is still running.
        releases[0].set()
        await tasks[0]
        assert poller._active_vendors == {vendor: 2}

        releases[1].set()
        await tasks[1]
        assert poller._active_vendors == {vendor: 1}

        # Finish the last: nothing tracked.
        releases[2].set()
        await tasks[2]
        assert poller._active_vendors == {}

    asyncio.run(run())


# -------------------------------------------------------------------------- #
# Step 6: connector ownership and graceful cleanup
# -------------------------------------------------------------------------- #


class _OwnedConnector(SandboxConnector):
    """Connector that records close() calls."""

    vendor = Vendor.CISCO

    def __init__(self, host="https://owned.device") -> None:
        self._device = SimpleNamespace(host=host)
        self.calls = 0
        self.close_calls = 0

    @property
    def host(self) -> str:
        return self._device.host

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used")

    def fetch_telemetry(self, interface=None):
        self.calls += 1
        return None

    def close(self) -> None:
        self.close_calls += 1


def _owned_poller_with_connector(store, *, shutdown_timeout=1.0):
    """Build a poller that owns a blocking connector via from_config semantics.

    The poller owns the connector (as from_config() would mark it), so it is
    responsible for closing it on shutdown.
    """

    connector = _OwnedConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        shutdown_timeout_seconds=shutdown_timeout,
    )
    # Mark as owned, mimicking from_config() construction.
    poller._owns_connectors["cisco"] = True
    return poller, connector


def test_poller_closes_owned_connector_on_shutdown():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    store = TelemetryStore()
    poller, connector = _owned_poller_with_connector(store)

    async def run():
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert not poller.is_running
        # Give the deferred drain task a chance to run.
        await asyncio.sleep(0)
        # Owned, idle connector closed exactly once.
        assert connector.close_calls == 1

    asyncio.run(run())


def test_poller_does_not_close_injected_connector():
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    store = TelemetryStore()
    connector = _OwnedConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        shutdown_timeout_seconds=1.0,
    )
    # connectros= constructor marks connectors caller-owned.
    assert poller._owns_connectors.get("cisco") is False

    async def run():
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert not poller.is_running
        # Give the deferred drain task a chance to run.
        await asyncio.sleep(0)
        # Injected connector never closed by the poller.
        assert connector.close_calls == 0

    asyncio.run(run())


def test_owned_connector_not_closed_while_worker_active():
    import threading

    store = TelemetryStore()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.close_calls = 0
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

        def close(self) -> None:
            self.close_calls += 1

    connector = _SlowConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        # Let one cycle start and block on the vendor fetch.
        assert await _wait_for_flag(lambda: connector.calls == 1)
        await poller.stop()
        assert poller._shutdown_completed
        # Worker still active: connector must NOT be closed yet.
        assert connector.close_calls == 0
        # Release the worker; the deferred drainer closes the connector.
        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        assert poller._active_vendors == {}

    asyncio.run(run())


def test_connector_closed_after_abandoned_worker_finishes():
    import threading

    store = TelemetryStore()
    release = threading.Event()

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.close_calls = 0
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return None

        def close(self) -> None:
            self.close_calls += 1

    connector = _SlowConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=0.05,  # truncate quickly
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.05,  # abandon quickly
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: connector.calls == 1)
        # Cycle truncates quickly, abandoning the worker.
        await poller.stop()
        assert poller._shutdown_completed
        assert poller._shutdown_abandoned_workers
        # Worker still active: connector not closed yet.
        assert connector.close_calls == 0
        # Release the worker; deferred drain closes the connector.
        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        assert poller._active_vendors == {}

    asyncio.run(run())


def test_repeated_stop_closes_connector_once():
    store = TelemetryStore()
    poller, connector = _owned_poller_with_connector(store)

    async def run():
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert not poller.is_running
        # Give the deferred drain task a chance to run.
        await asyncio.sleep(0)
        # Repeated stop must not close again.
        await poller.stop()
        await poller.stop()
        assert connector.close_calls == 1

    asyncio.run(run())


def test_restart_after_shutdown_does_not_reuse_closed_connector():
    import threading

    store = TelemetryStore()
    release = threading.Event()
    created = []

    class _SlowConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://slow.device")
            self.close_calls = 0
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            if not release.wait(timeout=5.0):
                raise TimeoutError("simulated fetch did not release in time")
            return NetworkTelemetry(
                device_id="restart-device",
                interface="GigabitEthernet0/1",
                timestamp="2026-01-01T00:00:00+00:00",
                cpu_percent=10.0,
                memory_percent=20.0,
                bandwidth_mbps=None,
                packet_loss_percent=None,
                latency_ms=None,
                interface_status="up",
            )

        def close(self) -> None:
            self.close_calls += 1

    def factory():
        connector = _SlowConnector()
        created.append(connector)
        return connector

    connector = factory()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.05,
    )
    poller._owns_connectors["cisco"] = True
    poller._connector_factories["cisco"] = factory

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        assert await _wait_for_flag(lambda: connector.calls == 1)
        await poller.stop()
        assert poller._shutdown_completed
        assert poller._shutdown_abandoned_workers
        # Shutdown abandoned the worker; connector not yet closed.
        assert connector.close_calls == 0
        # Release the worker; deferred drain closes the connector.
        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        assert poller._active_vendors == {}

        # A genuine restart installs a fresh connector: the closed one is
        # never handed back to the poll loop, and polling succeeds.
        await poller.start()
        restarted = poller.connectors["cisco"]
        assert restarted is not connector
        assert restarted.close_calls == 0
        await poller.poll_once()
        assert restarted.calls >= 1
        assert poller._last_cycle_outcome == "success"
        assert poller._last_error is None
        await poller.stop()
        assert restarted.close_calls == 1
        assert connector.close_calls == 1

    asyncio.run(run())


def test_cleanup_exception_contained_and_sanitized(caplog):
    import threading

    from app.connectors.base import SandboxConnector
    from app.inventory import Vendor

    store = TelemetryStore()
    release = threading.Event()

    class _FailingCloseConnector(SandboxConnector):
        vendor = Vendor.CISCO

        def __init__(self) -> None:
            self._device = SimpleNamespace(host="https://fail.device")
            self.close_calls = 0
            self.calls = 0

        @property
        def host(self) -> str:
            return self._device.host

        def connectivity_check(self) -> bool:
            return True

        def fetch_inventory(self, interface=None):
            raise NotImplementedError("not used")

        def fetch_telemetry(self, interface=None):
            self.calls += 1
            return None

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("connector close S3cr3t-boom")

    connector = _FailingCloseConnector()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        shutdown_timeout_seconds=1.0,
    )
    poller._owns_connectors["cisco"] = True

    asyncio.run(poller.start())
    asyncio.run(poller.stop())

    # Close attempted once despite the exception.
    assert connector.close_calls == 1
    # No credentials/raw exception text leaked into logs.
    for record in caplog.records:
        assert "S3cr3t" not in record.getMessage()
        assert "boom" not in record.getMessage()


# -------------------------------------------------------------------------- #
# Step 6: deadline-aware deferred cleanup and restart of owned connectors
# -------------------------------------------------------------------------- #


class _BlockingOwnedConnector(SandboxConnector):
    """Offline connector that blocks in ``fetch_telemetry`` until released."""

    vendor = Vendor.CISCO

    def __init__(self, release) -> None:
        self._device = SimpleNamespace(host="https://slow.device")
        self._release = release
        self.close_calls = 0
        self.calls = 0

    @property
    def host(self) -> str:
        return self._device.host

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used")

    def fetch_telemetry(self, interface=None):
        self.calls += 1
        if not self._release.wait(timeout=5.0):
            raise TimeoutError("simulated fetch did not release in time")
        return None

    def close(self) -> None:
        self.close_calls += 1


class _RestartableOwnedConnector(SandboxConnector):
    """Owned connector that returns valid telemetry and records close calls."""

    vendor = Vendor.CISCO

    def __init__(self, serial: int) -> None:
        self._device = SimpleNamespace(host="https://owned.device")
        self.serial = serial
        self.close_calls = 0
        self.calls = 0

    @property
    def host(self) -> str:
        return self._device.host

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used")

    def fetch_telemetry(self, interface=None):
        self.calls += 1
        return NetworkTelemetry(
            device_id=f"owned-{self.serial}",
            interface="GigabitEthernet0/1",
            timestamp="2026-01-01T00:00:00+00:00",
            cpu_percent=10.0,
            memory_percent=20.0,
            bandwidth_mbps=None,
            packet_loss_percent=None,
            latency_ms=None,
            interface_status="up",
        )

    def close(self) -> None:
        self.close_calls += 1


def test_connector_left_open_when_worker_outlives_drain_deadline():
    import threading
    import time

    store = TelemetryStore()
    release = threading.Event()
    connector = _BlockingOwnedConnector(release)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.05,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert await _wait_for_flag(lambda: connector.calls == 1)

        started = time.monotonic()
        await poller.stop()
        elapsed = time.monotonic() - started
        # stop() is bounded and must not wait for the blocked worker.
        assert elapsed < 1.0
        assert connector.close_calls == 0
        drain_task = poller._drain_task
        assert drain_task is not None
        # A repeated stop() must not schedule a duplicate drain task.
        await poller.stop()
        assert poller._drain_task is drain_task

        # Wait well past the drain deadline while the worker is still
        # blocked: the connector must remain open, not be closed mid-fetch.
        await asyncio.sleep(0.3)
        assert connector.close_calls == 0
        assert poller._pending_close  # kept queued for eventual cleanup

        # Let the worker finish; deferred cleanup must now close it once.
        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        await asyncio.sleep(0.2)
        assert connector.close_calls == 1
        assert poller._active_connectors == {}
        assert poller._pending_close == {}
        assert poller._drain_task is None

    asyncio.run(run())


def test_caller_owned_connector_not_closed_after_blocked_worker_finishes():
    import threading

    store = TelemetryStore()
    release = threading.Event()
    connector = _BlockingOwnedConnector(release)
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.05,
    )
    # Deliberately do not mark the connector owned: connectors= is
    # caller-owned and must never be closed by the poller.
    assert poller._owns_connectors["cisco"] is False

    async def run() -> None:
        await poller.start()
        assert await _wait_for_flag(lambda: connector.calls == 1)
        await poller.stop()
        assert connector.close_calls == 0
        release.set()
        assert await _wait_for_flag(lambda: poller._active_vendors == {})
        await asyncio.sleep(0.2)
        assert connector.close_calls == 0
        assert poller._pending_close == {}

    asyncio.run(run())


def test_owned_connector_restart_recreates_connector_and_polls_successfully():
    store = TelemetryStore()
    created = []

    def factory():
        connector = _RestartableOwnedConnector(len(created) + 1)
        created.append(connector)
        return connector

    first = factory()
    poller = TelemetryPoller(
        store=store,
        connectors={"cisco": first},
        enabled=True,
        interval_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )
    poller._owns_connectors["cisco"] = True
    poller._connector_factories["cisco"] = factory

    async def run() -> None:
        await poller.start()
        await poller.poll_once()
        assert first.calls == 1
        assert poller._last_cycle_outcome == "success"

        await poller.stop()
        assert first.close_calls == 1
        assert poller.connectors["cisco"] is first

        # Restart must install a fresh connector rather than reuse the one
        # whose HTTP client was closed by stop().
        await poller.start()
        second = poller.connectors["cisco"]
        assert second is not first
        assert second.serial == 2
        assert second.close_calls == 0

        # A subsequent polling cycle must succeed through the new connector.
        await poller.poll_once()
        assert second.calls == 1
        assert poller._last_cycle_outcome == "success"
        assert poller._last_error is None

        await poller.stop()
        assert second.close_calls == 1
        # The prior connector is never closed a second time.
        assert first.close_calls == 1

    asyncio.run(run())


def test_owned_connector_recreated_via_from_config_factory():
    """from_config() records a real factory used on restart."""

    import app.telemetry_poller as poller_module

    created = []

    def fake_make_connector(vendor, environ=None):
        connector = _RestartableOwnedConnector(len(created) + 1)
        created.append(connector)
        return connector

    original = poller_module.make_connector
    poller_module.make_connector = fake_make_connector
    try:
        store = TelemetryStore()
        poller = TelemetryPoller.from_config(
            store, enabled=True, environ={}, vendors=["cisco"]
        )
        assert poller.connectors["cisco"] is created[0]
        assert poller._owns_connectors["cisco"] is True
        assert "cisco" in poller._connector_factories

        async def run() -> None:
            await poller.start()
            await poller.poll_once()
            assert created[0].calls == 1
            await poller.stop()
            assert created[0].close_calls == 1

            await poller.start()
            assert poller.connectors["cisco"] is created[1]
            await poller.poll_once()
            assert created[1].calls == 1
            assert poller._last_cycle_outcome == "success"
            await poller.stop()
            assert created[1].close_calls == 1

        asyncio.run(run())
    finally:
        poller_module.make_connector = original


# -------------------------------------------------------------------------- #
# Step 7: connector removal and restart failure handling
# -------------------------------------------------------------------------- #


class _BlockingTelemetryConnector(SandboxConnector):
    """Owned connector that blocks, then returns valid telemetry."""

    vendor = Vendor.CISCO

    def __init__(self, release) -> None:
        self._device = SimpleNamespace(host="https://removal.device")
        self._release = release
        self.close_calls = 0
        self.calls = 0
        self.closed_during_fetch = None
        self.on_host = None

    @property
    def host(self) -> str:
        if self.on_host is not None:
            callback = self.on_host
            self.on_host = None
            callback()
        return self._device.host

    def connectivity_check(self) -> bool:
        return True

    def fetch_inventory(self, interface=None):
        raise NotImplementedError("not used")

    def fetch_telemetry(self, interface=None):
        self.calls += 1
        if not self._release.wait(timeout=5.0):
            raise TimeoutError("simulated fetch did not release in time")
        self.closed_during_fetch = self.close_calls
        return NetworkTelemetry(
            device_id="removal-device",
            interface="GigabitEthernet0/1",
            timestamp="2026-01-01T00:00:00+00:00",
            cpu_percent=10.0,
            memory_percent=20.0,
            bandwidth_mbps=None,
            packet_loss_percent=None,
            latency_ms=None,
            interface_status="up",
        )

    def close(self) -> None:
        self.close_calls += 1


def test_remove_connector_detaches_caller_owned_without_closing():
    connector = _RestartableOwnedConnector(1)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
    )

    poller.remove_connector("cisco")

    assert poller.connectors == {}
    assert "cisco" not in poller._owns_connectors
    assert connector.close_calls == 0
    assert poller._pending_close == {}


def test_remove_connector_closes_idle_owned_connector():
    connector = _RestartableOwnedConnector(1)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
    )
    poller._owns_connectors["cisco"] = True

    poller.remove_connector("cisco")

    assert poller.connectors == {}
    assert "cisco" not in poller._owns_connectors
    assert connector.close_calls == 1
    assert poller._pending_close == {}


def test_remove_connector_defers_owned_connector_with_active_worker():
    import threading

    release = threading.Event()
    connector = _BlockingTelemetryConnector(release)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert await _wait_for_flag(lambda: connector.calls == 1)

        poller.remove_connector("cisco")
        # Detached from the active poll set, but still owned until the
        # in-flight worker finishes.
        assert "cisco" not in poller.connectors
        assert connector.close_calls == 0
        assert poller._pending_close[id(connector)] == ("cisco", connector)

        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        assert poller._pending_close == {}
        assert poller._active_vendors == {}

        # The removed connector is never polled again.
        calls = connector.calls
        await poller.poll_once()
        assert connector.calls == calls

        await poller.stop()

    asyncio.run(run())


def test_restart_factory_failure_detaches_closed_connector(caplog):
    import app.telemetry_poller as poller_module

    created = []
    fail_restart = {"value": False}

    def fake_make_connector(vendor, environ=None):
        if fail_restart["value"]:
            raise RuntimeError("factory S3cr3t failure")
        connector = _RestartableOwnedConnector(len(created) + 1)
        created.append(connector)
        return connector

    original = poller_module.make_connector
    poller_module.make_connector = fake_make_connector
    try:
        poller = TelemetryPoller.from_config(
            TelemetryStore(),
            enabled=True,
            environ={},
            vendors=["cisco"],
        )
        first = poller.connectors["cisco"]
        fail_restart["value"] = True

        async def run() -> None:
            await poller.start()
            await poller.stop()
            assert first.close_calls == 1

            # Restart must not raise for a per-vendor factory failure, but the
            # closed connector must be gone and the vendor must be reported as
            # unavailable instead of being polled.
            await poller.start()
            assert "cisco" not in poller.connectors
            assert poller.unconfigured_vendors == {"cisco"}
            assert poller.status().unconfigured_vendors == ("cisco",)

            await poller.poll_once()
            assert first.calls == 0
            assert first.close_calls == 1

            await poller.stop()

        asyncio.run(run())
    finally:
        poller_module.make_connector = original

    assert not any("S3cr3t" in record.getMessage() for record in caplog.records)


def test_restart_one_failing_factory_keeps_healthy_and_caller_owned_vendors():
    import app.telemetry_poller as poller_module

    created = []
    fail_restart = {"value": False}

    def fake_make_connector(vendor, environ=None):
        if fail_restart["value"] and vendor is Vendor.CISCO:
            raise RuntimeError("cisco factory unavailable")
        connector = _RestartableOwnedConnector(len(created) + 1)
        created.append((vendor.value, connector))
        return connector

    original = poller_module.make_connector
    poller_module.make_connector = fake_make_connector
    try:
        poller = TelemetryPoller.from_config(
            TelemetryStore(),
            enabled=True,
            environ={},
            vendors=["cisco", "arista"],
        )
        cisco_first = poller.connectors["cisco"]
        arista_first = poller.connectors["arista"]
        caller_connector = _RestartableOwnedConnector(99)
        poller.add_connector("juniper", caller_connector)
        fail_restart["value"] = True

        async def run() -> None:
            await poller.start()
            await poller.stop()
            assert cisco_first.close_calls == 1
            assert arista_first.close_calls == 1
            assert caller_connector.close_calls == 0

            await poller.start()
            assert "cisco" not in poller.connectors
            assert "arista" in poller.connectors
            assert poller.connectors["arista"] is not arista_first
            assert poller.connectors["juniper"] is caller_connector
            assert poller.unconfigured_vendors == {"cisco"}

            await poller.poll_once()
            assert poller.connectors["arista"].calls == 1
            assert caller_connector.calls == 1
            assert cisco_first.calls == 0
            assert cisco_first.close_calls == 1

            await poller.stop()
            assert poller.connectors["arista"].close_calls == 1
            assert caller_connector.close_calls == 0

        asyncio.run(run())
    finally:
        poller_module.make_connector = original


def test_restart_without_factory_for_owned_connector_raises_and_detaches():
    connector = _RestartableOwnedConnector(1)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert poller.is_running
        await poller.stop()
        assert connector.close_calls == 1
        assert poller._shutdown_completed

        with pytest.raises(RuntimeError, match="no recreation factory"):
            await poller.start()

        assert not poller.is_running
        assert "cisco" not in poller.connectors
        assert poller.unconfigured_vendors == {"cisco"}

        # Neither a direct cycle nor a repeated stop may touch the closed
        # connector again.
        await poller.poll_once()
        assert connector.calls == 0
        assert connector.close_calls == 1
        await poller.stop()
        assert connector.close_calls == 1

    asyncio.run(run())


def test_repeated_stop_does_not_close_pending_connector_twice():
    import threading

    release = threading.Event()
    connector = _BlockingTelemetryConnector(release)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.05,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert await _wait_for_flag(lambda: connector.calls == 1)

        await poller.stop()
        assert connector.close_calls == 0
        assert poller._pending_close

        # Force the stop scan to run again (a restart would normally clear
        # this flag) to prove an already-pending connector is not closed
        # inline while its worker is still active.
        poller._shutdown_completed = False
        await poller.stop()
        assert connector.close_calls == 0
        assert poller._pending_close

        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        await asyncio.sleep(0.2)
        assert connector.close_calls == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Milestone 6 – worker-registration and exactly-once close regression tests
# ---------------------------------------------------------------------------


def test_remove_during_identity_to_worker_gap_never_closes_before_fetch():
    """Regression: connector must not be closed between identity check and
    worker execution.  The worker is reserved synchronously in _poll_vendor
    so remove_connector() sees it as active and defers."""
    import threading

    release = threading.Event()
    connector = _BlockingTelemetryConnector(release)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )
    poller._owns_connectors["cisco"] = True

    # Schedule remove_connector from within host() so it runs after the
    # identity check but before the worker thread actually starts.
    def schedule_remove() -> None:
        poller.remove_connector("cisco")

    connector.on_host = schedule_remove

    async def run() -> None:
        await poller.start()
        assert await _wait_for_flag(lambda: connector.calls == 1)

        # The connector was reserved when remove ran, so it was not closed
        # before the worker fetched.
        assert connector.close_calls == 0
        assert poller._pending_close.get(id(connector)) is not None

        release.set()
        # After the worker finishes, the deferred drain should close once.
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        assert connector.closed_during_fetch == 0
        await asyncio.sleep(0.2)
        assert connector.close_calls == 1

        await poller.stop()

    asyncio.run(run())


def test_shutdown_while_executor_work_submitted_but_not_started():
    """Regression: shutdown must not close a connector whose worker has been
    submitted but has not yet started; the worker must be abandoned safely."""
    import threading

    release = threading.Event()
    submitted = threading.Event()
    allow_start = threading.Event()
    connector = _RestartableOwnedConnector(1)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.1,
    )
    poller._owns_connectors["cisco"] = True

    original_to_thread = asyncio.to_thread

    async def gated_to_thread(func, /, *args, **kwargs):
        def gated():
            submitted.set()
            if not allow_start.wait(timeout=5.0):
                raise TimeoutError("gated worker not released")
            return func(*args, **kwargs)

        return await original_to_thread(gated)

    with patch_asyncio_to_thread(gated_to_thread):
        async def run() -> None:
            await poller.start()
            assert await _wait_for_flag(submitted.is_set)

            # Stop while the worker is gated (submitted but not started).
            await poller.stop()

            # The connector must not have been closed while the worker was
            # still pending.
            assert connector.close_calls == 0

            # Let the gated worker proceed; it should be abandoned safely.
            allow_start.set()
            await asyncio.sleep(0.2)

            # The connector was closed exactly once by the shutdown path.
            assert connector.close_calls == 1
            assert poller._active_connectors == {}
            assert poller._active_vendors == {}

        asyncio.run(run())


def test_remove_after_idle_shutdown_closes_exactly_once():
    """Regression: removing an owned connector after a normal idle shutdown
    must not close it a second time."""
    connector = _RestartableOwnedConnector(1)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        await poller.stop()
        assert connector.close_calls == 1

        poller.remove_connector("cisco")
        assert connector.close_calls == 1
        assert poller._pending_close == {}

    asyncio.run(run())


def test_remove_after_deferred_shutdown_closes_exactly_once():
    """Regression: removing an owned connector after deferred cleanup must
    not close it a second time."""
    import threading

    release = threading.Event()
    connector = _BlockingTelemetryConnector(release)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=0.05,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        assert await _wait_for_flag(lambda: connector.calls == 1)

        await poller.stop()
        assert connector.close_calls == 0

        release.set()
        assert await _wait_for_flag(lambda: connector.close_calls == 1)
        await asyncio.sleep(0.2)

        poller.remove_connector("cisco")
        assert connector.close_calls == 1
        await asyncio.sleep(0.2)
        assert connector.close_calls == 1

        await poller.stop()

    asyncio.run(run())


def test_repeated_stop_and_remove_no_duplicate_closes_or_leaks():
    """Regression: repeated stop and remove operations must not duplicate
    closes or leak reservations."""
    connector = _RestartableOwnedConnector(1)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
    )
    poller._owns_connectors["cisco"] = True

    async def run() -> None:
        await poller.start()
        await poller.stop()
        await poller.stop()
        poller.remove_connector("cisco")
        poller.remove_connector("cisco")
        await poller.stop()

        assert connector.close_calls == 1
        assert poller._active_connectors == {}
        assert poller._active_vendors == {}
        assert poller._pending_close == {}
        assert poller._reserved_connectors == set()

    asyncio.run(run())


def test_cancellation_and_submission_failure_release_reservations():
    """Regression: cancellation and executor-submission failure must
    eventually release reservations without leaking."""
    import threading

    # --- Part A: cancellation while worker is running ---
    release = threading.Event()
    connector = _BlockingTelemetryConnector(release)
    poller = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector},
        enabled=True,
        interval_seconds=0.01,
        cycle_timeout_seconds=60.0,
        vendor_timeout_seconds=60.0,
        shutdown_timeout_seconds=1.0,
    )
    poller._owns_connectors["cisco"] = True

    async def run_cancel() -> None:
        task = asyncio.create_task(poller.poll_once())
        assert await _wait_for_flag(lambda: connector.calls == 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Reservation stays while the worker is running.
        assert poller._active_connectors.get(id(connector), 0) >= 1
        release.set()
        assert await _wait_for_flag(
            lambda: poller._active_connectors.get(id(connector), 0) == 0
        )
        assert connector.close_calls == 0

        await poller.stop()
        assert connector.close_calls == 1

    asyncio.run(run_cancel())

    # --- Part B: executor-submission failure ---
    connector2 = _RestartableOwnedConnector(2)
    poller2 = TelemetryPoller(
        store=TelemetryStore(),
        connectors={"cisco": connector2},
        enabled=True,
    )
    poller2._owns_connectors["cisco"] = True

    def boom(*args, **kwargs):
        raise RuntimeError("executor unavailable")

    with patch_asyncio_to_thread(boom):
        async def run_fail() -> None:
            await poller2.poll_once()
            # Reservation must be released; no leaked counts.
            assert poller2._active_connectors == {}
            assert poller2._active_vendors == {}
            assert poller2._reserved_connectors == set()
            assert connector2.close_calls == 0

            await poller2.stop()
            assert connector2.close_calls == 1

        asyncio.run(run_fail())


# Need to import patch_asyncio_to_thread helper
from unittest.mock import patch as _patch


class _AsyncioToThreadPatcher:
    """Context manager that temporarily replaces asyncio.to_thread."""

    def __init__(self, replacement):
        self._replacement = replacement
        self._original = None

    def __enter__(self):
        self._original = asyncio.to_thread
        asyncio.to_thread = self._replacement
        return self

    def __exit__(self, *exc):
        asyncio.to_thread = self._original
        return False


def patch_asyncio_to_thread(replacement):
    return _AsyncioToThreadPatcher(replacement)
