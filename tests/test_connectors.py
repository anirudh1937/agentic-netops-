"""Tests for the multi-vendor sandbox connector abstraction.

The connectors turn each vendor's device payload (Cisco IOS XE leaves or the
OpenConfig shapes Juniper/Arista expose) into the project's shared
``NetworkTelemetry`` / ``DeviceInventory`` models without fabricating metrics.
HTTP is mocked with ``httpx.MockTransport`` following the pattern in
``tests/test_cisco_adapter.py`` so the parsing logic is exercised without a
live device.
"""

import httpx
import pytest

from app.cisco import CiscoClient, CiscoConnector, CiscoDevice
from app.connectors import (
    available_vendors,
    get_connector_class,
    make_connector,
)
from app.connectors.arista import AristaClient, AristaConnector, AristaDevice
from app.connectors.base import SandboxConnectorError, UnsupportedVendorError
from app.connectors.juniper import JuniperClient, JuniperConnector, JuniperDevice
from app.inventory import Vendor


DEVICE_HOST = "https://sandbox.local"
USERNAME = "devnet"
PASSWORD = "S3cr3t-please-do-not-leak"


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------


CPU_BODY = {
    "Cisco-IOS-XE-process-cpu-oper:cpu-utilization": {
        "five-seconds": 92.5,
        "one-second": 91.0,
        "five-minute": 80.0,
        "one-minute": 78.0,
        "total": 92.5,
    }
}

MEMORY_BODY = {
    "Cisco-IOS-XE-memory-oper:memory-statistic": [
        {"total-memory": 4000000, "used-memory": 3600000}
    ]
}

INTERFACES_BODY = {
    "Cisco-IOS-XE-interfaces-oper:interfaces": {
        "interface": [
            {"name": "GigabitEthernet1", "oper-status": "if-oper-state-ready"},
            {"name": "GigabitEthernet2", "oper-status": "if-oper-state-lower-layer-down"},
        ]
    }
}

HOSTNAME_BODY = {"Cisco-IOS-XE-native:hostname": "sandbox-iosxe"}


JP_SYSTEM = {"openconfig-system:system": {"state": {"hostname": "jnpr-core-1"}}}
JP_INTERFACES = {
    "openconfig-interfaces:interfaces": {
        "interface": [
            {"config": {"name": "ge-0/0/0"}, "state": {"oper-status": "UP"}},
            {"config": {"name": "ge-0/0/1"}, "state": {"oper-status": "DOWN"}},
        ]
    }
}
JP_PLATFORM = {
    "openconfig-system:system/platform": {
        "config": {"vendor": "Juniper Networks", "model": "MX240"}
    }
}
JP_UPTIME = {
    "openconfig-system:system/uptime": {"state": {"uptime-seconds": 12345}}
}

JP_ROUTES = {
    "openconfig-interfaces": (200, JP_INTERFACES),
    "system/platform": (200, JP_PLATFORM),
    "system/uptime": (200, JP_UPTIME),
    "openconfig-system:system": (200, JP_SYSTEM),
}

AP_SYSTEM = {"openconfig-system:system": {"state": {"hostname": "sw-agg-1"}}}
AP_INTERFACES = {
    "openconfig-interfaces:interfaces": {
        "interface": [
            {"config": {"name": "Ethernet1"}, "state": {"oper-status": "UP"}},
        ]
    }
}
AP_PLATFORM = {
    "openconfig-system:system/platform": {
        "config": {"vendor": "Arista", "model": "DCS-7050QX"}
    }
}
AP_UPTIME = {
    "openconfig-system:system/uptime": {"state": {"uptime-seconds": 900}}
}

