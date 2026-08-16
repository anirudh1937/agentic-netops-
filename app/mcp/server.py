from mcp.server import MCPServer

from app.mcp.network_tools import get_device_health
from app.telemetry import NetworkTelemetry


mcp = MCPServer("Agentic NetOps")


@mcp.tool()
def check_device_health(
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
    """Analyze network telemetry and return device health and detected anomalies."""

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

    return get_device_health(telemetry)


if __name__ == "__main__":
    mcp.run()