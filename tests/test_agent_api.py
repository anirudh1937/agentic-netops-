import asyncio
import os
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


class _Part:
    def __init__(self, text):
        self.text = text


class _Content:
    def __init__(self, text):
        self.parts = [_Part(text)]


class _FinalEvent:
    """A minimal stand-in for an ADK final-response event."""

    def __init__(self, text):
        self.text = text

    def is_final_response(self):
        return True

    @property
    def content(self):
        return _Content(self.text)


async def _yield_events(*texts):
    """Async iterator yielding fake final-response events carrying ``texts``."""
    for text in texts:
        yield _FinalEvent(text)


def _fake_session_and_runner(events):
    session_service = Mock()
    session_service.create_session = AsyncMock(
        return_value=SimpleNamespace(id="session-01")
    )
    runner = Mock()
    runner.run_async.return_value = events
    return session_service, runner


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


def test_analyze_telemetry_returns_text_when_runner_yields_final_response():
    session_service = Mock()
    session_service.create_session = AsyncMock(
        return_value=SimpleNamespace(id="session-01")
    )
    runner = Mock()
    runner.run_async.return_value = _yield_events("CPU is high; investigate.")

    with patch(
        "app.api.agent.InMemorySessionService",
        return_value=session_service,
    ), patch("app.api.agent.Runner", return_value=runner):
        result = asyncio.run(
            analyze_telemetry(NetworkTelemetry(**TELEMETRY_PAYLOAD))
        )

    assert result == "CPU is high; investigate."


def test_analyze_telemetry_rejects_whitespace_only_response():
    session_service = Mock()
    session_service.create_session = AsyncMock(
        return_value=SimpleNamespace(id="session-01")
    )
    runner = Mock()
    runner.run_async.return_value = _yield_events("   \n\t  ")

    with patch(
        "app.api.agent.InMemorySessionService",
        return_value=session_service,
    ), patch("app.api.agent.Runner", return_value=runner):
        with pytest.raises(AgentAnalysisError):
            asyncio.run(
                analyze_telemetry(NetworkTelemetry(**TELEMETRY_PAYLOAD))
            )


def test_analyze_telemetry_propagates_runner_error():
    # A model/backend failure inside the runner must surface so the endpoint
    # can translate it into an HTTP 503 rather than returning a fabricated
    # analysis.
    session_service = Mock()
    session_service.create_session = AsyncMock(
        return_value=SimpleNamespace(id="session-01")
    )
    runner = Mock()
    runner.run_async.side_effect = RuntimeError("model backend timeout")

    with patch(
        "app.api.agent.InMemorySessionService",
        return_value=session_service,
    ), patch("app.api.agent.Runner", return_value=runner):
        with pytest.raises(RuntimeError, match="backend unavailable|timeout"):
            asyncio.run(
                analyze_telemetry(NetworkTelemetry(**TELEMETRY_PAYLOAD))
            )


def test_analyze_endpoint_associates_analysis_with_telemetry():
    device = "switch-07"
    with patch(
        "app.main.analyze_telemetry",
        new_callable=AsyncMock,
    ) as mock_analyze:
        mock_analyze.return_value = (
            f"Device {device} is saturated on CPU; "
            "review workload and resource saturation."
        )
        response = client.post(
            "/analyze",
            json={**TELEMETRY_PAYLOAD, "device_id": device},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["device_id"] == device
    assert data["interface"] == TELEMETRY_PAYLOAD["interface"]
    # The returned analysis belongs to the submitted telemetry, not a stale
    # or default payload.
    assert device in data["analysis"]
    mock_analyze.assert_awaited_once()


def test_analyze_telemetry_real_model_integration():
    """End-to-end analysis with the real model backend.

    Gated behind an explicit opt-in so this never performs a live LLM call
    during a normal run. Set both ``AGNETNETOPS_LIVE_TEST=1`` and
    ``GEMINI_API_KEY`` to enable it; otherwise it skips cleanly and the
    real model path is only exercised by the mocked unit tests above.
    """
    if (
        os.environ.get("AGNETNETOPS_LIVE_TEST") != "1"
        or not os.environ.get("GEMINI_API_KEY")
    ):
        pytest.skip(
            "Opt-in only: set AGNETNETOPS_LIVE_TEST=1 and GEMINI_API_KEY to "
            "perform a real model call."
        )

    result = asyncio.run(
        analyze_telemetry(NetworkTelemetry(**TELEMETRY_PAYLOAD))
    )

    assert isinstance(result, str)
    assert result.strip()
