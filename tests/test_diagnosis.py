from app.diagnosis import CRITICAL_ANOMALIES, diagnose_network_issue
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


def test_high_cpu_explains_threshold_and_recommendation():
    telemetry = make_telemetry(cpu_percent=95)

    result = diagnose_network_issue(telemetry)

    # A single non-critical anomaly stays at medium severity.
    assert result["severity"] == "medium"
    assert "high_cpu" in result["anomalies"]
    assert (
        "CPU utilization is 95.0%, 10.0 percentage points above the "
        "85.0% threshold." in result["evidence"]
    )
    assert "Inspect CPU-intensive processes" in result["recommendation"]
    assert "workload changes" in result["recommendation"]
    assert "resource saturation" in result["recommendation"]


def test_high_memory_explains_threshold_and_recommendation():
    telemetry = make_telemetry(memory_percent=91)

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "medium"
    assert "high_memory" in result["anomalies"]
    assert (
        "Memory utilization is 91.0%, 6.0 percentage points above the "
        "85.0% threshold." in result["evidence"]
    )
    assert "Inspect memory-intensive processes" in result["recommendation"]
    assert "memory pressure" in result["recommendation"]
    assert "memory leaks" in result["recommendation"]


def test_packet_loss_explains_threshold_and_units():
    telemetry = make_telemetry(packet_loss_percent=5)

    result = diagnose_network_issue(telemetry)

    assert "high_packet_loss" in result["anomalies"]
    assert (
        "Packet loss is 5.0%, 3.0 percentage points above the 2.0% threshold."
        in result["evidence"]
    )
    assert "Inspect interface errors" in result["recommendation"]
    assert "link quality" in result["recommendation"]


def test_high_latency_explains_threshold_with_ms_units():
    telemetry = make_telemetry(latency_ms=180)

    result = diagnose_network_issue(telemetry)

    assert "high_latency" in result["anomalies"]
    assert (
        "Latency is 180.0 ms, 80.0 ms above the 100.0 ms threshold."
        in result["evidence"]
    )
    assert "routing path" in result["recommendation"]
    assert "upstream delays" in result["recommendation"]


def test_interface_down_alone_is_high_severity():
    # A single genuinely critical anomaly must reach high severity.
    telemetry = make_telemetry(interface_status="down")

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "high"
    assert "interface_down" in result["anomalies"]
    assert "The network interface is down." in result["evidence"]
    assert "administrative state" in result["recommendation"]
    assert "the remote device" in result["recommendation"]


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
    # Every anomaly produces an evidence line.
    assert len(result["evidence"]) == 3


def test_two_anomalies_produce_high_severity():
    telemetry = make_telemetry(
        cpu_percent=95,
        memory_percent=91,
    )

    result = diagnose_network_issue(telemetry)

    assert result["severity"] == "high"
    assert len(result["anomalies"]) == 2


def test_no_recommendations_invent_root_cause():
    # Evidence reports observations + thresholds; recommendations are possible
    # actions, not asserted outcomes. Nothing states a guaranteed cause/result.
    telemetry = make_telemetry(
        cpu_percent=95,
        interface_status="down",
    )

    result = diagnose_network_issue(telemetry)

    evidence = " ".join(result["evidence"])
    recommendation = result["recommendation"]

    assert "is down" in evidence
    for forbidden in ("caused by", "fixed", "resolve", "confirmed", "exhausted"):
        assert forbidden not in evidence.lower()
        assert forbidden not in recommendation.lower()


def test_critical_anomaly_set_matches_intended_signal():
    assert CRITICAL_ANOMALIES == {"interface_down"}
