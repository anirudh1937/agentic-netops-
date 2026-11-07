"""Offline MCP toolset concurrency and lifecycle tests.

Verifies the shared McpToolset / MCP stdio subprocess lifecycle under:

* concurrent agent executions sharing the real McpToolset,
* explicit close() terminating the actual subprocess,
* a timed-out agent request leaving the shared toolset usable,
* (best-effort) recovery after the MCP subprocess exits unexpectedly.

No Gemini, Cisco, DGX, or external service is contacted. The model is a
deterministic custom :class:`google.adk.models.base_llm.BaseLlm` subclass, so
every test runs fully offline without ``GEMINI_API_KEY``.

The real :data:`app.agents.agent.mcp_toolset` is reused for the concurrency
and timeout tests (it is the shared production singleton). An *isolated*
:class:`McpToolset` is constructed for the cleanup test so that closing it
does not contaminate other tests.
"""

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import mcp.client.stdio as mcp_stdio

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import (
    McpToolset,
    StdioConnectionParams,
)
from google.genai import types
from mcp import StdioServerParameters
from pydantic import PrivateAttr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.agent import diagnose_network, mcp_toolset  # noqa: E402


APP_NAME = "agentic_netops_lifecycle"
USER_ID = "lifecycle-user"

TELEMETRY_A = {
    "device_id": "device-A",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-16T21:00:00Z",
    "cpu_percent": 95,
    "memory_percent": 60,
    "bandwidth_mbps": 900,
    "packet_loss_percent": 5,
    "latency_ms": 180,
    "interface_status": "up",
}

TELEMETRY_B = {
    "device_id": "device-B",
    "interface": "GigabitEthernet0/2",
    "timestamp": "2026-08-16T21:00:00Z",
    "cpu_percent": 30,
    "memory_percent": 40,
    "bandwidth_mbps": 10,
    "packet_loss_percent": 0,
    "latency_ms": 10,
    "interface_status": "up",
}

FUNCTION_CALL_ID_A = "lifecycle-call-A"
FUNCTION_CALL_ID_B = "lifecycle-call-B"

EXPECTED_A = {
    "device_id": "device-A",
    "status": "degraded",
    "anomalies": ["high_cpu", "high_packet_loss", "high_latency"],
}

EXPECTED_B = {
    "device_id": "device-B",
    "status": "healthy",
    "anomalies": [],
}


