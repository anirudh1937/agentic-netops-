from app.anomaly import detect_anomalies
from app.telemetry import NetworkTelemetry


def get_device_health(telemetry: NetworkTelemetry) -> dict:
    anomalies = detect_anomalies(telemetry)

    return {
        "device_id": telemetry.device_id,
        "interface": telemetry.interface,
        "status": "degraded" if anomalies else "healthy",
        "anomalies": anomalies,
    }