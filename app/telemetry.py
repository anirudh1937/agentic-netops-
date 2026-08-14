from typing import Literal

from pydantic import BaseModel, Field


class NetworkTelemetry(BaseModel):
    device_id: str
    interface: str
    timestamp: str
    cpu_percent: float = Field(ge=0, le=100)
    memory_percent: float = Field(ge=0, le=100)
    bandwidth_mbps: float = Field(ge=0)
    packet_loss_percent: float = Field(ge=0, le=100)
    latency_ms: float = Field(ge=0)
    interface_status: Literal["up", "down"]