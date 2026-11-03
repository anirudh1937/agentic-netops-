import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _keep_background_polling_disabled(monkeypatch):
    monkeypatch.delenv("NETOPS_POLL_ENABLED", raising=False)


def _dashboard_html() -> str:
    with TestClient(app) as client:
        response = client.get("/dashboard")
    assert response.status_code == 200
    return response.text


def _function_body(html: str, name: str) -> str:
    """Return one top-level function body from the dashboard's inline script."""
    marker = f"function {name}("
    start = html.index(marker)
    brace = html.index("{", start)
    depth = 0
    for index in range(brace, len(html)):
        char = html[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return html[brace:index + 1]
    raise AssertionError(f"Could not find the end of dashboard function {name}")


def test_dashboard_poller_status_wiring():
    html = _dashboard_html()
    body = _function_body(html, "renderPollerStatus")

    assert 'id="freshness-poller-status"' in html
    assert '"/telemetry/status"' in _function_body(html, "refreshCollectionFreshness")

    for field in (
        "enabled",
        "running",
        "interval_seconds",
        "last_cycle_outcome",
        "last_cycle_started_at",
        "last_cycle_finished_at",
        "last_error",
    ):
        assert f"status.{field}" in body

    assert "Poller status unavailable" in body
    assert "Unavailable" in body


def test_dashboard_first_poll_states_wiring():
    body = _function_body(_dashboard_html(), "renderFirstPollState")

    for label in (
        "Polling disabled",
        "Awaiting first poll",
        "First poll in progress",
        "First poll failed",
        "Awaiting first successful poll",
    ):
        assert label in body

    assert "status.enabled === false" in body
    assert 'outcome === "degraded"' in body
    assert 'outcome === "error"' in body
    assert "status.last_error" in body
    assert "status.last_cycle_finished_at" in body


def test_dashboard_reading_failure_details_wiring():
    body = _function_body(_dashboard_html(), "renderCollectionFreshness")

    assert 'freshnessState === "POLLING_FAILED"' in body
    assert "reading.error" in body
    assert "reading.consecutive_failures" in body
    assert "reading.last_poll_attempt_at" in body
    assert "Failure details" in body
    assert "Last-known data (not live)" in body


def test_refresh_all_attempts_collection_freshness_after_integrations_error():
    body = _function_body(_dashboard_html(), "refreshAll")

    assert re.search(
        r"try\s*\{\s*await\s+loadIntegrations\(\);?\s*\}\s*catch",
        body,
        re.S,
    )
    assert body.index("await refreshCollectionFreshness()") > body.index(
        "await loadIntegrations()"
    )


def test_refresh_actions_force_live_telemetry_reloads():
    html = _dashboard_html()

    assert "loadAllVendorTelemetry(force)" in _function_body(html, "refreshOverview")
    assert "loadVendorTelemetry(v, force)" in _function_body(
        html, "loadAllVendorTelemetry"
    )
    assert "loadAllVendorTelemetry(true)" in _function_body(html, "refreshAlerts")
    assert "loadVendorTelemetry(vendor, force)" in _function_body(
        html, "fetchLiveTelemetry"
    )


def test_collection_freshness_fetches_status_independently_of_latest():
    body = _function_body(_dashboard_html(), "refreshCollectionFreshness")

    assert "Promise.allSettled" in body
    assert 'getJson("/telemetry/latest")' in body
    assert 'getJson("/telemetry/status")' in body
    assert "state.pollerStatusError = statusResult.reason" in body
