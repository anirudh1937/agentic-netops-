from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from app.connectors.base import SandboxConnector
from app.inventory import DeviceInventory, InterfaceInfo, Vendor
from app.telemetry import NetworkTelemetry


# Cisco telemetry is modeled as the project's NetworkTelemetry.
CiscoTelemetry = NetworkTelemetry


class CiscoTelemetryError(RuntimeError):
    """Raised when live device telemetry cannot be retrieved.

    The message intentionally omits credentials and any other secrets.
    """


class CiscoDevice:
    """Connection parameters for a read-only RESTCONF endpoint.

    Every value is sourced from environment variables so credentials never
    enter source control.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        api_root: str = "/restconf",
        timeout: float = 10.0,
        verify_tls: bool = False,
    ) -> None:
        if not host:
            raise CiscoTelemetryError(
                "Cisco device host is not configured "
                "(set CISCO_SANDBOX_HOST)."
            )
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout
        self.verify_tls = verify_tls

    @property
    def url(self) -> str:
        return f"{self.host}{self.api_root}"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "CiscoDevice":
        """Build a :class:`CiscoDevice` from environment variables.

        When ``environ`` is omitted the process environment is read (after
        loading ``.env``); passing ``environ`` makes the call deterministic.
        """

        if environ is None:
            load_dotenv(override=False)
            environ = dict(os.environ)

        host = environ.get("CISCO_SANDBOX_HOST") or ""
        username = environ.get("CISCO_SANDBOX_USERNAME", "")
        password = environ.get("CISCO_SANDBOX_PASSWORD", "")
        api_root = environ.get("CISCO_SANDBOX_API_ROOT", "/restconf")
        verify_tls = (
            environ.get("CISCO_SANDBOX_VERIFY_TLS", "false").strip().lower() == "true"
        )

        return cls(
            host=host,
            username=username,
            password=password,
            api_root=api_root,
            verify_tls=verify_tls,
        )


class CiscoClient:
    """Minimal RESTCONF client using HTTP Basic authentication.

    Requests the YANG JSON media type and treats a 404 as "endpoint not
    supported on this IOS XE version" so callers can tolerate partial data.
    """

    def __init__(self, device: CiscoDevice, client: httpx.Client | None = None) -> None:
        self._device = device
        self._client = client

    def _http_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(
            base_url=self._device.host,
            auth=(self._device.username, self._device.password),
            timeout=self._device.timeout,
            verify=self._device.verify_tls,
        )

    def get_json(self, path: str):
        """GET a RESTCONF path and parse it as JSON.

        Returns ``None`` for a 404 (unsupported endpoint) and raises
        :class:`CiscoTelemetryError` for transport errors, non-404 HTTP errors,
        and invalid JSON. Messages never include credentials.
        """

        url = f"{self._device.api_root}/{path.lstrip('/')}"
        headers = {"Accept": "application/yang-data+json"}

        client = self._http_client()

        try:
            response = client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CiscoTelemetryError(
                f"Could not reach Cisco device at '{self._device.host}'."
            ) from exc

        if response.status_code == 404:
            return None

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in {401, 403}:
                raise CiscoTelemetryError(
                    "Cisco device rejected authentication."
                ) from exc
            raise CiscoTelemetryError(
                f"Cisco device returned HTTP status {status}."
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise CiscoTelemetryError(
                "Cisco device returned an invalid JSON response."
            ) from exc


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _get_cpu(client: CiscoClient):
    data = client.get_json(
        "data/Cisco-IOS-XE-process-cpu-oper:cpu-usage/cpu-utilization"
    )
    if not isinstance(data, dict):
        return None

    usage = data.get("Cisco-IOS-XE-process-cpu-oper:cpu-usage")
    if not isinstance(usage, dict):
        return None

    try:
        value = float(usage["five_seconds"])
    except (KeyError, TypeError, ValueError):
        return None

    return _clamp(value, 0.0, 100.0)


def _get_memory(client: CiscoClient):
    data = client.get_json(
        "data/Cisco-IOS-XE-memory-oper:memory-statistics/memory-statistic"
    )
    if not isinstance(data, dict):
        return None

    # YANG container is "memory-statistics"; the value is a "memory-statistic"
    # list whose first entry carries total/used memory.
    stats = data.get("Cisco-IOS-XE-memory-oper:memory-statistics")
    if not isinstance(stats, dict):
        return None

    statistics = stats.get("memory-statistic")
    if isinstance(statistics, list):
        statistics = statistics[0] if statistics else {}
    if not isinstance(statistics, dict):
        return None

    try:
        total = float(statistics["total-memory"])
        used = float(statistics["used-memory"])
    except (KeyError, TypeError, ValueError):
        return None

    if total <= 0:
        return None

    return _clamp((used / total) * 100.0, 0.0, 100.0)


def _get_interface(client: CiscoClient, interface: str | None):
    data = client.get_json("data/Cisco-IOS-XE-interfaces-oper:interfaces")

    candidates = []
    if isinstance(data, dict):
        interfaces = data.get("Cisco-IOS-XE-interfaces-oper:interfaces")
        if isinstance(interfaces, dict):
            candidates = interfaces.get("interface", [])
    if not isinstance(candidates, list):
        candidates = []

    target = None
    for intf in candidates:
        if not isinstance(intf, dict):
            continue
        name = intf.get("name")
        if interface and name and str(name).lower() == str(interface).lower():
            target = intf
            break
    if target is None:
        for intf in candidates:
            if isinstance(intf, dict) and "oper-status" in intf:
                target = intf
                break

    if target is None:
        return None, None

    name = target.get("name")
    status_raw = target.get("oper-status")
    # "up" stays up; a present status that is not "up" (down, not-present,
    # disabled) maps to down; an absent status means unknown -> None so the
    # caller defaults it to "up" rather than falsely flagging an outage.
    if status_raw is None:
        status = None
    elif str(status_raw).lower() == "up":
        status = "up"
    else:
        status = "down"
    return (name or None), status


def _get_all_interfaces(client: CiscoClient) -> list[tuple[str | None, str | None]]:
    """Return ``(name, status)`` for every interface the device lists.

    Status may be ``None`` when a device does not report ``oper-status`` so the
    caller can default it to ``"up"`` instead of falsely flagging an outage.
    """

    data = client.get_json("data/Cisco-IOS-XE-interfaces-oper:interfaces")

    candidates = []
    if isinstance(data, dict):
        interfaces = data.get("Cisco-IOS-XE-interfaces-oper:interfaces")
        if isinstance(interfaces, dict):
            candidates = interfaces.get("interface", [])
    if not isinstance(candidates, list):
        candidates = []

    interfaces: list[tuple[str | None, str | None]] = []
    for intf in candidates:
        if not isinstance(intf, dict):
            continue
        name = intf.get("name")
        status = _interface_status(intf.get("oper-status"))
        interfaces.append((name, status))

    return interfaces


def _interface_status(status_raw):
    """Translate a YANG ``oper-status`` leaf into ``up``/``down``/``None``."""

    if status_raw is None:
        return None
    if str(status_raw).lower() == "up":
        return "up"
    return "down"


def _get_hostname(client: CiscoClient):
    data = client.get_json("data/Cisco-IOS-XE-native:native/hostname")
    if not isinstance(data, dict):
        return None

    native = data.get("Cisco-IOS-XE-native:native")
    if not isinstance(native, dict):
        return None

    value = native.get("hostname")
    return value if isinstance(value, str) and value else None


def fetch_telemetry(
    device: CiscoDevice | None = None,
    client: CiscoClient | None = None,
    interface: str | None = None,
) -> NetworkTelemetry:
    """Retrieve live device/interface telemetry and map it into ``NetworkTelemetry``.

    Metrics that cannot be sourced read-only (packet loss, latency, and
    bandwidth) are left ``None`` rather than zero-filled.
    """

    device = device or CiscoDevice.from_env()
    if not device.username or not device.password:
        raise CiscoTelemetryError(
            "Cisco device credentials are not configured. "
            "Set CISCO_SANDBOX_USERNAME and CISCO_SANDBOX_PASSWORD."
        )
    client = client or CiscoClient(device)

    cpu_percent = _get_cpu(client)
    memory_percent = _get_memory(client)
    name, status = _get_interface(client, interface)
    hostname = _get_hostname(client)

    telemetry = NetworkTelemetry(
        device_id=hostname or device.host,
        interface=name or interface or device.host,
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        cpu_percent=cpu_percent,
        memory_percent=memory_percent,
        bandwidth_mbps=None,
        packet_loss_percent=None,
        latency_ms=None,
        interface_status=status or "up",
    )

    return telemetry


class CiscoConnector(SandboxConnector):
    """Read-only connector for Cisco IOS XE devices over RESTCONF.

    Implements :class:`~app.connectors.base.SandboxConnector` by delegating to
    the module's RESTCONF helpers so the connector and the standalone
    ``fetch_telemetry`` function share the same parsing logic.
    """

    vendor = Vendor.CISCO

    def __init__(
        self,
        device: CiscoDevice | None = None,
        client: CiscoClient | None = None,
    ) -> None:
        self._device = device or CiscoDevice.from_env()
        self._client = client or CiscoClient(self._device)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "CiscoConnector":
        return cls(device=CiscoDevice.from_env(environ))

    def connectivity_check(self) -> bool:
        return _get_hostname(self._client) is not None

    def fetch_inventory(self, interface: str | None = None) -> DeviceInventory:
        hostname = _get_hostname(self._client)
        name, status = _get_interface(self._client, interface)
        if name is None:
            name = interface or self._device.host
        status = status or "up"

        return DeviceInventory(
            device_id=hostname or self._device.host,
            vendor=Vendor.CISCO,
            hostname=hostname,
            interfaces=[InterfaceInfo(name=name, status=status)],
        )

    def fetch_telemetry(self, interface: str | None = None) -> NetworkTelemetry:
        return fetch_telemetry(
            device=self._device,
            client=self._client,
            interface=interface,
        )