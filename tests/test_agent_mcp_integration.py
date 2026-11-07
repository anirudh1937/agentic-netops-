"""Offline ADK-to-MCP integration test.

Verifies the real execution path:

    ADK Runner -> real McpToolset -> real MCP stdio subprocess ->
    check_device_health -> MCP result -> ADK model -> final response.

No Gemini, Cisco, DGX, or other external service is contacted. The model is a
deterministic custom :class:`google.adk.models.base_llm.BaseLlm` subclass, so
this test runs fully offline and does not require ``GEMINI_API_KEY``.

The real :data:`app.agents.agent.mcp_toolset` is reused, which launches the
actual ``app.mcp.server`` over stdio. ``Runner.run_async`` drives the real
multi-turn flow: the fake model emits a ``FunctionCall``, ADK invokes the tool
through the real MCP toolset, the result is returned as a
``FunctionResponse``, and the fake model emits a final response incorporating
the actual tool-result values.
"""

import asyncio
import json
import sys
from pathlib import Path

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

# Ensure the repository root is importable when the test file is run directly
# or from a different working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agents.agent import diagnose_network, mcp_toolset, root_agent  # noqa: E402
from app.api.agent import AgentAnalysisError


APP_NAME = "agentic_netops_mcp_integration"
USER_ID = "mcp-integration-user"

TELEMETRY_ARGS = {
    "device_id": "prototype-router",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-16T21:00:00Z",
    "cpu_percent": 95,
    "memory_percent": 60,
    "bandwidth_mbps": 900,
    "packet_loss_percent": 5,
    "latency_ms": 180,
    "interface_status": "up",
}

# Expected MCP result for TELEMETRY_ARGS, derived from the deterministic
# diagnosis engine (app.diagnosis.diagnose_network_issue) and wrapped by the
# real MCP tool (app.mcp.network_tools.get_device_health).
EXPECTED_DEVICE_ID = "prototype-router"
EXPECTED_STATUS = "degraded"
EXPECTED_ANOMALIES = ["high_cpu", "high_packet_loss", "high_latency"]

FUNCTION_CALL_ID = "integration-call-1"


