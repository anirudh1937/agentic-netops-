from fastapi import FastAPI

from app.api.agent import analyze_telemetry
from app.api.telemetry import ingest_telemetry
from app.telemetry import NetworkTelemetry


app = FastAPI(title="Agentic NetOps")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Agentic NetOps is running"}


@app.post("/telemetry", response_model=NetworkTelemetry)
def receive_telemetry(telemetry: NetworkTelemetry) -> NetworkTelemetry:
    return ingest_telemetry(telemetry)


@app.post("/analyze")
async def analyze_network(telemetry: NetworkTelemetry) -> dict[str, str]:
    result = await analyze_telemetry(telemetry)

    return {
        "device_id": telemetry.device_id,
        "interface": telemetry.interface,
        "analysis": result,
    }