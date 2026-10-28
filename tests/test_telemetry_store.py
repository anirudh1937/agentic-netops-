"""Unit tests for the in-memory telemetry store.

All tests use in-memory objects only -- no live device, no network, no
external service is contacted.
"""

from app.telemetry_store import TelemetryReading, TelemetryStore


def _reading(**overrides) -> TelemetryReading:
    data = {
        "vendor": "cisco",
        "device_id": "sandbox-iosxe-1",
        "interface": "GigabitEthernet0/2",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "fetched_at": "2026-01-01T00:00:05+00:00",
        "source": "cisco",
        "telemetry": {"cpu_percent": 92.5},
        "diagnosis": {"severity": "high", "anomalies": ["high_cpu"]},
        "error": None,
        "last_poll_outcome": "success",
        "consecutive_failures": 0,
    }
    data.update(overrides)
    return TelemetryReading(**data)


def test_empty_store_returns_none():
    store = TelemetryStore()
    assert store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2") is None
    assert store.get_all() == []
    assert len(store) == 0


def test_insert_and_retrieve():
    store = TelemetryStore()
    reading = _reading()
    store.put(
        vendor=reading.vendor,
        device_id=reading.device_id,
        interface=reading.interface,
        timestamp=reading.timestamp,
        fetched_at=reading.fetched_at,
        source=reading.source,
        telemetry=reading.telemetry,
        diagnosis=reading.diagnosis,
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.vendor == "cisco"
    assert got.device_id == "sandbox-iosxe-1"
    assert got.interface == "GigabitEthernet0/2"
    assert got.telemetry == {"cpu_percent": 92.5}
    assert got.diagnosis == {"severity": "high", "anomalies": ["high_cpu"]}
    assert got.error is None


def test_upsert_by_composite_identity_replaces_reading():
    store = TelemetryStore()
    first = _reading(fetched_at="2026-01-01T00:00:05+00:00", telemetry={"cpu_percent": 10.0})
    store.put(
        vendor=first.vendor,
        device_id=first.device_id,
        interface=first.interface,
        timestamp=first.timestamp,
        fetched_at=first.fetched_at,
        source=first.source,
        telemetry=first.telemetry,
        diagnosis=first.diagnosis,
    )

    second = _reading(
        fetched_at="2026-01-01T00:01:00+00:00",
        telemetry={"cpu_percent": 55.0},
        diagnosis={"severity": "normal", "anomalies": []},
    )
    store.put(
        vendor=second.vendor,
        device_id=second.device_id,
        interface=second.interface,
        timestamp=second.timestamp,
        fetched_at=second.fetched_at,
        source=second.source,
        telemetry=second.telemetry,
        diagnosis=second.diagnosis,
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.fetched_at == "2026-01-01T00:01:00+00:00"
    assert got.telemetry == {"cpu_percent": 55.0}
    assert got.diagnosis == {"severity": "normal", "anomalies": []}
    assert len(store) == 1


def test_same_interface_different_devices_remain_separate():
    store = TelemetryStore()
    r1 = _reading(device_id="device-a", interface="GigabitEthernet0/1")
    r2 = _reading(device_id="device-b", interface="GigabitEthernet0/1")
    store.put(
        vendor=r1.vendor,
        device_id=r1.device_id,
        interface=r1.interface,
        timestamp=r1.timestamp,
        fetched_at=r1.fetched_at,
        source=r1.source,
        telemetry=r1.telemetry,
        diagnosis=r1.diagnosis,
    )
    store.put(
        vendor=r2.vendor,
        device_id=r2.device_id,
        interface=r2.interface,
        timestamp=r2.timestamp,
        fetched_at=r2.fetched_at,
        source=r2.source,
        telemetry=r2.telemetry,
        diagnosis=r2.diagnosis,
    )

    assert store.get("cisco", "device-a", "GigabitEthernet0/1") is not None
    assert store.get("cisco", "device-b", "GigabitEthernet0/1") is not None
    assert len(store) == 2


def test_same_device_different_interfaces_remain_separate():
    store = TelemetryStore()
    r1 = _reading(interface="GigabitEthernet0/1")
    r2 = _reading(interface="GigabitEthernet0/2")
    store.put(
        vendor=r1.vendor,
        device_id=r1.device_id,
        interface=r1.interface,
        timestamp=r1.timestamp,
        fetched_at=r1.fetched_at,
        source=r1.source,
        telemetry=r1.telemetry,
        diagnosis=r1.diagnosis,
    )
    store.put(
        vendor=r2.vendor,
        device_id=r2.device_id,
        interface=r2.interface,
        timestamp=r2.timestamp,
        fetched_at=r2.fetched_at,
        source=r2.source,
        telemetry=r2.telemetry,
        diagnosis=r2.diagnosis,
    )

    assert store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/1") is not None
    assert store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2") is not None
    assert len(store) == 2


def test_get_all_returns_all_readings():
    store = TelemetryStore()
    r1 = _reading(device_id="device-a")
    r2 = _reading(device_id="device-b", interface="GigabitEthernet0/2")
    store.put(
        vendor=r1.vendor,
        device_id=r1.device_id,
        interface=r1.interface,
        timestamp=r1.timestamp,
        fetched_at=r1.fetched_at,
        source=r1.source,
        telemetry=r1.telemetry,
        diagnosis=r1.diagnosis,
    )
    store.put(
        vendor=r2.vendor,
        device_id=r2.device_id,
        interface=r2.interface,
        timestamp=r2.timestamp,
        fetched_at=r2.fetched_at,
        source=r2.source,
        telemetry=r2.telemetry,
        diagnosis=r2.diagnosis,
    )

    all_readings = store.get_all()
    assert len(all_readings) == 2
    device_ids = {r.device_id for r in all_readings}
    assert device_ids == {"device-a", "device-b"}


def test_clear_removes_all_readings():
    store = TelemetryStore()
    reading = _reading()
    store.put(
        vendor=reading.vendor,
        device_id=reading.device_id,
        interface=reading.interface,
        timestamp=reading.timestamp,
        fetched_at=reading.fetched_at,
        source=reading.source,
        telemetry=reading.telemetry,
        diagnosis=reading.diagnosis,
    )
    assert len(store) == 1

    store.clear()
    assert len(store) == 0
    assert store.get_all() == []
    assert store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2") is None


def test_telemetry_and_diagnosis_schema_preserved():
    store = TelemetryStore()
    telemetry = {
        "device_id": "sandbox-iosxe-1",
        "interface": "GigabitEthernet0/2",
        "cpu_percent": 92.5,
        "memory_percent": None,
        "packet_loss_percent": None,
        "latency_ms": None,
        "bandwidth_mbps": None,
        "interface_status": "down",
    }
    diagnosis = {
        "device_id": "sandbox-iosxe-1",
        "interface": "GigabitEthernet0/2",
        "severity": "high",
        "diagnosis": "Network health degradation detected.",
        "anomalies": ["high_cpu", "interface_down"],
        "evidence": ["CPU utilization is 92.5%"],
        "recommendation": "Check link status.",
    }
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        source="cisco",
        telemetry=telemetry,
        diagnosis=diagnosis,
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.telemetry == telemetry
    assert got.diagnosis == diagnosis
    assert got.telemetry["cpu_percent"] == 92.5
    assert got.diagnosis["severity"] == "high"
    assert got.diagnosis["anomalies"] == ["high_cpu", "interface_down"]


def test_error_reading_records_failure_without_clobbering_success():
    store = TelemetryStore()
    # First, a successful reading.
    success = _reading(
        fetched_at="2026-01-01T00:00:05+00:00",
        telemetry={"cpu_percent": 50.0},
        diagnosis={"severity": "normal", "anomalies": []},
    )
    store.put(
        vendor=success.vendor,
        device_id=success.device_id,
        interface=success.interface,
        timestamp=success.timestamp,
        fetched_at=success.fetched_at,
        source=success.source,
        telemetry=success.telemetry,
        diagnosis=success.diagnosis,
    )

    # Then a failed poll: error set, no new telemetry/diagnosis payloads.
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:01:00+00:00",
        source="cisco",
        error="Could not reach device at 'sandbox.local'.",
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.error == "Could not reach device at 'sandbox.local'."
    # Last successful telemetry preserved; not overwritten by the failure.
    assert got.telemetry == {"cpu_percent": 50.0}
    assert got.diagnosis == {"severity": "normal", "anomalies": []}
    # fetched_at reflects the latest (failed) poll attempt.
    assert got.fetched_at == "2026-01-01T00:01:00+00:00"


def test_first_poll_failed_keeps_empty_telemetry_and_diagnosis():
    store = TelemetryStore()
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        source="cisco",
        error="Cisco device rejected authentication.",
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.error == "Cisco device rejected authentication."
    assert got.telemetry == {}
    assert got.diagnosis == {}
    assert got.fetched_at == "2026-01-01T00:00:05+00:00"


def test_failed_poll_preserves_last_success_timestamps():
    """A failed poll must not make old measurements appear freshly collected.

    The last successful device timestamp and server-side collection timestamp
    are preserved, while the latest attempt time and failure outcome are
    tracked separately.
    """
    store = TelemetryStore()
    # First, a successful reading.
    success = _reading(
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        telemetry={"cpu_percent": 50.0},
        diagnosis={"severity": "normal", "anomalies": []},
    )
    store.put(
        vendor=success.vendor,
        device_id=success.device_id,
        interface=success.interface,
        timestamp=success.timestamp,
        fetched_at=success.fetched_at,
        source=success.source,
        telemetry=success.telemetry,
        diagnosis=success.diagnosis,
    )

    # Then a failed poll at a later time.
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:01:00+00:00",
        source="cisco",
        error="Could not reach device at 'sandbox.local'.",
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.error == "Could not reach device at 'sandbox.local'."
    assert got.last_poll_outcome == "failure"
    assert got.consecutive_failures == 1
    # Latest attempt time updated.
    assert got.fetched_at == "2026-01-01T00:01:00+00:00"
    assert got.last_poll_attempt_at == "2026-01-01T00:01:00+00:00"
    # Last-success timestamps preserved, not clobbered by the failure.
    assert got.last_successful_timestamp == "2026-01-01T00:00:00+00:00"
    assert got.last_successful_fetched_at == "2026-01-01T00:00:05+00:00"
    # Last-success telemetry/diagnosis preserved.
    assert got.telemetry == {"cpu_percent": 50.0}
    assert got.diagnosis == {"severity": "normal", "anomalies": []}


def test_successful_recovery_clears_error_and_updates_success_timestamps():
    """After a failure, a successful poll clears the error and updates success timestamps."""
    store = TelemetryStore()
    # First, a successful reading.
    success = _reading(
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        telemetry={"cpu_percent": 50.0},
        diagnosis={"severity": "normal", "anomalies": []},
    )
    store.put(
        vendor=success.vendor,
        device_id=success.device_id,
        interface=success.interface,
        timestamp=success.timestamp,
        fetched_at=success.fetched_at,
        source=success.source,
        telemetry=success.telemetry,
        diagnosis=success.diagnosis,
    )

    # Then a failed poll.
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:01:00+00:00",
        source="cisco",
        error="Could not reach device at 'sandbox.local'.",
    )

    # Then a successful recovery at a later time.
    recovery = _reading(
        timestamp="2026-01-01T00:02:00+00:00",
        fetched_at="2026-01-01T00:02:05+00:00",
        telemetry={"cpu_percent": 55.0},
        diagnosis={"severity": "medium", "anomalies": ["high_cpu"]},
    )
    store.put(
        vendor=recovery.vendor,
        device_id=recovery.device_id,
        interface=recovery.interface,
        timestamp=recovery.timestamp,
        fetched_at=recovery.fetched_at,
        source=recovery.source,
        telemetry=recovery.telemetry,
        diagnosis=recovery.diagnosis,
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.error is None
    assert got.last_poll_outcome == "success"
    assert got.consecutive_failures == 0
    # Success timestamps updated to the recovery.
    assert got.last_successful_timestamp == "2026-01-01T00:02:00+00:00"
    assert got.last_successful_fetched_at == "2026-01-01T00:02:05+00:00"
    # Latest telemetry/diagnosis from the recovery.
    assert got.telemetry == {"cpu_percent": 55.0}
    assert got.diagnosis == {"severity": "medium", "anomalies": ["high_cpu"]}


def test_consecutive_failures_increment_on_repeated_failure():
    store = TelemetryStore()
    # First poll fails.
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        source="cisco",
        error="First failure.",
    )
    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got.consecutive_failures == 1

    # Second poll fails.
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:01:00+00:00",
        source="cisco",
        error="Second failure.",
    )
    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got.consecutive_failures == 2

    # Third poll succeeds: count resets.
    success = _reading(
        timestamp="2026-01-01T00:02:00+00:00",
        fetched_at="2026-01-01T00:02:05+00:00",
        telemetry={"cpu_percent": 30.0},
        diagnosis={"severity": "normal", "anomalies": []},
    )
    store.put(
        vendor=success.vendor,
        device_id=success.device_id,
        interface=success.interface,
        timestamp=success.timestamp,
        fetched_at=success.fetched_at,
        source=success.source,
        telemetry=success.telemetry,
        diagnosis=success.diagnosis,
    )
    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got.consecutive_failures == 0
    assert got.error is None


