# Agentic NetOps — Project Status Checkpoint

## Repository

- Path: `C:\Users\Aniru\agentic-netops-old`
- Branch: `feature/cisco-live-validation`
- Latest verified commit: `9c4d752` — `test: add MCP agent lifecycle coverage`

## Verified test results

### Offline regression suite
- Full offline test suite: **369 passed, 2 skipped, 1 xfailed, 17 warnings**
- The 2 skips are opt-in live Gemini and Cisco sandbox tests.
- The 1 xfail is pre-existing.
- Warnings are pre-existing Google ADK experimental/deprecation warnings.

### Live Cisco integration test (opt-in)
- Test command: `AGENTIC_NETOPS_LIVE_CISCO_TEST=1 .venv\Scripts\python.exe -m pytest -v tests/test_cisco_live_integration.py`
- Result: **PASSED**
- HTTPX request to Cisco Catalyst 9000v sandbox returned HTTP 200.
- RESTCONF hostname endpoint returned `{"Cisco-IOS-XE-native:hostname": "switch"}`.
- Read-only GET requests only; no device configuration changes.

## Persistent credential issue
- `.env` password correction: **still pending** — the working sandbox password was supplied temporarily through the Python process for the live test, but has not been confirmed as permanently updated in `.env`.

## Completed milestones

### Milestone 1 — Agent and Cisco integration
The existing telemetry and diagnosis pipeline, agent/MCP integration, Cisco RESTCONF adapter, and historical live Cisco sandbox validation are part of the project.

### Milestone 2 — Telemetry store and controlled poller
The in-memory telemetry store and periodic poller are implemented. Focused store and poller tests passed as part of the 47 passing telemetry tests.

### Milestone 3 — FastAPI lifecycle and latest telemetry
Verified implementation and test coverage include:
- One telemetry store and poller per application instance.
- Polling disabled by default.
- Polling enabled only for recognized truthy `NETOPS_POLL_ENABLED` values.
- Poller shutdown cleanup.
- Read-only `GET /telemetry/latest` endpoint.
- Tests for default-disabled polling, shutdown, and application-store isolation.

## DGX Spark / Ornith status

DGX connectivity remains unresolved:
- Tailscale connectivity to `100.123.159.98` works.
- Port `8080` responds.
- `/health` returns `ok: true` and an empty model list.
- `/v1/models` on port `8080` returns an invalid API key error without authentication.
- Port `8000` is unreachable from the development laptop.

Next DGX step: obtain the approved API endpoint, authentication method, and actual serving model ID from the DGX administrator. Do not guess credentials or modify the server without authorization.

## Git safety

At the last observed status:
- `docs/assets/netops-dashboard.png` was staged but uncommitted.
- `PROJECT_STATUS.md`, `test_results.txt`, and `tstest_integrations.py` were untracked.

Recheck Git status before staging, committing, or pushing anything.

## Next engineering milestone

Review and strengthen telemetry polling observability and API behavior, using offline tests and fake connectors. Keep live polling disabled during development. Do not claim new live-device validation without fresh evidence.