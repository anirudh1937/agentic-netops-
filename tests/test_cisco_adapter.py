import base64

import httpx
import pytest

from app.anomaly import detect_anomalies
from app.cisco import (
    CiscoClient,
    CiscoDevice,
    CiscoTelemetryError,
    fetch_telemetry,
)
from app.telemetry import NetworkTelemetry


DEVICE_HOST = "https://sandbox.local"
USERNAME = "devnet"
PASSWORD = "S3cr3t-please-do-not-leak"


CPU_BODY = {
    "Cisco-IOS-XE-process-cpu-oper:cpu-utilization": {
        "five-seconds": 92.5,
        "one-second": 91.0,
        "five-minute": 80.0,
        "one-minute": 78.0,
        "total": 92.5,
    }
}

# Real IOS XE returns the statistics list under the singular namespaced key.
MEMORY_BODY = {
    "Cisco-IOS-XE-memory-oper:memory-statistic": [
        {"total-memory": 4000000, "used-memory": 3600000},
    ]
}

INTERFACES_BODY = {
    "Cisco-IOS-XE-interfaces-oper:interfaces": {
        "interface": [
            {"name": "GigabitEthernet1", "oper-status": "if-oper-state-ready", "admin-status": "if-state-up"},
            {"name": "GigabitEthernet2", "oper-status": "if-oper-state-upper-layer-down", "admin-status": "if-state-up"},
        ]
    }
}

# Real IOS XE returns the leaf under the namespaced key directly.
HOSTNAME_BODY = {"Cisco-IOS-XE-native:hostname": "sandbox-iosxe"}


def make_device(**overrides) -> CiscoDevice:
    params = {
        "host": DEVICE_HOST,
        "username": USERNAME,
        "password": PASSWORD,
        "api_root": "/restconf",
    }
    params.update(overrides)
    return CiscoDevice(**params)


def make_client(routes, **device_kwargs) -> tuple[CiscoClient, CiscoDevice]:
    """Build a CiscoClient backed by a MockTransport.

    ``routes`` maps a path substring to a ``(status_code, body)`` tuple;
    ``body`` may be ``None`` to return an empty payload for that status.
    """

    device = make_device(**device_kwargs)

    def responder(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for key, value in routes.items():
            if key in path:
                status, body = value
                if body is None:
                    return httpx.Response(status, content=b"")
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"errors": [{"description": "not found"}]})

    transport = httpx.MockTransport(responder)
    http_client = httpx.Client(base_url=DEVICE_HOST, transport=transport)
    return CiscoClient(device, client=http_client), device


# --------------------------------------------------------------------------
# Successful parsing
# --------------------------------------------------------------------------


def test_fetch_telemetry_parses_full_response():
    routes = {
        "process-cpu-oper": (200, CPU_BODY),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, INTERFACES_BODY),
        "native:native": (200, HOSTNAME_BODY),
    }
    client, device = make_client(routes)

    telemetry = fetch_telemetry(device=device, client=client, interface="GigabitEthernet1")

    assert telemetry.device_id == "sandbox-iosxe"
    assert telemetry.interface == "GigabitEthernet1"
    assert telemetry.cpu_percent == 92.5
    assert telemetry.memory_percent == 90.0
    assert telemetry.interface_status == "up"
    # Probe/calculation metrics are unavailable read-only -> None, never 0.
    assert telemetry.packet_loss_percent is None
    assert telemetry.latency_ms is None
    assert telemetry.bandwidth_mbps is None


def test_interface_down_is_reported():
    interfaces = {
        "Cisco-IOS-XE-interfaces-oper:interfaces": {
            "interface": [
                {"name": "GigabitEthernet1", "oper-status": "down", "admin-status": "up"},
            ]
        }
    }
    routes = {
        "process-cpu-oper": (200, CPU_BODY),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, interfaces),
        "native:native": (200, HOSTNAME_BODY),
    }
    client, device = make_client(routes)

    telemetry = fetch_telemetry(device=device, client=client, interface="GigabitEthernet1")

    assert telemetry.interface_status == "down"


# --------------------------------------------------------------------------
# Missing fields (endpoints present but leaves absent)
# --------------------------------------------------------------------------


