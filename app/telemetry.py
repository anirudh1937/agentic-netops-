from typing import Literal, Optional

from pydantic import BaseModel, Field


class NetworkTelemetry(BaseModel):
    device_id: str
    interface: str
    timestamp: str
    # Read-only metrics that may be unavailable on some IOS XE versions or
    # require probing/calculations are optional rather than zero-filled.
    cpu_percent: Optional[float] = Field(default=None, ge=0, le=100)
    memory_percent: Optional[float] = Field(default=None, ge=0, le=100)
    bandwidth_mbps: Optional[float] = Field(default=None, ge=0)
    packet_loss_percent: Optional[float] = Field(default=None, ge=0, le=100)
    latency_ms: Optional[float] = Field(default=None, ge=0)
    interface_status: Literal["up", "down"]
