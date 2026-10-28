# Security Policy

## Supported versions

Security fixes are applied to the current default branch. Older commits and
feature branches are not maintained as separate supported versions.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting for this repository:

<https://github.com/anirudh1937/agentic-netops-/security/advisories/new>

Include, where possible:

- a short description of the issue
- affected endpoint or component
- reproduction steps
- impact assessment
- any suggested remediation

Please avoid including real device credentials, API keys, customer data, or
other sensitive values in the report.

## Security model

- RESTCONF device access is read-only.
- The project does not execute remediation actions.
- `.env` is ignored and must never be committed.
- Device credentials are never returned by the API or stored in telemetry
  errors. Connector errors use credential-safe messages, and unexpected
  exception details are reduced to class names or stable, sanitized messages.
- `GET /telemetry/latest` only reads local application state and never starts a
  poll or performs a network request.
- The optional `/analyze` endpoint sends submitted telemetry to the configured
  model backend. Do not send sensitive production data unless that is
  acceptable for your environment.
- `/cisco/telemetry`, `/vendors/{vendor}/telemetry`, `/vendors/{vendor}/inventory`,
  and `/integrations` can contact configured network devices. Protect these
  endpoints before exposing the service to untrusted networks.

## Deployment guidance

- Run behind authentication and TLS for any non-local deployment.
- Use dedicated, least-privilege, read-only device credentials.
- Keep `.env` outside version control and restrict file permissions.
- Review configured model-provider data-handling requirements before enabling
  `/analyze` with production telemetry.
- Keep live integration test flags disabled in CI and shared environments.
