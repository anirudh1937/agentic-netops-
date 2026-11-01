# Architecture

This document describes the main runtime paths in Agentic NetOps and the safety
boundaries between them.

## Component overview

```mermaid
flowchart LR
    Client[Operator / Dashboard] --> API[FastAPI application]

    API --> Diagnosis[Deterministic diagnosis]
    API --> Store[In-memory telemetry store]
    API --> Agent[Google ADK agent - optional]

    Agent --> Gemini[Gemini model]
    Agent --> MCP[MCP tools]

    Poller[Telemetry poller - opt-in] --> Store
    Poller --> Connectors[Vendor connectors]
    Connectors --> Devices[Cisco / Juniper / Arista devices]

    API --> Integrations[Integration status]
    Integrations --> Connectors
```

## Request paths

### Ingest and diagnose

1. A telemetry payload is validated by the shared `NetworkTelemetry` model.
2. The deterministic diagnosis engine evaluates CPU, memory, packet loss,
   latency, and interface state.
3. The response contains severity, anomalies, evidence, and a recommended
   action.

This path is local and does not require an external model.

### Read latest polled telemetry

`GET /telemetry/latest` reads from the application-owned `TelemetryStore` and
returns the stored readings. It does not call `poll_once()`, start a poller, or
contact a device.

### Read poller status

`GET /telemetry/status` reads the application-owned `TelemetryPoller` from
`request.app.state` and returns an immutable snapshot of its configuration and
last completed cycle. It does not call `poll_once()`, start a poller, or contact a
device. HTTP `503` is returned only when no poller is available.

The snapshot includes:

- `enabled` and `running`
- `interval_seconds`
- `configured_vendors` and `unconfigured_vendors`
- `last_cycle_started_at` and `last_cycle_finished_at`
- `last_cycle_outcome` (`success`, `degraded`, or `error`)
- `last_error`, a credential-safe summary (count-only for `degraded` cycles,
  exception class for `error` cycles)

Detailed per-vendor errors are available in the stored readings returned by
`GET /telemetry/latest` only when a verified reading identity exists. First-poll
failures without a verified device identity are logged but do not create a store
reading.

### Fetch live vendor telemetry

`GET /cisco/telemetry` and `GET /vendors/{vendor}/telemetry` resolve a connector
from the vendor registry, fetch telemetry through the connector, and run the
same deterministic diagnosis engine. These paths contact configured devices and
should be protected before exposure to untrusted networks.

### Optional agent analysis

`POST /analyze` runs the ADK agent against telemetry. The agent may use the
deterministic diagnosis tool and MCP tools. When the model backend is
unavailable, the endpoint returns HTTP `503` with a stable, credential-safe
error response.

## Polling lifecycle

```mermaid
sequenceDiagram
    participant App as FastAPI lifespan
    participant Poller as TelemetryPoller
    participant Connector as Vendor connector
    participant Store as TelemetryStore
    participant API as GET /telemetry/latest
    participant Status as GET /telemetry/status

    App->>Poller: from_config(store, enabled)
    alt NETOPS_POLL_ENABLED is true
        App->>Poller: start()
        loop every interval
            Poller->>Connector: fetch_telemetry()
            Connector-->>Poller: telemetry or error
            Poller->>Store: record success or failure
            Note over Poller: record cycle timestamps, outcome, sanitized error
        end
    else polling disabled
        App->>Poller: no background task
    end

    API->>Store: get_all()
    Store-->>API: stored readings

    Status->>Poller: status()
    Poller-->>Status: immutable snapshot
```

The poller is disabled by default. When disabled, the application still creates
the store and poller objects but does not start a background task.

A cycle that completes with one or more vendor failures is recorded as
`degraded`; a cycle with no failures is `success`. If an unexpected exception
escapes `poll_once()`, the loop records `error` with a credential-safe
`last_error`, keeps the task alive, and waits for the next interval. The loop
does not restart itself and does not suppress cancellation.

The poller's configuration and last-cycle status are exposed through
`GET /telemetry/status`. Reading the snapshot does not initiate a cycle.

## Store semantics

Readings are keyed by the composite identity:

```text
(vendor, device_id, interface)
```

Each reading tracks:

- the latest poll attempt timestamp and outcome
- the last successful telemetry measurement
- the last successful fetch timestamp
- consecutive failure count
- a sanitized error message when the latest attempt failed

The latest attempt is intentionally separate from the last successful
measurement. A failed poll must not overwrite the last known good telemetry or
make it appear freshly collected.

The store is in memory and is reset when the process restarts. Multi-worker
deployments are not supported by the current in-memory design.

## Safety boundaries

- Device access is read-only; no remediation actions are implemented.
- Connector configuration failures are reported without exposing credentials.
- Raw exception messages from arbitrary exceptions are replaced with class
  names or stable messages.
- A first-poll failure without a verified device identity does not create a
  fabricated reading.
- The latest-telemetry and poller-status read paths never trigger polling or
  network access.
- Live integration tests and live-test flags are not part of CI.
