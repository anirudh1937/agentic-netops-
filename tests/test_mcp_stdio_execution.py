"""Real MCP stdio execution tests (Milestone 7.5).

These tests launch the actual MCP server as a subprocess and invoke the
registered ``check_device_health`` tool over the real stdio protocol. They do
not call the tool functions directly and do not use the in-process FastMCP
object. No device, model, credential, or external service is involved.

The only non-public hook wraps the stdio client's process factory so the test
can retain the subprocess handle and verify that the transport terminates it.
The server is still started and driven by ``mcp.client.stdio.stdio_client``.
"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest

import mcp.client.stdio as mcp_stdio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.diagnosis import diagnose_network_issue
from app.telemetry import NetworkTelemetry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_MODULE = "app.mcp.server"
TOOL_NAME = "check_device_health"
PROTOCOL_TIMEOUT = timedelta(seconds=30)
TEST_TIMEOUT_SECONDS = 60.0


ANOMALOUS_TELEMETRY = {
    "device_id": "router-01",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-16T21:00:00Z",
    "cpu_percent": 95,
    "memory_percent": 70,
    "bandwidth_mbps": 900,
    "packet_loss_percent": 5,
    "latency_ms": 180,
    "interface_status": "up",
}


SPARSE_TELEMETRY = {
    **ANOMALOUS_TELEMETRY,
    "cpu_percent": None,
    "memory_percent": None,
    "bandwidth_mbps": None,
    "packet_loss_percent": None,
    "latency_ms": None,
}


@pytest.fixture(autouse=True)
def _offline_environment(monkeypatch):
    """Prove the tests do not depend on live credentials or polling."""

    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "CISCO_SANDBOX_HOST",
        "CISCO_SANDBOX_USERNAME",
        "CISCO_SANDBOX_PASSWORD",
        "JUNIPER_SANDBOX_HOST",
        "JUNIPER_SANDBOX_USERNAME",
        "JUNIPER_SANDBOX_PASSWORD",
        "ARISTA_SANDBOX_HOST",
        "ARISTA_SANDBOX_USERNAME",
        "ARISTA_SANDBOX_PASSWORD",
        "NETOPS_POLL_ENABLED",
        "AGNETNETOPS_LIVE_TEST",
        "AGENTIC_NETOPS_LIVE_TEST",
        "AGENTIC_NETOPS_LIVE_CISCO_TEST",
    ):
        monkeypatch.delenv(name, raising=False)


def _result_text(result) -> str:
    """Return the concatenated text content from an MCP tool result."""

    assert result.content, "MCP tool returned no content"
    text = "".join(
        part.text for part in result.content if getattr(part, "text", None)
    )
    assert text, "MCP result contained no text content"
    return text


def _payload(result) -> dict:
    """Assert a successful tool call and parse its JSON text payload."""

    assert result.isError is False, _result_text(result)
    payload = json.loads(_result_text(result))
    assert isinstance(payload, dict)
    return payload


def _assert_process_terminated(captured) -> None:
    """Assert the tracked stdio subprocess reached a terminal state."""

    assert len(captured) == 1, "expected exactly one MCP server subprocess"
    process = captured[0]
    assert process.returncode is not None, "MCP server subprocess is still running"


@asynccontextmanager
async def _stdio_session():
    """Launch the real MCP server and yield an initialized client session."""

    captured = []
    real_create = mcp_stdio._create_platform_compatible_process

    async def tracking_create(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        captured.append(process)
        return process

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", SERVER_MODULE],
        cwd=str(PROJECT_ROOT),
        # The stdio client intentionally inherits only a small safe subset of
        # the environment, so make the project importable explicitly.
        env={"PYTHONPATH": str(PROJECT_ROOT)},
    )

    # The patch only records the process handle. The real stdio client still
    # creates, drives, and terminates the subprocess.
    with patch.object(
        mcp_stdio,
        "_create_platform_compatible_process",
        tracking_create,
    ):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=PROTOCOL_TIMEOUT,
            ) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo is not None
                assert initialized.serverInfo.name == "Agentic NetOps"
                yield session, captured


def _run(scenario) -> None:
    """Run one async scenario with a bounded overall timeout."""

    asyncio.run(
        asyncio.wait_for(scenario(), timeout=TEST_TIMEOUT_SECONDS)
    )


def test_stdio_execution_anomalous_telemetry():
    async def scenario():
        async with _stdio_session() as (session, captured):
            result = await session.call_tool(TOOL_NAME, ANOMALOUS_TELEMETRY)

            payload = _payload(result)
            expected = diagnose_network_issue(
                NetworkTelemetry(**ANOMALOUS_TELEMETRY)
            )

            assert payload["device_id"] == ANOMALOUS_TELEMETRY["device_id"]
            assert payload["interface"] == ANOMALOUS_TELEMETRY["interface"]
            assert payload["status"] == "degraded"
            assert payload["anomalies"] == expected["anomalies"]
            assert payload["anomalies"] == [
                "high_cpu",
                "high_packet_loss",
                "high_latency",
            ]

        _assert_process_terminated(captured)

    _run(scenario)


def test_stdio_execution_sparse_telemetry():
    async def scenario():
        async with _stdio_session() as (session, captured):
            result = await session.call_tool(TOOL_NAME, SPARSE_TELEMETRY)

            payload = _payload(result)
            expected = diagnose_network_issue(
                NetworkTelemetry(**SPARSE_TELEMETRY)
            )

            assert expected["severity"] == "unknown"
            assert payload["device_id"] == SPARSE_TELEMETRY["device_id"]
            assert payload["interface"] == SPARSE_TELEMETRY["interface"]
            assert payload["status"] == "unknown"
            assert payload["anomalies"] == []

        _assert_process_terminated(captured)

    _run(scenario)


@pytest.mark.parametrize(
    "arguments,expected_field",
    [
        (
            {**ANOMALOUS_TELEMETRY, "cpu_percent": 150},
            "cpu_percent",
        ),
        (
            {
                key: value
                for key, value in ANOMALOUS_TELEMETRY.items()
                if key != "device_id"
            },
            "device_id",
        ),
    ],
    ids=["metric-out-of-range", "missing-required-field"],
)
def test_stdio_execution_rejects_invalid_arguments(arguments, expected_field):
    async def scenario():
        async with _stdio_session() as (session, captured):
            result = await session.call_tool(TOOL_NAME, arguments)

            # FastMCP reports tool argument validation as a normal
            # CallToolResult with isError=True, not as a transport exception.
            assert result.isError is True
            text = _result_text(result)
            assert "validation error" in text.lower()
            assert expected_field in text

        _assert_process_terminated(captured)

    _run(scenario)


def test_stdio_tool_schema_is_registered():
    async def scenario():
        async with _stdio_session() as (session, captured):
            tools = await session.list_tools()
            tool = next(
                (candidate for candidate in tools.tools if candidate.name == TOOL_NAME),
                None,
            )
            assert tool is not None

            schema = tool.inputSchema
            assert schema["type"] == "object"
            assert set(schema["required"]) == {
                "device_id",
                "interface",
                "timestamp",
                "cpu_percent",
                "memory_percent",
                "bandwidth_mbps",
                "packet_loss_percent",
                "latency_ms",
                "interface_status",
            }

            # Metrics are nullable but still required: callers must be
            # explicit about missing measurements rather than omitting them.
            for field in (
                "cpu_percent",
                "memory_percent",
                "bandwidth_mbps",
                "packet_loss_percent",
                "latency_ms",
            ):
                assert any(
                    option.get("type") == "null"
                    for option in schema["properties"][field]["anyOf"]
                )

        _assert_process_terminated(captured)

    _run(scenario)


def test_stdio_session_transport_and_subprocess_cleanup():
    async def scenario():
        async with _stdio_session() as (session, captured):
            result = await session.call_tool(TOOL_NAME, ANOMALOUS_TELEMETRY)
            assert result.isError is False

        # Both async context managers have exited. The tracked subprocess must
        # have terminated, and the client session must no longer be usable.
        _assert_process_terminated(captured)
        with pytest.raises(anyio.ClosedResourceError):
            await session.list_tools()

    _run(scenario)