AP_ROUTES = {
    "openconfig-interfaces": (200, AP_INTERFACES),
    "system/platform": (200, AP_PLATFORM),
    "system/uptime": (200, AP_UPTIME),
    "openconfig-system:system": (200, AP_SYSTEM),
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def mock_client(routes):
    transport = httpx.MockTransport(
        lambda request: _match(routes, request.url.path)
    )
    return httpx.Client(base_url=DEVICE_HOST, transport=transport)


def _match(routes, path):
    for key, value in routes.items():
        if key in path:
            status, body = value
            if body is None:
                return httpx.Response(status, content=b"")
            return httpx.Response(status, json=body)
    return httpx.Response(404, json={"errors": [{"description": "not found"}]})


def build_connector(connector_cls, device_cls, client_cls, routes):
    api_root = getattr(device_cls, "DEFAULT_API_ROOT", "/restconf")
    device = device_cls(
        host=DEVICE_HOST,
        username=USERNAME,
        password=PASSWORD,
        api_root=api_root,
    )
    client = client_cls(device=device, client=mock_client(routes))
    return connector_cls(device=device, client=client)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_available_vendors_is_sorted():
    assert available_vendors() == ["arista", "cisco", "juniper"]


def test_get_connector_class_by_enum():
    assert get_connector_class(Vendor.JUNIPER) is JuniperConnector


def test_get_connector_class_by_string():
    assert get_connector_class("arista") is AristaConnector


def test_unknown_vendor_raises_unsupported_error():
    with pytest.raises(UnsupportedVendorError):
        get_connector_class("huawei")


def test_unknown_vendor_is_sandbox_connector_error():
    with pytest.raises(SandboxConnectorError):
        get_connector_class("nec")


def test_make_connector_reads_environ(monkeypatch):
    monkeypatch.setenv("JUNIPER_SANDBOX_HOST", DEVICE_HOST)
    monkeypatch.setenv("JUNIPER_SANDBOX_USERNAME", USERNAME)
    monkeypatch.setenv("JUNIPER_SANDBOX_PASSWORD", PASSWORD)

    connector = make_connector(Vendor.JUNIPER)

    assert connector._device.host == DEVICE_HOST


def test_make_connector_rejects_unconfigured_device(monkeypatch):
    monkeypatch.delenv("JUNIPER_SANDBOX_HOST", raising=False)

    with pytest.raises(SandboxConnectorError):
        make_connector(Vendor.JUNIPER)


# --------------------------------------------------------------------------
# Cisco connector (unchanged adapter exposed through the connector API)
# --------------------------------------------------------------------------


def test_cisco_connector_parses_telemetry():
    routes = {
        "process-cpu-oper": (200, CPU_BODY),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, INTERFACES_BODY),
        "native:native": (200, HOSTNAME_BODY),
    }
    connector = build_connector(CiscoConnector, CiscoDevice, CiscoClient, routes)

    telemetry = connector.fetch_telemetry(interface="GigabitEthernet1")

    assert telemetry.cpu_percent == 92.5
    assert telemetry.interface == "GigabitEthernet1"
    assert telemetry.interface_status == "up"
    # Probe/calculation metrics are unavailable read-only -> None.
    assert telemetry.packet_loss_percent is None
    assert telemetry.latency_ms is None


def test_cisco_connector_inventory_filters_interface():
    routes = {
        "process-cpu-oper": (404, None),
        "memory-oper": (200, MEMORY_BODY),
        "interfaces-oper": (200, INTERFACES_BODY),
        "native:native": (200, HOSTNAME_BODY),
    }
    connector = build_connector(CiscoConnector, CiscoDevice, CiscoClient, routes)

    inventory = connector.fetch_inventory(interface="GigabitEthernet1")

    assert inventory.hostname == "sandbox-iosxe"
    assert inventory.vendor is Vendor.CISCO
    assert len(inventory.interfaces) == 1
    assert inventory.interfaces[0].name == "GigabitEthernet1"
    assert inventory.interfaces[0].status == "up"


# --------------------------------------------------------------------------
# Juniper connector
# --------------------------------------------------------------------------


def test_juniper_connector_inventory_full():
    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, JP_ROUTES
    )

    inventory = connector.fetch_inventory()

    assert inventory.vendor is Vendor.JUNIPER
    assert inventory.hostname == "jnpr-core-1"
    assert inventory.model == "MX240"
    assert inventory.version == "Juniper Networks"
    assert inventory.uptime_seconds == 12345
    assert [intf.name for intf in inventory.interfaces] == [
        "ge-0/0/0",
        "ge-0/0/1",
    ]
    assert inventory.interfaces[0].status == "up"
    assert inventory.interfaces[1].status == "down"


def test_juniper_connector_interface_down():
    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, JP_ROUTES
    )

    telemetry = connector.fetch_telemetry(interface="ge-0/0/1")

    assert telemetry.device_id == "jnpr-core-1"
    assert telemetry.interface == "ge-0/0/1"
    assert telemetry.interface_status == "down"
    # Juniper OpenConfig leaves no read-only CPU/packet-loss metrics -> None.
    assert telemetry.cpu_percent is None
    assert telemetry.packet_loss_percent is None


