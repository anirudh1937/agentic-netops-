from app.agents.tools import diagnose_network


def test_diagnose_network_tool_detects_multiple_anomalies():
    result = diagnose_network(
        device_id="router-01",
        interface="GigabitEthernet0/1",
        timestamp="2026-08-25T00:00:00Z",
        cpu_percent=92,
        memory_percent=60,
        bandwidth_mbps=100,
        packet_loss_percent=5,
        latency_ms=150,
        interface_status="up",
    )

    assert result["device_id"] == "router-01"
    assert result["interface"] == "GigabitEthernet0/1"
    assert result["severity"] == "high"

    assert "high_cpu" in result["anomalies"]
    assert "high_packet_loss" in result["anomalies"]
    assert "high_latency" in result["anomalies"]

    assert len(result["evidence"]) == 3
    assert result["recommendation"]