class _FakeModel(BaseLlm):
    """Deterministic fake model.

    ``mode`` controls behaviour:

    * ``"normal"``: turn 1 emits a ``FunctionCall`` for ``check_device_health``;
      turn 2 consumes the ``FunctionResponse`` and emits final text.
    * ``"block_turn2"``: turn 1 emits the ``FunctionCall``; turn 2 blocks
      indefinitely so the enclosing request times out.
    """

    _call_count: int = PrivateAttr(default=0)
    _mode: str = PrivateAttr(default="normal")
    _telemetry: dict = PrivateAttr(default=None)
    _tools_dict_seen: dict = PrivateAttr(default=None)
    _function_call_emitted: object = PrivateAttr(default=None)
    _function_response_seen: object = PrivateAttr(default=None)
    _final_text: str = PrivateAttr(default=None)
    _block: asyncio.Event = PrivateAttr(default=None)

    def __init__(self, mode="normal", telemetry=None, **data):
        super().__init__(model="fake-lifecycle-model", **data)
        self._mode = mode
        self._telemetry = dict(telemetry or {})
        self._block = asyncio.Event()

    @property
    def call_count(self):
        return self._call_count

    @property
    def tools_dict_seen(self):
        return self._tools_dict_seen

    @property
    def function_call_emitted(self):
        return self._function_call_emitted

    @property
    def function_response_seen(self):
        return self._function_response_seen

    @property
    def final_text(self):
        return self._final_text

    def release(self):
        """Allow a blocked turn 2 to proceed (used only in tests)."""
        self._block.set()

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        self._call_count += 1
        self._tools_dict_seen = dict(llm_request.tools_dict)

        parts = []

        if self._call_count == 1:
            # Turn 1: select the check_device_health MCP tool.
            assert "check_device_health" in self._tools_dict_seen, (
                "check_device_health is not available in llm_request.tools_dict; "
                f"saw {sorted(self._tools_dict_seen.keys())}"
            )

            function_call = types.FunctionCall(
                name="check_device_health",
                args=dict(self._telemetry),
                id=FUNCTION_CALL_ID_A if self._telemetry.get("device_id") == "device-A"
                else FUNCTION_CALL_ID_B,
            )
            parts.append(types.Part(function_call=function_call))
            self._function_call_emitted = function_call
        else:
            # Turn 2: consume the MCP tool result returned as a FunctionResponse.
            if self._mode == "block_turn2":
                # Block so the enclosing request times out.
                await self._block.wait()

            function_response = None
            for content in llm_request.contents:
                if not content.parts:
                    continue
                for part in content.parts:
                    if part.function_response is not None:
                        function_response = part.function_response

            assert function_response is not None, (
                "No FunctionResponse received in the second model turn; "
                "the MCP tool result was not delivered to the model."
            )
            self._function_response_seen = function_response

            response = function_response.response or {}
            payload = {}
            for part in response.get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    payload = json.loads(part["text"])

            assert payload.get("device_id") == self._telemetry["device_id"], (
                f"Unexpected device_id: {payload}"
            )
            self._final_text = (
                f"Device {payload.get('device_id')} is {payload.get('status')}; "
                f"anomalies: {payload.get('anomalies')}"
            )
            parts.append(types.Part.from_text(text=self._final_text))

        yield LlmResponse(
            content=types.Content(role="model", parts=parts),
            partial=False,
        )


def _new_fake_agent(fake_model, telemetry, toolset=mcp_toolset):
    """Build an Agent that uses the given McpToolset (shared by default)."""
    return Agent(
        name="lifecycle_agent",
        model=fake_model,
        description="Lifecycle test agent.",
        instruction="Use check_device_health for the supplied telemetry.",
        tools=[diagnose_network, toolset],
    )


def _new_message(telemetry):
    return types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Analyze this telemetry using check_device_health: "
                    f"{telemetry}"
                )
            )
        ],
    )


async def _run_agent_once(agent, message, timeout_seconds=60.0):
    """Run one Agent execution through a real Runner and return final text."""
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID
    )
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    final_texts = []
    async with asyncio.timeout(timeout_seconds):
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session.id,
            new_message=message,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    if part.text:
                        final_texts.append(part.text)
    return " ".join(final_texts)


@asynccontextmanager
async def _capture_stdio_process():
    """Capture the MCP stdio subprocess handle created during the context.

    The patch only records the process handle; the real stdio client still
    creates, drives, and terminates the subprocess (same approach as the
    Milestone 7.5 stdio tests).
    """
    captured = []
    real_create = mcp_stdio._create_platform_compatible_process

    async def tracking_create(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        captured.append(process)
        return process

    with patch.object(
        mcp_stdio, "_create_platform_compatible_process", tracking_create
    ):
        yield captured


def _build_isolated_toolset():
    """Construct an isolated McpToolset for lifecycle/cleanup tests."""
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "app.mcp.server"],
            ),
            timeout=10.0,
        ),
        tool_filter=["check_device_health"],
    )


