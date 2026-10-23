from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.agent import root_agent
from app.diagnosis import diagnose_network_issue
from app.telemetry import NetworkTelemetry


APP_NAME = "agentic_netops"
USER_ID = "api-user"


class AgentAnalysisError(RuntimeError):
    """Raised when the agent cannot produce a usable analysis."""


async def analyze_telemetry(telemetry: NetworkTelemetry) -> str:
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
