# Agentic NetOps

Agentic NetOps is a FastAPI-based network operations assistant for ingesting
telemetry, detecting anomalies, and producing agent-assisted diagnosis using
Google ADK, MCP, and Gemini.

The current focus is a production-ready core API that can later connect to
enterprise network sandboxes such as Cisco DevNet, Juniper, Arista, or internal
lab environments.

## Quick start

1. Create and activate a virtual environment.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Configure any required model credentials in `.env`.
4. Run the app:
   `uvicorn app.main:app --reload`
5. Open the dashboard:
   `http://127.0.0.1:8000/dashboard`

## Run tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## API

### `GET /`

Basic service banner.

Response:

```json
{
  "message": "Agentic NetOps is running"
}
```

### `GET /health`

Health endpoint for readiness checks, deployment probes, and sandbox
connectivity validation.

Response:

```json
{
  "status": "ok",
  "service": "agentic-netops"
}
```

### `POST /telemetry`

Validates and echoes a telemetry payload.

Request:

```json
{
  "device_id": "router-01",
  "interface": "GigabitEthernet0/1",
  "timestamp": "2026-08-25T00:00:00Z",
  "cpu_percent": 92,
  "memory_percent": 60,
  "bandwidth_mbps": 100,
  "packet_loss_percent": 5,
  "latency_ms": 150,
  "interface_status": "up"
}
```

### `POST /diagnose`

Runs the deterministic rule-based diagnosis engine and returns structured
output for dashboards and automation.

Successful response:

```json
{
  "device_id": "router-01",
  "interface": "GigabitEthernet0/1",
  "severity": "high",
  "diagnosis": "Network health degradation detected.",
  "anomalies": [
    "high_cpu",
    "high_packet_loss",
    "high_latency"
  ],
  "evidence": [
    "CPU utilization is 92.0%.",
    "Packet loss is 5.0%.",
    "Latency is 150.0 ms."
  ],
  "recommendation": "Inspect CPU-intensive processes and device resource utilization. Inspect interface errors, congestion, and link quality. Investigate congestion, routing path, and interface performance."
}
```

### `POST /analyze`

Runs the ADK agent against telemetry and returns a stable API response.

Successful response:

```json
{
  "device_id": "router-01",
  "interface": "GigabitEthernet0/1",
  "status": "completed",
  "analysis": "High CPU usage, elevated packet loss, and high latency detected."
}
```

If the agent backend fails or returns no usable response, the endpoint returns
HTTP `503`.

## Diagnosis rules

The local diagnosis engine detects:

- high CPU usage
- high memory usage
- high packet loss
- high latency
- interface down

Diagnosis responses include a stable schema with device identity, severity,
detected anomalies, evidence, and recommended action. This keeps the output
usable by dashboards, automation jobs, and future sandbox integrations.

## MCP integration

The MCP server exposes `check_device_health`, which analyzes the same telemetry
shape and returns device health plus detected anomalies. The ADK agent can use
both the local diagnosis tool and the MCP tool when producing analysis.

## Dashboard

The built-in dashboard is available at `/dashboard`. It provides:

- telemetry input for device, interface, utilization, packet loss, latency, and
  interface state
- deterministic diagnosis summary with severity, anomalies, evidence, and
  recommendation
- agent explanation panel for plain-English operational analysis
- backend health indicator

## Project structure

- `app/` contains the FastAPI application and agent modules
- `tests/` contains automated tests
- `docs/` will hold implementation notes and architecture diagrams

## Future work

- Add structured device inventory and sandbox connector interfaces.
- Add Cisco sandbox integration workflows.
- Add authentication and request tracing.
- Add optional DGX Spark / Qwen backend support.
