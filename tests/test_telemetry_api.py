from app.api.telemetry import ingest_telemetry
from app.telemetry import NetworkTelemetry


def test_ingest_telemetry():
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

    result = ingest_telemetry(telemetry)

    assert result == telemetry