def test_juniper_connector_interface_up():
    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, JP_ROUTES
    )

    telemetry = connector.fetch_telemetry(interface="ge-0/0/0")

    assert telemetry.interface_status == "up"


def test_juniper_connector_handles_missing_fields():
    routes = {
        "openconfig-interfaces": (
            200,
            {"openconfig-interfaces:interfaces": {"interface": [
                {"config": {"name": "ge-0/0/0"}},
            ]}},
        ),
        "openconfig-system:system": (200, {"openconfig-system:system": {}}),
    }
    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, routes
    )

    inventory = connector.fetch_inventory()

    # hostname absent -> fall back to device host; leaves absent -> None.
    assert inventory.device_id == DEVICE_HOST
    assert inventory.hostname is None
    assert inventory.model is None
    assert inventory.version is None
    assert inventory.uptime_seconds is None
    assert inventory.interfaces[0].status == "unknown"


def test_juniper_connector_connectivity_check():
    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, JP_ROUTES
    )
    assert connector.connectivity_check() is True


def test_juniper_connector_connectivity_check_down():
    routes = {"openconfig-system:system": (404, None)}
    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, routes
    )
    assert connector.connectivity_check() is False


# --------------------------------------------------------------------------
# Arista connector
# --------------------------------------------------------------------------


def test_arista_connector_inventory_full():
    connector = build_connector(
        AristaConnector, AristaDevice, AristaClient, AP_ROUTES
    )

    inventory = connector.fetch_inventory()

    assert inventory.vendor is Vendor.ARISTA
    assert inventory.hostname == "sw-agg-1"
    assert inventory.model == "DCS-7050QX"
    assert inventory.uptime_seconds == 900
    assert len(inventory.interfaces) == 1
    assert inventory.interfaces[0].status == "up"


def test_arista_connector_telemetry_up():
    connector = build_connector(
        AristaConnector, AristaDevice, AristaClient, AP_ROUTES
    )

    telemetry = connector.fetch_telemetry(interface="Ethernet1")

    assert telemetry.device_id == "sw-agg-1"
    assert telemetry.interface_status == "up"


def test_arista_connector_from_env_reads_configuration(monkeypatch):
    monkeypatch.setenv("ARISTA_SANDBOX_HOST", DEVICE_HOST)
    monkeypatch.setenv("ARISTA_SANDBOX_USERNAME", USERNAME)
    monkeypatch.setenv("ARISTA_SANDBOX_PASSWORD", PASSWORD)

    connector = make_connector(Vendor.ARISTA)

    assert connector._device.host == DEVICE_HOST
    assert connector.vendor is Vendor.ARISTA


# --------------------------------------------------------------------------
# Route integration
# --------------------------------------------------------------------------


def test_vendors_list_route():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/vendors")

    assert response.status_code == 200
    assert response.json()["vendors"] == ["arista", "cisco", "juniper"]


def test_vendored_inventory_route(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, JP_ROUTES
    )
    monkeypatch.setattr("app.main.make_connector", lambda vendor: connector)

    client = TestClient(app)
    response = client.get("/vendors/juniper/inventory")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "juniper"
    assert body["inventory"]["hostname"] == "jnpr-core-1"
    assert body["inventory"]["model"] == "MX240"
    assert [i["name"] for i in body["inventory"]["interfaces"]] == [
        "ge-0/0/0",
        "ge-0/0/1",
    ]


def test_vendored_telemetry_route_reports_interface_down(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    connector = build_connector(
        JuniperConnector, JuniperDevice, JuniperClient, JP_ROUTES
    )
    monkeypatch.setattr("app.main.make_connector", lambda vendor: connector)

    client = TestClient(app)
    response = client.get(
        "/vendors/juniper/telemetry", params={"interface": "ge-0/0/1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "juniper"
    assert "interface_down" in body["anomalies"]
    assert body["severity"] == "high"
    assert "interface" in body["telemetry"]


def test_unknown_vendor_route_returns_404():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.get("/vendors/huawei/inventory")

    assert response.status_code == 404
    assert "Unsupported vendor" in response.json()["detail"]