def test_fetch_telemetry_handles_missing_fields():
    routes = {
        "process-cpu-oper": (
            200,
            {"Cisco-IOS-XE-process-cpu-oper:cpu-utilization": {}},
        ),
        "memory-oper": (
            200,
            {"Cisco-IOS-XE-memory-oper:memory-statistic": [{}]},
        ),
        "interfaces-oper": (
            200,
            {
                "Cisco-IOS-XE-interfaces-oper:interfaces": {
                    "interface": [{"name": "GigabitEthernet1"}]
                }
            },
        ),
        "native:native": (200, {"Cisco-IOS-XE-native:hostname": {}}),
    }
    client, device = make_client(routes)

    telemetry = fetch_telemetry(device=device, client=client, interface="GigabitEthernet1")

    assert telemetry.cpu_percent is None
    assert telemetry.memory_percent is None
    assert telemetry.interface_status == "up"  # unknown -> not falsely flagged
    # hostname absent -> fall back to device host
    assert telemetry.device_id == DEVICE_HOST


def test_unsupported_endpoint_yields_none_not_error():
    routes = {
        "process-cpu-oper": (404, None),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, INTERFACES_BODY),
        "native:native": (200, HOSTNAME_BODY),
    }
    client, device = make_client(routes)

    telemetry = fetch_telemetry(device=device, client=client, interface="GigabitEthernet1")

    assert telemetry.cpu_percent is None
    assert telemetry.device_id == "sandbox-iosxe"


# --------------------------------------------------------------------------
# Authentication configuration
# --------------------------------------------------------------------------


def test_basic_auth_header_is_sent():
    recorded = {}

    def responder(request: httpx.Request) -> httpx.Response:
        recorded["authorization"] = request.headers.get("authorization")
        recorded.setdefault("paths", []).append(request.url.path)
        path = request.url.path
        if "process-cpu-oper" in path:
            return httpx.Response(200, json=CPU_BODY)
        if "memory-oper" in path:
            return httpx.Response(200, json=MEMORY_BODY)
        if "interfaces-oper" in path:
            return httpx.Response(200, json=INTERFACES_BODY)
        if "native:native" in path:
            return httpx.Response(200, json=HOSTNAME_BODY)
        return httpx.Response(404, json={"errors": []})

    transport = httpx.MockTransport(responder)
    # Production wiring sets Basic auth from device credentials.
    http_client = httpx.Client(
        base_url=DEVICE_HOST,
        auth=(USERNAME, PASSWORD),
        transport=transport,
    )
    device = make_device()
    client = CiscoClient(device, client=http_client)

    fetch_telemetry(device=device, client=client, interface="GigabitEthernet1")

    assert recorded["authorization"] is not None
    assert recorded["authorization"].startswith("Basic ")
    decoded = base64.b64decode(recorded["authorization"].split(" ", 1)[1]).decode()
    assert decoded == f"{USERNAME}:{PASSWORD}"


def test_credentials_appear_in_url_path_only_never():
    recorded = {}

    def responder(request: httpx.Request) -> httpx.Response:
        recorded["path"] = request.url.path
        return httpx.Response(404, json={"errors": []})

    transport = httpx.MockTransport(responder)
    http_client = httpx.Client(base_url=DEVICE_HOST, transport=transport)
    device = make_device()
    client = CiscoClient(device, client=http_client)

    client.get_json("data/Cisco-IOS-XE-native:native/hostname")

    assert PASSWORD not in recorded["path"]


def test_missing_credentials_raise():
    device = make_device(password="")
    with pytest.raises(CiscoTelemetryError) as exc:
        fetch_telemetry(device=device)
    assert "credentials" in str(exc.value).lower()


# --------------------------------------------------------------------------
# HTTP errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_is_credential_safe(status):
    device = make_device()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, text="unauthorized")
    )
    client = CiscoClient(device, client=httpx.Client(base_url=DEVICE_HOST, transport=transport))

    with pytest.raises(CiscoTelemetryError) as exc:
        client.get_json("data/Cisco-IOS-XE-native:native/hostname")

    message = str(exc.value).lower()
    assert "authentication" in message
    assert PASSWORD not in str(exc.value)


def test_http_500_is_credential_safe():
    device = make_device()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, text="internal error")
    )
    client = CiscoClient(device, client=httpx.Client(base_url=DEVICE_HOST, transport=transport))

    with pytest.raises(CiscoTelemetryError) as exc:
        client.get_json("data/Cisco-IOS-XE-native:native/hostname")

    assert "500" in str(exc.value)
    assert PASSWORD not in str(exc.value)


def test_connection_error_is_credential_safe():
    device = make_device()

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(responder)
    client = CiscoClient(device, client=httpx.Client(base_url=DEVICE_HOST, transport=transport))

    with pytest.raises(CiscoTelemetryError) as exc:
        client.get_json("data/Cisco-IOS-XE-native:native/hostname")

    assert "reach" in str(exc.value).lower()
    assert PASSWORD not in str(exc.value)


