from app.telemetry import NetworkTelemetry


def ingest_telemetry(telemetry: NetworkTelemetry) -> NetworkTelemetry:
    return telemetry