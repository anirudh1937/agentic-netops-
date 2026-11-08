import asyncio
import json
import logging
import math
import os
from typing import Mapping, Optional

from google.adk.agents import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

from app.agents.agent import diagnose_network, mcp_toolset, root_agent
from app.diagnosis import diagnose_network_issue
from app.telemetry import NetworkTelemetry


APP_NAME = "agentic_netops"
USER_ID = "api-user"

logger = logging.getLogger(__name__)

#: Default upper bound for one agent analysis request.
DEFAULT_AGENT_TIMEOUT_SECONDS = 60.0

#: Environment variable used to override the agent analysis timeout.
AGENT_TIMEOUT_ENV_VAR = "NETOPS_AGENT_TIMEOUT_SECONDS"


class AgentAnalysisError(RuntimeError):
    """Raised when the agent cannot produce a usable analysis."""


class AgentTimeoutError(AgentAnalysisError):
    """Raised when the agent exceeds the configured execution deadline."""


def read_agent_timeout_seconds(
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    """Read the agent execution timeout from the environment.

    ``NETOPS_AGENT_TIMEOUT_SECONDS`` must be a positive, finite number of
    seconds. Missing or blank values use the default silently. Malformed,
    zero, negative, or non-finite values log a warning and fall back to the
    default so a bad setting cannot remove the execution bound.
    """

    source = os.environ if environ is None else environ
    raw = source.get(AGENT_TIMEOUT_ENV_VAR)

    if raw is None:
        return DEFAULT_AGENT_TIMEOUT_SECONDS

    text = str(raw).strip()
    if not text:
        return DEFAULT_AGENT_TIMEOUT_SECONDS

    try:
        value = float(text)
    except (TypeError, ValueError):
        logger.warning(
            "%s must be a positive number; using the default of %.0f seconds.",
            AGENT_TIMEOUT_ENV_VAR,
            DEFAULT_AGENT_TIMEOUT_SECONDS,
        )
        return DEFAULT_AGENT_TIMEOUT_SECONDS

    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s must be a positive, finite number; using the default of %.0f "
            "seconds.",
            AGENT_TIMEOUT_ENV_VAR,
            DEFAULT_AGENT_TIMEOUT_SECONDS,
        )
        return DEFAULT_AGENT_TIMEOUT_SECONDS

    return value


class _OfflineDemoModel(BaseLlm):
    """Deterministic fake model for the offline agent->MCP demonstration.

    Turn 1: inspects ``llm_request.tools_dict`` and emits a
    :class:`FunctionCall` for ``check_device_health``.
    Turn 2: consumes the ``FunctionResponse`` and emits final text.
    """

    _call_count: int = PrivateAttr(default=0)
    _tools_dict_seen: dict = PrivateAttr(default=None)
    _function_call_emitted: object = PrivateAttr(default=None)
    _function_response_seen: object = PrivateAttr(default=None)
    _final_text: str = PrivateAttr(default=None)
    _telemetry: dict = PrivateAttr(default=None)

    def __init__(self, telemetry: dict, **data):
        super().__init__(model="offline-demo-model", **data)
        self._telemetry = dict(telemetry or {})

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
            # Turn 1: emit a FunctionCall selecting check_device_health.
            if "check_device_health" not in self._tools_dict_seen:
                raise AgentAnalysisError(
                    "check_device_health is not available in "
                    "llm_request.tools_dict"
                )

            function_call = types.FunctionCall(
                name="check_device_health",
                args=dict(self._telemetry),
                id="offline-demo-call-1",
            )
            parts.append(types.Part(function_call=function_call))
            self._function_call_emitted = function_call
        else:
            # Turn 2: consume the FunctionResponse from the tool result.
            function_response = None
            for content in llm_request.contents:
                if not content.parts:
                    continue
                for part in content.parts:
                    if part.function_response is not None:
                        function_response = part.function_response

            if function_response is None:
                raise AgentAnalysisError(
                    "No FunctionResponse received; MCP tool result "
                    "was not delivered to the model."
                )
            self._function_response_seen = function_response

            response = function_response.response or {}
            payload = {}
            for part in response.get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    payload = json.loads(part["text"])

            self._final_text = (
                f"Device {payload.get('device_id')} is "
                f"{payload.get('status')}; anomalies: "
                f"{payload.get('anomalies')}"
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


async def analyze_telemetry_offline(
    telemetry: NetworkTelemetry,
) -> dict:
    """Analyze telemetry with a bounded agent execution deadline.

    Uses a deterministic fake model and the real MCP toolset to drive
    the ADK Runner through a real MCP stdio subprocess. Returns a
    structured result describing the tool call, tool result, and final
    agent response.

    Does not require GEMINI_API_KEY or any external service.
    """

    fake_model = _OfflineDemoModel(telemetry=dict(telemetry.model_dump()))

    agent = Agent(
        name="offline_demo_agent",
        model=fake_model,
        description="Offline demonstration agent",
        instruction=(
            "Analyze network telemetry using the check_device_health tool."
        ),
        tools=[diagnose_network, mcp_toolset],
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    diagnosis = diagnose_network_issue(telemetry)
    diagnosis_block = (
        "Deterministic rule-engine diagnosis (use as the basis for your "
        "explanation; do not invent or contradict it):\n"
        f"severity={diagnosis.get('severity')}\n"
        f"anomalies={diagnosis.get('anomalies') or []}\n"
        f"summary={diagnosis.get('diagnosis')}\n"
        "evidence:\n"
        + "\n".join(f"  - {e}" for e in diagnosis.get("evidence") or [])
        + f"\nrecommended_action={diagnosis.get('recommendation')}\n"
    )

    telemetry_prompt = (
        "Analyze this network telemetry using the check_device_health "
        "tool.\n\n"
        f"device_id={telemetry.device_id}\n"
        f"interface={telemetry.interface}\n"
        f"timestamp={telemetry.timestamp}\n"
        f"cpu_percent={telemetry.cpu_percent}\n"
        f"memory_percent={telemetry.memory_percent}\n"
        f"bandwidth_mbps={telemetry.bandwidth_mbps}\n"
        f"packet_loss_percent={telemetry.packet_loss_percent}\n"
        f"latency_ms={telemetry.latency_ms}\n"
        f"interface_status={telemetry.interface_status}\n\n"
        + diagnosis_block
        + "\n"
        "Explain the detected issue, severity, evidence, "
        "and recommended action in plain English."
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=telemetry_prompt)],
    )

    timeout_seconds = read_agent_timeout_seconds()
    final_texts = []

    try:
        final_text = await asyncio.wait_for(
            _run_agent_with_fake_model(
                fake_model, agent, session_service, session, message
            ),
            timeout=timeout_seconds,
        )
        final_texts.append(final_text)
    except asyncio.TimeoutError as exc:
        # Bounded timeout exceeded — propagate so the API endpoint can assign
        # the correct HTTP status (504) and structured response.
        raise AgentTimeoutError(
            f"Agent analysis exceeded {timeout_seconds:.1f} seconds."
        ) from exc
    except AgentAnalysisError:
        # Agent-level error (e.g., no tool result delivered) — propagate so
        # the API endpoint can assign the correct HTTP status (503).
        raise

    return {
        "device_id": telemetry.device_id,
        "interface": telemetry.interface,
        "execution_mode": "offline_demo",
        "tool_invoked": fake_model.function_call_emitted is not None,
        "tool_name": (
            fake_model.function_call_emitted.name
            if fake_model.function_call_emitted is not None
            else None
        ),
        "tool_arguments": (
            dict(fake_model.function_call_emitted.args)
            if fake_model.function_call_emitted is not None
            else None
        ),
        "tool_result": (
            _extract_mcp_payload(fake_model.function_response_seen)
            if fake_model.function_response_seen is not None
            else None
        ),
        "final_agent_response": " ".join(final_texts),
        "execution_status": "completed"
        if fake_model.final_text is not None
        else "failed",
    }


