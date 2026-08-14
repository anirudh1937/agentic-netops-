from app.telemetry import NetworkTelemetry


def detect_anomalies(telemetry: NetworkTelemetry) -> list[str]:
    anomalies: list[str] = []

    if telemetry.cpu_percent > 85:
        anomalies.append("high_cpu")

    if telemetry.memory_percent > 85:
        anomalies.append("high_memory")

    if telemetry.packet_loss_percent > 2:
        anomalies.append("high_packet_loss")

    if telemetry.latency_ms > 100:
        anomalies.append("high_latency")

    if telemetry.interface_status == "down":
        anomalies.append("interface_down")

    return anomalies