class _FakeModel(BaseLlm):
    """Deterministic fake model for the ADK-to-MCP integration test.

    Turn 1: inspects ``llm_request.tools_dict`` and emits a
    ``FunctionCall`` for ``check_device_health``.
    Turn 2: inspects ``llm_request.contents`` for the matching
    ``FunctionResponse``, parses the nested MCP JSON payload, and emits a
    final text response incorporating the actual tool-result values.
    """

    _call_count: int = PrivateAttr(default=0)
    _tools_dict_seen: dict = PrivateAttr(default=None)
    _function_call_emitted: object = PrivateAttr(default=None)
    _function_response_seen: object = PrivateAttr(default=None)
    _final_text: str = PrivateAttr(default=None)

    def __init__(self, **data):
        super().__init__(model="fake-mcp-integration-model", **data)

    # Read-only accessors for assertions after the run completes.
    @property
    def call_count(self) -> int:
        return self._call_count

    @property
    def tools_dict_seen(self) -> dict:
        return self._tools_dict_seen

    @property
    def function_call_emitted(self):
        return self._function_call_emitted

    @property
    def function_response_seen(self):
        return self._function_response_seen

    @property
    def final_text(self) -> str:
        return self._final_text

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
                args=dict(TELEMETRY_ARGS),
                id=FUNCTION_CALL_ID,
            )
            parts.append(types.Part(function_call=function_call))
            self._function_call_emitted = function_call
        else:
            # Turn 2: consume the MCP tool result returned as a FunctionResponse.
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
            assert function_response.name == "check_device_health", (
                f"Unexpected FunctionResponse tool name: {function_response.name}"
            )
            assert function_response.id == FUNCTION_CALL_ID, (
                f"FunctionResponse id {function_response.id!r} does not match "
                f"FunctionCall id {FUNCTION_CALL_ID!r}"
            )

            self._function_response_seen = function_response

            # Parse the nested MCP JSON payload:
            # FunctionResponse.response = {'content': [{'type': 'text',
            #   'text': '{json payload}'}], 'isError': False}
            response = function_response.response or {}
            content_parts = response.get("content") or []
            payload = {}
            for part in content_parts:
                if part.get("type") == "text" and part.get("text"):
                    payload = json.loads(part["text"])

            assert payload.get("device_id") == EXPECTED_DEVICE_ID, (
                f"Unexpected device_id in MCP result: {payload}"
            )
            assert payload.get("status") == EXPECTED_STATUS, (
                f"Unexpected status in MCP result: {payload}"
            )
            assert payload.get("anomalies") == EXPECTED_ANOMALIES, (
                f"Unexpected anomalies in MCP result: {payload}"
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


def _extract_mcp_payload(function_response) -> dict:
    """Parse the nested MCP JSON payload from a FunctionResponse."""
    response = function_response.response or {}
    payload = {}
    for part in response.get("content") or []:
        if part.get("type") == "text" and part.get("text"):
            payload = json.loads(part["text"])
    return payload


def test_agent_invokes_mcp_tool_and_incorporates_result():
    """End-to-end: real Runner invokes real MCP tool via real stdio subprocess.

    Verifies discovery, invocation, result delivery, and final-response
    incorporation without any live model or external service.
    """

    fake_model = _FakeModel()

    # Construct an isolated agent that reuses the real diagnose_network tool
    # and real McpToolset but swaps in the deterministic fake model. This
    # avoids mutating the shared module-level root_agent.
    agent = Agent(
        name=root_agent.name,
        model=fake_model,
        description=root_agent.description,
        instruction=root_agent.instruction,
        tools=[diagnose_network, mcp_toolset],
    )

    session_service = InMemorySessionService()
    session = asyncio.run(
        session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    )

    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=(
                    "Analyze this telemetry using check_device_health: "
                    f"{TELEMETRY_ARGS}"
                )
            )
        ],
    )

    try:
        combined = asyncio.run(
            _execute_run(runner, session.id, message)
        )

        # 1. Discovery: check_device_health was available to the model.
        assert fake_model.call_count == 2, (
            f"Expected exactly 2 model turns, got {fake_model.call_count}"
        )
        assert "check_device_health" in fake_model.tools_dict_seen, (
            "check_device_health is missing from llm_request.tools_dict; "
            f"saw {sorted(fake_model.tools_dict_seen.keys())}"
        )

        # 2. Tool selection: the fake model emitted the expected FunctionCall.
        emitted = fake_model.function_call_emitted
        assert emitted is not None, "The fake model emitted no FunctionCall."
        assert emitted.name == "check_device_health", (
            f"Unexpected FunctionCall name: {emitted.name}"
        )
        assert emitted.id == FUNCTION_CALL_ID, (
            f"Unexpected FunctionCall id: {emitted.id}"
        )
        assert emitted.args == TELEMETRY_ARGS, (
            f"Unexpected FunctionCall args: {emitted.args}"
        )

        # 3 & 4. Invocation & result delivery: the Runner invoked the MCP tool
        # through the real McpToolset/stdio subprocess and delivered the
        # matching FunctionResponse back to the model.
        function_response = fake_model.function_response_seen
        assert function_response is not None, (
            "No FunctionResponse was delivered to the model; the MCP tool "
            "result did not reach the second turn."
        )
        assert function_response.name == "check_device_health", (
            f"Unexpected FunctionResponse name: {function_response.name}"
        )
        assert function_response.id == FUNCTION_CALL_ID, (
            f"FunctionResponse id {function_response.id!r} does not match "
            f"FunctionCall id {FUNCTION_CALL_ID!r}"
        )

        # 5. Parse the nested MCP JSON payload correctly.
        payload = _extract_mcp_payload(function_response)

        # 6. The returned device_id, status, and anomalies match expectations.
        assert payload.get("device_id") == EXPECTED_DEVICE_ID, (
            f"Unexpected device_id: {payload}"
        )
        assert payload.get("status") == EXPECTED_STATUS, (
            f"Unexpected status: {payload}"
        )
        assert payload.get("anomalies") == EXPECTED_ANOMALIES, (
            f"Unexpected anomalies: {payload}"
        )

        # 7. The final agent response incorporates the actual tool-result values.
        assert combined, "The agent produced no final response text."
        assert EXPECTED_DEVICE_ID in combined, (
            f"Final response does not reference device_id: {combined}"
        )
        assert EXPECTED_STATUS in combined, (
            f"Final response does not reference status: {combined}"
        )
        assert "high_cpu" in combined, (
            f"Final response does not reference anomaly: {combined}"
        )
        assert fake_model.final_text is not None, (
            "The fake model did not produce a final text."
        )
    finally:
        # 8 & 9. Bounded timeout enforced via asyncio.timeout inside
        # _execute_run; cleanup closes the real MCP toolset and terminates
        # its subprocess.
        print("Closing McpToolset...")
        asyncio.run(mcp_toolset.close())
        print("McpToolset closed.")


