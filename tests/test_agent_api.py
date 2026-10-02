import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Add project root to Python's import path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


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
            json={
                "device_id": "router-01",
                "interface": "GigabitEthernet0/1",
                "timestamp": "2026-08-25T00:00:00Z",
                "cpu_percent": 92,
                "memory_percent": 60,
                "bandwidth_mbps": 100,
                "packet_loss_percent": 5,
                "latency_ms": 150,
                "interface_status": "up",
            },
        )

    assert response.status_code == 200

    data = response.json()

    assert data["device_id"] == "router-01"
    assert data["interface"] == "GigabitEthernet0/1"
    assert data["analysis"]

    mock_analyze.assert_awaited_once()