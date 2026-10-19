from __future__ import annotations

import os
from abc import ABC

import httpx
from dotenv import load_dotenv

from app.connectors.base import SandboxConnectorError


def _map_status(status_raw) -> str | None:
    if status_raw is None:
        return None
    upper = str(status_raw).upper()
    if upper in {"UP", "TRUE", "1", "ADMIN_UP", "LOWER_LINK_UP"}:
        return "up"
    if upper in {"DOWN", "ADMIN_DOWN", "NOT PRESENT", "LOWER_LINK_DOWN"}:
        return "down"
    return None


class RestconfDevice(ABC):
    """Common parameters for a read-only RESTCONF endpoint.

    ``from_env`` reads the vendor's credentials from environment variables;
    subclasses pass the exact variable names and default API root so the error
    message names the missing variable.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        api_root: str,
        timeout: float = 10.0,
        verify_tls: bool = False,
    ) -> None:
        if not host:
            raise SandboxConnectorError(
                f"{type(self).__name__} host is not configured."
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
    def from_env(
        cls,
        environ: dict[str, str] | None = None,
        *,
        host_key: str,
        username_key: str = "USERNAME",
        password_key: str = "PASSWORD",
        apiroot_key: str = "API_ROOT",
        verify_key: str = "VERIFY_TLS",
        default_api_root: str = "/restconf",
    ) -> "RestconfDevice":
        if environ is None:
            load_dotenv(override=False)
            environ = dict(os.environ)

        device = cls(
            host=environ.get(host_key) or "",
            username=environ.get(username_key, ""),
            password=environ.get(password_key, ""),
            api_root=environ.get(apiroot_key, default_api_root),
            verify_tls=(
                environ.get(verify_key, "false").strip().lower() == "true"
            ),
        )
        if not device.host:
            raise SandboxConnectorError(
                f"{host_key} is not set for {cls.__name__}."
            )
        return device


class RestconfClient:
    """Minimal RESTCONF client using HTTP Basic authentication.

    A 404 means the endpoint is unsupported on this device's software version,
    so callers tolerate partial data rather than treating it as an error.
    """

    def __init__(self, device: RestconfDevice, client: httpx.Client | None = None) -> None:
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
        url = f"{self._device.api_root}/{path.lstrip('/')}"
        headers = {"Accept": "application/yang-data+json"}

        client = self._http_client()

        try:
            response = client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SandboxConnectorError(
                f"Could not reach device at '{self._device.host}'."
            ) from exc

        if response.status_code == 404:
            return None

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in {401, 403}:
                raise SandboxConnectorError(
                    "Device rejected authentication."
                ) from exc
            raise SandboxConnectorError(
                f"Device returned HTTP status {status}."
            ) from exc

        try:
            return response.json()
        except ValueError as exc:
            raise SandboxConnectorError(
                "Device returned an invalid JSON response."
            ) from exc