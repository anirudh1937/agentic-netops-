from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.api.agent import AgentAnalysisError, analyze_telemetry
from app.api.schemas import (
    AnalyzeResponse,
    HealthResponse,
    IntegrationAgent,
    IntegrationBackend,
    IntegrationMcp,
    IntegrationVendor,
    IntegrationsResponse,
)
from app.api.telemetry import ingest_telemetry
from app.cisco import CiscoTelemetryError, fetch_telemetry
from app.connectors import (
    SandboxConnectorError,
    available_vendors,
    make_connector,
)
from app.diagnosis import DiagnosisResult, diagnose_network_issue
from app.inventory import Vendor
from app.telemetry import NetworkTelemetry
from app.telemetry_poller import TelemetryPoller
from app.telemetry_store import TelemetryStore


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Application lifespan for telemetry store and poller.

    Creates one application-owned :class:`TelemetryStore` and builds a
    :class:`TelemetryPoller` from environment configuration via
    :func:`TelemetryPoller.from_config`. Polling is disabled by default and is
    only started when ``NETOPS_POLL_ENABLED`` is set to a truthy value.

    The store and poller are attached to ``app.state`` so the
    ``GET /telemetry/latest`` endpoint can read stored readings without
    initiating any poll or network call.

    On shutdown, the poller is stopped cleanly if it was started.
    """

    store = TelemetryStore()
    poller = TelemetryPoller.from_config(store, enabled=_polling_enabled())

    app.state.telemetry_store = store
    app.state.telemetry_poller = poller

    try:
        if poller.enabled:
            await poller.start()
        yield
    finally:
        if poller.is_running:
            await poller.stop()


def _polling_enabled() -> bool:
    """Return whether background polling is enabled.

    Controlled by the ``NETOPS_POLL_ENABLED`` environment variable. Disabled
    by default so importing or constructing the application never starts
    polling or makes a live network call.
    """

    value = os.environ.get("NETOPS_POLL_ENABLED", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


app = FastAPI(title="Agentic NetOps", lifespan=app_lifespan)
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


@app.get("/telemetry/latest")
def read_latest_telemetry() -> dict:
    """Return the latest stored telemetry readings from the poller.

    This is read-only: it only returns data already persisted in the
    application-owned :class:`TelemetryStore`. It never initiates a poll or
    performs any external network request.

    The response includes, for each stored reading, the vendor/device/interface
    identity, the latest poll attempt and its outcome, the last successful
    measurement and its timestamps, consecutive failure count, and any
    sanitized error. This lets consumers distinguish fresh (recently and
    successfully collected) from stale or unavailable data.

    When no readings have been stored, returns an empty ``readings`` list with
    HTTP ``200``.
    """

    store: TelemetryStore = app.state.telemetry_store
    readings = [reading.model_dump() for reading in store.get_all()]

    return {
        "readings": readings,
        "count": len(readings),
        "generated_at": _now_iso(),
    }


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


@app.get("/vendors")
def list_vendors() -> dict[str, list[str]]:
    """Return the list of vendor connectors currently registered."""

    return {"vendors": available_vendors()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _llm_configured() -> tuple[bool, str]:
    """Best-effort check that the agent's model credentials exist.

    ``reachable`` still depends on ``/analyze`` succeeding; presence of keys
    only means the model *might* run.
    """

    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(key):
            return True, (
                "Model credentials configured; "
                "run the agent to confirm reachability."
            )
    return False, "No model credentials configured. Set GEMINI_API_KEY."


def _mcp_available() -> bool:
    try:
        import app.mcp.server  # noqa: F401
    except Exception:
        return False
    return True


@app.get("/integrations", response_model=IntegrationsResponse)
def integrations_status() -> IntegrationsResponse:
    """Aggregate connectivity + inventory status for every vendor.

    Read-only: only performs reachability probes and inventory reads against
    configured devices. Missing configuration is reported honestly rather than
    as a healthy device, and credential-safe error messages are surfaced.
    """

    # Read agent status from the process environment *before* the vendor loop,
    # because make_connector() runs load_dotenv() internally and would
    # repopulate keys such as GEMINI_API_KEY from .env.
    configured, detail = _llm_configured()

    vendors: list[IntegrationVendor] = []
    for vendor in available_vendors():
        checked = _now_iso()
        entry = IntegrationVendor(
            vendor=vendor,
            label=Vendor(vendor).value.title(),
            last_checked=checked,
        )
        try:
            # Reflect the *current process environment* rather than re-reading
            # .env: from_env(environ) skips load_dotenv() when ``environ`` is
            # supplied, so a value cleared (or monkey-patched) in the process
            # env is respected instead of being re-injected from .env.
            connector = make_connector(
                Vendor(vendor), environ=dict(os.environ)
            )
        except RuntimeError as exc:
            # Both SandboxConnectorError and CiscoTelemetryError derive from
            # RuntimeError and carry a credential-safe "not configured" message.
            entry.error = str(exc)
            vendors.append(entry)
            continue
        except Exception as exc:  # noqa: BLE001 - surface, do not crash endpoint
            entry.error = "Unable to initialize device connector."
            vendors.append(entry)
            continue

        entry.configured = True
        entry.host = getattr(connector, "host", None)

        try:
            reachable = connector.connectivity_check()
        except RuntimeError as exc:
            entry.error = str(exc)
            reachable = False
        except Exception as exc:  # noqa: BLE001
            entry.error = f"Device connectivity check failed: {type(exc).__name__}"
            reachable = False
        entry.reachable = reachable

        if reachable:
            try:
                inventory = connector.fetch_inventory()
            except Exception as exc:  # noqa: BLE001
                entry.error = "Device inventory could not be read."
            else:
                entry.hostname = inventory.hostname
                entry.interfaces = [intf.name for intf in inventory.interfaces]
                entry.interface_count = len(entry.interfaces)

        vendors.append(entry)

    return IntegrationsResponse(
        backend=IntegrationBackend(),
        agent=IntegrationAgent(configured=configured, detail=detail),
        mcp=IntegrationMcp(available=_mcp_available(), detail="Module importable."),
        vendors=vendors,
        generated_at=_now_iso(),
    )


def _resolve_connector(vendor: str):
    """Build the connector for ``vendor`` or raise a 404/503 HTTPException."""

    try:
        vendor_value = Vendor(vendor)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail=f"Unsupported vendor '{vendor}'.",
        )

    try:
        return make_connector(vendor_value)
    except SandboxConnectorError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc


@app.get("/vendors/{vendor}/telemetry")
def vendored_telemetry(vendor: str, interface: str | None = None) -> dict:
    """Fetch telemetry for ``vendor`` and run the standard diagnosis.

    This generalizes the ``/cisco/telemetry`` route to every supported vendor.
    Missing (probe/calculation) metrics arrive as ``null`` so the diagnosis
    reflects only what the device actually reported.
    """

    connector = _resolve_connector(vendor)

    try:
        telemetry = connector.fetch_telemetry(interface=interface)
    except SandboxConnectorError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Telemetry retrieval failed.",
        ) from exc

    diagnosis = diagnose_network_issue(telemetry)

    return {
        "source": vendor,
        "telemetry": telemetry.model_dump(),
        **diagnosis,
    }


@app.get("/vendors/{vendor}/inventory")
def vendored_inventory(vendor: str) -> dict:
    """Return a device/interface inventory snapshot for ``vendor``."""

    connector = _resolve_connector(vendor)

    try:
        inventory = connector.fetch_inventory()
    except SandboxConnectorError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Inventory retrieval failed.",
        ) from exc

    return {"source": vendor, "inventory": inventory.model_dump()}


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
