from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.api.agent import AgentAnalysisError, analyze_telemetry
from app.api.schemas import AnalyzeResponse, HealthResponse
from app.api.telemetry import ingest_telemetry
from app.cisco import CiscoTelemetryError, fetch_telemetry
from app.diagnosis import DiagnosisResult, diagnose_network_issue
from app.telemetry import NetworkTelemetry


app = FastAPI(title="Agentic NetOps")
DASHBOARD_PATH = Path(__file__).parent / "dashboard" / "index.html"


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": "Agentic NetOps is running"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_PATH.read_text(encoding="utf-8"))


@app.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return {
        "status": "ok",
        "service": "agentic-netops",
    }


@app.post("/telemetry", response_model=NetworkTelemetry)
def receive_telemetry(telemetry: NetworkTelemetry) -> NetworkTelemetry:
    return ingest_telemetry(telemetry)


@app.post("/diagnose", response_model=DiagnosisResult)
def diagnose_network(telemetry: NetworkTelemetry) -> DiagnosisResult:
    return diagnose_network_issue(telemetry)


@app.get("/cisco/telemetry")
def cisco_telemetry(interface: str | None = None) -> dict:
    """Fetch live Cisco IOS XE RESTCONF telemetry and run the standard diagnosis.

    This is additive: existing endpoints and their response contracts are
    unchanged. Missing (probe/calculation) metrics arrive as ``null`` so the
    diagnosis reflects only what the device actually reported.
    """
    try:
        telemetry = fetch_telemetry(interface=interface)
    except CiscoTelemetryError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Cisco telemetry retrieval failed.",
        ) from exc

    diagnosis = diagnose_network_issue(telemetry)

    return {
        "source": "cisco",
        "telemetry": telemetry.model_dump(),
        **diagnosis,
    }


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
