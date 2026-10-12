from typing import Literal

from pydantic import BaseModel

from app.telemetry import NetworkTelemetry
from app.anomaly import detect_anomalies


class DiagnosisResult(BaseModel):
    device_id: str
    interface: str
    severity: Literal["normal", "medium", "high"]
    diagnosis: str
    anomalies: list[str]
    evidence: list[str]
    recommendation: str


def diagnose_network_issue(telemetry: NetworkTelemetry) -> dict:
    anomalies = detect_anomalies(telemetry)

    if not anomalies:
        return DiagnosisResult(
            device_id=telemetry.device_id,
            interface=telemetry.interface,
            severity="normal",
            diagnosis="No significant network anomaly detected.",
            anomalies=[],
            evidence=[],
            recommendation="Continue monitoring.",
        ).model_dump()

    evidence = []
    recommendations = []

    if "high_cpu" in anomalies:
        evidence.append(
            f"CPU utilization is {telemetry.cpu_percent}%."
        )
        recommendations.append(
            "Inspect CPU-intensive processes and device resource utilization."
        )

    if "high_memory" in anomalies:
        evidence.append(
            f"Memory utilization is {telemetry.memory_percent}%."
        )
        recommendations.append(
            "Inspect memory-intensive processes and device memory utilization."
        )

    if "high_packet_loss" in anomalies:
        evidence.append(
            f"Packet loss is {telemetry.packet_loss_percent}%."
        )
        recommendations.append(
            "Inspect interface errors, congestion, and link quality."
        )

    if "high_latency" in anomalies:
        evidence.append(
            f"Latency is {telemetry.latency_ms} ms."
        )
        recommendations.append(
            "Investigate congestion, routing path, and interface performance."
        )

    if telemetry.interface_status == "down":
        evidence.append("The network interface is down.")
        recommendations.append(
            "Check interface configuration, link status, and physical connectivity."
        )

    return DiagnosisResult(
        device_id=telemetry.device_id,
        interface=telemetry.interface,
        severity="high" if len(anomalies) >= 2 else "medium",
        diagnosis="Network health degradation detected.",
        anomalies=anomalies,
        evidence=evidence,
        recommendation=" ".join(recommendations),
    ).model_dump()
