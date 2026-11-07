"""MCP execution tests that inspect the actual returned payload.

The MCP server is invoked in-process. No device, network, model, credential,
or Google ADK Runner is involved.
"""

import asyncio
import json

from app.diagnosis import diagnose_network_issue
from app.mcp.server import mcp
from app.telemetry import NetworkTelemetry


TELEMETRY = {
    "device_id": "router-01",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-16T21:00:00Z",
    "cpu_percent": 95,
    "memory_percent": 70,
    "bandwidth_mbps": 900,
    "packet_loss_percent": 5,
    "latency_ms": 180,
    "interface_status": "up",
}


def _call_tool(arguments: dict) -> dict:
    """Invoke the MCP tool and parse its TextContent JSON payload."""

    result = asyncio.run(mcp.call_tool("check_device_health", arguments))

    assert result, "MCP tool returned no content"
    text = "".join(part.text for part in result if getattr(part, "text", None))
    return json.loads(text)


def test_check_device_health_execution():
    payload = _call_tool(TELEMETRY)
    deterministic = diagnose_network_issue(NetworkTelemetry(**TELEMETRY))

    assert payload["device_id"] == TELEMETRY["device_id"]
    assert payload["interface"] == TELEMETRY["interface"]
    assert payload["status"] == "degraded"
    assert payload["anomalies"] == deterministic["anomalies"]
    assert payload["anomalies"] == [
        "high_cpu",
        "high_packet_loss",
        "high_latency",
    ]


def test_check_device_health_execution_sparse_telemetry():
    arguments = {
        **TELEMETRY,
        "cpu_percent": None,
        "memory_percent": None,
        "bandwidth_mbps": None,
        "packet_loss_percent": None,
        "latency_ms": None,
        "interface_status": "up",
    }

    payload = _call_tool(arguments)
    deterministic = diagnose_network_issue(NetworkTelemetry(**arguments))

    assert deterministic["severity"] == "unknown"
    assert payload["device_id"] == arguments["device_id"]
    assert payload["interface"] == arguments["interface"]
    assert payload["status"] == "unknown"
    assert payload["anomalies"] == []
