"""Opt-in, read-only live integration test for the Cisco RESTCONF adapter.

This test is intentionally excluded from ordinary runs (and CI): it performs
real HTTP requests against the configured Cisco sandbox, so it only ever runs
when an operator explicitly enables it. It mirrors the existing
``test_analyze_telemetry_real_model_integration`` two-gate pattern in
``tests/test_agent_api.py``.

It is enabled ONLY when both conditions hold:

* ``AGENTIC_NETOPS_LIVE_CISCO_TEST=1`` is set in the environment, and
* ``CISCO_SANDBOX_HOST``, ``CISCO_SANDBOX_USERNAME`` and
  ``CISCO_SANDBOX_PASSWORD`` are all non-empty.

The opt-in flag is checked before the configuration, and configuration is
inspected *before* any client is built or any network request is made, so an
accidental flag can never expose the device.

The test is strictly read-only: it reuses the project's existing
``CiscoClient`` / ``fetch_telemetry`` implementation, which performs only
bounded-timeout ``GET`` requests to RESTCONF paths and never modifies device
configuration. Credentials, environment values, and authorization headers are
never printed or asserted on. Any transport/auth/HTTP/JSON failure propagates
out of the test (recorded by pytest as a failure) instead of being swallowed,
and ``CiscoTelemetryError`` messages are already documented to be
credential-safe, so no secret can leak through the failure output.
"""

import os

from dotenv import load_dotenv
import pytest

from app.cisco import CiscoClient, CiscoDevice, fetch_telemetry


# Values that ``app.cisco.adapter._cisco_oper_status`` can return and that
# ``fetch_telemetry`` maps into ``interface_status``. Anything outside this set
# indicates a device-shape change that must not be silently accepted.
_SUPPORTED_INTERFACE_STATES = ("up", "down")


def _extract_hostname(response):
    """Return the device hostname string, or ``None`` for an unexpected shape.

    The response is expected to be a RESTCONF JSON container. IOS XE reports it
    under either the namespaced key (``...:hostname``) or nested inside a
    ``native`` container; both accepted shapes are handled. Any other shape
    yields ``None`` so the test fails loudly instead of trusting bad data.
    """

    if not isinstance(response, dict):
        return None

    native = response.get("Cisco-IOS-XE-native:native")
    if isinstance(native, dict):
        value = native.get("hostname")
        return value if isinstance(value, str) and value else None

    value = response.get("Cisco-IOS-XE-native:hostname")
    return value if isinstance(value, str) and value else None


def test_cisco_live_integration():
    # Gate 1: explicit opt-in. Checked first so the device is never touched
    # unless the operator intentionally enables the test.
    if os.environ.get("AGENTIC_NETOPS_LIVE_CISCO_TEST") != "1":
        pytest.skip(
            "Opt-in only: set AGNETNETOPS_LIVE_CISCO_TEST=1 to run the Cisco "
            "sandbox smoke test."
        )

    # Populate environment variables from .env (idempotent) before inspecting
    # configuration, so a missing secret fails the gate below instead of
    # constructing a client and reaching the device unexpectedly.
    load_dotenv(override=False)

    host = os.environ.get("CISCO_SANDBOX_HOST") or ""
    username = os.environ.get("CISCO_SANDBOX_USERNAME", "")
    password = os.environ.get("CISCO_SANDBOX_PASSWORD", "")

    # Gate 2: required configuration. Inspected before the client is built.
    if not (host and username and password):
        pytest.skip(
            "Cisco device credentials not configured: set CISCO_SANDBOX_HOST, "
            "CISCO_SANDBOX_USERNAME, and CISCO_SANDBOX_PASSWORD."
        )

    # Only now is a client constructed. ``from_env`` applies the same
    # api_root/verify_tls parsing as the adapter; host is guaranteed
    # non-empty, so the "host is not configured" error cannot be raised here.
    device = CiscoDevice.from_env()
    client = CiscoClient(device)

    # 1) Read-only hostname lookup: proves reachability + Basic auth + JSON
    #    parse. Any transport/auth/HTTP/JSON failure propagates (recorded as a
    #    failure) and is credential-safe.
    hostname_response = client.get_json(
        "data/Cisco-IOS-XE-native:native/hostname"
    )
    hostname_leaf = _extract_hostname(hostname_response)
    assert hostname_leaf, "Cisco hostname leaf is missing or malformed."

    # 2) Full telemetry mapping: proves device identity and interface status.
    telemetry = fetch_telemetry(device=device, client=client)
    assert telemetry.device_id, "Device identifier is empty after mapping."
    # The reported identity must match the hostname we validated, tying the
    # returned analysis back to the device that was actually contacted.
    assert telemetry.device_id == hostname_leaf

    # Interface status must be one the adapter documents as supported; anything
    # else indicates an unsupported device shape and must not pass.
    assert telemetry.interface_status in _SUPPORTED_INTERFACE_STATES