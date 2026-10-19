from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from app.connectors.openconfig import (
    interfaces_from,
    openconfig_block,
    openconfig_leaf,
    platform_from,
    uptime_from,
)
from app.connectors.restconf import RestconfClient, RestconfDevice
from app.connectors.base import SandboxConnector, SandboxConnectorError, Vendor
from app.inventory import DeviceInventory, InterfaceInfo
from app.telemetry import NetworkTelemetry


OPENCONFIG_INTERFACES = "openconfig-interfaces:interfaces"
OPENCONFIG_SYSTEM = "openconfig-system:system"
OPENCONFIG_PLATFORM = "openconfig-system:system/platform"
OPENCONFIG_UPTIME = "openconfig-system:system/uptime"


class JuniperDevice(RestconfDevice):
    """Connection parameters for a read-only Junos RESTCONF endpoint.

    Junos exposes RESTCONF under ``/etc/restconf`` by default, unlike the more
    common ``/restconf`` root used by Cisco and Arista.
    """

    DEFAULT_API_ROOT = "/etc/restconf"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "JuniperDevice":
        if environ is None:
            load_dotenv(override=False)
            environ = dict(os.environ)

        device = cls(
            host=environ.get("JUNIPER_SANDBOX_HOST") or "",
            username=environ.get("JUNIPER_SANDBOX_USERNAME", ""),
            password=environ.get("JUNIPER_SANDBOX_PASSWORD", ""),
            api_root=environ.get("JUNIPER_SANDBOX_API_ROOT", cls.DEFAULT_API_ROOT),
            verify_tls=(
                environ.get("JUNIPER_SANDBOX_VERIFY_TLS", "false")
                .strip()
                .lower()
                == "true"
            ),
        )
        if not device.host:
            raise SandboxConnectorError(
                "Juniper device host is not configured "
                "(set JUNIPER_SANDBOX_HOST)."
            )
        return device


class JuniperClient(RestconfClient):
    """Juniper RESTCONF client (behavior identical to the shared base)."""


class JuniperConnector(SandboxConnector):
    """Read-only connector for Juniper Junos devices over RESTCONF."""

    vendor = Vendor.JUNIPER

    def __init__(
        self,
        device: JuniperDevice | None = None,
        client: JuniperClient | None = None,
    ) -> None:
        self._device = device or JuniperDevice.from_env()
        self._client = client or JuniperClient(self._device)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "JuniperConnector":
        return cls(device=JuniperDevice.from_env(environ))

    def connectivity_check(self) -> bool:
        return self._client.get_json(f"data/{OPENCONFIG_SYSTEM}") is not None

    def fetch_inventory(self, interface: str | None = None) -> DeviceInventory:
        system = self._client.get_json(f"data/{OPENCONFIG_SYSTEM}")
        hostname = openconfig_leaf(openconfig_block(system, OPENCONFIG_SYSTEM), "hostname")

        interfaces = [
            InterfaceInfo(name=name, status=status or "unknown")
            for name, status in interfaces_from(
                self._client.get_json(f"data/{OPENCONFIG_INTERFACES}"),
                OPENCONFIG_INTERFACES,
            )
        ]

        if interface is not None and interfaces and not any(
            intf.name == interface for intf in interfaces
        ):
            interfaces = [InterfaceInfo(name=interface, status="unknown")]

        vendor, model = platform_from(
            self._client.get_json(f"data/{OPENCONFIG_PLATFORM}"),
            "openconfig-system:system/platform",
        )
        uptime = uptime_from(
            self._client.get_json(f"data/{OPENCONFIG_UPTIME}"),
            "openconfig-system:system/uptime",
        )

        return DeviceInventory(
            device_id=hostname or self._device.host,
            vendor=Vendor.JUNIPER,
            hostname=hostname,
            model=model,
            version=vendor,
            uptime_seconds=uptime,
            interfaces=interfaces,
        )

    def fetch_telemetry(self, interface: str | None = None) -> NetworkTelemetry:
        system = self._client.get_json(f"data/{OPENCONFIG_SYSTEM}")
        hostname = openconfig_leaf(openconfig_block(system, OPENCONFIG_SYSTEM), "hostname")
        display = interface or self._device.host
        status = "up"

        for name, intf_status in interfaces_from(
            self._client.get_json(f"data/{OPENCONFIG_INTERFACES}"),
            OPENCONFIG_INTERFACES,
        ):
            if name == display:
                if intf_status is not None:
                    status = intf_status
                break
            display = name
            status = intf_status if intf_status is not None else "up"

        return NetworkTelemetry(
            device_id=hostname or self._device.host,
            interface=display,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            cpu_percent=None,
            memory_percent=None,
            bandwidth_mbps=None,
            packet_loss_percent=None,
            latency_ms=None,
            interface_status=status,
        )