# Agentic NetOps

[![Tests](https://github.com/anirudh1937/agentic-netops-/actions/workflows/tests.yml/badge.svg)](https://github.com/anirudh1937/agentic-netops-/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141.1-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**AI-assisted network operations with deterministic diagnosis, vendor-aware
telemetry connectors, and a safe-by-default polling lifecycle.**

Agentic NetOps is a FastAPI service for ingesting network telemetry, detecting
anomalies with explainable rules, and optionally using Google ADK, Gemini, and
MCP tools to produce operational analysis. It includes a Cisco IOS XE RESTCONF
adapter, vendor connector abstractions for Cisco, Juniper, and Arista, a
built-in dashboard, and a read-only API for the latest polled telemetry.

## Highlights

- **Explainable first:** deterministic diagnosis rules identify high CPU,
  memory pressure, packet loss, latency, and interface-down conditions with
  evidence and recommendations.
- **Vendor-aware:** a common connector interface keeps device-access logic out
  of the API layer and makes additional vendors straightforward to add.
- **Safe by default:** background polling is disabled unless explicitly enabled,
  and `GET /telemetry/latest` / `GET /telemetry/status` only read local state.
- **Honest freshness:** stored readings carry a read-time collection-freshness
  state, so stale or failed collection is never presented as live telemetry.
- **Agent-ready:** the optional `/analyze` path combines Google ADK, Gemini, and
  MCP tools with the deterministic diagnosis output.
- **Offline-testable:** the default test suite uses fakes and mocks. Live device
  and model integrations are opt-in and skipped by default.
- **Dashboard included:** a small browser UI exercises telemetry input,
  diagnosis, agent analysis, and backend health.

## Architecture

```mermaid
flowchart LR
    Operator[Operator / Dashboard] --> API[FastAPI API]
    API --> Diagnosis[Rule-based diagnosis]
    API --> Store[In-memory telemetry store]
    API --> Agent[ADK agent - optional]
    Agent --> Gemini[Gemini]
    Agent --> MCP[MCP tools]
    Poller[Opt-in telemetry poller] --> Store
    Poller --> Connectors[Vendor connectors]
    Connectors --> Devices[Cisco / Juniper / Arista devices]
```

The polling path is deliberately separate from the read path. The poller is the
only component that writes polled readings, while the API exposes those readings
without initiating another poll. See [docs/architecture.md](docs/architecture.md)
for component-level details.

## Features

### Telemetry and diagnosis

- Validates the shared `NetworkTelemetry` model used by the API, dashboard, and
  agent tools.
- Runs a deterministic diagnosis engine that returns severity, anomalies,
  evidence, and a recommended action.
- Treats unavailable metrics as `null` instead of zero-filling them, so a
  diagnosis only reflects data the device actually reported.

### Vendor connectors

- Cisco IOS XE RESTCONF adapter with read-only telemetry and inventory support.
- Shared connector interface for Cisco, Juniper, and Arista.
- Missing configuration is reported by `/integrations` and the poller instead
  of being treated as a healthy device.
- Credential-safe error handling keeps secrets out of API responses and stored
  readings.

### Controlled polling

- Disabled by default; enabled with `NETOPS_POLL_ENABLED=true`.
- One poller task per application process, with non-overlapping poll cycles.
- Readings keyed by `(vendor, device_id, interface)`.
- Latest poll attempt is tracked separately from the last successful
  measurement.
- Poller-level status is available at `GET /telemetry/status`, including the
  last cycle start/finish timestamps, outcome, and a sanitized error.
- Unexpected exceptions in a poll cycle are recorded and do not stop later
  cycles.
- A first-poll failure does not fabricate a device identity.
- In-memory store by design: data resets when the process restarts.

### Collection freshness and stale devices

- Collection freshness is derived **at read time** from
  `last_successful_fetched_at`; it is never persisted and recovers automatically
  after a successful poll.
- Each `GET /telemetry/latest` reading includes a nested `freshness` object with
  `state`, `age_seconds`, and `threshold_seconds`.
- States are `HEALTHY`, `STALE`, `POLLING_FAILED`, and `UNKNOWN`. `UNCONFIGURED`
  is vendor-level and comes from the poller's existing
  `unconfigured_vendors` status, never from a fabricated reading.
- A stale reading takes precedence over a recent poll failure: the failure
  fields remain visible, but old data is labelled stale.
- The default threshold is 300 seconds and can be changed with
  `NETOPS_STALE_THRESHOLD_SECONDS`.
- The dashboard shows collection freshness separately from metric anomalies and
  labels stale or polling-failed rows as last-known data.
- Freshness does not change diagnosis severity, anomalies, evidence, or
  recommendations.

### Optional AI and MCP

- `/analyze` uses the configured Google ADK/Gemini backend to produce a
  plain-English operational summary.
- The MCP server exposes `check_device_health` for the same core telemetry
  fields.
- If the agent backend is unavailable, the endpoint fails cleanly with HTTP
  `503` rather than returning a partial response.

## Quick start

### Prerequisites

- Python 3.12+
- A virtual environment is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

On macOS/Linux:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open:

- Dashboard: <http://127.0.0.1:8000/dashboard>
- Interactive API docs: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

The application starts without Cisco credentials, Gemini credentials, or any
live device access. Configure only the integrations you intend to use.

## Configuration

Copy `.env.example` to `.env` and fill in only the values you need. Never commit
`.env`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | Optional | Enables the `/analyze` agent path. |
| `CISCO_SANDBOX_HOST` | Optional | Cisco IOS XE RESTCONF host. |
| `CISCO_SANDBOX_USERNAME` | Optional | Cisco RESTCONF username. |
| `CISCO_SANDBOX_PASSWORD` | Optional | Cisco RESTCONF password. |
| `CISCO_SANDBOX_API_ROOT` | Optional | RESTCONF API root; defaults to `/restconf`. |
| `CISCO_SANDBOX_VERIFY_TLS` | Optional | Set to `true` to verify the device TLS certificate. |
| `NETOPS_POLL_ENABLED` | Optional | Set to `true` to start background polling. Default: disabled. |
| `NETOPS_STALE_THRESHOLD_SECONDS` | Optional | Age in seconds after which a stored reading is `STALE`. Must be a positive, finite number; defaults to `300`. |
| `NETOPS_AGENT_TIMEOUT_SECONDS` | Optional | Maximum time for one `/analyze` agent request. Must be a positive, finite number; defaults to `60`. |

The stale threshold defaults to 300 seconds. A valid override must be a positive,
finite number of seconds. Malformed, zero, negative, or non-finite configured
values are invalid: the application logs a warning and falls back to 300 seconds.
Missing or blank values also use the 300-second default. The threshold is read
once at application startup, so changing `NETOPS_STALE_THRESHOLD_SECONDS`
requires restarting the application.

The agent timeout defaults to 60 seconds and applies to each `/analyze` request.
A valid override must be a positive, finite number of seconds. Malformed, zero,
negative, or non-finite values log a warning and fall back to 60 seconds. Missing
or blank values also use the 60-second default. The value is read for each
request, so a restart is not required after changing it.

The example file also contains `GOOGLE_CLOUD_PROJECT` and
`CISCO_SANDBOX_INTERFACE`; the current application does not read those values
directly. Select the Cisco interface with the `interface` query parameter.

Juniper and Arista connectors read `JUNIPER_SANDBOX_*` and `ARISTA_SANDBOX_*`
environment variables when those integrations are used.

## API

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Service banner. |
| `GET` | `/health` | Readiness probe. |
| `GET` | `/dashboard` | Built-in dashboard. |
| `POST` | `/telemetry` | Validate and echo a telemetry payload. |
| `GET` | `/telemetry/latest` | Read the latest stored polled readings. Never starts a poll. |
| `GET` | `/telemetry/status` | Read poller configuration and last-cycle status. Never starts a poll. |
| `POST` | `/diagnose` | Run the deterministic diagnosis engine. |
| `GET` | `/cisco/telemetry` | Fetch Cisco telemetry and run diagnosis. |
| `GET` | `/vendors` | List registered vendor connectors. |
| `GET` | `/vendors/{vendor}/telemetry` | Fetch vendor telemetry and run diagnosis. |
| `GET` | `/vendors/{vendor}/inventory` | Return a device/interface inventory snapshot. |
| `GET` | `/integrations` | Report backend, agent, MCP, and vendor connectivity/inventory status. |
| `POST` | `/analyze` | Run the optional ADK/Gemini analysis path. |

### Example: diagnosis

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

Send it to `POST /diagnose` to receive severity, anomalies, evidence, and a
recommended action.

### Example: latest telemetry

`GET /telemetry/latest` returns already-stored readings. An empty store keeps
the original top-level shape:

```json
{
  "readings": [],
  "count": 0,
  "generated_at": "2026-10-29T00:00:00+00:00"
}
```

Each non-empty reading includes the vendor/device/interface identity, the latest
poll outcome, the last successful measurement, failure count, any sanitized
error, and a nested read-time `freshness` object:

```json
{
  "readings": [
    {
      "vendor": "cisco",
      "device_id": "router-01",
      "interface": "GigabitEthernet0/1",
      "last_poll_outcome": "failure",
      "last_successful_fetched_at": "2026-10-29T00:00:05+00:00",
      "error": "Device rejected authentication.",
      "freshness": {
        "state": "STALE",
        "age_seconds": 900.0,
        "threshold_seconds": 300.0
      }
    }
  ],
  "count": 1,
  "generated_at": "2026-10-29T00:15:05+00:00"
}
```

Freshness precedence is:

1. No valid `last_successful_fetched_at`: `UNKNOWN`.
2. A success timestamp further in the future than the documented clock-skew
   tolerance (60 seconds): `UNKNOWN`, never `HEALTHY`.
3. Age greater than `threshold_seconds`: `STALE`, including when the latest poll
   also failed.
4. Latest poll failed but the last success is within the threshold:
   `POLLING_FAILED`.
5. Latest poll succeeded and the last success is within the threshold:
   `HEALTHY`.
6. Any other/missing outcome: `UNKNOWN`.

`age_seconds` is `null` when the success timestamp is missing or malformed; the
API never substitutes the current time. A configured vendor with no stored
reading yet is shown as awaiting its first successful poll rather than as a
synthetic reading. `UNCONFIGURED` is vendor-level and comes from
`/telemetry/status`.

### Example: poller status

`GET /telemetry/status` returns the poller's configuration and last completed
cycle:

```json
{
  "enabled": true,
  "running": true,
  "interval_seconds": 60.0,
  "configured_vendors": ["cisco"],
  "unconfigured_vendors": ["arista", "juniper"],
  "last_cycle_started_at": "2026-10-29T00:00:00+00:00",
  "last_cycle_finished_at": "2026-10-29T00:00:01+00:00",
  "last_cycle_outcome": "degraded",
  "last_error": "1 vendor poll failed."
}
```

`last_cycle_outcome` is `success`, `degraded`, or `error`. A `degraded` cycle
completed with at least one vendor failure; `error` means an unexpected
exception interrupted the cycle. `last_error` contains only a credential-safe
summary: a count-only message for `degraded` cycles, or a sanitized exception
class for `error` cycles. Detailed per-vendor errors are available through
`GET /telemetry/latest` only when a verified reading identity exists. A
first-poll failure without a verified device identity is logged but does not
create a store reading. The status endpoint returns HTTP `503` only when the
poller is unavailable and never triggers a poll or device request.

## Polling lifecycle

Polling is intentionally opt-in:

```powershell
$env:NETOPS_POLL_ENABLED = "true"
uvicorn app.main:app --reload
```

When enabled, the FastAPI lifespan creates one application-owned store and
poller, starts the poller, and stops it cleanly on shutdown. When disabled, the
poller object exists but no background task is started.

Each cycle records its start and finish timestamps, an outcome (`success`,
`degraded`, or `error`), and a sanitized `last_error` (count-only for `degraded`
cycles). Per-vendor failures are isolated and produce a `degraded` cycle, while
an unexpected exception is contained by the loop, recorded as `error`, and
followed by another cycle after the normal interval. `GET /telemetry/status`
exposes this state without polling.

The current implementation supports one configured connector per vendor.
Multi-device polling and persistent storage are future work; multi-worker
deployments are not supported by the current in-memory store.

Collection freshness is computed only when a consumer reads the store. It uses
the server-side `last_successful_fetched_at` clock, so a failed attempt cannot
make preserved telemetry look fresh, and a later successful poll recovers the
reading automatically. Diagnosis severity remains a metric-health signal and is
independent of collection freshness.

## Testing

Run the offline suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

or:

```bash
python -m pytest -q
```

The default suite does not contact Cisco, Juniper, Arista, Gemini, or any other
external service. Live integration tests are opt-in and are skipped unless the
corresponding environment flags are set. CI runs only the offline suite.

### Opt-in live integration tests

Live tests are skipped by default and require credentials. Set the flags in your
shell rather than in `.env` so an ordinary test run cannot enable live calls
accidentally.

Gemini:

```powershell
$env:AGENTIC_NETOPS_LIVE_TEST = "1"
$env:GEMINI_API_KEY = "<your-key>"
.\.venv\Scripts\python.exe -m pytest -q tests/test_agent_api.py::test_analyze_telemetry_real_model_integration
```

`AGENTIC_NETOPS_LIVE_TEST` is the canonical flag. The legacy
`AGNETNETOPS_LIVE_TEST=1` name is still honored.

Cisco (read-only):

```powershell
$env:AGENTIC_NETOPS_LIVE_CISCO_TEST = "1"
# Set CISCO_SANDBOX_HOST, CISCO_SANDBOX_USERNAME, and
# CISCO_SANDBOX_PASSWORD in .env or the environment.
.\.venv\Scripts\python.exe -m pytest -q tests/test_cisco_live_integration.py::test_cisco_live_integration
```

## Project structure

```text
app/
  api/                  FastAPI request/response helpers
  cisco/                Cisco IOS XE RESTCONF adapter
  connectors/           Vendor connector abstraction and implementations
  dashboard/            Built-in browser dashboard
  mcp/                  MCP server and network tools
  agents/               Google ADK agent configuration and tools
  diagnosis.py          Deterministic diagnosis rules
  freshness.py          Read-time collection freshness assessment
  telemetry_poller.py   Controlled background polling
  telemetry_store.py    In-memory telemetry store
  main.py               FastAPI application and lifespan
tests/                  Offline and opt-in integration tests
docs/                   Architecture and implementation notes
.github/workflows/      GitHub Actions CI
```

## Security

- `.env` is ignored and must never be committed.
- RESTCONF access is read-only; this project does not perform remediation.
- Device credentials are never returned. Connector errors use credential-safe
  messages, and unexpected exception details are reduced to class names or
  stable, sanitized messages.
- `GET /telemetry/latest` and `GET /telemetry/status` never trigger a network
  request.
- Endpoints such as `/cisco/telemetry`, `/vendors/{vendor}/telemetry`, and
  `/integrations` contact configured devices and should be protected before
  exposure to untrusted networks.
- The optional `/analyze` path sends submitted telemetry to the configured
  model backend. Do not submit sensitive production data unless that is
  acceptable for your environment.

For vulnerability reporting, see [SECURITY.md](SECURITY.md).

## Roadmap

- Add authentication and authorization for externally exposed APIs.
- Add a persistent telemetry store while preserving the current freshness
  semantics.
- Support multiple devices per vendor.
- Expand vendor-specific telemetry and inventory coverage.
- Add request tracing and structured observability.
- Add deployment packaging and example reverse-proxy configuration.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for local
setup, testing, and pull-request guidance.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
