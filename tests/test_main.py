from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "message": "Agentic NetOps is running"
    }


def test_health_check():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "agentic-netops",
    }


def test_dashboard_loads():
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "Agentic NetOps Dashboard" in response.text


def test_diagnose_endpoint_returns_structured_result():
    response = client.post(
        "/diagnose",
        json={
            "device_id": "router-01",
            "interface": "GigabitEthernet0/1",
            "timestamp": "2026-08-25T00:00:00Z",
            "cpu_percent": 92,
            "memory_percent": 91,
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
    assert data["severity"] == "high"
    assert data["anomalies"] == [
        "high_cpu",
        "high_memory",
        "high_packet_loss",
        "high_latency",
    ]
    assert data["evidence"]
    assert data["recommendation"]
