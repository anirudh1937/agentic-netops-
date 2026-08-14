import pytest
from pydantic import ValidationError

from app.telemetry import NetworkTelemetry


def test_network_telemetry_model():
    telemetry = NetworkTelemetry(
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp="2026-08-15T02:00:00Z",
        cpu_percent=42.5,
        memory_percent=61.2,
        bandwidth_mbps=850.0,
        packet_loss_percent=0.2,
        latency_ms=18.5,
        interface_status="up",
    )

    assert telemetry.device_id == "router-01"
    assert telemetry.interface == "GigabitEthernet0/1"
    assert telemetry.cpu_percent == 42.5
    assert telemetry.memory_percent == 61.2
    assert telemetry.packet_loss_percent == 0.2
    assert telemetry.latency_ms == 18.5
    assert telemetry.interface_status == "up"


def test_invalid_cpu_is_rejected():
    with pytest.raises(ValidationError):
        NetworkTelemetry(
            device_id="router-01",
            interface="GigabitEthernet0/1",
            timestamp="2026-08-15T02:00:00Z",
            cpu_percent=150,
            memory_percent=61.2,
            bandwidth_mbps=850.0,
            packet_loss_percent=0.2,
            latency_ms=18.5,
            interface_status="up",
        )


def test_invalid_interface_status_is_rejected():
    with pytest.raises(ValidationError):
        NetworkTelemetry(
            device_id="router-01",
            interface="GigabitEthernet0/1",
            timestamp="2026-08-15T02:00:00Z",
            cpu_percent=42.5,
            memory_percent=61.2,
            bandwidth_mbps=850.0,
            packet_loss_percent=0.2,
            latency_ms=18.5,
            interface_status="unknown",
        )