from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Vendor(str, Enum):
    """Supported network-device vendors.

    Subclassing ``str`` keeps the enum values JSON-friendly and comparable to
    plain strings, which lets route path parameters match without coercion while
    still exposing ``.value`` for labels and the registry.
    """

    CISCO = "cisco"
    JUNIPER = "juniper"
    ARISTA = "arista"


class InterfaceInfo(BaseModel):
    """A single interface as reported during an inventory sweep."""

    name: str
    status: Literal["up", "down", "unknown"] = "unknown"


class DeviceInventory(BaseModel):
    """A point-in-time inventory snapshot for a single device.

    Vendor-specific fields that are not exposed by the device's model
    (model, version, uptime) are optional rather than zero-filled so an
    inventory never fabricates data the device did not report.
    """

    device_id: str
    vendor: Vendor
    hostname: Optional[str] = None
    model: Optional[str] = None
    version: Optional[str] = None
    uptime_seconds: Optional[int] = None
    interfaces: list[InterfaceInfo] = Field(default_factory=list)
    fetched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )