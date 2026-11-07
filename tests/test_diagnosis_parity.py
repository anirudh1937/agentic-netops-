"""Cross-path diagnosis parity tests (Milestone 7.2).

The deterministic rule engine, the MCP health tool, the MCP server tool, and
the agent diagnosis tool must agree on anomaly names, ordering, identity, and
coarse health status for identical telemetry.

All tests are offline and deterministic: no device polling, no network access,
no Gemini calls, and no Google ADK Runner is constructed.
"""

import pytest
from pydantic import ValidationError

from app.agents.tools import diagnose_network
from app.diagnosis import diagnose_network_issue
from app.mcp.network_tools import get_device_health
from app.mcp.server import check_device_health
from app.telemetry import NetworkTelemetry


BASE_TELEMETRY = {
    "device_id": "router-01",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-25T00:00:00Z",
    "cpu_percent": 40,
    "memory_percent": 60,
    "bandwidth_mbps": 500,
    "packet_loss_percent": 0.1,
    "latency_ms": 20,
    "interface_status": "up",
}


def _make_telemetry(**overrides):
    return NetworkTelemetry(**{**BASE_TELEMETRY, **overrides})


SCENARIOS = [
    ("normal", {}, [], "healthy"),
    ("high_cpu", {"cpu_percent": 95}, ["high_cpu"], "degraded"),
    ("high_memory", {"memory_percent": 91}, ["high_memory"], "degraded"),
    (
        "high_packet_loss",
        {"packet_loss_percent": 5},
        ["high_packet_loss"],
        "degraded",
    ),
    ("high_latency", {"latency_ms": 180}, ["high_latency"], "degraded"),
    ("interface_down", {"interface_status": "down"}, ["interface_down"], "degraded"),
]


@pytest.mark.parametrize(
    "name,overrides,expected_anomalies,expected_status",
    SCENARIOS,
    ids=[scenario[0] for scenario in SCENARIOS],
)
def test_diagnosis_paths_agree(
    name, overrides, expected_anomalies, expected_status
):
    telemetry = _make_telemetry(**overrides)
    arguments = telemetry.model_dump()

    deterministic = diagnose_network_issue(telemetry)
    mcp_health = get_device_health(telemetry)
    agent = diagnose_network(**arguments)
    server = check_device_health(**arguments)

    # The agent tool delegates to the deterministic engine: exact equality.
    assert agent == deterministic

    # The MCP server wrapper delegates to the MCP health tool: exact equality.
    assert server == mcp_health

    # Anomaly names and ordering match across every path.
    assert deterministic["anomalies"] == expected_anomalies
    assert mcp_health["anomalies"] == expected_anomalies
    assert agent["anomalies"] == expected_anomalies
    assert server["anomalies"] == expected_anomalies

    # Device and interface identity are preserved by every path.
    for result in (deterministic, mcp_health, agent, server):
        assert result["device_id"] == telemetry.device_id
        assert result["interface"] == telemetry.interface

    # Normal maps to healthy; medium/high map to degraded.
    if expected_status == "healthy":
        assert deterministic["severity"] == "normal"
    else:
        assert deterministic["severity"] in {"medium", "high"}
    assert mcp_health["status"] == expected_status
    assert server["status"] == expected_status


def test_sparse_telemetry_reports_unknown_across_paths():
    telemetry = _make_telemetry(
        cpu_percent=None,
        memory_percent=None,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status="up",
    )
    arguments = telemetry.model_dump()

    deterministic = diagnose_network_issue(telemetry)
    mcp_health = get_device_health(telemetry)
    server = check_device_health(**arguments)

    # No usable health measurements: deterministic and MCP both say unknown.
    assert deterministic["severity"] == "unknown"
    assert mcp_health["status"] == "unknown"
    assert server["status"] == "unknown"

    # No anomaly is invented from missing data.
    assert deterministic["anomalies"] == []
    assert mcp_health["anomalies"] == []
    assert server["anomalies"] == []

    # Identity survives the unknown verdict.
    assert mcp_health["device_id"] == telemetry.device_id
    assert mcp_health["interface"] == telemetry.interface
    assert server["device_id"] == telemetry.device_id
    assert server["interface"] == telemetry.interface


INVALID_METRICS = [
    ("cpu_percent", 150),
    ("memory_percent", 150),
    ("packet_loss_percent", 150),
    ("bandwidth_mbps", -1),
    ("latency_ms", -1),
    ("cpu_percent", "not-a-number"),
    ("interface_status", "flapping"),
]


@pytest.mark.parametrize(
    "field,value",
    INVALID_METRICS,
    ids=[f"{field}={value}" for field, value in INVALID_METRICS],
)
def test_agent_tool_rejects_invalid_telemetry(field, value):
    with pytest.raises(ValidationError):
        diagnose_network(**{**BASE_TELEMETRY, field: value})


@pytest.mark.parametrize(
    "field,value",
    INVALID_METRICS,
    ids=[f"{field}={value}" for field, value in INVALID_METRICS],
)
def test_mcp_server_rejects_invalid_telemetry(field, value):
    with pytest.raises(ValidationError):
        check_device_health(**{**BASE_TELEMETRY, field: value})