async def _run_agent_with_fake_model(
    fake_model: _OfflineDemoModel,
    agent: Agent,
    session_service: InMemorySessionService,
    session,
    message: types.Content,
) -> str:
    """Run the ADK Runner with a fake model and collect final text."""

    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    final_texts = []
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


async def analyze_telemetry(telemetry: NetworkTelemetry) -> str:
    """Analyze telemetry with a bounded agent execution deadline."""

    timeout_seconds = read_agent_timeout_seconds()

    try:
        return await asyncio.wait_for(
            _run_agent(telemetry),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise AgentTimeoutError(
            f"Agent analysis exceeded {timeout_seconds:.1f} seconds."
        ) from exc


async def _run_agent(telemetry: NetworkTelemetry) -> str:
    session_service = InMemorySessionService()

    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # Reuse the deterministic rule engine so the agent explains findings the
    # device actually produced, rather than re-deriving or inventing them.
    # This includes the honest "unknown" case when telemetry is too sparse.
    diagnosis = diagnose_network_issue(telemetry)
    diagnosis_anomalies = diagnosis.get("anomalies") or []
    diagnosis_evidence = diagnosis.get("evidence") or []

    diagnosis_block = (
        "Deterministic rule-engine diagnosis (use as the basis for your "
        "explanation; do not invent or contradict it):\n"
        f"severity={diagnosis.get('severity')}\n"
        f"anomalies={diagnosis_anomalies}\n"
        f"summary={diagnosis.get('diagnosis')}\n"
        "evidence:\n"
        + "\n".join(f"  - {e}" for e in diagnosis_evidence)
        + f"\nrecommended_action={diagnosis.get('recommendation')}\n"
    )

    telemetry_prompt = (
        "Analyze this network telemetry using the diagnose_network tool.\n\n"
        f"device_id={telemetry.device_id}\n"
        f"interface={telemetry.interface}\n"
        f"timestamp={telemetry.timestamp}\n"
        f"cpu_percent={telemetry.cpu_percent}\n"
        f"memory_percent={telemetry.memory_percent}\n"
        f"bandwidth_mbps={telemetry.bandwidth_mbps}\n"
        f"packet_loss_percent={telemetry.packet_loss_percent}\n"
        f"latency_ms={telemetry.latency_ms}\n"
        f"interface_status={telemetry.interface_status}\n\n"
        + diagnosis_block
        + "\n"
        "Explain the detected issue, severity, evidence, "
        "and recommended action in plain English."
    )

    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=telemetry_prompt,
            )
        ],
    )

    final_text = ""

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=message,
    ):
        if event.is_final_response() and event.content:
            for part in event.content.parts or []:
                if part.text:
                    final_text += part.text

    final_text = final_text.strip()

    if not final_text:
        raise AgentAnalysisError("Agent returned an empty analysis.")

    return final_text
