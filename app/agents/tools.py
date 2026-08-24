from app.diagnosis import diagnose_network_issue
from app.telemetry import NetworkTelemetry


def diagnose_network(
    device_id: str,
    interface: str,
    timestamp: str,
    cpu_percent: float,
    memory_percent: float,
    bandwidth_mbps: float,
    packet_loss_percent: float,
    latency_ms: float,
    interface_status: str,
) -> dict:
    """Diagnose network health from device telemetry."""

    telemetry = NetworkTelemetry(
        device_id=device_id,
        interface=interface,
        timestamp=timestamp,
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        bandwidth_mbps=bandwidth_mbps,
        packet_loss_percent=packet_loss_percent,
        latency_ms=latency_ms,
        interface_status=interface_status,
    )

    return diagnose_network_issue(telemetry)