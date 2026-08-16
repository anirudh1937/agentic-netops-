from app.mcp.network_tools import get_device_health
from app.telemetry import NetworkTelemetry


def test_healthy_device():
    telemetry = NetworkTelemetry(
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp="2026-08-16T20:00:00Z",
        cpu_percent=40,
        memory_percent=60,
        bandwidth_mbps=500,
        packet_loss_percent=0.1,
        latency_ms=20,
        interface_status="up",
    )

    result = get_device_health(telemetry)

    assert result["device_id"] == "router-01"
    assert result["status"] == "healthy"
    assert result["anomalies"] == []


def test_degraded_device():
    telemetry = NetworkTelemetry(
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp="2026-08-16T20:00:00Z",
        cpu_percent=95,
        memory_percent=70,
        bandwidth_mbps=900,
        packet_loss_percent=5,
        latency_ms=180,
        interface_status="up",
    )

    result = get_device_health(telemetry)

    assert result["device_id"] == "router-01"
    assert result["status"] == "degraded"
    assert result["anomalies"] == [
        "high_cpu",
        "high_packet_loss",
        "high_latency",
    ]


def test_interface_down():
    telemetry = NetworkTelemetry(
        device_id="router-02",
        interface="GigabitEthernet0/2",
        timestamp="2026-08-16T20:00:00Z",
        cpu_percent=30,
        memory_percent=50,
        bandwidth_mbps=0,
        packet_loss_percent=0,
        latency_ms=0,
        interface_status="down",
    )

    result = get_device_health(telemetry)

    assert result["status"] == "degraded"
    assert result["anomalies"] == ["interface_down"]