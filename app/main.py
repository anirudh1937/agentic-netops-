from fastapi import FastAPI

from app.api.telemetry import ingest_telemetry
from app.telemetry import NetworkTelemetry

app = FastAPI(title="Agentic NetOps")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Agentic NetOps is running"}


@app.post("/telemetry", response_model=NetworkTelemetry)
def receive_telemetry(telemetry: NetworkTelemetry) -> NetworkTelemetry:
    return ingest_telemetry(telemetry)