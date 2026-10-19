from typing import Literal, Optional

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class AnalyzeResponse(BaseModel):
    device_id: str
    interface: str
    status: Literal["completed"]
    analysis: str


class IntegrationBackend(BaseModel):
    """Status of the API itself, always available while the process runs."""

    status: Literal["ok"] = "ok"
    service: str = "agentic-netops"


class IntegrationAgent(BaseModel):
    """LLM agent backend availability.

    ``reachable`` stays ``None`` until the dashboard actually exercises
    ``/analyze``; presence of credentials only means the model *might* run.
    """

    configured: bool = False
    reachable: Optional[bool] = None
    detail: str = ""


class IntegrationMcp(BaseModel):
    """MCP tooling availability (importable, not a live device handshake)."""

    available: bool = False
    detail: str = ""


class IntegrationVendor(BaseModel):
    """Connectivity + inventory status for one vendor connector."""

    vendor: str
    label: str
    configured: bool = False
    reachable: Optional[bool] = None
    host: Optional[str] = None
    hostname: Optional[str] = None
    interfaces: list[str] = Field(default_factory=list)
    interface_count: int = 0
    error: Optional[str] = None
    last_checked: str = ""


class IntegrationsResponse(BaseModel):
    backend: IntegrationBackend
    agent: IntegrationAgent
    mcp: IntegrationMcp
    vendors: list[IntegrationVendor]
    generated_at: str
