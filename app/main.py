from fastapi import FastAPI, HTTPException

from app.api.agent import AgentAnalysisError, analyze_telemetry
from app.api.schemas import AnalyzeResponse, HealthResponse
from app.api.telemetry import ingest_telemetry
from app.telemetry import NetworkTelemetry


app = FastAPI(title="Agentic NetOps")


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Agentic NetOps is running"}


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return {
        "status": "ok",
        "service": "agentic-netops",
    }


@app.post("/telemetry", response_model=NetworkTelemetry)
def receive_telemetry(telemetry: NetworkTelemetry) -> NetworkTelemetry:
    return ingest_telemetry(telemetry)


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_network(telemetry: NetworkTelemetry) -> AnalyzeResponse:
    try:
        result = await analyze_telemetry(telemetry)
    except AgentAnalysisError as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent analysis is currently unavailable.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent analysis failed.",
        ) from exc

    return {
        "device_id": telemetry.device_id,
        "interface": telemetry.interface,
        "status": "completed",
        "analysis": result,
    }
