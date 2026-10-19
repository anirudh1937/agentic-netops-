"""Read-only Cisco IOS XE RESTCONF adapter.

Fetches device and interface telemetry from a Cisco DevNet sandbox (or any
RESTCONF-enabled device) and maps it into the project's ``NetworkTelemetry``
model without inventing measurements.
"""

from .adapter import (
    CiscoClient,
    CiscoDevice,
    CiscoTelemetry,
    CiscoTelemetryError,
    fetch_telemetry,
)

__all__ = [
    "CiscoClient",
    "CiscoDevice",
    "CiscoTelemetry",
    "CiscoTelemetryError",
    "fetch_telemetry",
]