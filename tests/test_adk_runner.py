import asyncio
import sys
from pathlib import Path

# Add the project root to Python's import path when this file
# is executed directly with: python tests\test_adk_runner.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.agent import root_agent


# Load local environment variables from .env
load_dotenv()


APP_NAME = "agentic_netops"
USER_ID = "test-user"


async def main() -> None:
    # Create an in-memory session service.
    session_service = InMemorySessionService()

    # Create a session for this test execution.
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    # Create the ADK runner.
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    # Network telemetry supplied to the agent.
    telemetry_prompt = (
        "Analyze this network telemetry using the diagnose_network tool.\n\n"
        "device_id=router-01\n"
        "interface=GigabitEthernet0/1\n"
        "timestamp=2026-08-25T00:00:00Z\n"
        "cpu_percent=92\n"
        "memory_percent=60\n"
        "bandwidth_mbps=100\n"
        "packet_loss_percent=5\n"
        "latency_ms=150\n"
        "interface_status=up\n\n"
        "Explain the detected issue, severity, evidence, "
        "and recommended action in plain English."
    )

    # Build the user message expected by ADK.
    message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=telemetry_prompt,
            )
        ],
    )

    print("Starting ADK agent...")
    print(f"Agent: {root_agent.name}")
    print(f"Model: {root_agent.model}")
    print(f"Session: {session.id}")
    print()

    # Execute the agent.
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=message,
    ):
        if event.is_final_response():
            print("FINAL RESPONSE:")
            print(event.content)


if __name__ == "__main__":
    asyncio.run(main())