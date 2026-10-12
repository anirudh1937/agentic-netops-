from app.anomaly import detect_anomalies
from app.diagnosis import diagnose_network_issue
from app.telemetry import NetworkTelemetry


def make_telemetry(**overrides):
    data = {
        "device_id": "router-01",
        "interface": "GigabitEthernet0/1",
        "timestamp": "2026-08-16T21:00:00Z",
        "cpu_percent": 40,
        "memory_percent": 60,
        "bandwidth_mbps": 500,
        "packet_loss_percent": 0.1,
        "latency_ms": 20,
        "interface_status": "up",
    }

    data.update(overrides)
    return NetworkTelemetry(**data)


def test_healthy_network():
    telemetry = make_telemetry()

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "normal"
    assert result["diagnosis"] == "No significant network anomaly detected."
    assert result["anomalies"] == []
    assert result["evidence"] == []
    assert result["recommendation"] == "Continue monitoring."


def test_high_cpu_diagnosis():
    telemetry = make_telemetry(cpu_percent=95)

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "medium"
    assert "high_cpu" in result["anomalies"]
    assert "CPU utilization is 95.0%." in result["evidence"]


def test_high_memory_diagnosis():
    telemetry = make_telemetry(memory_percent=91)

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "medium"
    assert "high_memory" in result["anomalies"]
    assert "Memory utilization is 91.0%." in result["evidence"]
    assert "Inspect memory-intensive processes" in result["recommendation"]


def test_multiple_anomalies_produce_high_severity():
    telemetry = make_telemetry(
        cpu_percent=95,
        packet_loss_percent=5,
        latency_ms=180,
    )

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "high"
    assert "high_cpu" in result["anomalies"]
    assert "high_packet_loss" in result["anomalies"]
    assert "high_latency" in result["anomalies"]


def test_interface_down_is_included_in_diagnosis():
    telemetry = make_telemetry(interface_status="down")

    result = diagnose_network_issue(telemetry)

    assert "The network interface is down." in result["evidence"]
    assert "Check interface configuration" in result["recommendation"]