def test_invalid_json_is_credential_safe():
    device = make_device()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="not-json {")
    )
    client = CiscoClient(device, client=httpx.Client(base_url=DEVICE_HOST, transport=transport))

    with pytest.raises(CiscoTelemetryError) as exc:
        client.get_json("data/Cisco-IOS-XE-native:native/hostname")

    assert "invalid json" in str(exc.value).lower()
    assert PASSWORD not in str(exc.value)


# --------------------------------------------------------------------------
# Environment configuration
# --------------------------------------------------------------------------


def test_from_env_reads_configuration(monkeypatch):
    monkeypatch.setenv("CISCO_SANDBOX_HOST", DEVICE_HOST)
    monkeypatch.setenv("CISCO_SANDBOX_USERNAME", USERNAME)
    monkeypatch.setenv("CISCO_SANDBOX_PASSWORD", PASSWORD)

    device = CiscoDevice.from_env()

    assert device.host == DEVICE_HOST
    assert device.username == USERNAME
    assert device.password == PASSWORD
    assert device.api_root == "/restconf"


def test_from_env_verify_tls_flag(monkeypatch):
    monkeypatch.setenv("CISCO_SANDBOX_HOST", DEVICE_HOST)
    monkeypatch.setenv("CISCO_SANDBOX_VERIFY_TLS", "true")

    device = CiscoDevice.from_env()

    assert device.verify_tls is True


def test_from_env_rejects_empty_host():
    with pytest.raises(CiscoTelemetryError):
        CiscoDevice.from_env(environ={})


# --------------------------------------------------------------------------
# Route integration (additive /cisco/telemetry)
# --------------------------------------------------------------------------


def test_cisco_telemetry_route_runs_diagnosis(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    fake = NetworkTelemetry(
        device_id="sandbox-iosxe",
        interface="GigabitEthernet1",
        timestamp="2026-08-16T21:00:00+00:00",
        cpu_percent=95.0,
        memory_percent=70.0,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status="down",
    )

    monkeypatch.setattr("app.main.fetch_telemetry", lambda interface=None: fake)

    client = TestClient(app)
    response = client.get("/cisco/telemetry")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "cisco"
    assert body["device_id"] == "sandbox-iosxe"
    assert body["severity"] == "high"  # interface_down is critical
    assert "interface_down" in body["anomalies"]
    assert body["telemetry"]["packet_loss_percent"] is None


def test_cisco_telemetry_route_returns_503_on_failure(monkeypatch):
    from fastapi.testclient import TestClient

    from app.cisco import CiscoTelemetryError
    from app.main import app

    def boom(interface=None):
        raise CiscoTelemetryError("unreachable")

    monkeypatch.setattr("app.main.fetch_telemetry", boom)

    client = TestClient(app)
    response = client.get("/cisco/telemetry")

    assert response.status_code == 503


# --------------------------------------------------------------------------
# IOS XE oper-status enum mapping (regression: "ready" must not be "down")
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("if-oper-state-ready", "up"),
        ("if-oper-state-up", "up"),
        ("up", "up"),
        ("if-oper-state-upper-layer-down", "down"),
        ("if-oper-state-lower-layer-down", "down"),
        ("if-oper-state-no-pass", "down"),
        ("if-oper-state-down", "down"),
        (None, None),
    ],
)
def test_cisco_oper_status_mapping(raw, expected):
    from app.cisco.adapter import _cisco_oper_status

    assert _cisco_oper_status(raw) == expected


def test_fetch_ready_interface_is_not_flagged_down():
    routes = {
        "process-cpu-oper": (200, CPU_BODY),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, INTERFACES_BODY),
        "native:native": (200, HOSTNAME_BODY),
    }
    client, device = make_client(routes)

    telemetry = fetch_telemetry(device=device, client=client, interface="GigabitEthernet1")

    # GigabitEthernet1 reports "if-oper-state-ready" -> must be "up", not "down".
    assert telemetry.interface_status == "up"
    assert "interface_down" not in detect_anomalies(telemetry)


def test_detect_anomalies_helper_available():
    from app.anomaly import detect_anomalies

    from app.telemetry import NetworkTelemetry

    healthy = NetworkTelemetry(
        device_id="sandbox-iosxe",
        interface="GigabitEthernet1",
        timestamp="2026-01-01T00:00:00+00:00",
        cpu_percent=10.0,
        memory_percent=20.0,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status="up",
    )
    assert detect_anomalies(healthy) == []