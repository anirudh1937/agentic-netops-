from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class AnalyzeResponse(BaseModel):
    device_id: str
    interface: str
    status: Literal["completed"]
    analysis: str
