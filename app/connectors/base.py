from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.inventory import DeviceInventory, Vendor
from app.telemetry import NetworkTelemetry


class SandboxConnectorError(RuntimeError):
    """Raised when a device cannot be reached or answered.

    Messages intentionally omit credentials and host secrets so a failed probe
    cannot leak device access details through API error bodies.
    """


class UnsupportedVendorError(SandboxConnectorError):
    """Raised when a request references a vendor we do not connect to."""


class SandboxConnector(ABC):
    """Common interface for per-vendor device connectors.

    Every connector must map its vendor's device and interface telemetry into
    the project's :class:`~app.telemetry.NetworkTelemetry` without inventing
    measurements, and expose a small inventory snapshot. Metrics the device
    cannot report read-only are left ``None``.
    """

    #: Short vendor identifier, e.g. ``Vendor.CISCO``.
    vendor: Vendor

    @property
    def label(self) -> str:
        """Human-readable vendor label."""

        return self.vendor.value.replace("-", " ").title()

    @property
    def host(self) -> Optional[str]:
        """Management address of the configured device, when available.

        This is a public management address only -- never a credential. It is
        surfaced so dashboards can label a device, but callers must treat it
        as non-secret.
        """

        device = getattr(self, "_device", None)
        if device is not None:
            return getattr(device, "host", None)
        return None

    @abstractmethod
    def connectivity_check(self) -> bool:
        """Return ``True`` when the device answers a reachability probe."""

    @abstractmethod
    def fetch_inventory(self, interface: str | None = None) -> DeviceInventory:
        """Return a device/interface inventory snapshot."""

    @abstractmethod
    def fetch_telemetry(self, interface: str | None = None) -> NetworkTelemetry:
        """Return the current device/interface telemetry as ``NetworkTelemetry``."""

    def __repr__(self) -> str:  # pragma: no cover - cosmetic only
        return f"<{type(self).__name__} vendor={self.vendor!r}>"