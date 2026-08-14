from app.anomaly import detect_anomalies
from app.telemetry import NetworkTelemetry


def test_healthy_network_has_no_anomalies():
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

    assert detect_anomalies(telemetry) == []


def test_degraded_network_detects_anomalies():
    telemetry = NetworkTelemetry(
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp="2026-08-15T02:00:00Z",
        cpu_percent=95.0,
        memory_percent=91.0,
        bandwidth_mbps=950.0,
        packet_loss_percent=8.0,
        latency_ms=240.0,
        interface_status="up",
    )

    assert detect_anomalies(telemetry) == [
        "high_cpu",
        "high_memory",
        "high_packet_loss",
        "high_latency",
    ]


def test_interface_down_is_detected():
    telemetry = NetworkTelemetry(
        device_id="router-02",
        interface="GigabitEthernet0/2",
        timestamp="2026-08-15T02:00:00Z",
        cpu_percent=30.0,
        memory_percent=50.0,
        bandwidth_mbps=0.0,
        packet_loss_percent=0.0,
        latency_ms=0.0,
        interface_status="down",
    )

    assert detect_anomalies(telemetry) == ["interface_down"]