from app.diagnosis import diagnose_network_issue
from app.telemetry import NetworkTelemetry


def get_device_health(telemetry: NetworkTelemetry) -> dict:
    """Return a coarse health verdict derived from deterministic diagnosis.

    ``status`` is ``degraded`` when any anomaly is detected, ``unknown`` when
    the telemetry lacks the measurements needed to judge health, and
    ``healthy`` only when usable measurements exist and no anomaly is found.
    """

    diagnosis = diagnose_network_issue(telemetry)
    anomalies = diagnosis["anomalies"]

    if anomalies:
        status = "degraded"
    elif diagnosis["severity"] == "unknown":
        status = "unknown"
    else:
        status = "healthy"

    return {
        "device_id": telemetry.device_id,
        "interface": telemetry.interface,
        "status": status,
        "anomalies": anomalies,
    }