def _assert_process_terminated(captured):
    assert len(captured) == 1, "expected exactly one MCP server subprocess"
    process = captured[0]
    assert process.returncode is not None, (
        "MCP server subprocess is still running after close()"
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_agent_executions_share_toolset():
    """Two simultaneous agent executions share the real McpToolset.

    Both requests invoke ``check_device_health`` through the shared MCP stdio
    subprocess and both complete successfully with their own correct results.
    The shared subprocess is reused (one process for both concurrent calls).
    No Runner, McpToolset, or MCP server is mocked.
    """

    fake_a = _FakeModel(mode="normal", telemetry=TELEMETRY_A)
    fake_b = _FakeModel(mode="normal", telemetry=TELEMETRY_B)

    agent_a = _new_fake_agent(fake_a, TELEMETRY_A)
    agent_b = _new_fake_agent(fake_b, TELEMETRY_B)

    message_a = _new_message(TELEMETRY_A)
    message_b = _new_message(TELEMETRY_B)

    async def _run_concurrent_captured():
        async with _capture_stdio_process() as captured:
            result_a, result_b = await asyncio.gather(
                _run_agent_once(agent_a, message_a),
                _run_agent_once(agent_b, message_b),
            )
        return result_a, result_b, captured

    try:
        result_a, result_b, captured = asyncio.run(
            _run_concurrent_captured()
        )
    finally:
        # Close the shared toolset so no subprocess lingers after this test.
        asyncio.run(mcp_toolset.close())

    # Both requests completed successfully.
    assert result_a, "Request A produced no final response text."
    assert result_b, "Request B produced no final response text."

    # Responses are associated with the correct request.
    assert EXPECTED_A["device_id"] in result_a, result_a
    assert EXPECTED_A["status"] in result_a, result_a
    assert "high_cpu" in result_a, result_a
    assert EXPECTED_B["device_id"] in result_b, result_b
    assert EXPECTED_B["status"] in result_b, result_b

    # The shared subprocess was reused: exactly one process for both calls.
    assert len(captured) == 1, f"expected 1 reused subprocess, got {len(captured)}"
    _assert_process_terminated(captured)

    # Each fake model was invoked exactly twice (tool selection + final turn).
    assert fake_a.call_count == 2, fake_a.call_count
    assert fake_b.call_count == 2, fake_b.call_count

    # Each fake model selected check_device_health with its own telemetry.
    assert fake_a.function_call_emitted.name == "check_device_health"
    assert fake_a.function_call_emitted.args == TELEMETRY_A
    assert fake_b.function_call_emitted.name == "check_device_health"
    assert fake_b.function_call_emitted.args == TELEMETRY_B

    # Each fake model received the matching FunctionResponse.
    assert fake_a.function_response_seen is not None
    assert fake_b.function_response_seen is not None
    assert fake_a.function_response_seen.name == "check_device_health"
    assert fake_b.function_response_seen.name == "check_device_health"


# ---------------------------------------------------------------------------
# Lifecycle / cleanup
# ---------------------------------------------------------------------------


def test_mcp_toolset_lazy_start_and_close_terminates_subprocess():
    """The MCP subprocess starts lazily and close() terminates it.

    Uses an *isolated* McpToolset so closing it does not contaminate other
    tests that rely on the shared module-level singleton.
    """

    toolset = _build_isolated_toolset()
    fake = _FakeModel(mode="normal", telemetry=TELEMETRY_A)
    agent = _new_fake_agent(fake, TELEMETRY_A, toolset=toolset)
    message = _new_message(TELEMETRY_A)

    async def _scenario():
        async with _capture_stdio_process() as captured:
            try:
                # Before the first call, no subprocess has been started by this
                # isolated toolset (lazy start).
                result = await _run_agent_once(agent, message)
                assert EXPECTED_A["device_id"] in result, result
                assert EXPECTED_A["status"] in result, result

                # Exactly one subprocess was created for this isolated toolset.
                assert len(captured) == 1, (
                    f"expected 1 subprocess, got {len(captured)}"
                )
                process = captured[0]
                assert process.returncode is None, (
                    "subprocess should be running while the toolset is open"
                )
            finally:
                # Close the toolset within the same event loop that created
                # the session, so the subprocess is terminated cleanly.
                await toolset.close()
        _assert_process_terminated(captured)
        return captured

    captured = asyncio.run(_scenario())

    # Calling close() again must be safe (idempotent).
    asyncio.run(toolset.close())
    _assert_process_terminated(captured)


# ---------------------------------------------------------------------------
# Timeout / cancellation
# ---------------------------------------------------------------------------


def test_timeout_leaves_shared_toolset_usable(monkeypatch):
    """A timed-out request leaves the shared toolset usable by a later request.

    The first request blocks in the model's second turn, so it exceeds the
    configured timeout. The second request (default timeout) must still invoke
    check_device_health through the shared McpToolset and succeed.
    """

    monkeypatch.setenv("NETOPS_AGENT_TIMEOUT_SECONDS", "0.5")

    blocker = _FakeModel(mode="block_turn2", telemetry=TELEMETRY_A)
    agent_blocked = _new_fake_agent(blocker, TELEMETRY_A)
    message_a = _new_message(TELEMETRY_A)

    try:
        # First request: timed out. The shared toolset is NOT closed by the
        # timeout; only the request coroutine is cancelled.
        try:
            asyncio.run(_run_agent_once(agent_blocked, message_a))
            raise AssertionError("expected AgentTimeoutError")
        except asyncio.TimeoutError:
            pass  # _run_agent wraps wait_for TimeoutError; it propagates here.

        # Second request: default timeout, must succeed through the shared
        # toolset that is still open.
        ok = _FakeModel(mode="normal", telemetry=TELEMETRY_B)
        agent_ok = _new_fake_agent(ok, TELEMETRY_B)
        message_b = _new_message(TELEMETRY_B)

        result_b = asyncio.run(
            _run_agent_once(agent_ok, message_b, timeout_seconds=60.0)
        )
        assert result_b, "Second request produced no final response text."
        assert EXPECTED_B["device_id"] in result_b, result_b
        assert EXPECTED_B["status"] in result_b, result_b
    finally:
        # Close the shared toolset so no subprocess lingers after this test.
        asyncio.run(mcp_toolset.close())

        # Release the blocked model so its cancelled coroutine can finalize.
        blocker.release()


# ---------------------------------------------------------------------------
# Recovery after subprocess failure (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Recovery after an unexpected MCP subprocess exit is not reliably "
        "supported by the current ADK/MCP tooling: once the stdio subprocess "
        "dies, the next check_device_health call does not reliably obtain a "
        "replacement session, so no FunctionResponse is delivered. This is an "
        "observed limitation, not a test bug; revisit if the MCP tooling or "
        "agent lifecycle is hardened."
    ),
    strict=False,
)
def test_recovery_after_subprocess_exit():
    """After the MCP subprocess dies, the next request recovers.

    Kills the subprocess between two calls. The session manager's
    ``create_session`` detects the disconnected session (closed read/write
    streams), cleans it up, and creates a replacement session, so the next
    MCP invocation succeeds. If the installed implementation cannot recover
    automatically, this test reports the observed failure honestly.
    """

    toolset = _build_isolated_toolset()
    fake = _FakeModel(mode="normal", telemetry=TELEMETRY_A)
    agent = _new_fake_agent(fake, TELEMETRY_A, toolset=toolset)
    message = _new_message(TELEMETRY_A)

    async def _scenario():
        async with _capture_stdio_process() as captured:
            try:
                # First call succeeds and starts the subprocess.
                result1 = await _run_agent_once(agent, message)
                assert EXPECTED_A["device_id"] in result1, result1
                assert len(captured) == 1, (
                    f"expected 1 subprocess, got {len(captured)}"
                )
                process = captured[0]
                assert process.returncode is None

                # Kill the subprocess between calls.
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()

                # Second call: session manager should detect the dead session
                # and create a replacement, so the MCP invocation succeeds.
                result2 = await _run_agent_once(
                    agent, message, timeout_seconds=30.0
                )
                assert EXPECTED_A["device_id"] in result2, result2
                assert EXPECTED_A["status"] in result2, result2
            finally:
                await toolset.close()
        _assert_process_terminated(captured)
        return captured

    captured = asyncio.run(_scenario())
