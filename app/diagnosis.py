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


# Thresholds mirror detect_anomalies() in app.anomaly. They are collected here so
# the evidence text reports the exact limits the detector enforces, rather than
# duplicating limits in multiple places where they could drift out of sync.
CPU_THRESHOLD = 85.0
MEMORY_THRESHOLD = 85.0
PACKET_LOSS_THRESHOLD = 2.0
LATENCY_THRESHOLD = 100.0

# Anomalies whose presence alone is severe enough to warrant "high" severity.
CRITICAL_ANOMALIES = {"interface_down"}


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
            f"CPU utilization is {telemetry.cpu_percent:.1f}%, "
            f"{telemetry.cpu_percent - CPU_THRESHOLD:.1f} percentage points "
            f"above the {CPU_THRESHOLD:.1f}% threshold."
        )
        recommendations.append(
            "Inspect CPU-intensive processes, recent workload changes, and "
            "resource saturation on this device."
        )

    if "high_memory" in anomalies:
        evidence.append(
            f"Memory utilization is {telemetry.memory_percent:.1f}%, "
            f"{telemetry.memory_percent - MEMORY_THRESHOLD:.1f} percentage points "
            f"above the {MEMORY_THRESHOLD:.1f}% threshold."
        )
        recommendations.append(
            "Inspect memory-intensive processes, memory pressure, and possible "
            "memory leaks."
        )

    if "high_packet_loss" in anomalies:
        evidence.append(
            f"Packet loss is {telemetry.packet_loss_percent:.1f}%, "
            f"{telemetry.packet_loss_percent - PACKET_LOSS_THRESHOLD:.1f} "
            f"percentage points above the {PACKET_LOSS_THRESHOLD:.1f}% threshold."
        )
        recommendations.append(
            "Inspect interface errors, congestion, and link quality on this interface."
        )

    if "high_latency" in anomalies:
        evidence.append(
            f"Latency is {telemetry.latency_ms:.1f} ms, "
            f"{telemetry.latency_ms - LATENCY_THRESHOLD:.1f} ms above the "
            f"{LATENCY_THRESHOLD:.1f} ms threshold."
        )
        recommendations.append(
            "Investigate congestion, routing path, and upstream delays."
        )

    if telemetry.interface_status == "down":
        evidence.append("The network interface is down.")
        recommendations.append(
            "Check link status, physical connectivity, administrative state, "
            "and the remote device."
        )

    severity = "high" if (
        len(anomalies) >= 2 or any(a in CRITICAL_ANOMALIES for a in anomalies)
    ) else "medium"

    return DiagnosisResult(
        device_id=telemetry.device_id,
        interface=telemetry.interface,
        severity=severity,
        diagnosis="Network health degradation detected.",
        anomalies=anomalies,
        evidence=evidence,
        recommendation=" ".join(recommendations),
    ).model_dump()