async def _execute_run(
    runner: Runner, session_id: str, message: types.Content
) -> str:
    """Run the ADK Runner with a bounded timeout and collect final text."""
    final_texts = []
    async with asyncio.timeout(60.0):
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=message,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts or []:
                    if part.text:
                        final_texts.append(part.text)
    return " ".join(final_texts)


def test_agent_mcp_tool_discovery():
    """The real McpToolset exposes check_device_health (reinforces Level 2).

    This is distinct from tests/test_adk_mcp_discovery.py only insofar as it
    is co-located with the end-to-end integration test; it does not duplicate
    that file's assertions in a conflicting way.
    """
    tools = asyncio.run(mcp_toolset.get_tools(None))
    names = [tool.name for tool in tools]
    assert "check_device_health" in names, (
        f"check_device_health not exposed by McpToolset; saw {names}"
    )


# ============================================================
# Endpoint tests for /analyze/demo
# ==========================================================

DEMO_TELEMETRY = {
    "device_id": "demo-router",
    "interface": "GigabitEthernet0/1",
    "timestamp": "2026-08-16T21:00:00Z",
    "cpu_percent": 95,
    "memory_percent": 60,
    "bandwidth_mbps": 900,
    "packet_loss_percent": 5,
    "latency_ms": 180,
    "interface_status": "up",
}

EXPECTED_DEMO_DEVICE_ID = "demo-router"
EXPECTED_DEMO_STATUS = "degraded"
EXPECTED_DEMO_ANOMALIES = ["high_cpu", "high_packet_loss", "high_latency"]


def _call_analyze_endpoint(telemetry: dict):
    client = TestClient(app)
    return client.post("/analyze/demo", json=telemetry)


def test_agent_mcp_integration_endpoint():
    """POST /analyze/demo executes real MCP tool via real stdio subprocess."""
    response = _call_analyze_endpoint(DEMO_TELEMETRY)

    assert response.status_code == 200, f"Unexpected status: {response.status_code}"

    data = response.json()
    assert data["device_id"] == EXPECTED_DEMO_DEVICE_ID
    assert data["interface"] == DEMO_TELEMETRY["interface"]
    assert data["execution_mode"] == "offline_demo"
    assert data["tool_invoked"] is True
    assert data["tool_name"] == "check_device_health"
    assert data["tool_arguments"] == DEMO_TELEMETRY

    payload = data["tool_result"]
    assert payload is not None
    assert payload.get("device_id") == EXPECTED_DEMO_DEVICE_ID
    assert payload.get("status") == EXPECTED_DEMO_STATUS
    assert payload.get("anomalies") == EXPECTED_DEMO_ANOMALIES

    final = data["final_agent_response"]
    assert final is not None
    assert EXPECTED_DEMO_DEVICE_ID in final
    assert EXPECTED_DEMO_STATUS in final


def test_agent_mcp_integration_endpoint_timeout(monkeypatch):
    """A blocked model triggers timeout; execution_status is 'timeout'."""

    async def blocking_analyze(telemetry):
        await asyncio.sleep(10)
        raise asyncio.TimeoutError()

    import app.main as main_module

    monkeypatch.setattr(main_module, "analyze_telemetry_offline", blocking_analyze)

    try:
        response = _call_analyze_endpoint(DEMO_TELEMETRY)
        assert response.status_code == 200
        data = response.json()
        assert data["execution_status"] == "timeout"
    finally:
        monkeypatch.undo()


def test_agent_mcp_integration_endpoint_error(monkeypatch):
    """Agent error returns error status, not fabricated result."""

    async def failing_analyze(telemetry):
        raise AgentAnalysisError("check_device_health not available")

    import app.main as main_module

    monkeypatch.setattr(main_module, "analyze_telemetry_offline", failing_analyze)

    try:
        response = _call_analyze_endpoint(DEMO_TELEMETRY)
        assert response.status_code == 500
    finally:
        monkeypatch.undo()
