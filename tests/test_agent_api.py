import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

# Add project root to Python's import path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient
import pytest

from app.api.agent import AgentAnalysisError, analyze_telemetry
from app.main import app
from app.telemetry import NetworkTelemetry


client = TestClient(app)


TELEMETRY_PAYLOAD = {
    "device_id": "router-01",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-25T00:00:00Z",
    "cpu_percent": 92,
    "memory_percent": 60,
    "bandwidth_mbps": 100,
    "packet_loss_percent": 5,
    "latency_ms": 150,
    "interface_status": "up",
}


def test_analyze_endpoint():
    with patch(
        "app.main.analyze_telemetry",
        new_callable=AsyncMock,
    ) as mock_analyze:

        mock_analyze.return_value = (
            "High CPU usage, elevated packet loss, "
            "and high latency detected."
        )

        response = client.post(
            "/analyze",
            json=TELEMETRY_PAYLOAD,
        )

    assert response.status_code == 200

    data = response.json()

    assert data["device_id"] == "router-01"
    assert data["interface"] == "GigabitEthernet0/1"
    assert data["status"] == "completed"
    assert data["analysis"]

    mock_analyze.assert_awaited_once()


def test_analyze_endpoint_returns_503_when_agent_is_unavailable():
    with patch(
        "app.main.analyze_telemetry",
        new_callable=AsyncMock,
    ) as mock_analyze:
        mock_analyze.side_effect = RuntimeError("model backend unavailable")

        response = client.post(
            "/analyze",
            json=TELEMETRY_PAYLOAD,
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent analysis failed."}

    mock_analyze.assert_awaited_once()


def test_analyze_telemetry_rejects_empty_agent_response():
    async def empty_events():
        if False:
            yield None

    session_service = Mock()
    session_service.create_session = AsyncMock(
        return_value=SimpleNamespace(id="session-01")
    )

    runner = Mock()
    runner.run_async.return_value = empty_events()

    with patch(
        "app.api.agent.InMemorySessionService",
        return_value=session_service,
    ), patch("app.api.agent.Runner", return_value=runner):
        with pytest.raises(AgentAnalysisError):
            asyncio.run(
                analyze_telemetry(NetworkTelemetry(**TELEMETRY_PAYLOAD))
            )