def test_first_poll_failure_has_empty_success_timestamps():
    store = TelemetryStore()
    store.put(
        vendor="cisco",
        device_id="sandbox-iosxe-1",
        interface="GigabitEthernet0/2",
        timestamp="2026-01-01T00:00:00+00:00",
        fetched_at="2026-01-01T00:00:05+00:00",
        source="cisco",
        error="Cisco device rejected authentication.",
    )

    got = store.get("cisco", "sandbox-iosxe-1", "GigabitEthernet0/2")
    assert got is not None
    assert got.error == "Cisco device rejected authentication."
    assert got.last_successful_timestamp == ""
    assert got.last_successful_fetched_at == ""
    assert got.last_poll_outcome == "failure"
    assert got.consecutive_failures == 1


def test_reading_is_pydantic_model_with_expected_fields():
    reading = _reading()
    assert reading.vendor == "cisco"
    assert reading.device_id == "sandbox-iosxe-1"
    assert reading.interface == "GigabitEthernet0/2"
    assert reading.source == "cisco"
    assert reading.error is None
    # Serialization round-trip includes new freshness fields.
    dumped = reading.model_dump()
    assert dumped["vendor"] == "cisco"
    assert dumped["error"] is None
    assert dumped["last_poll_outcome"] == "success"
    assert dumped["consecutive_failures"] == 0
